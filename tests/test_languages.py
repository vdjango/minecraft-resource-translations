import time
import unittest
import sqlite3
from pathlib import Path
from contextlib import closing

from test_platform import FakeModel, FakeSources, PlatformFixture, REF
from mc_translate.content import no_translation_reason, prepare
from mc_translate.hashing import digest, text_hash
from mc_translate.languages import LANGUAGES, normalize_language
from mc_translate.store import Store


class LanguageRules(unittest.TestCase):
    def test_aliases_do_not_merge_chinese_scripts(self):
        self.assertEqual(normalize_language('zh_CN'), 'zh-Hans')
        self.assertEqual(normalize_language('zh-TW'), 'zh-Hant')
        self.assertEqual(normalize_language('en-US'), 'en')
        for value in (None, 'und', 'pt-PT', 'made-up'):
            with self.assertRaises(ValueError):
                normalize_language(value)

    def test_unicode_and_conservative_skip_rules(self):
        for text in ('这是简体中文', '日本語の説明', '한국어 설명', 'Описание проекта'):
            segments, _, _ = prepare(text, 'plain', 'Fixture')
            self.assertTrue(segments)
            self.assertIsNone(no_translation_reason(dict(text=text, format='plain', source_language='und'), 'zh-Hant', 'Fixture', segments))
        self.assertEqual(no_translation_reason(dict(text='`fabric` 1.21 https://example.org', format='markdown'), 'ja'), 'technical_content')
        self.assertEqual(no_translation_reason(dict(text='English content', format='plain', source_language='en-US'), 'en'), 'already_target_language')
        self.assertIsNone(no_translation_reason(dict(text='繁體內容', format='plain', source_language='zh-Hant'), 'zh-Hans'))


