"""Room-whitelisted S3 uploads: bounded reservations, checksums and lease-aware cleanup."""
import asyncio
import logging
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from .mirror_storage import S3Storage, StorageError, profile_unit

TTL = 600
WINDOW = 32


class RoomDirectUploads:
    def __init__(self, cloud):
        self.cloud, self.mirror = cloud, cloud.mirror
        self.verifying = asyncio.Semaphore(4)

    async def open(self):
        await self.mirror.db.executescript('''
            CREATE TABLE IF NOT EXISTS room_direct_uploads(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,digest TEXT NOT NULL,
                profile TEXT NOT NULL,route TEXT NOT NULL,object_key TEXT NOT NULL,
                size INTEGER NOT NULL,day INTEGER NOT NULL,expires REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending');
            CREATE INDEX IF NOT EXISTS room_direct_active
                ON room_direct_uploads(scope,digest,state);
        ''')
        await self.mirror.db.commit()

    async def reserved(self, profile, day):
        row = await self.mirror.one('''SELECT COALESCE(SUM(size),0) bytes,
            COALESCE(SUM(CASE WHEN day=? THEN size ELSE 0 END),0) today
            FROM room_direct_uploads WHERE profile=? AND state IN ('pending','abandoned')''', (day, profile))
        return row

    async def attach(self, scope, items):
        """No network probes on the plan path. Return a bounded batch of exact-file grants."""
        if self.cloud.settings()['qiniu_paused']:
            return
        for item in items:
            route_name = self.mirror.room_cache.route_name(item['path'])
            route = self.cloud.route(route_name)
            profile = self.cloud.profile(route['profile'])
            if route['mode'] != 'cloud' or not profile or not profile['private'] or profile.get('provider') not in ('s3', 'rainyun'):
                continue  # Other providers and local copies retain the verified relay.
            endpoint = urlsplit(profile.get('endpoint','')).hostname or ''
            if profile.get('provider') != 'rainyun' and not endpoint.endswith('.amazonaws.com'):
                continue  # Unknown compatible providers must prove checksum support first.
            try:
                async with self.cloud.upload_lock:
                    now, day = time.time(), int(time.time() // 86400)
                    ref = await self.mirror.room_cache.one('SELECT need,lease_until FROM files WHERE scope=? AND digest=?', (scope,item['sha256']))
                    if not ref or not ref['need'] or ref['lease_until'] <= now:
                        continue
                    row = await self.mirror.one('''SELECT * FROM room_direct_uploads
                        WHERE scope=? AND digest=? AND profile=? AND route=? AND state='pending'
                        ORDER BY expires DESC LIMIT 1''', (scope, item['sha256'], profile['id'], route_name))
                    if row and row['expires'] <= now:
                        row = None
                    if not row:
                        other = await self.mirror.one('''SELECT 1 FROM room_direct_uploads
                            WHERE digest=? AND profile=? AND route=? AND scope!=? AND state='pending' AND expires>? LIMIT 1''',
                            (item['sha256'],profile['id'],route_name,scope,now))
                        if other:
                            item['direct_deferred'] = True
                            continue
                        count = await self.mirror.one("SELECT COUNT(*) n FROM room_direct_uploads WHERE scope=? AND state='pending' AND expires>?", (scope, now))
                        if count['n'] >= WINDOW:
                            item['direct_deferred'] = True
                            continue
                        used = await self.mirror.one("SELECT COALESCE(SUM(size),0) bytes FROM object_locations WHERE profile=? AND state='ready'", (profile['id'],))
                        spent = await self.mirror.one('SELECT uploaded FROM cloud_budget WHERE profile=? AND day=?', (profile['id'], day))
                        reserved = await self.reserved(profile['id'], day)
                        transfers = [entry for entry in self.cloud.reservations.values() if entry['profile'] == profile['id']]
                        if (used['bytes'] + reserved['bytes'] + sum(e['size'] for e in transfers) + item['size'] > profile['quota_gib'] * profile_unit(profile)
                                or (spent['uploaded'] if spent else 0) + reserved['today'] + sum(e['size'] for e in transfers if e['day'] == day) + item['size'] > profile['daily_upload_gib'] * profile_unit(profile)):
                            item['direct_deferred'] = True
                            continue
                        identifier = secrets.token_hex(16)
                        # Content-addressed key + signed, cloud-checked SHA-256:
                        # even a replay can write only these exact bytes, never
                        # replace another resource or allocate arbitrary keys.
                        key = f"{profile['prefix']}/{route['path']}/sha256/{item['sha256'][:2]}/{item['sha256']}"
                        row = dict(id=identifier, scope=scope, digest=item['sha256'], profile=profile['id'],
                            route=route_name, object_key=key, size=item['size'], day=day, expires=now+TTL)
                        await self.mirror.db.execute('''INSERT INTO room_direct_uploads
                            (id,scope,digest,profile,route,object_key,size,day,expires) VALUES(?,?,?,?,?,?,?,?,?)''',
                            tuple(row[k] for k in ('id','scope','digest','profile','route','object_key','size','day','expires')))
                        await self.mirror.db.commit()
                    expires = max(1, int(row['expires'] - now))
                    storage = self.cloud.storage(profile['id'])
                    if not isinstance(storage, S3Storage):
                        continue
                    item['direct_upload'] = dict(storage.direct_upload(row['object_key'], row['digest'], row['size'], expires=expires),
                        id=row['id'], expires_at=int(row['expires']))
            except StorageError:
                logging.warning('Direct room upload unavailable for profile %s; using relay', profile['id'])

    async def complete(self, request):
        from .room_cache import authorize
        self.mirror.room_cache.enabled()
        scope, _ = authorize(request, write=True)
        try:
            body = await request.clone(client_max_size=8192).json()
            ids = body['uploads']
            if set(body) != {'uploads'} or not isinstance(ids, list) or not 1 <= len(ids) <= 16 or any(
                    not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value) for value in ids):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text='上传确认清单无效。') from None
        results = await asyncio.gather(*(self.confirm(scope, identifier) for identifier in dict.fromkeys(ids)))
        return web.json_response(dict(files=results), headers={'Cache-Control':'no-store'})

    async def confirm(self, scope, identifier):
        async with self.verifying:
            row = await self.mirror.one('SELECT * FROM room_direct_uploads WHERE id=? AND scope=?', (identifier,scope))
            if not row:
                return dict(id=identifier, state='rejected', reason='源站未请求此文件')
            cache = self.mirror.room_cache
            ref = await cache.one('SELECT * FROM files WHERE scope=? AND digest=?', (scope,row['digest']))
            if not ref or ref['lease_until'] <= time.time() or row['state'] == 'abandoned':
                return dict(id=identifier, state='rejected', reason='房间缓存需求已结束')
            if row['state'] == 'ready':
                return dict(id=identifier, state='cached', sha256=row['digest'], size=row['size'])
            if not ref['need'] or row['expires'] + 120 <= time.time():
                return dict(id=identifier, state='rejected', reason='上传凭证已过期或文件已不再需要')
            try:
                await self.cloud.storage(row['profile']).verify_direct(row['object_key'],row['digest'],row['size'])
            except StorageError:
                return dict(id=identifier, state='pending', reason='云端文件尚未通过大小和 SHA-256 校验')
            # Recheck after cloud I/O. Closing the room wins over an in-flight completion.
            async with cache.lock:
                ref = await cache.one('SELECT * FROM files WHERE scope=? AND digest=?', (scope,row['digest']))
                if not ref or ref['lease_until'] <= time.time() or not ref['need']:
                    return dict(id=identifier, state='rejected', reason='房间缓存需求已结束')
                async with self.cloud.upload_lock:
                    fresh = await self.mirror.one('SELECT state FROM room_direct_uploads WHERE id=?', (identifier,))
                    if not fresh or fresh['state'] != 'pending':
                        return dict(id=identifier, state='cached' if fresh and fresh['state']=='ready' else 'rejected', sha256=row['digest'], size=row['size'])
                    await self.mirror.db.execute('''INSERT OR REPLACE INTO object_locations
                        (scope,digest,profile,route,object_key,size,state,verified_at,last_error)
                        VALUES('room',?,?,?,?,?,'ready',?,NULL)''',
                        (row['digest'],row['profile'],row['route'],row['object_key'],row['size'],time.time()))
                    await self.mirror.db.execute('''INSERT INTO cloud_budget VALUES(?,?,?)
                        ON CONFLICT(profile,day) DO UPDATE SET uploaded=uploaded+excluded.uploaded''',
                        (row['profile'],row['day'],row['size']))
                    await self.mirror.db.execute("UPDATE room_direct_uploads SET state='ready' WHERE id=?", (identifier,))
                    await self.mirror.db.commit()
                await cache.db.execute('''INSERT INTO objects(digest,size,created,local) VALUES(?,?,?,0)
                    ON CONFLICT(digest) DO NOTHING''', (row['digest'],row['size'],time.time()))
                await cache.db.commit()
            return dict(id=identifier, state='cached', sha256=row['digest'], size=row['size'])

    async def release(self, scope):
        await self.mirror.db.execute("UPDATE room_direct_uploads SET state='abandoned' WHERE scope=? AND state='pending'", (scope,))
        await self.mirror.db.commit()

    async def cleanup(self):
        # Keep abandoned reservations until signatures expire: a late PUT must
        # not recreate an untracked object after cleanup or free quota early.
        async with self.mirror.db.execute('''SELECT * FROM room_direct_uploads
            WHERE expires<? ORDER BY expires LIMIT 32''', (time.time()-120,)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            if row['state'] != 'ready':
                # An earlier expired grant can share its content-addressed key
                # with a later grant or a now-verified copy in another room.
                ready = await self.mirror.one("SELECT 1 FROM object_locations WHERE profile=? AND object_key=? AND state='ready'", (row['profile'],row['object_key']))
                active = await self.mirror.one("SELECT 1 FROM room_direct_uploads WHERE id!=? AND profile=? AND object_key=? AND expires>? LIMIT 1", (row['id'],row['profile'],row['object_key'],time.time()-120))
                if active and not ready:
                    continue
                try:
                    if not ready:
                        await self.cloud.storage(row['profile']).delete(row['object_key'])
                except StorageError:
                    continue
            await self.mirror.db.execute('DELETE FROM room_direct_uploads WHERE id=?', (row['id'],))
        await self.mirror.db.commit()
