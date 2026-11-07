"""Shared, hash-bound content routing; decisions never perform network IO."""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

CONTENT_CDNS = {
    'edge.forgecdn.net': 'CurseForge',
    'mediafilez.forgecdn.net': 'CurseForge',
    'media.forgecdn.net': 'CurseForge',
    'cdn.modrinth.com': 'Modrinth',
}


def content_key(url):
    host = urlsplit(url).hostname
    return 'cdn:' + host if host in CONTENT_CDNS else None


def recent_health(samples, threshold, now=None):
    """Short-term circuit breaker, independent of the long-term grade."""
    now = time.time() if now is None else now
    rows = sorted((r for r in samples if now - 2700 <= r['at'] <= now
                   and r['outcome'] != 'missing'
                   and (r['outcome'] != 'ok' or r.get('bytes',65536)>=65536)), key=lambda r: r['at'])
    state, slow, recovery, previous = 'unknown', 0, 0, -float('inf')
    for row in rows:
        # Multiple files sampled together are one observation, not three outages.
        if row['at'] - previous < 60:
            continue
        previous = row['at']
        if row['outcome'] != 'ok':
            state, slow, recovery = 'unavailable', 0, 0
        elif row['speed'] < threshold * 1024:
            slow, recovery = slow + 1, 0
            if slow >= 3:
                state = 'slow'
        elif row['speed'] >= threshold * 2048:
            slow, recovery = 0, recovery + 1
            if state not in ('slow', 'unavailable') or recovery >= 2:
                state = 'healthy'
        else:
            slow, recovery = 0, 0
            if state not in ('slow', 'unavailable'):
                state = 'healthy'
    last = rows[-1] if rows else None
    return dict(health=state, recent_at=last['at'] if last else None,
                recent_speed_bps=last['speed'] if last and last['outcome'] == 'ok' else None,
                recent_samples=len(rows), slow_streak=slow)


class ContentRouting:
    def __init__(self, cache):
        self.cache = cache
        self.context = ContextVar('content-routing',default=None)

    @contextmanager
    def batch(self):
        """One consistent settings/quality snapshot per manifest, task-local."""
        if self.context.get() is not None:
            yield
            return
        token = self.context.set(dict(settings=self.cache.mirror.quality.settings(),metrics={},now=time.time()))
        try:
            yield
        finally:
            self.context.reset(token)

    def evidence(self, candidate):
        if self.context.get() is None:
            with self.batch():
                return self.evidence(candidate)
        item = dict(candidate)
        quality = self.cache.mirror.quality
        context = self.context.get()
        host = urlsplit(item['url']).hostname
        key = item.get('source') or host
        if key not in context['metrics']:
            if item.get('source'):
                metric = quality.metric(key)
                context['metrics'][key] = dict(metric, **recent_health(quality.recent.get(key, []),
                    context['settings']['quality_slow_kibps']))
            else:
                context['metrics'][key] = quality.content_metric(item['url'])
        metric = context['metrics'][key]
        threshold = context['settings']['quality_slow_kibps']
        health = metric['health']
        fresh = item.get('updated', 0) >= context['now'] - 2 * 86400
        speed = metric['recent_speed_bps']
        if (health == 'unknown' and fresh and metric['recent_at'] is None
                and (metric.get('last_at') or 0) <= item.get('updated', 0)):
            speed = item.get('speed_kibps', 0) * 1024 or None
            if item['state'] == 'slow' or speed is not None and speed < threshold * 1024:
                health = 'slow'
        if item['state'] == 'unavailable':
            health = 'unavailable'
        # Zero is unmeasured, never proof that a fallback copy can be deleted.
        return dict(item, health=health, confirmed_fast=(health not in ('slow', 'unavailable')
                    and speed is not None and speed >= threshold * 1024),
                    measured_at=metric['recent_at'] or item.get('updated'),
                    effective_speed_bps=speed)

    def ranked(self, candidates, *, include_slow=False):
        with self.batch():
            rows = [self.evidence(row) for row in candidates]
        rows = [row for row in rows if urlsplit(row['url']).hostname != 'mod.mcimirror.top'
                and (include_slow or row['health'] not in ('slow', 'unavailable'))]
        def rank(row):
            publisher = CONTENT_CDNS.get(urlsplit(row['url']).hostname, '')
            return (row['health'] in ('slow', 'unavailable'), publisher == 'Modrinth',
                    not row['confirmed_fast'], -(row['effective_speed_bps'] or 0),
                    row.get('source_preference', (False, -1)))
        return sorted(rows, key=rank)

    def fast(self, candidates):
        return [row for row in self.ranked(candidates) if row['confirmed_fast']]
