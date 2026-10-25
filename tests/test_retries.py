import asyncio
import time

from test_platform import FakeSources, PlatformFixture, REF
from mc_translate.model import ModelError
from mc_translate.store import billing_period


class RetryCase(PlatformFixture):
    async def fail(self, field='summary'):
        self.original_translate = self.app['engine'].model.translate
        async def unavailable(segments, language):
            raise ModelError('model_unavailable')
        self.app['engine'].model.translate = unavailable
        result=await self.ensure(self.payload(field))
        job=result['results'][0]['tickets'][0]['job_id']
        self.assertEqual((await self.done(job))['state'],'failed')
        return job

    async def test_revisit_exposes_ensure_and_restarts_one_failed_task(self):
        job=await self.fail()
        before=await self.store.one('SELECT * FROM tasks')
        downloads=FakeSources.downloads
        field=await self.resolve()
        self.assertEqual(field['state'],'missing')
        self.assertEqual(field['update']['action'],'ensure')
        self.assertEqual(field['last_failure']['code'],'model_unavailable')
        self.assertTrue(field['error']['retryable'])
        self.assertEqual(FakeSources.downloads,downloads)
        self.assertEqual((await self.store.one('SELECT state FROM tasks'))['state'],'failed')
        # A new visit creates a new logical ensure. Network retries keep their key.
        replay=await self.ensure()
        self.assertEqual(replay['results'][0]['tickets'][0]['job_id'],job)
        self.assertEqual((await self.store.one('SELECT state FROM tasks'))['state'],'failed')
        retried=await self.ensure(key='new-visit-retry-001')
        self.schema('EnsureResponse',retried)
        self.assertEqual(retried['results'][0]['tickets'][0]['state'],'deferred')
        after=await self.store.one('SELECT * FROM tasks')
        self.assertEqual(after['id'],before['id'])
        self.assertEqual(after['attempt'],0)
        self.assertIsNone(after['lease'])
        self.assertGreaterEqual(after['next_attempt'],before['updated']+60)
        self.assertFalse(await self.store.task_state(before,'failed',dict(code='late_worker')))
        self.app['engine'].model.translate=self.original_translate
        async with self.store.transaction() as db:
            await db.execute('UPDATE tasks SET next_attempt=0')
        self.app['engine'].wake.set()
        self.assertEqual((await self.done(job))['state'],'ready')
        self.assertEqual((await self.resolve())['state'],'ready')

    async def test_concurrent_retries_deduplicate_and_queue_limit_is_atomic(self):
        job=await self.fail()
        await self.app['engine'].close()
        task=await self.store.one('SELECT * FROM tasks')
        with self.assertRaises(OverflowError):
            await self.store.retry_task(task['id'],max_queue=0)
        self.assertEqual((await self.store.one('SELECT state FROM tasks'))['state'],'failed')
        tickets=await asyncio.gather(*(self.store.enqueue(REF,'summary',self.source['fields']['summary']['source_hash'],'zh-Hans','detail',owner='client:'+str(i)) for i in range(12)))
        self.assertEqual(len(set(tickets)),12)
        self.assertEqual((await self.store.one('SELECT COUNT(*) AS n FROM tasks'))['n'],1)
        self.assertEqual((await self.store.ticket(job,await self.store.owner(self.headers['Authorization'][7:])))['state'],'deferred')

    async def test_admin_retry_auth_success_and_budget_pause(self):
        job=await self.fail()
        task=await self.store.one('SELECT * FROM tasks')
        path='/v1/admin/tasks/'+task['id']+'/retry'
        self.assertEqual((await self.client.post(path,json={})).status,401)
        self.config.admin_token='test-admin'
        headers={'Authorization':'Bearer test-admin'}
        pause=time.time()+3600
        async with self.store.transaction() as db:
            await db.execute('INSERT INTO budget_pause VALUES(1,?,?)',(pause,'daily_budget'))
        result=await self.client.post(path,json={},headers=headers)
        self.assertEqual(result.status,202)
        data=await result.json()
        self.schema('AdminTaskRetry',data)
        self.assertEqual(data['state'],'deferred')
        self.assertGreater(data['retry_after_seconds'],3500)
        self.assertEqual((await self.client.post(path,json={},headers=headers)).status,202)
        self.assertEqual((await self.client.post('/v1/admin/tasks/not-found/retry',json={},headers=headers)).status,404)
        self.app['engine'].model.translate=self.original_translate
        async with self.store.transaction() as db:
            await db.execute('DELETE FROM budget_pause')
            await db.execute('UPDATE tasks SET next_attempt=0')
        self.app['engine'].wake.set()
        self.assertEqual((await self.done(job))['state'],'ready')
        self.assertEqual((await self.client.post(path,json={},headers=headers)).status,409)

    async def test_retry_reuses_completed_segments(self):
        FakeSources.body='\n\n'.join('Paragraph '+str(i)+' '+('text '*200) for i in range(10))
        self.app['engine'].sources.last_checked.clear()
        self.source=await self.app['engine'].sources.fetch(REF,force=True)
        calls=[]
        failing=True
        async def partial(segments,language):
            calls.append([s.id for s in segments])
            if len(calls)==2 and failing:
                raise ModelError('model_unavailable')
            return {s.id:'Translated '+s.text for s in segments}
        self.app['engine'].model.translate=partial
        result=await self.ensure(self.payload('body'))
        job=result['results'][0]['tickets'][0]['job_id']
        self.assertEqual((await self.done(job))['state'],'failed')
        self.assertEqual(len(calls),2)
        failing=False
        async with self.store.transaction() as db:
            await db.execute('UPDATE tasks SET updated=?',(time.time()-61,))
        await self.ensure(self.payload('body'),key='reuse-segments-retry')
        self.assertEqual((await self.done(job))['state'],'ready')
        self.assertEqual(len(calls),3)
        self.assertFalse(set(calls[0])&set(calls[2]))
        self.assertEqual(calls[1],calls[2])

    async def test_usage_filters_leave_totals_intact_and_bind_cursor(self):
        self.config.admin_token='test-admin'
        headers={'Authorization':'Bearer test-admin'}
        day,month=billing_period()
        async with self.store.transaction() as db:
            await db.executemany('INSERT INTO usage(id,day,month,tokens,actual,created,status) VALUES(?,?,?,?,?,?,?)',[(str(i),day,month,10,1,time.time()-i,'failed' if i<102 else 'succeeded') for i in range(104)])
        page=await (await self.client.get('/v1/admin/usage?status=failed',headers=headers)).json()
        self.schema('AdminUsageList',page)
        self.assertEqual(len(page['calls']),100)
        self.assertTrue(all(row['status']=='failed' for row in page['calls']))
        self.assertEqual(page['summary']['calls'],104)
        response=await self.client.get('/v1/admin/usage',params={'status':'succeeded','cursor':page['next_cursor']},headers=headers)
        self.assertEqual(response.status,409)
        self.assertEqual((await self.client.get('/v1/admin/usage?status=other',headers=headers)).status,400)

    async def test_admin_session_probe_and_neutral_initial_html(self):
        self.assertEqual((await self.client.get('/v1/admin/session')).status,401)
        self.config.admin_token='test-admin'
        response=await self.client.post('/v1/admin/session',json={},headers={'Authorization':'Bearer test-admin'})
        self.assertEqual(response.status,200)
        self.assertTrue(response.cookies['translation_admin']['httponly'])
        response=await self.client.get('/v1/admin/session')
        self.assertEqual(await response.json(),{'authenticated':True})
        for path in ('/admin','/admin/tasks','/admin/model','/admin/usage','/admin/clients','/admin/mail','/admin/feedback'):
            text=await (await self.client.get(path)).text()
            self.assertIn('id="login-card" class="admin-card login-card hidden"',text)
            self.assertIn('id="session-loading"',text)
        await self.client.delete('/v1/admin/session')
        self.assertEqual((await self.client.get('/v1/admin/session')).status,401)
