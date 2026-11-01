"""Optional, verified Kodo storage; secrets never enter mirror settings or APIs."""
import asyncio
import base64
import hashlib
import json
import os
import re
import threading
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote, urlsplit


ROUTE_NAMES = ('core', 'loaders', 'mods', 'java', 'libraries', 'assets',
               'room_mods', 'room_resourcepacks', 'room_shaderpacks')
LOCAL_ROUTES = {name: {'mode': 'local', 'profile': '', 'path': name} for name in ROUTE_NAMES}
_SDK_LOCK = threading.Lock()


class StorageError(RuntimeError):
    pass


def _https_origin(value, *, qiniu_upload=False):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and bool(host) and not parsed.username and not parsed.password
                and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
                and parsed.port in (None, 443)
                and (not qiniu_upload or host.endswith('.qiniup.com'))
                and (qiniu_upload or not host.endswith('.clouddn.com')))
    except ValueError:
        return False


def validate_cloud_settings(settings):
    profiles = settings['qiniu_profiles']
    if not isinstance(profiles, list) or len(profiles) > 8:
        raise ValueError('invalid qiniu profiles')
    ids = set()
    required = {'id', 'bucket', 'domain', 'prefix', 'upload_host', 'private', 'quota_gib', 'daily_upload_gib'}
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != required:
            raise ValueError('invalid qiniu profile fields')
        if (not isinstance(profile['id'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,31}', profile['id'])
                or profile['id'] in ids):
            raise ValueError('invalid or duplicate qiniu profile id')
        ids.add(profile['id'])
        if not isinstance(profile['bucket'], str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,62}', profile['bucket']):
            raise ValueError('invalid qiniu bucket')
        if not _https_origin(profile['domain']) or not _https_origin(profile['upload_host'], qiniu_upload=True):
            raise ValueError('qiniu domains must be valid HTTPS origins')
        if not isinstance(profile['prefix'], str) or not re.fullmatch('[a-z0-9][a-z0-9/_-]{0,79}', profile['prefix']) or '//' in profile['prefix'] or profile['prefix'].endswith('/'):
            raise ValueError('invalid qiniu prefix')
        if type(profile['private']) is not bool or any(type(profile[name]) is not int or not 1 <= profile[name] <= 1000000 for name in ('quota_gib', 'daily_upload_gib')):
            raise ValueError('invalid qiniu profile policy')
    routes = settings['qiniu_routes']
    if not isinstance(routes, dict) or set(routes) != set(ROUTE_NAMES):
        raise ValueError('invalid qiniu routes')
    profile_by_id = {profile['id']: profile for profile in profiles}
    for name, route in routes.items():
        if not isinstance(route, dict) or set(route) != {'mode', 'profile', 'path'}:
            raise ValueError('invalid qiniu route')
        if route['mode'] not in ('local', 'copy', 'cloud') or not isinstance(route['profile'], str):
            raise ValueError('invalid qiniu route mode')
        if route['mode'] != 'local' and route['profile'] not in ids:
            raise ValueError('qiniu route has no configured profile')
        if name.startswith('room_') and route['mode'] != 'local' and not profile_by_id[route['profile']]['private']:
            raise ValueError('temporary room files require a private Qiniu bucket')
        if not isinstance(route['path'], str) or not re.fullmatch('[a-z0-9][a-z0-9/_-]{0,79}', route['path']) or '//' in route['path'] or route['path'].endswith('/'):
            raise ValueError('invalid qiniu object path')
    if type(settings['qiniu_paused']) is not bool:
        raise ValueError('invalid qiniu pause state')
    if type(settings['qiniu_signed_url_minutes']) is not int or not 30 <= settings['qiniu_signed_url_minutes'] <= 1440:
        raise ValueError('invalid qiniu signed URL lifetime')


def credentials(profile_id):
    path = os.environ.get('TRANSLATION_QINIU_CREDENTIALS_FILE', '')
    try:
        data = (json.loads(Path(path).read_text(encoding='utf-8')) if path else
                json.loads(os.environ.get('TRANSLATION_QINIU_CREDENTIALS', '{}')))
        entry = data.get(profile_id, {})
        access, secret = entry.get('access_key'), entry.get('secret_key')
        if isinstance(access, str) and isinstance(secret, str) and access and secret:
            return access, secret
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return None


def sha256_file(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def multipart_etag(path, part_size=1024 * 1024):
    pieces = []
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(part_size), b''):
            pieces.append(hashlib.sha1(chunk).digest())
    if not pieces:
        pieces.append(hashlib.sha1(b'').digest())
    return base64.urlsafe_b64encode(b'\x9e' + hashlib.sha1(b''.join(pieces)).digest()).decode('ascii')


class QiniuStorage:
    def __init__(self, profile, *, signed_url_minutes=240):
        pair = credentials(profile['id'])
        if not pair:
            raise StorageError('Qiniu credentials are not configured for this profile')
        try:
            import qiniu
        except ImportError as exc:
            raise StorageError('Install the qiniu optional dependency') from exc
        self.sdk, self.profile = qiniu, profile
        self.auth = qiniu.Auth(*pair)
        self.manager = qiniu.BucketManager(self.auth)
        self.signed_url_minutes = signed_url_minutes

    def key(self, route_path, digest):
        if not re.fullmatch('[a-z0-9][a-z0-9/_-]{0,79}', route_path) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid cloud object identity')
        return f"{self.profile['prefix']}/{route_path}/sha256/{digest[:2]}/{digest}"

    async def stat(self, key):
        data, info = await asyncio.to_thread(self.manager.stat, self.profile['bucket'], key)
        if info.status_code == 612:
            return None
        if info.status_code != 200 or not isinstance(data, dict):
            raise StorageError(f'Kodo stat returned HTTP {info.status_code}')
        return data

    def _expected_etag(self, path):
        if path.stat().st_size > self.sdk.config.get_default('default_upload_threshold'):
            return multipart_etag(path)
        return self.sdk.etag(str(path))

    def _upload(self, path, key, digest, progress=None):
        sdk = self.sdk
        region = sdk.region.Region(up_host=self.profile['upload_host'], scheme='https')
        with _SDK_LOCK:
            sdk.config.set_default(default_zone=region, connection_timeout=120)
            size = path.stat().st_size
            token = self.auth.upload_token(self.profile['bucket'], key, 3600,
                                           {'insertOnly': 1, 'fsizeLimit': max(size, 1)})
            result, info = sdk.put_file_v2(token, key, str(path), check_crc=True,
                                            version='v2', bucket_name=self.profile['bucket'],
                                            metadata={'x-qn-meta-sha256': digest},
                                            regions=[region], part_size=1024 * 1024,
                                            progress_handler=progress)
            expected = self._expected_etag(path)
        if info.status_code == 614:
            return expected
        if info.status_code not in (200, 298) or not isinstance(result, dict) or result.get('key') != key or result.get('hash') != expected:
            raise StorageError(f'Kodo upload or ETag validation failed (HTTP {info.status_code})')
        return expected

    async def put_verified(self, path, key, digest, size, *, progress=None):
        path = Path(path)
        actual, actual_size = await asyncio.to_thread(sha256_file, path)
        if actual != digest or actual_size != size:
            raise StorageError('Local SHA-256 or size changed before cloud upload')
        existing = await self.stat(key)
        if existing:
            etag = await asyncio.to_thread(self._expected_etag, path)
            if existing.get('fsize') == size and existing.get('hash') == etag:
                return
            raise StorageError('Cloud object key already contains different bytes')
        etag = await asyncio.to_thread(self._upload, path, key, digest, progress)
        remote = await self.stat(key)
        if not remote or remote.get('fsize') != size or remote.get('hash') != etag:
            raise StorageError('Cloud object did not pass size and ETag verification')

    def download_url(self, key, *, expires_seconds=None):
        url = self.profile['domain'].rstrip('/') + '/' + quote(key, safe='/')
        ttl = min(self.signed_url_minutes * 60, max(1, int(expires_seconds))) if expires_seconds is not None else self.signed_url_minutes * 60
        return self.auth.private_download_url(url, expires=ttl) if self.profile['private'] else url

    async def delete(self, key):
        _, info = await asyncio.to_thread(self.manager.delete, self.profile['bucket'], key)
        if info.status_code not in (200, 612):
            raise StorageError(f'Kodo delete returned HTTP {info.status_code}')

    async def probe(self):
        key = self.profile['prefix'] + '/_connection_check_' + os.urandom(16).hex()
        return await self.stat(key) is None

    async def test(self):
        """Explicit 32-byte write, download and deletion check; never bulk-migrates."""
        payload = os.urandom(32)
        digest = hashlib.sha256(payload).hexdigest()
        key = self.profile['prefix'] + '/_connection_check_' + digest
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / digest
            path.write_bytes(payload)
            try:
                await self.put_verified(path, key, digest, len(payload))
                url = self.download_url(key)
                def fetch():
                    with urlopen(Request(url, headers={'Range': 'bytes=0-31'}), timeout=12) as response:
                        return response.read(64)
                if await asyncio.to_thread(fetch) != payload:
                    raise StorageError('Kodo test download returned different bytes')
                return True
            finally:
                await self.delete(key)
