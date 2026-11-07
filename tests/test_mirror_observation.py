"""Only real stored objects appear in bounded mirror and room-cache views."""
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.mirror_catalog import write_json
from mc_translate.mirror_observation import page_arguments, room_files


class ObservationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = Config(database=str(root / 'translation.sqlite'), mirror_root=str(root / 'mirror'))
        write_json(settings_path(config), validate_settings(dict(auto_sources=False, auto_refresh=False,
                                                               quality_enabled=False, room_cache_probe_enabled=False)))
        self.client = TestClient(TestServer(await create_mirror(config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_cursor_filter_and_only_cached_files(self):
        db = self.mirror.db
        now = time.time()
        for index in range(55):
            url = f'https://resources.download.minecraft.net/{index:02x}/file{index}'
            digest = f'{index:064x}'
            await db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',
                             (url, digest, 4096, f'{index:040x}', 1, now-index, 'application/octet-stream'))
            await db.execute('INSERT INTO expected VALUES(?,?,?,?)', (url, None, 4096, 'assets-metadata'))
        await db.execute('INSERT INTO expected VALUES(?,?,?,?)', ('https://example.com/uncached', None, 1, 'core'))
        await db.commit()
        first = await self.mirror.observation.files(dict(category='assets'))
        self.assertEqual(len(first['files']), 50)
        self.assertEqual({row['category'] for row in first['files']}, {'assets'})
        second = await self.mirror.observation.files(dict(category='assets', cursor=first['next_cursor']))
        self.assertEqual(len(second['files']), 5)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({row['url'] for row in first['files'] + second['files']}), 55)
        self.assertEqual((await self.mirror.observation.files(dict(query='uncached')))['files'], [])
        for bad in ('not-base64!', 'e30=', 'e10='):
            with self.assertRaises(ValueError):
                page_arguments(dict(cursor=bad))

    async def test_room_files_deduplicate_and_keep_redirects_out_of_storage(self):
        cache = self.mirror.room_cache
        now = time.time()
        for index in range(52):
            digest = f'{index:064x}'
            await cache.db.execute('INSERT INTO files(scope,digest,path,size,sha1,urls,need,reason,touched,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                   ('room-a', digest, f'mods/file{index}.jar', 2048, None, '[]', 1, 'source_unavailable', now-index, now+3600))
        await cache.db.execute('INSERT INTO files(scope,digest,path,size,sha1,urls,need,reason,touched,lease_until) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               ('room-b', f'{0:064x}', 'mods/file0.jar', 2048, None, '[]', 1, 'source_unavailable', now, now+3600))
        await cache.db.execute('INSERT INTO objects(digest,size,created) VALUES(?,?,?)', (f'{0:064x}', 2048, now))
        await cache.db.commit()
        first = await room_files(cache, {})
        self.assertEqual(len(first['files']), 50)
        self.assertEqual(first['files'][0]['references_count'], 2)
        self.assertEqual(first['files'][0]['state'], 'cached')
        self.assertEqual(first['files'][1]['state'], 'awaiting_upload')
        second = await room_files(cache, dict(cursor=first['next_cursor']))
        self.assertEqual(len(second['files']), 2)
        self.assertIsNone(second['next_cursor'])

    async def test_job_history_marks_restart_interruption(self):
        self.mirror.job_state = dict(action='refresh', trigger='manual', state='running', started_at=time.time())
        await self.mirror.observation.record()
        await self.mirror.observation.open()
        history = (await self.mirror.observation.snapshot())['history']
        self.assertEqual(history[0]['state'], 'interrupted')

    async def test_expired_room_files_are_hidden_before_cleanup_and_leave_no_history(self):
        cache=self.mirror.room_cache
        await cache.db.execute('INSERT INTO files(scope,digest,path,size,urls,touched,lease_until) VALUES(?,?,?,?,?,?,?)',
            ('expired-room','e'*64,'mods/expired.jar',10,'[]',time.time(),time.time()-1))
        await cache.db.commit()
        self.assertEqual((await room_files(cache,{}))['files'],[])
        await cache.cleanup()
        self.assertEqual((await cache.one('SELECT COUNT(*) n FROM files'))['n'],0)


if __name__ == '__main__':
    unittest.main()
