import ast
import hashlib
import io
import json
import os
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from backend_test_support import load_backend
from test_admin_sync import Database, Transaction, transactional
import test_push_rules as access


class DeploymentSecurityTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.records = {
            'users/customer': {'uid': 'customer', 'role': 'customer', 'email': 'customer@example.com', 'fullName': 'Admin'},
            'users/admin': {'uid': 'admin', 'role': 'admin', 'email': 'admin@example.com'},
            'settings/loyalty_config': {'enabled': True, 'epoch': 'test', 'pointValueCents': 100},
            'products/p1': {'name': 'Paper', 'price': 10, 'stock': 100},
        }
        def user(uid):
            return types.SimpleNamespace(uid=uid, email=uid + '@example.com', disabled=False,
                                         display_name=uid, tokens_valid_after_timestamp=0)
        authentication = types.SimpleNamespace(get_user=user,
            verify_id_token=lambda token, **kwargs: {'uid': token, 'auth_time': 100},
            UserNotFoundError=type('MissingUser', (Exception,), {}))
        self.backend = load_backend({'db': self.db, 'auth': authentication,
            'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time'),
            'permanently_delete_archived_item': Mock(return_value=True)})
        self.app = self.backend.app = Flask(__name__)
        self.app.secret_key = os.urandom(48)
        self.app.logger.disabled = True
        self.patch = patch.object(Transaction, 'create', Transaction.set, create=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        names = ['verify_protected_request', 'delete_archive_permanent', 'current_account', 'handle_orders',
                 'save_fcm_token_endpoint', 'delete_fcm_token_endpoint', 'upload_order_file_endpoint', 'get_user_notifications']
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer customer'}

    def test_customer_named_admin_cannot_delete_archives(self):
        response = self.client.delete('/api/archive/synthetic', headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.backend.permanently_delete_archived_item.assert_not_called()

    def test_inventory_alerts_are_server_generated_admin_only_and_repeatable(self):
        response = self.client.post('/api/notifications', headers=self.headers,
                                    json={'userId': 'ADMIN', 'message': 'Injected notification'})
        self.assertEqual(response.status_code, 403)
        self.db.records['products/p1']['stock'] = 4
        headers = {'Authorization': 'Bearer admin'}
        first = self.client.post('/api/notifications', headers=headers, json={'message': 'Injected notification'})
        second = self.client.post('/api/notifications', headers=headers)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json['created'], 1)
        self.assertEqual(second.json['created'], 0)
        alerts = [data for path, data in self.db.records.items() if path.startswith('notifications/inventory_')]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['message'], 'Paper has 4 items remaining.')

    def test_forged_session_role_is_revalidated(self):
        with self.client.session_transaction() as state:
            state.update(uid='customer', email='customer@example.com', role='admin', authTime=100)
        self.assertEqual(self.client.delete('/api/archive/synthetic').status_code, 403)

    def test_old_source_secret_cannot_forge_a_session(self):
        old_app = Flask('old-session')
        old_app.secret_key = 'printingshop_secret_key'
        serializer = old_app.session_interface.get_signing_serializer(old_app)
        forged = serializer.dumps({'uid': 'admin', 'email': 'admin@example.com', 'role': 'admin', 'authTime': 100})
        self.client.set_cookie('session', forged)
        self.assertEqual(self.client.delete('/api/archive/synthetic').status_code, 401)

    def test_missing_disabled_and_archived_profiles_are_rejected(self):
        for changes in [{'archived': True}, {'isArchived': True}, {'disabled': True}, {'isDisabled': True}, {'status': 'disabled'}, {'role': 'unsupported'}]:
            original = dict(self.db.records['users/customer'])
            self.db.records['users/customer'].update(changes)
            self.assertEqual(self.client.get('/api/auth/me', headers=self.headers).status_code, 403)
            self.db.records['users/customer'] = original
        del self.db.records['users/customer']
        self.assertEqual(self.client.get('/api/auth/me', headers=self.headers).status_code, 403)

    def test_indexed_uppercase_legacy_archive_blocks_login(self):
        self.db.records['customers/old'] = {'email': 'CUSTOMER@example.com', 'emailNormalized': 'customer@example.com', 'archived': True}
        self.assertEqual(self.client.get('/api/auth/me', headers=self.headers).status_code, 403)

    def test_order_price_manipulation_and_non_finite_totals_are_rejected(self):
        for total in [0.01, 'NaN', 'Infinity', -1]:
            response = self.client.post('/api/orders', headers=self.headers,
                json={'submissionId': 'x' * 32, 'items': [{'id': 'p1', 'qty': 2}], 'total': total, 'payment': 'Cash on Pickup'})
            self.assertEqual(response.status_code, 409)
        self.assertFalse(any(path.startswith('artifacts/') for path in self.db.records))

    def test_repeat_ordinary_checkout_has_one_order_and_canonical_owner(self):
        self.db.records['loyalty_accounts/customer'] = {'balance': 0, 'reserved': 0, 'cashOnPickupUnlocked': True}
        payload = {'submissionId': 'x' * 32, 'items': [{'id': 'p1', 'qty': 2}], 'total': 20,
                   'payment': 'Cash on Pickup', 'customerId': 'other', 'email': 'other@example.com'}
        first = self.client.post('/api/orders', headers=self.headers, json=payload)
        second = self.client.post('/api/orders', headers=self.headers, json=payload)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json['orderId'], second.json['orderId'])
        self.assertEqual(first.json['order']['customerId'], 'customer')
        self.assertEqual(first.json['order']['total'], 20)

    def test_push_registration_ignores_supplied_uid_and_admin_role(self):
        token = 'synthetic-device-token-' * 4
        response = self.client.post('/api/notifications/fcm-token', headers=self.headers,
                                    json={'token': token, 'uid': 'admin', 'role': 'admin'})
        self.assertEqual(response.status_code, 200)
        path = 'fcm_tokens/' + hashlib.sha256(token.encode()).hexdigest()
        record = self.db.records[path]
        self.assertEqual((record['uid'], record['role']), ('customer', 'customer'))
        self.client.delete('/api/notifications/fcm-token', headers={'Authorization': 'Bearer admin'}, json={'token': token})
        self.assertIn(path, self.db.records)

    def test_foreign_uploaded_file_and_forged_url_are_rejected(self):
        self.backend.sc = types.SimpleNamespace(SUPABASE_URL='https://storage.example.com', PRIVATE_BUCKET_NAME='private')
        url = 'https://storage.example.com/storage/v1/object/sign/private/orders/other/test.pdf?token=expired'
        key = hashlib.sha256(b'private/orders/other/test.pdf').hexdigest()
        self.db.records['file_ownership/' + key] = {'uid': 'other', 'purpose': 'document'}
        with self.assertRaises(PermissionError):
            self.backend.upload_metadata(url, {'uid': 'customer'})
        with self.assertRaises(ValueError):
            self.backend.upload_metadata('https://foreign.example.com/' + key, {'uid': 'customer'})

    def test_private_upload_sets_server_owner_and_catalog_upload_requires_admin(self):
        import supabase_config as storage
        self.backend.sc = storage
        url = 'https://storage.example.com/storage/v1/object/sign/private/orders/customer/document.pdf'
        with patch.object(storage, 'upload_order_file', return_value=('orders/customer/document.pdf', url)):
            response = self.client.post('/api/upload', headers=self.headers, data={
                'file': (io.BytesIO(b'%PDF-synthetic'), 'document.pdf'), 'purpose': 'document', 'uid': 'admin'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['fileUrl'], url)
        ownership = [value for path, value in self.db.records.items() if path.startswith('file_ownership/')]
        self.assertEqual(len(ownership), 1)
        self.assertEqual((ownership[0]['uid'], ownership[0]['purpose']), ('customer', 'document'))
        response = self.client.post('/api/upload', headers=self.headers, data={
            'file': (io.BytesIO(b'image'), 'catalog.png'), 'purpose': 'catalog'})
        self.assertEqual(response.status_code, 403)

    def test_partial_upload_cleans_new_objects_without_committing_ownership(self):
        import supabase_config as storage
        self.backend.sc = storage
        with patch.object(storage, 'upload_order_file', side_effect=[('temporary.pdf', 'https://example.com/signed'), RuntimeError('interrupted')]), \
                patch.object(storage, 'delete_file') as cleanup:
            response = self.client.post('/api/upload', headers=self.headers, data={
                'file': [(io.BytesIO(b'%PDF-one'), 'one.pdf'), (io.BytesIO(b'%PDF-two'), 'two.pdf')]})
        self.assertEqual(response.status_code, 503)
        cleanup.assert_called_once_with('temporary.pdf', bucket_name=storage.PRIVATE_BUCKET_NAME)
        self.assertFalse(any(path.startswith('file_ownership/') for path in self.db.records))


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class DeploymentRulesTests(unittest.TestCase):
    setUp = access.PushRulesTests.setUp
    call = access.PushRulesTests.call

    def test_private_records_cannot_be_read_across_customers(self):
        paths = ['/artifacts/printcraft-pro/public/data/orders/other', '/conversations/other/messages/private',
                 '/fcm_tokens/other', '/customers/other', '/users/other']
        for path in paths:
            self.call('PATCH', path, {'fields': {'customerId': {'stringValue': 'other'}}}, 'owner')
            self.assertEqual(self.call('GET', path, bearer=access.token('customer1')), 403)

    def test_public_otp_and_legacy_inventory_bypasses_are_closed(self):
        data = {'fields': {'value': {'stringValue': 'synthetic'}}}
        for path in ['/pending_users/test', '/verification_challenges/test', '/account_identity_index/test']:
            self.assertEqual(self.call('PATCH', path, data), 403)
            self.assertEqual(self.call('GET', path, bearer=access.token('customer1')), 403)
        path = '/artifacts/printcraft-pro/public/data/store_products/test'
        self.assertEqual(self.call('PATCH', path, data, access.token('customer1')), 403)
        self.assertEqual(self.call('PATCH', path, data, access.token('admin1')), 200)

    def test_customers_cannot_reactivate_or_change_profile_identity(self):
        for field in ['role', 'uid', 'email', 'loyaltyUid', 'archived', 'isDisabled', 'status']:
            data = {'fields': {field: {'stringValue': 'tampered'}}}
            self.assertEqual(self.call('PATCH', '/users/customer1?updateMask.fieldPaths=' + field, data, access.token('customer1')), 403)

    def test_customer_order_queries_must_be_owner_filtered(self):
        parent = '/artifacts/printcraft-pro/public/data:runQuery'
        query = {'structuredQuery': {'from': [{'collectionId': 'orders'}], 'where': {'fieldFilter': {
            'field': {'fieldPath': 'customerId'}, 'op': 'EQUAL', 'value': {'stringValue': 'customer1'}}}}}
        self.assertEqual(self.call('POST', parent, query, access.token('customer1')), 200)
        query['structuredQuery'].pop('where')
        self.assertEqual(self.call('POST', parent, query, access.token('customer1')), 403)

    def test_foreign_conversation_uid_cannot_be_bypassed_by_matching_email(self):
        path = '/conversations/foreign-uid'
        data = {'fields': {'customerId': {'stringValue': 'other'},
                          'customerEmail': {'stringValue': 'customer1@example.com'}}}
        self.assertEqual(self.call('PATCH', path, data, 'owner'), 200)
        self.assertEqual(self.call('GET', path, bearer=access.token('customer1')), 403)
