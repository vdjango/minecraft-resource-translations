"""Nonblocking ACME DNS-01 maintenance and Rainyun certificate deployment."""
import asyncio
import hashlib
import importlib.util
import json
import random
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .mirror_catalog import write_json
from .mirror_storage import credential_entry, StorageError


class CloudTLS:
    def __init__(self, cloud):
        self.cloud = cloud
        self.root = cloud.mirror.root / 'cloud-tls'
        self.tasks = {}
        self.worker = None
        self.states = {}
        try:
            self.states = json.loads((self.root / 'status.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    def requirements(self, profile):
        entry = credential_entry(profile['id'])
        missing = []
        if not entry.get('rainyun_api_key'):
            missing.append('雨云账户 API Key（不是 S3 密钥）')
        if not entry.get('cloudflare_api_token'):
            missing.append('Cloudflare DNS 编辑 Token')
        if not importlib.util.find_spec('certbot_dns_cloudflare'):
            missing.append('服务端 acme 可选依赖')
        return missing

    def status(self, profile):
        missing = self.requirements(profile)
        state = dict(self.states.get(profile['id'], {}))
        if missing:
            state.update(state='waiting_configuration', message='待配置：' + '、'.join(missing))
        elif profile['id'] not in self.tasks and state.get('state') in ('queued', 'issuing', 'deploying'):
            state.update(state='pending', message='上次维护被中断，等待后台恢复')
        state.setdefault('message', '自动维护已开启，等待检查' if profile.get('tls_auto') else '自动维护未开启')
        state['automatic'] = profile.get('tls_auto', False)
        state['dns_token_expires'] = credential_entry(profile['id']).get('cloudflare_expires_on')
        return state

    def update(self, identifier, **state):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.states[identifier] = {**self.states.get(identifier, {}), **state}
        write_json(self.root / 'status.json', self.states)

    def schedule(self, identifier):
        profile = self.cloud.profile(identifier)
        if not profile or profile.get('provider') != 'rainyun':
            raise StorageError('仅雨云空间支持此证书部署方式')
        missing = self.requirements(profile)
        if missing:
            raise StorageError('待配置：' + '、'.join(missing))
        if identifier not in self.tasks:
            self.update(identifier, state='queued', message='证书维护已排队')
            task = asyncio.create_task(self.maintain(dict(profile)))
            self.tasks[identifier] = task
            task.add_done_callback(lambda done: self.tasks.pop(identifier, None))

    def start(self):
        self.worker = asyncio.create_task(self.run())

    async def close(self):
        tasks = [*self.tasks.values(), *([self.worker] if self.worker else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        await asyncio.sleep(30)
        while True:
            for profile in self.cloud.settings()['qiniu_profiles']:
                if profile.get('tls_auto') and not self.requirements(profile):
                    checked = self.states.get(profile['id'], {}).get('checked', 0)
                    if time.time() - checked >= 6 * 3600:
                        self.schedule(profile['id'])
                await asyncio.sleep(1)
            await asyncio.sleep(random.uniform(10, 14) * 3600)

    @staticmethod
    async def api(session, key, method, path, payload=None):
        try:
            async with session.request(method, 'https://api.v2.rainyun.com' + path,
                    headers={'x-api-key': key},
                    **({'params': payload} if method == 'GET' else {'json': payload}),
                    allow_redirects=False) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    raise StorageError(f'雨云证书接口失败：{method} {path}，HTTP {response.status}，响应不是 JSON') from None
                if response.status != 200 or not isinstance(data, dict) or data.get('code', 200) not in (0, 200):
                    code = data.get('code') if isinstance(data, dict) else 'invalid_response'
                    reason = '，雨云拒绝此 API Key 访问，请在雨云 SSL 中心导入证书包' if response.status == 403 else ''
                    raise StorageError(f'雨云证书接口失败：{method} {path}，HTTP {response.status}，code={code}{reason}')
                return data.get('data', data)
        except StorageError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise StorageError('雨云证书接口不可达或返回无效数据') from None

    async def deploy(self, profile, directory, state):
        entry = credential_entry(profile['id'])
        key, host = entry['rainyun_api_key'], urlsplit(profile['domain']).hostname
        certificate = (directory / 'fullchain.pem').read_text(encoding='ascii')
        fingerprint = hashlib.sha256(certificate.encode()).hexdigest()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
            # Rainyun currently rejects the bucket-detail endpoint for API keys;
            # its public list endpoint provides the same proxy settings.
            result = await self.api(session, key, 'GET', '/product/ros/bucket/', {'options': '{}'})
            rows = result if isinstance(result, list) else result.get('Records', result.get('records', []))
            bucket = next((row for row in rows if row.get('name') == profile['bucket']), None)
            if not bucket:
                raise StorageError('未找到对应雨云 Bucket')
            bucket_id = entry.get('rainyun_bucket_id', bucket['id'])
            if not str(bucket_id).isdigit():
                raise StorageError('rainyun_bucket_id 必须是数字')
            path = '/product/ros/bucket/' + str(bucket_id)
            if str(bucket['id']) != str(bucket_id):
                raise StorageError('雨云 Bucket ID 与配置名称不一致，未改动设置')
            if state.get('fingerprint') != fingerprint:
                await self.api(session, key, 'POST', '/product/sslcenter/',
                    dict(cert=certificate, key=(directory / 'privkey.pem').read_text(encoding='ascii')))
                # Rainyun's certificate center selects matching domain certificates
                # for ROS automatically. Keep the previous certificate for rollback.
                self.update(profile['id'], fingerprint=fingerprint)
            domains = bucket.get('domainList') or []
            if host not in domains:
                await self.api(session, key, 'PATCH', path + '/proxy',
                    dict(setting='domain', domain_list=[*domains, host]))
            if not bucket.get('ssl_is_enable'):
                await self.api(session, key, 'PATCH', path + '/proxy',
                    dict(setting='ssl', ssl_settings=dict(is_enable=True, is_force=True)))
        # Verify the actual download domain; a successful upload is not TLS success.
        context = ssl.create_default_context()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 443,
            ssl=context, server_hostname=host), timeout=15)
        try:
            peer = writer.get_extra_info('ssl_object').getpeercert()
            expires = ssl.cert_time_to_seconds(peer['notAfter'])
            expected_leaf = ssl.PEM_cert_to_DER_cert(certificate[:certificate.index('-----END CERTIFICATE-----') + len('-----END CERTIFICATE-----')])
            actual_leaf = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
            if hashlib.sha256(actual_leaf).digest() != hashlib.sha256(expected_leaf).digest():
                raise StorageError('雨云下载域名尚未使用新证书，保留旧证书并稍后自动重试')
            if expires - time.time() < 7 * 86400:
                raise StorageError('雨云下载域名仍在使用即将到期的证书，稍后自动重试')
        finally:
            writer.close()
            await writer.wait_closed()
        return expires

    async def maintain(self, profile):
        identifier = profile['id']
        directory = self.root / identifier
        process = None
        credentials = directory / 'cloudflare.ini'
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            token = credential_entry(identifier)['cloudflare_api_token']
            if '\n' in token or '\r' in token:
                raise StorageError('Cloudflare Token 格式无效')
            credentials.write_text('dns_cloudflare_api_token = ' + token + '\n', encoding='ascii')
            credentials.chmod(0o600)
            host = urlsplit(profile['domain']).hostname
            self.update(identifier, state='issuing', message='正在检查或申请免费证书；DNS 验证不占下载端口')
            args = [sys.executable, '-c', 'from certbot.main import main; raise SystemExit(main())',
                'certonly', '--non-interactive', '--agree-tos',
                '--register-unsafely-without-email', '--dns-cloudflare', '--dns-cloudflare-credentials', str(credentials),
                '--dns-cloudflare-propagation-seconds', '60', '--config-dir', str(directory / 'config'),
                '--work-dir', str(directory / 'work'), '--logs-dir', str(directory / 'logs'),
                '--cert-name', identifier, '--keep-until-expiring', '-d', host]
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                          stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(process.wait(), timeout=900)
            if process.returncode:
                raise StorageError('证书申请未通过，请检查 DNS 编辑权限、CAA 和域名解析；详情见服务器 cloud-tls 私有日志')
            certificate_dir = directory / 'config' / 'live' / identifier
            local = ssl._ssl._test_decode_cert(str(certificate_dir / 'fullchain.pem'))
            self.update(identifier, issued_expires=ssl.cert_time_to_seconds(local['notAfter']), downloadable=True)
            self.update(identifier, state='deploying', message='正在部署证书到雨云并验证下载域名')
            expires = await self.deploy(profile, certificate_dir, self.states.get(identifier, {}))
            self.update(identifier, state='ready', message='免费证书已部署，后台定期检查并自动续期',
                        expires=expires, checked=time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.update(identifier, state='deployment_pending' if self.states.get(identifier, {}).get('downloadable') else 'failed', message=str(exc) if isinstance(exc, StorageError)
                        else '证书维护失败：' + type(exc).__name__, checked=time.time())
        finally:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            credentials.unlink(missing_ok=True)
