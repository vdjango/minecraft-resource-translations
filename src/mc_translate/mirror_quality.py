"""Small, staggered transfer samples. Redirects never reveal client throughput."""
import asyncio
import ipaddress
import math
import random
import re
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .federation import PublicResolver
from .content_routing import CONTENT_CDNS, content_key, recent_health

QUALITY_DEFAULTS = dict(quality_enabled=True, quality_adaptive_cache=True,
                        quality_interval_hours=6, quality_daily_mib=64,
                        quality_sample_kib=256, quality_fast_kibps=2048,
                        quality_slow_kibps=128, quality_content_minutes=5,
                        quality_backup_minutes=10, quality_modrinth_minutes=15)


def public_sample_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password or len(value) > 8192):
        raise ValueError('Invalid sample download URL')
    # PublicResolver checks DNS results; IP literals bypass DNS resolution.
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname == 'localhost' or parsed.hostname.endswith('.local'):
            raise ValueError('Private sample download blocked')
    else:
        if not address.is_global:
            raise ValueError('Private sample download blocked')
    return value


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
    speed_kib = speed / 1024 if speed is not None else 0
    speed_tier = min(7, max(0, math.floor(math.log2(speed_kib / 64)) + 1)) if speed_kib >= 64 else 0
    score = round(min(100, speed_tier * 10 + (success or 0) * 30)) if mature else None
    return dict(grade=grade, score=score, speed_tier=speed_tier, samples=len(attempts), days=days, success_rate=success,
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
        self.recent = {}
        self.content_targets = {}
        self.targets_checked = 0

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
        # Old probes stopped at a normal CDN redirect and miscounted it as an outage.
        await self.mirror.db.execute(
            "UPDATE source_quality_schedule SET next_at=MIN(next_at,?) WHERE source IN ("
            "SELECT DISTINCT source FROM source_quality_samples "
            "WHERE outcome='error' AND status BETWEEN 300 AND 399)",
            (time.time() + random.uniform(30, 90),))
        await self.mirror.db.execute("DELETE FROM source_quality_samples WHERE outcome='error' AND status BETWEEN 300 AND 399")
        async with self.mirror.db.execute('SELECT source,next_at FROM source_quality_schedule') as cursor:
            self.next_check = {r['source']: r['next_at'] for r in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT source,url FROM source_quality_identity') as cursor:
            self.identities = {r['source']: r['url'] for r in await cursor.fetchall()}
        last = await self.mirror.one('SELECT MAX(at) at FROM source_quality_samples')
        self.next_global = (last['at'] or 0) + random.uniform(15, 35)
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
        return read_settings(self.mirror.config,effective=False)

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
        self.recent = {key: rows[-40:] for key, rows in grouped.items()}
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
        current.update({'cdn:' + host: 'https://' + host for host in CONTENT_CDNS})
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

    def content_metric(self, url):
        key = content_key(url)
        return {**self.metrics.get(key, assessment([], self.settings())),
                **recent_health(self.recent.get(key, []), self.settings()['quality_slow_kibps'])}

    async def sync_content_targets(self):
        """Only sample indexed public files, never unverified room-supplied URLs."""
        now = time.time()
        if now - self.targets_checked < 60:
            return
        cache = getattr(self.mirror, 'room_cache', None)
        if not cache or not getattr(cache, 'db', None):
            return
        targets = {}
        for host in CONTENT_CDNS:
            prefix = 'https://' + host + '/'
            async with cache.db.execute('''SELECT url,size,updated FROM catalog_files
                    WHERE url>=? AND url<? AND updated>? AND size>=65536
                    ORDER BY updated DESC LIMIT 16''',
                    (prefix, prefix[:-1] + '0', now - 30*86400)) as cursor:
                rows = [dict(row) for row in await cursor.fetchall()]
            if rows:
                targets['cdn:' + host] = rows
        self.content_targets, self.targets_checked = targets, now

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
        await self.sync_content_targets()
        ids = {r['id'] for r in settings['source_registries'] if r['enabled']} | self.content_targets.keys()
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
        await self.sync_content_targets()
        async with self.mirror.db.execute(
                'SELECT source,COUNT(*) AS mapped_files FROM source_files WHERE expires>? GROUP BY source',
                (time.time(),)) as cursor:
            mapped = {row['source']: row['mapped_files'] for row in await cursor.fetchall()}
        return dict(observer=settings['public_url'], perspective='node-to-source',
                    daily_bytes=budget['bytes'] if budget else 0,
                    daily_limit=settings['quality_daily_mib'] * 1024 ** 2,
                    last_probe=self.last_probe, next_at=min(self.next_check.values(), default=None),
                    sources={r['id']: {**self.metric(r['id']), 'next_at': self.next_check.get(r['id']),
                                       **recent_health(self.recent.get(r['id'], []), settings['quality_slow_kibps']),
                                       'mapped_files': mapped.get(r['id'], 0)}
                             for r in settings['source_registries']},
                    content_sources=[dict(id='cdn:'+host, host=host, name=publisher,
                        enabled='cdn:'+host in self.content_targets,
                        next_at=self.next_check.get('cdn:'+host),
                        mapped_files=len(self.content_targets.get('cdn:'+host, [])),
                        **self.content_metric('https://'+host)) for host,publisher in CONTENT_CDNS.items()],
                    budget_paused=bool(budget and budget['bytes'] >= settings['quality_daily_mib'] * 1024**2))

    @asynccontextmanager
    async def sample_response(self, target, cap):
        # BMCLAPI dispatches to download nodes. Keep the byte cap and a single
        # total timeout across the entire chain, checking every public address.
        async with asyncio.timeout(10):
            for _ in range(5):
                target = public_sample_url(target)
                async with self.http.get(target, headers={'Range': f'bytes=0-{cap-1}'}, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('Download redirect has no location')
                        target = urljoin(str(response.url), location)
                        continue
                    yield response
                    return
            raise ValueError('Too many download redirects')

    async def probe(self, source, target, size):
        settings = self.settings()
        maximum = min(128, settings['quality_sample_kib']) if source.startswith('cdn:') else settings['quality_sample_kib']
        cap = min(maximum * 1024, size or maximum * 1024)
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        async with self.lock:
            budget = await self.mirror.one('SELECT bytes FROM source_quality_budget WHERE day=?', (day,))
            if (budget['bytes'] if budget else 0) + cap > settings['quality_daily_mib'] * 1024 ** 2:
                return False
            # Reserve the full cap before network IO, including failed attempts.
            await self.mirror.db.execute('INSERT INTO source_quality_budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes', (day, cap))
            await self.mirror.db.commit()
        start = time.monotonic()
        written, status, ttfb, outcome, payload_started = 0, None, None, 'error', None
        try:
            async with self.sample_response(target, cap) as response:
                status = response.status
                if status in (404, 410):
                    outcome = 'missing'
                elif status in (200, 206) and response.headers.get('Content-Encoding', 'identity') == 'identity':
                    if response.headers.get('Content-Type', '').split(';')[0] in ('text/html', 'application/xhtml+xml'):
                        raise ValueError('Expected a file, received HTML')
                    if status == 206:
                        match = re.fullmatch(r'bytes 0-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if not match or int(match[1]) + 1 != min(cap, int(match[2])):
                            raise ValueError('Invalid range response')
                        if size and int(match[2]) != size:
                            raise ValueError('File size changed')
                    elif size and response.content_length != size:
                        raise ValueError('File size changed')
                    payload_started = time.monotonic()
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
        elapsed = max(time.monotonic()-(payload_started or start), .001)
        await self.mirror.db.execute('INSERT INTO source_quality_samples(source,target,at,outcome,bytes,speed,ttfb_ms,status) VALUES(?,?,?,?,?,?,?,?)',
            (source, target, time.time(), outcome, written, written/elapsed if outcome == 'ok' else 0, ttfb, status))
        await self.mirror.db.commit()
        self.last_probe = dict(source=source, at=time.time(), outcome=outcome)
        await self.refresh_metrics()
        if source.startswith('cdn:') and outcome == 'missing':
            cache = self.mirror.room_cache
            await cache.db.execute("UPDATE catalog_files SET state='unavailable',updated=? WHERE url=?", (time.time(),target))
            await cache.db.commit()
        return True

    async def tick(self):
        settings, now = self.settings(), time.time()
        if not settings['enabled'] or not settings['quality_enabled']:
            return
        await self.sync_sources()
        await self.sync_content_targets()
        if now < self.next_global:
            return
        enabled = {r['id'] for r in settings['source_registries'] if r['enabled']} | self.content_targets.keys()
        for key in enabled - self.next_check.keys():
            self.next_check[key] = now + (random.uniform(15,60) if key.startswith('cdn:') else random.uniform(120,3600))
            await self.mirror.db.execute('INSERT OR REPLACE INTO source_quality_schedule VALUES(?,?)', (key, self.next_check[key]))
        due = sorted((self.next_check[k], k) for k in enabled if self.next_check[k] <= now)
        if not due:
            await self.mirror.db.commit()
            return
        key = due[0][1]
        # At most one real, verified mapping per tick. No directory/HTML speed tests.
        if key.startswith('cdn:'):
            target = random.choice(self.content_targets[key])
            row = dict(target=target['url'], size=target['size'])
        else:
            count = await self.mirror.one('SELECT COUNT(*) n FROM source_files WHERE source=? AND expires>?', (key, now))
            row = await self.mirror.one('SELECT f.target,e.size FROM source_files f LEFT JOIN expected e ON e.url=f.origin WHERE f.source=? AND f.expires>? ORDER BY f.path LIMIT 1 OFFSET ?',
                (key, now, random.randrange(count['n']))) if count['n'] else None
        if row:
            await self.probe(key, row['target'], row['size'])
        self.next_global = time.time() + random.uniform(15, 35)
        if key.startswith('cdn:'):
            host = key[4:]
            active = max(t['updated'] for t in self.content_targets[key]) > now - 86400
            minutes = settings['quality_modrinth_minutes'] if CONTENT_CDNS[host] == 'Modrinth' else (
                settings['quality_content_minutes'] if active else settings['quality_backup_minutes'])
            interval = minutes * 60 * random.uniform(.8, 1.2)
        else:
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
                await asyncio.wait_for(self.wake.wait(), random.uniform(15, 35))
            except asyncio.TimeoutError:
                pass
