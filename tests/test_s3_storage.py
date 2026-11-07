import hashlib
import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit
from aiohttp import web

from mc_translate.mirror_storage import S3Storage, StorageError
from mc_translate.mirror import validate_settings
from mc_translate.mirror_tls import CloudTLS
from mc_translate.mirror_control import MirrorControl
from mc_translate.mirror import read_settings, settings_path
from mc_translate.mirror_catalog import write_json

PROFILE = dict(id='rainyun1',provider='rainyun',bucket='bugg',domain='https://s3oss.example.com',
    endpoint='https://s3.example.com',region='cn-nb1',addressing_style='path',private=True,
    prefix='mirror',upload_host='https://up-z0.qiniup.com',quota_unit='GB',quota_gib=80,
    daily_upload_gib=1,monthly_egress_gib=400,egress_threshold=90,tls_auto=False)


class S3Case(unittest.IsolatedAsyncioTestCase):
    async def test_category_storage_save_preserves_other_settings_and_reports_invalid_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            config=SimpleNamespace(mirror_root=temporary)
            before=validate_settings(dict(quota_gib=20,concurrency=7,qiniu_profiles=[PROFILE]))
            write_json(settings_path(config),before)
            api=SimpleNamespace(config=config,require_admin=Mock())
            control=MirrorControl(api)
            async def status(body=None):
                self.assertIsNone(body, 'Cloud changes must not run local disk trimming')
                return dict(running=True,settings=read_settings(config),cloud_storage={})
            control.call=AsyncMock(side_effect=status)
            route=dict(mode='copy',profile='rainyun1',path='game/assets')
            request=SimpleNamespace(method='POST',json=AsyncMock(return_value={'qiniu_routes':{**before['qiniu_routes'],'assets':route}}))
            request.clone=lambda **kwargs:request
            response=await control.admin(request)
            self.assertEqual(response.status,200)
            saved=read_settings(config)
            self.assertEqual(saved['qiniu_routes']['assets'],route)
            self.assertEqual(saved['concurrency'],7)
            request.json=AsyncMock(return_value={'qiniu_routes':{**saved['qiniu_routes'],'assets':{**route,'profile':''}}})
            response=await control.admin(request)
            self.assertEqual(response.status,400)
            self.assertIn('原版资源',json.loads(response.text)['message'])
            self.assertEqual(read_settings(config)['qiniu_routes']['assets'],route)

    async def test_categories_can_use_distinct_spaces_of_the_same_provider(self):
        from mc_translate.mirror_storage import LOCAL_ROUTES
        profiles=[{**PROFILE,'id':f'rainyun{n}','name':f'雨云 {n}'} for n in range(1,10)]
        profiles.append({**PROFILE,'id':'other','provider':'s3','name':'其他 S3'})
        settings=validate_settings(dict(qiniu_profiles=profiles,qiniu_routes={**LOCAL_ROUTES,
            'core':dict(mode='cloud',profile='rainyun1',path='core'),
            'assets':dict(mode='copy',profile='rainyun2',path='assets'),
            'room_mods':dict(mode='cloud',profile='other',path='rooms/mods')}))
        self.assertEqual(settings['qiniu_routes']['assets']['profile'],'rainyun2')
        self.assertEqual(settings['qiniu_profiles'][0]['name'],'雨云 1')

    def storage(self):
        with patch('mc_translate.mirror_storage.credentials',return_value=('example-access','example-secret')):
            return S3Storage(PROFILE)

    async def test_custom_domain_signature_and_head_are_distinct(self):
        storage = self.storage()
        get = urlsplit(storage.download_url('mirror/a b.jar',expires_seconds=60))
        head = urlsplit(storage.download_url('mirror/a b.jar',expires_seconds=60,method='HEAD'))
        self.assertEqual(get.hostname,'s3oss.example.com')
        self.assertEqual(get.path,'/mirror/a%20b.jar')
        self.assertEqual(parse_qs(get.query)['X-Amz-Expires'],['60'])
        self.assertNotEqual(parse_qs(get.query)['X-Amz-Signature'],parse_qs(head.query)['X-Amz-Signature'])
        self.assertNotIn('example-secret',get.query)
        original = urlsplit(storage.endpoint_download_url('mirror/a b.jar',expires_seconds=60))
        self.assertEqual(original.hostname,'s3.example.com')
        self.assertEqual(original.path,'/bugg/mirror/a%20b.jar')
        self.assertEqual(parse_qs(original.query)['X-Amz-Expires'],['60'])
        self.assertNotEqual(parse_qs(original.query)['X-Amz-Signature'],parse_qs(get.query)['X-Amz-Signature'])

    async def test_remote_metadata_mismatch_preserves_local_file(self):
        storage = self.storage()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'object';path.write_bytes(b'original')
            digest = hashlib.sha256(b'original').hexdigest()
            with patch.object(storage,'stat',AsyncMock(return_value=dict(size=8,sha256=digest,etag='wrong'))):
                with self.assertRaises(StorageError):
                    await storage.put_verified(path,'mirror/test',digest,8)
            self.assertEqual(path.read_bytes(),b'original')

    def test_single_pass_fingerprint_matches_single_and_multipart_etags(self):
        storage = self.storage()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'object'
            for payload in (b'', b'small object', b'chunk-data'*2_000_000):
                path.write_bytes(payload)
                digest, size, etag = storage._fingerprint(path)
                self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
                self.assertEqual(size, len(payload))
                self.assertEqual(etag, storage._etag(path))

    def test_network_error_without_http_response_is_readable_and_retryable(self):
        failure = OSError('connection closed')
        failure.response = None
        error = S3Storage._error('upload', failure)
        self.assertIn('HTTP network, OSError', str(error))
        self.assertTrue(error.retryable)
        failure.response = {'ResponseMetadata': {'HTTPStatusCode':403}, 'Error': {'Code':'AccessDenied'}}
        error = S3Storage._error('upload', failure)
        self.assertIn('HTTP 403, AccessDenied', str(error))
        self.assertFalse(error.retryable)

    async def test_invalid_tls_provider_and_missing_keys_fail_before_job(self):
        self.assertEqual(validate_settings(dict(qiniu_profiles=[PROFILE]))['qiniu_profiles'][0]['quota_gib'],80)
        with self.assertRaises(ValueError):
            validate_settings(dict(qiniu_profiles=[{**PROFILE,'provider':'s3','tls_auto':True}]))
        with tempfile.TemporaryDirectory() as temporary:
            class Cloud:
                class mirror:
                    root=Path(temporary)
                def profile(self,identifier):return PROFILE
            manager = CloudTLS(Cloud())
            with patch('mc_translate.mirror_tls.credential_entry',return_value={}):
                with self.assertRaises(StorageError):manager.schedule('rainyun1')
                self.assertEqual(manager.status(PROFILE)['state'],'waiting_configuration')
                self.assertFalse(manager.tasks)

    async def test_certificate_deploy_preserves_other_bucket_domains(self):
        with tempfile.TemporaryDirectory() as temporary:
            class Cloud:
                class mirror:
                    root=Path(temporary)
            manager=CloudTLS(Cloud())
            directory=Path(temporary)
            (directory/'fullchain.pem').write_text('test cert')
            (directory/'privkey.pem').write_text('test key')
            api=AsyncMock(side_effect=[{'Records':[{'id':12,'name':'bugg','domainList':['existing.example.com'],'ssl_is_enable':False}]},{},{},{}])
            with patch('mc_translate.mirror_tls.credential_entry',return_value={'rainyun_api_key':'test','rainyun_bucket_id':12}),patch.object(manager,'api',api),patch('asyncio.open_connection',AsyncMock(side_effect=OSError('offline'))):
                with self.assertRaises(OSError):await manager.deploy(PROFILE,directory,{})
            self.assertEqual(api.call_args_list[2].args[4]['domain_list'],['existing.example.com','s3oss.example.com'])
            self.assertEqual(api.call_args_list[3].args[4]['ssl_settings'],{'is_enable':True,'is_force':True})
            self.assertNotEqual(manager.states['rainyun1'].get('state'),'ready')

    async def test_certificate_bundle_requires_admin_and_disables_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            api=SimpleNamespace(config=SimpleNamespace(mirror_root=temporary),require_admin=Mock(side_effect=web.HTTPUnauthorized))
            control=MirrorControl(api)
            request=SimpleNamespace(query={'profile':'rainyun1'})
            with self.assertRaises(web.HTTPUnauthorized):await control.certificate(request)
            api.require_admin.side_effect=None
            root=Path(temporary)/'cloud-tls/rainyun1/config/live/rainyun1';root.mkdir(parents=True)
            (root/'fullchain.pem').write_bytes(b'example certificate')
            (root/'privkey.pem').write_bytes(b'example private key')
            with patch('mc_translate.mirror_control.read_settings',return_value={'qiniu_profiles':[PROFILE]}):
                response=await control.certificate(request)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            with zipfile.ZipFile(io.BytesIO(response.body)) as package:
                self.assertEqual(package.read('privkey.pem'),b'example private key')
