"""Small, staggered transfer samples. Redirects never reveal client throughput."""
import asyncio
import random
import re
import time
from datetime import datetime, timezone

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .federation import PublicResolver

QUALITY_DEFAULTS = dict(quality_enabled=True, quality_adaptive_cache=True,
                        quality_interval_hours=6, quality_daily_mib=32,
                        quality_sample_kib=256, quality_fast_kibps=2048,
                        quality_slow_kibps=512)


def assessment(samples, settings, now=None):
    """Only recent, sufficiently spread observations can establish stability."""
    now = time.time() if now is None else now
    rows = [r for r in samples if now - 7 * 86400 <= r['at'] <= now]
    attempts = [r for r in rows if r['outcome'] != 'missing']
    speeds = [r['speed'] for r in attempts if r['outcome'] == 'ok' and r['bytes'] >= 65536]
    days = len({int(r['at'] // 86400) for r in attempts})
    success = sum(r['outcome'] == 'ok' for r in attempts) / len(attempts) if attempts else None
    speed = sum(speeds) / len(speeds) if speeds else None
    last = max((r['at'] for r in rows), default=None)
    mature = len(attempts) >= 8 and days >= 3
    grade = 'observing'
    if mature and success < .90:
        grade = 'poor'
    elif mature and len(speeds) >= 3:
        grade = ('excellent' if success >= .98 and speed >= settings['quality_fast_kibps'] * 1024
                 else 'good' if success >= .90 and speed >= settings['quality_slow_kibps'] * 1024
                 else 'poor')
    return dict(grade=grade, samples=len(attempts), days=days, success_rate=success,
                speed_bps=speed, last_at=last, missing=sum(r['outcome'] == 'missing' for r in rows),
                cache_priority=grade in ('good', 'poor'))


class SourceQuality:
    def __init__(self, mirror):
        self.mirror = mirror
        self.http = None
        self.task = None
        self.metrics = {}
        self.next_check = {}
        self.wake = asyncio.Event()
        self.last_probe = None
        self.next_global = 0
        self.identities = {}
        self.lock = asyncio.Lock()

    async def open(self):
        await self.mirror.db.executescript('''
            CREATE TABLE IF NOT EXISTS source_quality_samples(
                id INTEGER PRIMARY KEY,source TEXT NOT NULL,target TEXT NOT NULL,
                at REAL NOT NULL,outcome TEXT NOT NULL,bytes INTEGER NOT NULL,
                speed REAL NOT NULL,ttfb_ms REAL,status INTEGER);
            CREATE INDEX IF NOT EXISTS quality_source_at ON source_quality_samples(source,at);
            CREATE INDEX IF NOT EXISTS quality_at ON source_quality_samples(at);
            CREATE TABLE IF NOT EXISTS source_quality_schedule(source TEXT PRIMARY KEY,next_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS source_quality_budget(day TEXT PRIMARY KEY,bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS source_quality_identity(source TEXT PRIMARY KEY,url TEXT NOT NULL);
        ''')
        await self.mirror.db.commit()
        async with self.mirror.db.execute('SELECT source,next_at FROM source_quality_schedule') as cursor:
            self.next_check = {r['source']: r['next_at'] for r in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT source,url FROM source_quality_identity') as cursor:
            self.identities = {r['source']: r['url'] for r in await cursor.fetchall()}
        last = await self.mirror.one('SELECT MAX(at) at FROM source_quality_samples')
        self.next_global = (last['at'] or 0) + random.uniform(120, 300)
        await self.sync_sources()
        await self.refresh_metrics()
        self.http = ClientSession(timeout=ClientTimeout(total=10, connect=4),
            connector=TCPConnector(limit=1, resolver=PublicResolver()), auto_decompress=False,
            headers={'User-Agent': 'Buggcraft-Source-Quality/1.0', 'Accept-Encoding': 'identity'})
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.http:
            await self.http.close()

    def settings(self):
        from .mirror import read_settings
        return read_settings(self.mirror.config)

    async def refresh_metrics(self):
        now = time.time()
        await self.mirror.db.execute('DELETE FROM source_quality_samples WHERE at<?', (now - 30 * 86400,))
        await self.mirror.db.execute('DELETE FROM source_quality_budget WHERE day<?',
                                     (datetime.fromtimestamp(now - 31 * 86400, timezone.utc).strftime('%Y-%m-%d'),))
        async with self.mirror.db.execute('SELECT * FROM source_quality_samples WHERE at>=? ORDER BY at', (now - 7 * 86400,)) as cursor:
            grouped = {}
            for row in await cursor.fetchall():
                grouped.setdefault(row['source'], []).append(dict(row))
        self.metrics = {key: assessment(rows, self.settings(), now) for key, rows in grouped.items()}
        await self.mirror.db.commit()

    def metric(self, source):
        current = next((r for r in self.settings()['source_registries'] if r['id'] == source), None)
        if not current or self.identities.get(source) != current['kind'] + ':' + current['url']:
            return assessment([], self.settings())
        metric = self.metrics.get(source, assessment([], self.settings()))
        if metric['last_at'] and time.time() - metric['last_at'] > 2 * 86400:
            return {**metric, 'grade': 'stale', 'cache_priority': False}
        return metric

    async def sync_sources(self):
        current = {r['id']: r['kind'] + ':' + r['url'] for r in self.settings()['source_registries']}
        for key in set(self.identities) | set(current):
            if self.identities.get(key) == current.get(key):
                continue
            # Changing a repository address starts a new observation period.
            await self.mirror.db.execute('DELETE FROM source_quality_samples WHERE source=?', (key,))
            await self.mirror.db.execute('DELETE FROM source_quality_schedule WHERE source=?', (key,))
            await self.mirror.db.execute('DELETE FROM source_quality_identity WHERE source=?', (key,))
            self.metrics.pop(key, None)
            self.next_check.pop(key, None)
            if key in current:
                await self.mirror.db.execute('INSERT INTO source_quality_identity VALUES(?,?)', (key, current[key]))
        self.identities = current
        await self.mirror.db.commit()

    def ranked(self, path, targets):
        def score(target):
            source = self.mirror.sources.owners.get((path, target))
            metric = self.metric(source)
            # Preserve discovery's domestic preference for equal quality.
            return {'excellent': 0, 'good': 1, 'observing': 2, 'stale': 3, 'poor': 4}[metric['grade']]
        return sorted(targets, key=score)

    def needs_cache(self, path, targets):
        if not self.settings()['quality_adaptive_cache'] or not targets:
            return False
        # A healthy alternative is enough; no duplicate storage for a bad peer.
        return all(self.metric(self.mirror.sources.owners.get((path, t)))['cache_priority'] for t in targets)

    async def schedule(self, source=None):
        await self.sync_sources()
        settings = self.settings()
        ids = {r['id'] for r in settings['source_registries'] if r['enabled']}
        if source and source not in ids:
            raise ValueError('Unknown or disabled source')
        # Manual requests still use the single staggered worker and byte budget.
        for key in ([source] if source else ids):
            self.next_check[key] = time.time() + random.uniform(5, 120)
            await self.mirror.db.execute('INSERT OR REPLACE INTO source_quality_schedule VALUES(?,?)', (key, self.next_check[key]))
        await self.mirror.db.commit()
        self.wake.set()

    async def history(self, source):
        async with self.mirror.db.execute('SELECT at,outcome,bytes,speed,ttfb_ms,status FROM source_quality_samples WHERE source=? ORDER BY at DESC LIMIT 40', (source,)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def snapshot(self):
        settings = self.settings()
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        budget = await self.mirror.one('SELECT bytes FROM source_quality_budget WHERE day=?', (day,))
        return dict(observer=settings['public_url'], perspective='node-to-source',
                    daily_bytes=budget['bytes'] if budget else 0,
                    daily_limit=settings['quality_daily_mib'] * 1024 ** 2,
                    last_probe=self.last_probe, next_at=min(self.next_check.values(), default=None),
                    sources={r['id']: {**self.metric(r['id']), 'next_at': self.next_check.get(r['id'])}
                             for r in settings['source_registries']})

    async def probe(self, source, target, size):
        settings = self.settings()
        cap = min(settings['quality_sample_kib'] * 1024, size or settings['quality_sample_kib'] * 1024)
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        async with self.lock:
            budget = await self.mirror.one('SELECT bytes FROM source_quality_budget WHERE day=?', (day,))
            if (budget['bytes'] if budget else 0) + cap > settings['quality_daily_mib'] * 1024 ** 2:
                return False
            # Reserve the full cap before network IO, including failed attempts.
            await self.mirror.db.execute('INSERT INTO source_quality_budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes', (day, cap))
            await self.mirror.db.commit()
        start = time.monotonic()
        written, status, ttfb, outcome = 0, None, None, 'error'
        try:
            async with self.http.get(target, headers={'Range': f'bytes=0-{cap-1}'}, allow_redirects=False) as response:
                status = response.status
                if status in (404, 410):
                    outcome = 'missing'
                elif status in (200, 206) and response.headers.get('Content-Encoding', 'identity') == 'identity':
                    if status == 206:
                        match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or int(match[1]) + 1 != min(cap, int(match[2])):
                            raise ValueError('Invalid range response')
                    while written < cap:
                        block = await response.content.read(min(32768, cap-written))
                        if not block:
                            break
                        if ttfb is None:
                            ttfb = (time.monotonic()-start)*1000
                        written += len(block)
                    expected = min(cap, response.content_length) if response.content_length is not None else cap
                    if written and written == expected:
                        outcome = 'ok'
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        except Exception:
            # aiohttp transport failures should not terminate the scheduler.
            pass
        elapsed = max(time.monotonic()-start, .001)
        await self.mirror.db.execute('INSERT INTO source_quality_samples(source,target,at,outcome,bytes,speed,ttfb_ms,status) VALUES(?,?,?,?,?,?,?,?)',
            (source, target, time.time(), outcome, written, written/elapsed if outcome == 'ok' else 0, ttfb, status))
        await self.mirror.db.commit()
        self.last_probe = dict(source=source, at=time.time(), outcome=outcome)
        await self.refresh_metrics()
        return True

    async def tick(self):
        settings, now = self.settings(), time.time()
        if not settings['enabled'] or not settings['quality_enabled']:
            return
        await self.sync_sources()
        if now < self.next_global:
            return
        enabled = {r['id'] for r in settings['source_registries'] if r['enabled']}
        for key in enabled - self.next_check.keys():
            self.next_check[key] = now + random.uniform(120, 3600)
            await self.mirror.db.execute('INSERT OR REPLACE INTO source_quality_schedule VALUES(?,?)', (key, self.next_check[key]))
        due = sorted((self.next_check[k], k) for k in enabled if self.next_check[k] <= now)
        if not due:
            await self.mirror.db.commit()
            return
        key = due[0][1]
        # At most one real, verified mapping per tick. No directory/HTML speed tests.
        count = await self.mirror.one('SELECT COUNT(*) n FROM source_files WHERE source=? AND expires>?', (key, now))
        row = await self.mirror.one('SELECT f.target,e.size FROM source_files f LEFT JOIN expected e ON e.url=f.origin WHERE f.source=? AND f.expires>? ORDER BY f.path LIMIT 1 OFFSET ?',
            (key, now, random.randrange(count['n']))) if count['n'] else None
        if row:
            await self.probe(key, row['target'], row['size'])
        self.next_global = time.time() + random.uniform(120, 300)
        interval = settings['quality_interval_hours'] * 3600 * random.uniform(.75, 1.25)
        if self.last_probe and self.last_probe['source'] == key and self.last_probe['outcome'] == 'error':
            # A bad source is observed less often; it must not consume the budget.
            interval *= 2
        self.next_check[key] = time.time() + interval
        await self.mirror.db.execute('INSERT OR REPLACE INTO source_quality_schedule VALUES(?,?)', (key, self.next_check[key]))
        await self.mirror.db.commit()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging
                logging.exception('Source quality sampling will retry')
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), random.uniform(120, 300))
            except asyncio.TimeoutError:
                pass
