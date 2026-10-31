"""Local administrator ownership and one-use browser setup links."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.admin-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('密码需要 12–256 个字符。')
    salt = salt or secrets.token_hex(16)
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ':' + result.hex()


def verify_password(password, encoded):
    try:
        return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)
    except (ValueError, TypeError, AttributeError):
        return False


class AdminAccount:
    def __init__(self, path, origin, *, legacy=False):
        self.path = Path(path)
        self.origin = origin.rstrip('/')
        self.setup_path = self.path.with_name('admin-setup.json')
        self.link_path = self.path.with_name('admin-setup-link.txt')
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if not self.initialized and not legacy and not self.setup_path.exists():
            self.prepare_setup()

    @property
    def initialized(self):
        return bool(self.state.get('admin_hash'))

    @property
    def setup_available(self):
        if not self.setup_path.exists():
            return False
        return self.setup_data().get('expires_at', 0) > time.time()

    def setup_data(self):
        return json.loads(self.setup_path.read_text(encoding='utf-8'))

    def prepare_setup(self):
        """Permit recovery without invalidating the current account until saved."""
        token = secrets.token_urlsafe(32)
        write_private(self.setup_path, json.dumps({'token_hash': hashlib.sha256(token.encode()).hexdigest(),
                                                  'expires_at': int(time.time()) + 7 * 86400}))
        link = self.origin + '/admin/setup#' + token
        write_private(self.link_path, link + '\n')
        return link

    def complete(self, token, username, password, confirmation):
        if not isinstance(token, str) or len(token) > 256 or not self.setup_available:
            raise PermissionError('初始化链接已失效，请在服务器重新生成。')
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), self.setup_data()['token_hash']):
            raise PermissionError('请使用安装程序提供的完整初始化链接。')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', username):
            raise ValueError('用户名使用 1–32 个英文字母、数字、下划线或短横线。')
        if password != confirmation:
            raise ValueError('两次输入的密码不一致。')
        state = dict(self.state, admin_user=username, admin_hash=password_hash(password),
                     admin_configured_at=int(time.time()))
        write_private(self.path, json.dumps(state, ensure_ascii=False, indent=2))
        self.state.clear()
        self.state.update(state)
        self.setup_path.unlink(missing_ok=True)
        self.link_path.unlink(missing_ok=True)
        # Old installations wrote a plaintext generated password here.
        old = self.path.with_name('admin-credentials.txt')
        if old.exists():
            write_private(old, 'Console: ' + self.origin + '/admin/\nUsername: ' + username +
                          '\nAdmin password: (chosen in browser; not stored)\n')
        return username


def main():
    parser = argparse.ArgumentParser(description='Create a private one-use administrator setup/recovery link.')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--state-name', default='admin.json')
    args = parser.parse_args()
    account = AdminAccount(args.directory / args.state_name, args.origin, legacy=True)
    print(account.prepare_setup())
    print('Valid for 7 days; the link is consumed after saving. Keep it private.')


if __name__ == '__main__':
    main()
