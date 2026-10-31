"""One-use origin invitations and live revocation without exposing secrets."""
import base64
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mc_translate.room_node_access import NodeAccess
from mc_translate.room_cache import keys


class RoomNodeAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = NodeAccess(self.root)
        self.url = 'https://mirror.example.org'

    def ticket(self, invite):
        data = json.loads(base64.urlsafe_b64decode(invite.split('.')[1] + '==='))
        self.assertEqual(data['url'], self.url)
        self.assertEqual(set(data), {'url', 'ticket'})
        return data['ticket']

    def test_claim_is_single_use_persistent_and_revocable(self):
        ticket = self.ticket(self.registry.invite(self.url, '测试节点'))
        self.assertNotIn('secret', str(self.registry.list()))
        value = self.registry.claim(ticket, 'node_1')
        self.assertEqual(self.registry.list()[0]['state'], 'connected')
        with self.assertRaises(ValueError):
            self.registry.claim(ticket, 'node_2')
        with patch.dict('os.environ', {'TRANSLATION_ROOM_CACHE_KEYS': '{}'}):
            self.assertEqual(keys(self.root)[value['issuer']], value['secret'])
            NodeAccess(self.root).revoke(value['issuer'])
            self.assertNotIn(value['issuer'], keys(self.root))
        self.assertFalse(self.registry.list()[0]['enabled'])

    def test_concurrent_claim_allows_one_node_only(self):
        ticket = self.ticket(self.registry.invite(self.url, '并发节点'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda i: self._claim(ticket, f'node_{i}'), range(2)))
        self.assertEqual(sum(result is True for result in results), 1)

    def _claim(self, ticket, node_id):
        try:
            self.registry.claim(ticket, node_id)
            return True
        except ValueError:
            return False

    def test_invalid_origin_and_ticket_rejected(self):
        for url in ('http://mirror.example.org', 'https://mirror.example.org:8443',
                    'https://user@mirror.example.org', 'https://mirror.example.org/secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.registry.invite(url, 'x')
        with self.assertRaises(ValueError):
            self.registry.claim('not-a-ticket', 'node_1')
        with self.assertRaises(ValueError):
            self.registry.invite(self.url, '')
