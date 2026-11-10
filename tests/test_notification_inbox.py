"""Notification audience and per-account state tests without SDK initialization."""
import ast
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from test_admin_sync import Database, Collection


class NotificationInboxTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.env = {'db': self.db, 'firestore': types.SimpleNamespace(SERVER_TIMESTAMP='server-time')}
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        names = {'get_notifications', '_save_notification_states', 'mark_all_notifications_read', 'delete_all_notifications'}
        module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
        exec(compile(module, 'app.py', 'exec'), self.env)
        self.get_patch = patch.object(Collection, 'get', Collection.stream, create=True)
        self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        for key, data in {
            'own': {'userId': 'u1'}, 'email': {'userId': 'a@example.com'},
            'legacy': {'userId': 'CUST-1', 'recipientEmail': 'a@example.com'},
            'admin': {'userId': 'ADMIN'}, 'all': {'userId': 'ALL'},
            'other': {'userId': 'u2'},
        }.items():
            self.db.records['notifications/' + key] = {**data, 'status': 'Unread'}

    def test_customer_audience_excludes_admin_and_other_customer(self):
        result = self.env['get_notifications']('u1', 'a@example.com', 'u1')
        self.assertEqual({n['id'] for n in result}, {'own', 'email', 'legacy', 'all'})

    def test_read_and_clear_are_per_account_and_preserve_shared_records(self):
        self.assertTrue(self.env['mark_all_notifications_read']('u1', 'a@example.com', 'u1'))
        self.assertTrue(all(n['status'] == 'Read' for n in self.env['get_notifications']('u1', 'a@example.com', 'u1')))
        self.assertEqual(self.db.records['notifications/all']['status'], 'Unread')
        self.assertTrue(self.env['delete_all_notifications']('u1', 'a@example.com', 'u1'))
        self.assertEqual(self.env['get_notifications']('u1', 'a@example.com', 'u1'), [])
        self.assertIn('notifications/all', self.db.records)
        self.assertEqual({n['id'] for n in self.env['get_notifications']('ADMIN', 'admin@example.com', 'admin1')}, {'admin', 'all'})


    def test_legacy_system_notification_is_english_without_rewriting_chat(self):
        import re
        self.env['re'] = re
        self.db.records['notifications/status'] = {'userId': 'u1', 'title': 'Order Status: Approved',
            'message': 'Ang iyong Printing (PRNT-123456) ay nasa status na: Approved.'}
        self.db.records['notifications/chat'] = {'userId': 'u1', 'type': 'chat', 'message': 'Salamat po'}
        records = {item['id']: item for item in self.env['get_notifications']('u1')}
        self.assertEqual(records['status']['message'], 'Your Printing (PRNT-123456) is now Approved.')
        self.assertEqual(records['chat']['message'], 'Salamat po')
