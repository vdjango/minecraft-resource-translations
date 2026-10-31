"""Indexed source coverage, paginated files, redirect counters and opt-in cleanup."""
import asyncio
import time
from collections import Counter

from .mirror_sources import PRESET, document, stamp


class SourceManagement:
    def __init__(self, mirror):
        self.mirror = mirror
        self.pending = Counter()
        self.pending_totals = Counter()
        self.index_stamp = None
        self.owners = {}

    async def open(self):
        await self.mirror.db.executescript('''
            CREATE TABLE IF NOT EXISTS source_files(
                source TEXT,path TEXT,target TEXT,origin TEXT,sha1 TEXT,expires REAL,
                PRIMARY KEY(source,path));
            CREATE INDEX IF NOT EXISTS source_origin ON source_files(origin);
            CREATE INDEX IF NOT EXISTS source_target ON source_files(target,path);
            CREATE TABLE IF NOT EXISTS source_hits(path TEXT,target TEXT,hits INTEGER,PRIMARY KEY(path,target));
            CREATE TABLE IF NOT EXISTS source_totals(source TEXT PRIMARY KEY,hits INTEGER NOT NULL);
        ''')
        await self.synchronize()
        await self.mirror.db.execute('''INSERT OR IGNORE INTO source_totals
            SELECT f.source,SUM(h.hits) FROM source_files f JOIN source_hits h ON h.path=f.path AND h.target=f.target GROUP BY f.source''')
        await self.mirror.db.commit()

    def hit(self, path, target):
        # Bound memory even for malicious requests against manual directory rules.
        if len(self.pending)<4096 or (path,target) in self.pending:
            self.pending[path,target] += 1
            source = self.owners.get((path,target))
            if source:
                self.pending_totals[source] += 1

    async def flush(self):
        if not self.pending:
            return
        batch,self.pending = self.pending,Counter()
        totals,self.pending_totals = self.pending_totals,Counter()
        try:
            await self.mirror.db.executemany('INSERT INTO source_hits VALUES(?,?,?) ON CONFLICT(path,target) DO UPDATE SET hits=hits+excluded.hits',
                                            [(path,target,count) for (path,target),count in batch.items()])
            await self.mirror.db.executemany('INSERT INTO source_totals VALUES(?,?) ON CONFLICT(source) DO UPDATE SET hits=hits+excluded.hits',list(totals.items()))
            await self.mirror.db.commit()
        except BaseException:
            self.pending.update(batch)
            self.pending_totals.update(totals)
            raise

    async def synchronize(self):
        from .mirror import read_settings,upstreams
        path = self.mirror.root/'automatic-sources.json'
        settings = read_settings(self.mirror.config)
        enabled = {row['id']:row['url'] for row in settings['source_registries'] if row['enabled']}
        signature = (*stamp(path),*stamp(PRESET),tuple(enabled.items()),settings['auto_sources'])
        if signature==self.index_stamp:
            return
        data = document(str(path),*stamp(path))
        rows = []
        owners = {}
        if settings['auto_sources']:
            for item in data.get('sources',[]):
                origins = upstreams(item['prefix'])
                for entry in item.get('candidates',[]):
                    if enabled.get(entry['source'])==entry.get('base'):
                        rows.append((entry['source'],item['prefix'],entry['target'],origins[0] if origins else '',entry['sha1'],entry['expires']))
                        owners[item['prefix'],entry['target']] = entry['source']
            if not data and enabled.get('central')=='https://repo.maven.apache.org/maven2/':
                seed = document(str(PRESET),*stamp(PRESET))
                expires = seed.get('generated_at',0)+8*86400
                if expires>time.time():
                    for item in seed.get('sources',[]):
                        sha1 = seed.get('checksums',{}).get(item['prefix'])
                        origins = upstreams(item['prefix'])
                        if sha1 and origins:
                            rows.append(('central',item['prefix'],item['target'],origins[0],sha1,expires))
                            owners[item['prefix'],item['target']] = 'central'
        async with self.mirror.space_lock:
            await self.mirror.db.execute('DELETE FROM source_files')
            await self.mirror.db.executemany('INSERT OR REPLACE INTO source_files VALUES(?,?,?,?,?,?)',rows)
            await self.mirror.db.commit()
        self.index_stamp = signature
        self.owners = owners

    async def summary(self):
        async with self.mirror.db.execute('''SELECT f.source,COUNT(*) files,
            SUM(CASE WHEN c.url IS NOT NULL AND c.sha1=f.sha1 THEN 1 ELSE 0 END) local_files,
            COALESCE(SUM(CASE WHEN c.sha1=f.sha1 THEN c.size ELSE 0 END),0) local_bytes,
            COALESCE(SUM(h.hits),0) redirects FROM source_files f
            LEFT JOIN cache c ON c.url=f.origin LEFT JOIN source_hits h ON h.path=f.path AND h.target=f.target
            WHERE f.expires>? GROUP BY f.source''',(time.time(),)) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
        async with self.mirror.db.execute('SELECT source,hits FROM source_totals') as cursor:
            totals = {row['source']:row['hits'] for row in await cursor.fetchall()}
        by_source = {row['source']:row for row in rows}
        from .mirror import read_settings
        for source in read_settings(self.mirror.config)['source_registries']:
            row = by_source.setdefault(source['id'],dict(source=source['id'],files=0,local_files=0,local_bytes=0,redirects=0))
            row['redirects'] = totals.get(source['id'],0)+self.pending_totals[source['id']]
        rows = list(by_source.values())
        return rows

    async def files(self, data):
        source,offset = str(data.get('source','')),int(data.get('offset',0))
        if not 0<=offset<=30000 or len(source)>64:
            raise ValueError('invalid source pagination')
        where = 'f.expires>?'+(' AND f.source=?' if source else '')
        args = (time.time(),source) if source else (time.time(),)
        total = await self.mirror.one('SELECT COUNT(*) n FROM source_files f WHERE '+where,args)
        async with self.mirror.db.execute('''SELECT f.source,f.path,f.target,f.sha1,
            CASE WHEN c.sha1=f.sha1 THEN c.size ELSE NULL END local_bytes,COALESCE(h.hits,0) redirects
            FROM source_files f LEFT JOIN cache c ON c.url=f.origin
            LEFT JOIN source_hits h ON h.path=f.path AND h.target=f.target
            WHERE '''+where+' ORDER BY f.source,f.path LIMIT 50 OFFSET ?',(*args,offset)) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            row['redirects'] += self.pending[row['path'],row['target']]
        return dict(files=rows,total=total['n'],offset=offset,next_offset=offset+50 if offset+50<total['n'] else None)

    async def eligible(self):
        from .mirror import read_settings
        settings = read_settings(self.mirror.config)
        # Only mappings that currently win routing can authorize a deletion.
        targets = {row['prefix']:row['target'] for row in settings['sources'] if row['enabled'] and row['mode']=='redirect'}
        async with self.mirror.db.execute('''SELECT f.path,f.target,f.sha1,c.url,c.object,c.size
            FROM source_files f JOIN cache c ON c.url=f.origin AND c.sha1=f.sha1 AND c.checked=1
            WHERE f.expires>? ORDER BY f.path,f.source''',(time.time(),)) as cursor:
            rows = await cursor.fetchall()
        seen,result = set(),[]
        for row in rows:
            from .mirror_routing import source_targets
            adaptive = self.mirror.quality.needs_cache(row['path'], source_targets(settings, row['path'], 'redirect'))
            if not adaptive and row['url'] not in seen and targets.get(row['path'])==row['target']:
                seen.add(row['url']);result.append(dict(row))
        return result

    async def preview(self):
        rows = await self.eligible()
        by_object = {}
        for row in rows:
            by_object.setdefault(row['object'],[]).append(row)
        reclaimable = 0
        async with self.mirror.db.execute('SELECT object,COUNT(*) n FROM cache GROUP BY object') as cursor:
            references = {row['object']:row['n'] for row in await cursor.fetchall()}
        for digest,aliases in by_object.items():
            if references.get(digest)==len(aliases) and not self.mirror.serving.get(digest):
                reclaimable += aliases[0]['size']
        return dict(files=len(rows),reclaimable_bytes=reclaimable,examples=rows[:20])

    async def cleanup(self):
        from .mirror import read_settings
        mirror = self.mirror
        rows = await self.eligible()
        mirror.job_state.update(state='running',done=0,total=len(rows),removed=0,freed_bytes=0,skipped=0)
        for row in rows:
            try:
                # Recheck both the checksum and file endpoint immediately before
                # deleting. A checksum sidecar alone does not prove availability.
                if (await mirror.discovery.checksum(row['target'])!=row['sha1']
                        or not await mirror.discovery.available(row['target'],row['size'])):
                    mirror.job_state['skipped'] += 1
                    continue
                async with mirror.space_lock:
                    settings = read_settings(mirror.config)
                    from .mirror_routing import source_target, source_targets
                    current = await mirror.cached(row['url'])
                    if (not current or current['sha1']!=row['sha1'] or current['object']!=row['object']
                            or mirror.serving.get(row['object']) or row['url'] in mirror.inflight
                            or row['url'] in mirror.backups
                            or mirror.quality.needs_cache(row['path'], source_targets(settings,row['path'],'redirect'))
                            or source_target(settings,row['path'],'redirect')!=row['target']):
                        mirror.job_state['skipped'] += 1
                        continue
                    count = await mirror.one('SELECT COUNT(*) n FROM cache WHERE object=?',(row['object'],))
                    if count['n']==1:
                        file = mirror.root/'objects'/row['object'][:2]/row['object']
                        await asyncio.to_thread(file.unlink,missing_ok=True)
                        await mirror.db.execute('DELETE FROM objects WHERE digest=?',(row['object'],))
                        await mirror.db.execute('UPDATE totals SET bytes=bytes-? WHERE id=1',(row['size'],))
                        mirror.job_state['freed_bytes'] += row['size']
                    await mirror.db.execute('DELETE FROM cache WHERE url=?',(row['url'],))
                    await mirror.db.execute('UPDATE totals SET files=files-1 WHERE id=1')
                    await mirror.db.commit()
                    mirror.job_state['removed'] += 1
            except Exception:
                mirror.job_state['skipped'] += 1
            finally:
                mirror.job_state['done'] += 1
        mirror.job_state['state'] = 'finished'
