"""Batch lookups across the public mirror and publisher indexes, without IO to sources."""
import time

from aiohttp import web

from .mirror_routing import requested_bytes


async def public_index(mirror, wanted):
    sources, copies = {}, {}
    settings = mirror.room_cache.settings()
    registries = {row['id']: (settings.get('source_prefer_cn', True) and row['region'] != 'cn', index)
                  for index, row in enumerate(settings['source_registries']) if row['enabled']}
    for start in range(0, len(wanted), 300):
        batch = wanted[start:start + 300]
        marks = ','.join('?' for _ in batch)
        if settings['auto_sources']:
            async with mirror.db.execute('''SELECT f.source,f.target url,f.sha1,f.expires,
                    COALESCE(e.size,c.size) size FROM source_files f
                    LEFT JOIN expected e ON e.url=f.origin AND e.sha1=f.sha1
                    LEFT JOIN cache c ON c.url=f.origin AND c.sha1=f.sha1 AND c.checked=1
                    WHERE f.sha1 IN (''' + marks + ') AND f.expires>?', [*batch,time.time()]) as cursor:
                for row in await cursor.fetchall():
                    if row['source'] in registries and row['size'] is not None:
                        sources.setdefault(row['sha1'], []).append(dict(row, state='available',
                            speed_kibps=0, updated=0, source_preference=registries[row['source']]))
        async with mirror.db.execute('''SELECT c.*,o.local FROM cache c JOIN objects o ON o.digest=c.object
                WHERE c.checked=1 AND (c.sha1 IN (''' + marks + ') OR c.object IN (' + marks + '))',
                [*batch,*batch]) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            item = dict(row)
            path = mirror.root/'objects'/row['object'][:2]/row['object']
            if not path.is_file() and not await mirror.cloud.download_url('mirror', row['object']):
                continue
            for digest in (row['object'],row['sha1']):
                if digest in batch:
                    copies.setdefault(digest,item)
    return sources, copies


async def download_mirror_copy(cache, request):
    """Serve an existing public mirror copy; never populate a missing one here."""
    mirror = cache.mirror
    digest = request.match_info['digest']
    _, copies = await public_index(mirror, [digest])
    item = copies.get(digest)
    if not item:
        return None
    candidates, _ = await public_index(mirror, [item['sha1']])
    fast = cache.routing.fast(candidates.get(item['sha1'], []))
    if fast:
        return web.Response(status=307, headers={'Location':fast[0]['url'],'Cache-Control':'no-store'})
    cloud_url = await mirror.cloud.download_url('mirror', digest,
        reserve=request.method != 'HEAD', method=request.method)
    if cloud_url:
        return web.Response(status=307, headers={'Location':cloud_url,'Cache-Control':'no-store',
            'Cloudflare-CDN-Cache-Control':'no-store'})
    from .mirror_policy import category_for
    category = category_for(item['url'])
    if cache.settings()['qiniu_routes'][category]['mode'] == 'cloud':
        # A migration staging file does not silently become a permanent origin.
        return mirror.redirect(item['url'],category,'cloud_unavailable')
    path = mirror.root/'objects'/digest[:2]/digest
    if not path.is_file():
        return None
    async with mirror.space_lock:
        blocked = await mirror.egress.admit(requested_bytes(request,item['size']),cache.settings(),
            sum(mirror.serving.values()) + sum(cache.serving.values()))
        if blocked:
            return mirror.redirect(item['url'],category,'egress_limit')
        mirror.serving[digest] = mirror.serving.get(digest,0) + 1
    from .mirror import PinnedFileResponse
    response = PinnedFileResponse(path,headers={'Content-Type':item['type'] or 'application/octet-stream',
        'Cache-Control':'public, max-age=31536000, immutable','X-Content-Type-Options':'nosniff',
        'X-Mirror-Verified':'upstream-sha1'})
    response.mirror,response.digest = mirror,digest
    await mirror.db.execute('UPDATE objects SET accessed=? WHERE digest=?',(time.time(),digest))
    await mirror.db.commit()
    return response
