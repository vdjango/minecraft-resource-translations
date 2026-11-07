"""Bounded automatic backfill, independent of catalogue change detection."""
import hashlib
import json
import time
from .mirror_catalog import prefetch_matches_platform, write_json
from .mirror_policy import category_for


def prefetch_exclusion(item, settings):
    if not prefetch_matches_platform(item, settings['prefetch_platform']):
        return 'platform'
    category = item.get('category', '')
    if category.endswith('_mappings'):
        return 'developer_mapping'
    if category in ('game-server', 'game-windows_server'):
        return 'server_file'
    if category == 'java-lzma':
        return 'duplicate_compression'
    if settings['policies'].get(category_for(item['url'], category)) not in ('cache', 'backup'):
        return 'redirect_policy'
    return None


class AutomaticSync:
    def __init__(self, mirror):
        self.mirror = mirror
        self.path = mirror.root / 'automatic-sync.json'
        self.state = dict(cursor=0, scanned=0, queued=0, state='starting', next_at=0)
        try:
            self.state.update(json.loads(self.path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass
        if 'round_counts' not in self.state:
            self.state.update(cursor=0, next_at=0)

    async def batch(self, files, settings, limit=200):
        signature = hashlib.sha256(json.dumps({k: settings.get(k) for k in
            ('policies', 'prefetch_platform', 'qiniu_routes', 'qiniu_paused', 'quota_gib', 'reserve_gib')}, sort_keys=True).encode()).hexdigest()
        if self.state.get('signature') != signature:
            self.state.update(cursor=0, scanned=0, queued=0, next_at=0, signature=signature)
        if self.state['next_at'] > time.time():
            return []
        counts = dict(self.state.get('round_counts', {})) if self.state['cursor'] else {}
        files = sorted(files, key=lambda item: (-item.get('priority', 0), item['url']))
        async with self.mirror.db.execute("""SELECT c.url FROM cache c JOIN objects o ON o.digest=c.object
            WHERE o.local=1 OR EXISTS(SELECT 1 FROM object_locations l
                WHERE l.scope='mirror' AND l.digest=o.digest AND l.state='ready')""") as cursor:
            existing = {row['url'] for row in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT url FROM mirror_failed_files') as cursor:
            failed = {row['url'] for row in await cursor.fetchall()}
        selected, skipped = [], {}
        position = min(self.state['cursor'], len(files))
        examined = 0
        while position < len(files) and len(selected) < limit and examined < 2000:
            item = files[position]
            position += 1
            examined += 1
            reason = prefetch_exclusion(item, settings)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
            elif item['url'] in existing:
                reason = 'existing'
            elif item['url'] in failed:
                reason = 'retrying'
            else:
                reason = 'queued'
                selected.append(item)
            counts[reason] = counts.get(reason, 0) + 1
        completed = position >= len(files)
        self.state.update(cursor=0 if completed else position,
            scanned=self.state['scanned']+examined, queued=self.state['queued']+len(selected),
            total=len(files), position=position, skipped=skipped, round_counts=counts, checked_at=time.time(),
            state='watching' if completed else 'backfilling',
            next_at=time.time()+(settings['refresh_minutes']*60 if completed else 5))
        write_json(self.path, self.state)
        return selected

    def wait_capacity(self):
        self.state.update(state='waiting_capacity', next_at=time.time()+300)
        write_json(self.path, self.state)
