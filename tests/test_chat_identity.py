import ast
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from flask import Flask
from backend_test_support import load_backend
from test_admin_sync import Database


class ChatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.backend = load_backend({'db': self.db,
            'auth': types.SimpleNamespace(get_user_by_email=lambda email: types.SimpleNamespace(uid='u1', display_name='Customer')),
            'get_conversation_messages': Mock(return_value=[]),
            'login_required': lambda function: function})
        self.app = self.backend.app = Flask(__name__)
        self.app.secret_key = 'synthetic-test-only'
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ['conversation_messages_endpoint', 'mark_conversation_read_endpoint']:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(uid='u1', email='a.b@example.com', role='customer')

    def test_colliding_legacy_email_does_not_reuse_another_customers_thread(self):
        legacy_id = self.backend.get_safe_conversation_id('a.b@example.com')
        self.db.records['conversations/' + legacy_id] = {'customerEmail': 'a_b@example.com', 'customerId': 'u2'}
        thread = self.backend.get_or_create_conversation('a.b@example.com')
        self.assertEqual(thread['id'], 'conv_u1')
        self.assertEqual(self.db.records['conversations/' + legacy_id]['customerId'], 'u2')
        self.assertEqual(thread['customerId'], 'u1')

    def test_native_uid_thread_supports_read_receipts(self):
        self.db.records['conversations/conv_u1'] = {'customerEmail': 'a.b@example.com', 'customerId': 'u1'}
        response = self.client.post('/api/chat/conversations/conv_u1/read')
        self.assertEqual(response.status_code, 200)
        self.backend.get_conversation_messages.assert_called_once_with('conv_u1', mark_read_for_role='customer')

    def test_foreign_owner_or_colliding_email_cannot_read_messages_or_mark_read(self):
        for data in [{'customerEmail': 'a_b@example.com', 'customerId': 'u2'},
                     {'customerEmail': 'a.b@example.com', 'customerId': 'u2'}]:
            self.db.records['conversations/collision'] = data
            self.assertEqual(self.client.get('/api/chat/conversations/collision/messages').status_code, 403)
            self.assertEqual(self.client.post('/api/chat/conversations/collision/read').status_code, 403)
        self.backend.get_conversation_messages.assert_not_called()