class MultilingualCase(PlatformFixture):
    async def translated(self, language, field='summary', key=None):
        payload = self.payload(field)
        payload['target_language'] = language
        result = await self.ensure(payload, key=key or ('language-'+language+'-'+field))
        for ticket in result['results'][0]['tickets']:
            self.assertEqual((await self.done(ticket['job_id']))['state'], 'ready')
        return await self.resolve(payload)

    async def test_capabilities_default_alias_and_unsupported(self):
        capabilities = await (await self.client.get('/v1/capabilities')).json()
        self.schema('Capabilities', capabilities)
        self.assertEqual(capabilities['default_target_language'], 'zh-Hans')
        self.assertEqual(set(capabilities['target_languages']), set(LANGUAGES))
        await self.translated('zh_CN')
        payload = self.payload()
        del payload['target_language']
        self.assertEqual((await self.resolve(payload))['state'], 'ready')
        payload['target_language'] = 'zz'
        self.assertEqual((await self.client.post('/v1/translations:resolve', json=payload)).status, 400)

    async def test_language_cache_catalogue_and_search_are_isolated(self):
        calls = []
        async def translate(segments, language):
            calls.append(language)
            return {s.id: ('快速模组' if language == 'zh-Hans' else '日本語専用') for s in segments}
        self.app['engine'].model.translate = translate
        await self.translated('zh-Hans')
        missing = self.payload()
        missing['target_language'] = 'ja'
        self.assertEqual((await self.resolve(missing))['state'], 'missing')
        await self.translated('ja')
        await self.translated('ja-JP', key='alias-second-ensure')
        self.assertEqual(calls, ['zh-Hans', 'ja'])
        for lang, expected in [('zh-Hans', '快速模组'), ('ja', '日本語専用')]:
            page = await (await self.client.get('/v1/catalog/search', params={'target_language': lang})).json()
            self.schema('CatalogueResponse', page)
            self.assertEqual(page['projects'][0]['localized_summary'], expected)
            self.assertEqual(page['projects'][0]['translation_state'], 'partial')
        for query in ('日本語', '日本'):
            page = await (await self.client.get('/v1/catalog/search', params={'q':query, 'target_language':'ja'})).json()
            self.assertEqual(page['total_count'], 1)
            page = await (await self.client.get('/v1/catalog/search', params={'q':query, 'target_language':'zh-Hans'})).json()
            self.assertEqual(page['total_count'], 0)
        page = await (await self.client.get('/v1/catalog/search', params={'target_language':'de'})).json()
        self.assertNotIn('localized_summary', page['projects'][0])

    async def test_title_and_technical_content_ready_without_model_even_during_pause(self):
        async with self.store.transaction() as db:
            await db.execute('INSERT INTO budget_pause VALUES(1,?,?)', (time.time()+3600,'daily_budget'))
        field = await self.translated('ja', 'title')
        self.assertFalse(field['translation']['translation_required'])
        self.assertEqual(field['translation']['no_translation_reason'], 'project_name')
        self.assertEqual(field['update']['code'], 'not_required')
        artifact = await (await self.client.get('/v1/artifacts/'+field['translation']['artifact']['artifact_sha256'])).json()
        self.schema('Artifact', artifact)
        self.assertEqual(artifact['text'], 'Fixture')
        self.assertFalse(artifact['translation_required'])
        self.config.admin_token = 'test-admin-secret'
        tasks = await (await self.client.get('/v1/admin/tasks', headers={'Authorization':'Bearer test-admin-secret'})).json()
        self.schema('AdminTaskList',tasks)
        self.assertEqual(tasks['tasks'][0]['result_quality'],'original')
        self.assertEqual(tasks['tasks'][0]['no_translation_reason'],'project_name')
        FakeSources.body = '```toml\nloader="fabric"\n```\n\n1.21 https://example.org'
        self.app['engine'].sources.last_checked.clear()
        self.source = await self.app['engine'].sources.fetch(REF, force=True)
        field = await self.translated('de', 'body')
        self.assertEqual(field['translation']['no_translation_reason'], 'technical_content')
        self.assertEqual(FakeModel.calls, 0)
        self.assertEqual((await self.store.one('SELECT count(*) AS n FROM usage'))['n'], 0)
        again = await self.ensure(dict(self.payload('body'), target_language='de'), key='no-repeat-original')
        self.assertEqual(again['results'][0]['tickets'], [])

    async def test_empty_content_and_already_target_language(self):
        for index, (text, source_language, reason) in enumerate([('', 'und', 'empty_content'), ('Already in English.', 'en', 'already_target_language')]):
            field = self.source['fields']['summary']
            field.update(text=text, source_language=source_language, source_hash=text_hash(text, 'summary', 'plain', source_language))
            self.source['snapshot_hash'] = digest(str(index))
            await self.store.save_source(self.source)
            result = await self.translated('en', key='already-target-'+str(index))
            self.assertEqual(result['translation']['no_translation_reason'], reason)
        self.assertEqual(FakeModel.calls, 0)

    async def test_model_unchanged_is_original_and_source_change_is_rechecked(self):
        async def unchanged(segments, language):
            return {s.id:s.text for s in segments}
        self.app['engine'].model.translate = unchanged
        result = await self.translated('en')
        self.assertEqual(result['translation']['no_translation_reason'], 'unchanged')
        FakeSources.summary = 'New source content.'
        self.app['engine'].sources.last_checked.clear()
        self.source = await self.app['engine'].sources.fetch(REF, force=True)
        self.assertEqual((await self.resolve(dict(self.payload(), target_language='en')))['state'], 'missing')
        self.assertEqual(await self.store.rows('SELECT * FROM localized_catalogue'), [])

    async def test_update_repairs_each_requested_language_only(self):
        await self.translated('zh-Hans')
        await self.translated('ja')
        FakeSources.summary = 'A fast Minecraft mod. New update.'
        self.app['engine'].sources.last_checked.clear()
        async with self.store.transaction() as db:
            await db.execute('UPDATE resources SET checked=0')
        await self.app['engine'].synchronize_once()
        rows = await self.store.rows('SELECT DISTINCT language FROM tasks WHERE source_hash<>?', (self.source['fields']['summary']['source_hash'],))
        self.assertEqual({r['language'] for r in rows}, {'zh-Hans','ja'})

    async def test_cursor_binds_target_language(self):
        other = dict(REF, project_id='Fixture02', entity_id='Fixture02')
        await self.app['engine'].sources.fetch(other)
        page = await (await self.client.get('/v1/catalog/search?limit=1&target_language=ja')).json()
        self.assertIsNotNone(page['next_cursor'])
        response = await self.client.get('/v1/catalog/search', params={'limit':1, 'target_language':'zh-Hans','cursor':page['next_cursor']})
        self.assertEqual(response.status, 409)

    async def test_manifest_uses_selected_language(self):
        await self.translated('ja', 'title')
        for language, state in [('ja','ready'), ('en','missing')]:
            result = await (await self.client.get('/v1/projects/modrinth/Fixture01/manifest', params={'target_language':language})).json()
            self.schema('Manifest',result)
            title = next(f for f in result['fields'] if f['field']=='title')
            self.assertEqual(title['state'], state)
            if language=='ja':
                self.assertFalse(title['translation_required'])
                self.assertEqual(title['no_translation_reason'],'project_name')

    async def test_migration_preserves_existing_chinese_cache(self):
        result = await self.translated('zh-Hans')
        path=Path(self.temp.name)/'old.sqlite'
        with closing(sqlite3.connect(self.store.path)) as original, closing(sqlite3.connect(path)) as db:
            original.backup(db)
            db.executescript('DROP TABLE multilingual_catalogue_migration; DROP TRIGGER localized_insert; DROP TRIGGER localized_update; DROP TRIGGER localized_delete; DROP TABLE localized_catalogue_fts; DROP TABLE localized_catalogue; ALTER TABLE translations DROP COLUMN no_translation_reason;')
        for _ in range(2):
            migrated=await Store(str(path)).open()
            try:
                rows=await migrated.rows('SELECT * FROM localized_catalogue')
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['language'],'zh-Hans')
                self.assertEqual(rows[0]['text'],result['translation']['text'])
                translation=await migrated.one('SELECT revision FROM translations')
                self.assertEqual(translation['revision'],result['translation']['translation_revision'])
            finally:
                await migrated.close()
