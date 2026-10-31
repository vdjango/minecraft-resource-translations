"""Bounded source mappings and conservative local egress admission."""
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from .federation import origin

GIB = 1024 ** 3
ROUTING_DEFAULTS = dict(monthly_egress_gib=0, egress_threshold=90,
                        bandwidth_mbps=0, max_serving=32, sources=[])
_SOURCE_INDEX = {}


def validate_sources(sources, public_url):
    if not isinstance(sources, list) or len(sources) > 2048:
        raise ValueError('invalid source mappings')
    result = []
    for row in sources:
        if not isinstance(row, dict) or set(row) != {'prefix', 'target', 'mode', 'enabled'}:
            raise ValueError('invalid source mapping')
        prefix, target = row['prefix'], row['target']
        if (not isinstance(prefix, str) or not prefix.startswith('/') or len(prefix) > 1024
                or any(c in prefix for c in ('%', '\\', '?', '#', ':', '\x00'))
                or any(ord(c)<32 for c in prefix)
                or any(p in ('.', '..') for p in prefix.split('/'))
                or not isinstance(target, str) or len(target) > 1600 or any(ord(c)<32 for c in target)
                or row['mode'] not in ('redirect', 'fallback') or type(row['enabled']) is not bool):
            raise ValueError('invalid source mapping')
        parsed = urlsplit(target)
        base = origin(f'{parsed.scheme}://{parsed.netloc}')
        if (base == public_url or parsed.query or parsed.fragment
                or any(c in parsed.path for c in ('%', '\\', '\x00'))
                or any(p in ('.', '..') for p in parsed.path.split('/'))
                or prefix.endswith('/') != parsed.path.endswith('/')):
            raise ValueError('source must map the same file or directory shape')
        if any(n['prefix'] == prefix and n['mode'] == row['mode'] for n in result):
            raise ValueError('duplicate source mapping')
        result.append(dict(row, target=base + parsed.path))
    return result


def source_target(settings, path, mode):
    sources = settings['sources']
    key = id(sources)
    indexed = _SOURCE_INDEX.get(key)
    if indexed is None:
        exact, directories = {}, []
        for row in sources:
            if not row['enabled']:
                continue
            if row['prefix'].endswith('/'):
                directories.append(row)
            else:
                exact[(row['mode'], row['prefix'])] = row.get('targets',[row['target']])
        if len(_SOURCE_INDEX) >= 8:
            _SOURCE_INDEX.pop(next(iter(_SOURCE_INDEX)))
        indexed = (sources, exact, sorted(directories, key=lambda row: len(row['prefix']), reverse=True))
        _SOURCE_INDEX[key] = indexed
    exact = indexed[1].get((mode, path))
    if exact:
        return exact[0]
    for row in indexed[2]:
        prefix = row['prefix']
        if (row['enabled'] and row['mode'] == mode
                and path.startswith(prefix)):
            return row['target'] + quote(path[len(prefix):], safe='/@+')
    return None


def source_targets(settings, path, mode):
    first = source_target(settings,path,mode)
    indexed = _SOURCE_INDEX[id(settings['sources'])]
    return indexed[1].get((mode,path),[first] if first else [])


def requested_bytes(request, size):
    if request.method == 'HEAD':
        return 0
    try:
        start, stop = request.http_range.start, request.http_range.stop
    except ValueError:
        return 0  # FileResponse itself returns 416.
    if start is None:
        return size
    if start < 0:
        return min(size, -start)
    if start >= size:
        return 0
    return max(0, min(size, stop if stop is not None else size) - start)


class Egress:
    def __init__(self, db):
        self.db = db
        self.recent = deque()
        self.window_bytes = 0

    async def open(self):
        await self.db.execute('CREATE TABLE IF NOT EXISTS egress(period TEXT PRIMARY KEY,bytes INTEGER NOT NULL)')
        await self.db.commit()

    def window(self):
        now = time.monotonic()
        while self.recent and now-self.recent[0][0] >= 60:
            self.window_bytes -= self.recent.popleft()[1]
        return self.window_bytes

    async def snapshot(self, settings):
        period = datetime.now(timezone.utc).strftime('%Y-%m')
        async with self.db.execute('SELECT bytes FROM egress WHERE period=?', (period,)) as cursor:
            row = await cursor.fetchone()
        return dict(period=period, bytes=row[0] if row else 0, window_bytes=self.window(),
                    monthly_limit_bytes=settings['monthly_egress_gib']*GIB,
                    threshold_percent=settings['egress_threshold'], bandwidth_mbps=settings['bandwidth_mbps'])

    async def reason(self, size, settings, active):
        state = await self.snapshot(settings)
        if settings['monthly_egress_gib'] and state['bytes']+size > state['monthly_limit_bytes']*settings['egress_threshold']/100:
            return 'monthly_budget'
        # Admission based on bytes accepted in a rolling minute, not packet shaping.
        if settings['bandwidth_mbps'] and state['window_bytes']+size > settings['bandwidth_mbps']*1_000_000/8*60*settings['egress_threshold']/100:
            return 'bandwidth_budget'
        if active >= settings['max_serving']:
            return 'serving_busy'
        return None

    async def admit(self, size, settings, active):
        # Caller serializes admission with the cache pin/reservation lock.
        reason = await self.reason(size, settings, active)
        if reason or not size:
            return reason
        period = datetime.now(timezone.utc).strftime('%Y-%m')
        await self.db.execute('INSERT INTO egress VALUES(?,?) ON CONFLICT(period) DO UPDATE SET bytes=bytes+excluded.bytes', (period, size))
        await self.db.commit()
        second = int(time.monotonic())
        if self.recent and self.recent[-1][0] == second:
            self.recent[-1] = (second, self.recent[-1][1]+size)
        else:
            self.recent.append((second, size))
        self.window_bytes += size
        return None
