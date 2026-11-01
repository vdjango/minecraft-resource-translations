"""Bounded, authenticated views of stored objects and background work."""
import base64
import binascii
import json
import time
from urllib.parse import urlsplit

from .mirror_policy import category_for


def page_arguments(data):
    query = str(data.get('query', '')).strip()
    category = str(data.get('category', ''))
    if len(query) > 160 or category not in ('', 'core', 'loaders', 'mods', 'java', 'libraries', 'assets'):
        raise ValueError('invalid resource filter')
    cursor = data.get('cursor')
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            cursor = json.loads(base64.urlsafe_b64decode(cursor.encode('ascii')))
            if len(cursor) != 2 or not isinstance(cursor[0], (int, float)) or not isinstance(cursor[1], str) or len(cursor[1]) > 2048:
                raise ValueError()
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            raise ValueError('invalid resource cursor') from None
    return query, category, cursor


def next_cursor(stamp, key):
    return base64.urlsafe_b64encode(json.dumps([stamp, key]).encode()).decode()


class MirrorObservation:
    def __init__(self, mirror):
        self.mirror = mirror
        self.transfers = {}

    async def open(self):
        db = self.mirror.db
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS mirror_jobs(id INTEGER PRIMARY KEY, started REAL, body TEXT);
            CREATE INDEX IF NOT EXISTS cache_listing ON cache(fetched DESC,url DESC);
            CREATE INDEX IF NOT EXISTS expected_category ON expected(category,url);
        ''')
        async with db.execute('SELECT id,body FROM mirror_jobs') as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            data = json.loads(row['body'])
            if data.get('state') == 'running':
                data.update(state='interrupted', finished_at=time.time())
                await db.execute('UPDATE mirror_jobs SET body=? WHERE id=?', (json.dumps(data), row['id']))
        await db.commit()

    async def record(self):
        state = dict(self.mirror.job_state)
        stamp = state.get('started_at', time.time())
        await self.mirror.db.execute('INSERT OR REPLACE INTO mirror_jobs VALUES(?,?,?)',
                                    (int(stamp * 1000000), stamp, json.dumps(state)))
        await self.mirror.db.execute('DELETE FROM mirror_jobs WHERE id NOT IN (SELECT id FROM mirror_jobs ORDER BY id DESC LIMIT 100)')
        await self.mirror.db.commit()

    async def snapshot(self):
        async with self.mirror.db.execute('SELECT body FROM mirror_jobs ORDER BY id DESC LIMIT 12') as cursor:
            history = [json.loads(row['body']) for row in await cursor.fetchall()]
        async with self.mirror.db.execute('SELECT COUNT(*) count FROM mirror_failed_files') as cursor:
            failed_count = (await cursor.fetchone())['count']
        async with self.mirror.db.execute(
                'SELECT url,category,status,error,attempts,last_failed FROM mirror_failed_files ORDER BY last_failed DESC LIMIT 8') as cursor:
            failed_files = [dict(row) for row in await cursor.fetchall()]
        now = time.time()
        active = [dict(url=url, **row, speed=row['bytes'] / max(now-row['started_at'], .1))
                  for url, row in list(self.transfers.items())[:16]]
        return dict(transfers=active, history=history,
                    failed_queue=dict(count=failed_count, files=failed_files),
                    source_scanning=bool(self.mirror.source_job and not self.mirror.source_job.done()),
                    source_scan=dict(self.mirror.source_scan_state),
                    reserved_bytes=self.mirror.reserved, observed_at=now)

    async def files(self, data):
        query, category, cursor = page_arguments(data)
        where, args = [], []
        if query:
            where.append('(instr(lower(c.url),?)>0 OR instr(c.sha1,?)>0 OR instr(c.object,?)>0)')
            args.extend([query.lower()] * 3)
        # Inventory covers known files; URL classification also covers on-demand loaders.
        expression = """CASE
            WHEN e.category IN ('assets','assets-metadata') OR c.url LIKE '%resources.download.minecraft.net/%' THEN 'assets'
            WHEN e.category LIKE 'java-%' OR c.url LIKE '%/java-runtime/%' THEN 'java'
            WHEN e.category IN ('libraries','natives','logging') OR c.url LIKE '%libraries.minecraft.net/%' THEN 'libraries'
            WHEN c.url LIKE '%modrinth%' OR c.url LIKE '%forgecdn%' OR e.category='mods' THEN 'mods'
            WHEN e.category='loaders' OR c.url LIKE '%forge%' OR c.url LIKE '%fabric%' OR c.url LIKE '%quilt%' THEN 'loaders'
            ELSE 'core' END"""
        if category:
            where.append(expression+'=?')
            args.append(category)
        if cursor:
            where.append('(c.fetched,c.url)<(?,?)')
            args.extend(cursor)
        sql = (f'SELECT c.*, {expression} category, o.local local, '
               "(SELECT GROUP_CONCAT(profile, ', ') FROM object_locations l WHERE l.scope='mirror' AND l.digest=c.object AND l.state='ready') cloud_profiles "
               'FROM cache c LEFT JOIN expected e ON e.url=c.url LEFT JOIN objects o ON o.digest=c.object')
        if where:
            sql += ' WHERE '+' AND '.join(where)
        async with self.mirror.db.execute(sql+' ORDER BY c.fetched DESC,c.url DESC LIMIT 51', args) as result:
            rows = [dict(row) for row in await result.fetchall()]
        more = len(rows) > 50
        rows = rows[:50]
        for row in rows:
            row['source'] = urlsplit(row['url']).hostname
            row['path'] = urlsplit(row['url']).path
            row['category'] = category_for(row['url'], row['category'])
        return dict(files=rows, next_cursor=next_cursor(rows[-1]['fetched'], rows[-1]['url']) if more else None)


async def room_files(cache, data):
    query, _, cursor = page_arguments(data)
    where, args = [], []
    if query:
        where.append('(instr(lower(f.path),?)>0 OR instr(f.digest,?)>0 OR instr(f.sha1,?)>0)')
        args.extend([query.lower()] * 3)
    sql = '''SELECT f.digest,MIN(f.path) path,MAX(f.size) size,MAX(f.touched) touched,
        MAX(f.lease_until) expires_at,MAX(f.need) needed,MAX(f.reason) reason,
        MAX(o.size*o.local) stored_bytes,COUNT(DISTINCT f.scope) references_count
        FROM files f LEFT JOIN objects o ON o.digest=f.digest'''
    if where:
        sql += ' WHERE '+' AND '.join(where)
    sql += ' GROUP BY f.digest'
    if cursor:
        sql += ' HAVING (MAX(f.touched),f.digest)<(?,?)'
        args.extend(cursor)
    async with cache.db.execute(sql+' ORDER BY touched DESC,f.digest DESC LIMIT 51', args) as result:
        rows = [dict(row) for row in await result.fetchall()]
    more, now = len(rows) > 50, time.time()
    rows = rows[:50]
    locations = {}
    if rows:
        digests = [row['digest'] for row in rows]
        marks = ','.join('?' for _ in digests)
        async with cache.mirror.db.execute(
            "SELECT digest,GROUP_CONCAT(profile, ', ') profiles FROM object_locations "
            f"WHERE scope='room' AND state='ready' AND digest IN ({marks}) GROUP BY digest", digests
        ) as cursor:
            locations = {row['digest']: row['profiles'] for row in await cursor.fetchall()}
    for row in rows:
        digest = row['digest']
        row['cloud_profiles'] = locations.get(digest)
        row['state'] = ('uploading' if digest in cache.uploading else 'cached' if row['stored_bytes'] is not None
                        else 'expired' if row['expires_at'] and row['expires_at'] <= now
                        else 'awaiting_upload' if row['needed'] else 'redirect')
        row['readers'] = cache.serving.get(digest, 0)
        row['progress'] = getattr(cache, 'upload_progress', {}).get(digest, 0)
    return dict(files=rows, next_cursor=next_cursor(rows[-1]['touched'], rows[-1]['digest']) if more else None)
