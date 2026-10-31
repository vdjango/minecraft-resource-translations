"""One-use web pairing; upload issuer keys remain on the two servers."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit


class NodeAccess:
    def __init__(self, root):
        self.path = Path(root) / 'room-node-access.sqlite3'

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS nodes (issuer TEXT PRIMARY KEY, name TEXT, secret TEXT, enabled INTEGER, invite_hash TEXT, expires REAL, claimed REAL, node_id TEXT)')
        os.chmod(self.path, 0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        if not self.path.exists():
            return []
        with self.database() as db:
            return [dict(issuer=r['issuer'], name=r['name'], enabled=bool(r['enabled']),
                         state='connected' if r['claimed'] else 'pending' if r['expires'] > time.time() else 'expired',
                         node_id=r['node_id'], claimed_at=r['claimed'])
                    for r in db.execute('SELECT * FROM nodes ORDER BY rowid DESC LIMIT 100')]

    def invite(self, url, name):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('请先保存源站的公开 HTTPS 地址。')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('请填写节点名称（最多 80 字）。')
        issuer, ticket = 'web-' + secrets.token_hex(12), secrets.token_urlsafe(32)
        with self.database() as db:
            db.execute('DELETE FROM nodes WHERE claimed=0 AND expires<?', (time.time() - 86400,))
            if db.execute('SELECT COUNT(*) FROM nodes WHERE enabled=1').fetchone()[0] >= 100:
                raise ValueError('最多接入 100 个节点，请先移除不用的节点。')
            db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)',
                       (issuer, name.strip(), '', 1, hashlib.sha256(ticket.encode()).hexdigest(), time.time() + 600, 0, ''))
        payload = json.dumps(dict(url=url.rstrip('/'), ticket=ticket), separators=(',', ':')).encode()
        return 'buggcache1.' + base64.urlsafe_b64encode(payload).decode().rstrip('=')

    def claim(self, ticket, node_id):
        if not isinstance(ticket, str) or len(ticket) > 128 or not isinstance(node_id, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,128}', node_id):
            raise ValueError('节点或接入邀请无效。')
        secret = secrets.token_urlsafe(48)
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM nodes WHERE invite_hash=? AND enabled=1 AND claimed=0 AND expires>?',
                             (hashlib.sha256(ticket.encode()).hexdigest(), time.time())).fetchone()
            if row is None:
                raise ValueError('接入邀请已使用、过期或撤销，请在源站后台重新生成。')
            db.execute('UPDATE nodes SET secret=?,claimed=?,node_id=?,invite_hash=? WHERE issuer=?',
                       (secret, time.time(), node_id, '', row['issuer']))
        return dict(issuer=row['issuer'], secret=secret, name=row['name'])

    def revoke(self, issuer):
        if not isinstance(issuer, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', issuer):
            raise ValueError('节点标识无效。')
        with self.database() as db:
            db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(issuer) DO UPDATE SET enabled=0,secret=?,invite_hash=?',
                       (issuer, issuer, '', 0, '', 0, 0, '', '', ''))

    def apply(self, keys):
        if not self.path.exists():
            return keys
        with self.database() as db:
            for row in db.execute('SELECT issuer,secret,enabled FROM nodes'):
                if not row['enabled']:
                    keys.pop(row['issuer'], None)
                elif row['secret']:
                    keys[row['issuer']] = row['secret']
        return keys
