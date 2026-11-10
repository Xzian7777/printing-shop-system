"""Google bootstrap security and profile preservation without live credentials."""
import ast
import copy
import os
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from flask import Flask
from backend_test_support import load_backend
from test_admin_sync import Database, transactional


class GoogleLoginTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.claims = {'uid': 'u1', 'email': 'person@example.com', 'email_verified': True,
                       'firebase': {'sign_in_provider': 'google.com'}}
        self.record = types.SimpleNamespace(uid='u1', email='person@example.com',
                                            display_name='Person', disabled=False)
        self.auth = types.SimpleNamespace(verify_id_token=Mock(side_effect=lambda *a, **k: self.claims),
                                         get_user=Mock(side_effect=lambda uid: self.record),
                                         UserNotFoundError=type('MissingUser', (Exception,), {}))
        self.backend = load_backend({'db': self.db, 'firebase_auth': self.auth, 'auth': self.auth,
            'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time')})
        self.backend.app = Flask(__name__)
        self.backend.app.secret_key = 'test-only'
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        route = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'api_google_login')
        exec(compile(ast.Module(body=[route], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.client = self.backend.app.test_client()

    def login(self, **extra):
        return self.client.post('/api/google-login', json={'idToken': 'verified-token', **extra})

    def test_missing_invalid_expired_revoked_and_forged_tokens_never_fall_back_to_email(self):
        self.assertEqual(self.client.post('/api/google-login', json={'email': 'admin@example.com'}).status_code, 401)
        for reason in ['expired', 'revoked', 'forged']:
            self.auth.verify_id_token.side_effect = ValueError(reason)
            self.assertEqual(self.login(email='admin@example.com', uid='admin').status_code, 401)
        self.assertEqual(self.db.records, {})
        self.auth.verify_id_token.assert_called_with('verified-token', check_revoked=True)

    def test_non_google_unverified_and_incomplete_claims_are_denied(self):
        for changes in [{'firebase': {'sign_in_provider': 'password'}},
                        {'email_verified': False}, {'uid': ''}, {'email': ''}]:
            original = self.claims.copy()
            self.claims.update(changes)
            self.assertEqual(self.login().status_code, 401)
            self.claims = original
        self.assertEqual(self.db.records, {})

    def test_new_customer_zero_balance_idempotent_and_ignores_supplied_admin_identity(self):
        first = self.login(email='admin@example.com', uid='admin', fullName='Spoofed', role='admin')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json['user'], {'uid': 'u1', 'email': 'person@example.com', 'role': 'customer', 'username': 'Person'})
        before = copy.deepcopy(self.db.records)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.db.records, before)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 0)
        self.assertEqual(self.db.records['users/u1']['points'], 0)
        self.assertEqual(self.db.records['customers/u1']['uid'], 'u1')
        with self.client.session_transaction() as session:
            self.assertEqual((session['uid'], session['role']), ('u1', 'customer'))

    def test_existing_admin_and_customer_data_are_preserved(self):
        self.db.records.update({
            'users/u1': {'uid': 'u1', 'email': 'person@example.com', 'role': 'admin', 'fullName': 'Existing', 'points': 15, 'rfidUid': 'RFID-1'},
            'customers/CUST-1': {'uid': 'u1', 'email': 'person@example.com', 'id': 'CUST-1', 'points': 15, 'rfidUid': 'RFID-1'},
            'loyalty_accounts/u1': {'balance': 50, 'reserved': 10},
            'orders/old': {'customerId': 'u1'},
        })
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['redirect'], '/admin')
        self.assertEqual(self.db.records['users/u1']['fullName'], 'Existing')
        self.assertEqual(self.db.records['users/u1']['points'], 15)
        self.assertEqual(self.db.records['customers/CUST-1']['rfidUid'], 'RFID-1')
        self.assertEqual(self.db.records['customers/CUST-1']['id'], 'CUST-1')
        self.assertEqual(self.db.records['loyalty_accounts/u1'], {'balance': 50, 'reserved': 10})
        self.assertEqual(self.db.records['orders/old'], {'customerId': 'u1'})

    def test_archived_disabled_and_ambiguous_identities_do_not_write(self):
        fixtures = [
            {'users/u1': {'archived': True}},
            {'customers/legacy': {'email': 'PERSON@example.com', 'isArchived': True}},
            {'users/u1': {'disabled': True}},
            {'customers/legacy': {'email': 'person@example.com', 'uid': 'another-uid'}},
            {'users/legacy': {'email': 'person@example.com', 'role': 'admin'}},
            {'customers/a': {'email': 'person@example.com'}, 'customers/b': {'email': 'person@example.com'}},
            {'archives/user_old': {'itemType': 'user', 'data': {'email': 'person@example.com'}}},
            {'users/u1': {'email': 'another@example.com'}},
            {'users/u1': {'loyaltyUid': 'another-uid'}},
            {'users/u1': {'customerId': 'old-id'}, 'customers/different-id': {'email': 'person@example.com', 'uid': 'u1'}},
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.db.records = copy.deepcopy(fixture)
                self.assertIn(self.login().status_code, [403, 409])
                self.assertEqual(self.db.records, fixture)
        self.db.records.clear()
        self.record.disabled = True
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(self.db.records, {})

    def test_interrupted_transaction_does_not_establish_web_session(self):
        self.db.transaction = Mock(side_effect=RuntimeError('database unavailable'))
        self.assertEqual(self.login().status_code, 503)
        with self.client.session_transaction() as session:
            self.assertNotIn('uid', session)


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')),
                     'Local Firestore Emulator required')
class GoogleProfileEmulatorTests(unittest.TestCase):
    def setUp(self):
        from google.auth.credentials import AnonymousCredentials
        from google.cloud.firestore import Client
        self.db = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        self.addCleanup(self.db.close)
        self.backend = load_backend({'db': self.db})
        self.uid = 'google_' + uuid.uuid4().hex
        self.email = self.uid + '@example.com'

    def test_concurrent_onboarding_creates_one_profile_and_one_zero_balance(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.backend.sync_google_profile(self.uid, self.email, 'Google customer'), range(2)))
        self.assertEqual(results[0], results[1])
        mirrors = list(self.db.collection('customers').where('email', '==', self.email).stream())
        self.assertEqual(len(mirrors), 1)
        self.assertEqual(mirrors[0].id, self.uid)
        self.assertEqual(self.db.collection('loyalty_accounts').document(self.uid).get().to_dict()['balance'], 0)

    def test_returning_account_retains_balance_and_archive_blocks_new_writes(self):
        profile = self.db.collection('users').document(self.uid)
        profile.set({'uid': self.uid, 'email': self.email, 'role': 'admin', 'rfidUid': 'card'})
        account = self.db.collection('loyalty_accounts').document(self.uid)
        account.set({'balance': 80, 'reserved': 10})
        self.assertEqual(self.backend.sync_google_profile(self.uid, self.email, 'Google customer')['role'], 'admin')
        self.assertEqual(account.get().to_dict(), {'balance': 80, 'reserved': 10})
        self.assertEqual(profile.get().to_dict()['rfidUid'], 'card')
        profile.update({'archived': True})
        with self.assertRaises(PermissionError):
            self.backend.sync_google_profile(self.uid, self.email, 'Google customer')


if __name__ == '__main__':
    unittest.main()
