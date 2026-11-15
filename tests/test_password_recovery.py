import ast
import copy
import datetime
import re
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
from flask import Flask
from backend_test_support import load_backend
from test_admin_sync import Database


class MissingUser(Exception):
    pass


class PasswordRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.email = 'person@gmail.com'
        self.password = None
        self.user = types.SimpleNamespace(uid='u1', email=self.email, disabled=False, display_name='Person',
            tokens_valid_after_timestamp=0, provider_data=[types.SimpleNamespace(provider_id='google.com')])
        self.db.records = {
            'users/u1': {'uid': 'u1', 'email': self.email, 'role': 'customer'},
            'customers/a': {'uid': 'u1', 'email': self.email, 'rfidUid': 'card-a'},
            'customers/b': {'uid': 'u1', 'email': self.email, 'rfidUid': 'card-b'},
            'loyalty_accounts/u1': {'balance': 20, 'reserved': 3},
            'orders/o1': {'customerId': 'u1', 'total': 100},
        }
        def get_user_by_email(email):
            if not self.user or self.user.email != email:
                raise MissingUser()
            return self.user
        def update_user(uid, password, **fields):
            self.assertEqual(uid, self.user.uid)
            self.password = password
            self.user.provider_data = [types.SimpleNamespace(provider_id=name) for name in ['google.com', 'password']]
        self.lock = threading.RLock()
        def transactional(function):
            def run(transaction):
                with self.lock:
                    result = function(transaction)
                    transaction.commit()
                    return result
            return run
        self.auth = types.SimpleNamespace(get_user_by_email=get_user_by_email, get_user=lambda uid: self.user,
            UserNotFoundError=MissingUser, update_user=Mock(side_effect=update_user))
        self.sdk = types.SimpleNamespace(transactional=transactional, DELETE_FIELD='__delete_field__', SERVER_TIMESTAMP='server-time')
        self.backend = load_backend({'db': self.db, 'auth': self.auth, 'firestore': self.sdk, 'send_smtp_email': Mock(return_value=True)})
        self.backend.verify_password_with_firebase = lambda email, password: (
            (True, {'localId': 'u1'}) if self.password and password == self.password else (False, 'INVALID_LOGIN_CREDENTIALS'))
        self.app = self.backend.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.app.logger.disabled = True
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ['forgot_password', 'verify_reset_otp', 'reset_password', 'login', 'api_login']:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.client = self.app.test_client()

    def request_code(self):
        response = self.client.post('/api/forgot-password', json={'email': self.email})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('otp', response.json)
        return re.search(r'<b>([0-9]{6})</b>', self.backend.send_smtp_email.call_args.args[2]).group(1)

    def reset(self, code, password='ValidSecret7!'):
        return self.client.post('/api/reset-password', json={'email': self.email, 'otp': code, 'newPassword': password})

    def challenge(self):
        return self.backend.challenge_ref('reset', self.email)

    def test_google_account_gains_password_without_changing_business_data(self):
        before = copy.deepcopy(self.db.records)
        code = self.request_code()
        stored = self.challenge().get().to_dict()
        self.assertNotIn('otp', stored)
        self.assertNotEqual(stored['otpHash'], code)
        self.assertEqual(self.reset(code).status_code, 200)
        self.auth.update_user.assert_called_once_with('u1', password='ValidSecret7!', email_verified=True)
        for path, value in before.items():
            self.assertEqual(self.db.records[path], value)
        self.assertEqual(self.reset(code).status_code, 400)
        self.assertNotIn('otpHash', self.challenge().get().to_dict())

    def test_verified_step_cannot_bypass_wrong_or_expired_code(self):
        code = self.request_code()
        self.assertEqual(self.client.post('/api/verify-reset-otp', json={'email': self.email, 'otp': code}).status_code, 200)
        wrong = '000000' if code != '000000' else '111111'
        self.assertEqual(self.reset(wrong).status_code, 400)
        self.challenge().set({'expiresAt': datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)}, merge=True)
        self.assertEqual(self.reset(code).status_code, 400)
        self.auth.update_user.assert_not_called()

    def test_five_wrong_codes_and_resend_cooldown(self):
        code = self.request_code()
        wrong = '000000' if code != '000000' else '111111'
        for attempt in range(5):
            self.assertEqual(self.reset(wrong).status_code, 429 if attempt == 4 else 400)
        self.assertEqual(self.reset(code).status_code, 429)
        self.assertEqual(self.client.post('/api/forgot-password', json={'email': self.email}).status_code, 429)

    def test_smtp_failure_allows_retry_and_has_no_plaintext_code(self):
        self.backend.send_smtp_email.return_value = False
        self.assertEqual(self.client.post('/api/forgot-password', json={'email': self.email}).status_code, 503)
        self.backend.send_smtp_email.return_value = True
        self.request_code()

    def test_independent_worker_uses_same_persisted_challenge(self):
        code = self.request_code()
        worker = load_backend({'db': self.db, 'auth': self.auth, 'firestore': self.sdk})
        self.assertEqual(worker.verify_challenge('reset', self.email, code)['uid'], 'u1')
        self.assertNotIn('otp_memory_store', worker.__dict__)

    def test_failed_auth_update_releases_claim_and_concurrent_retries_consume_once(self):
        code = self.request_code()
        original = self.auth.update_user.side_effect
        self.auth.update_user.side_effect = RuntimeError('network unavailable')
        self.assertEqual(self.reset(code).status_code, 503)
        self.auth.update_user.side_effect = original
        self.auth.update_user.reset_mock()
        def submit(_):
            with self.app.test_client() as client:
                return client.post('/api/reset-password', json={'email': self.email, 'otp': code, 'newPassword': 'ValidSecret7!'}).status_code
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(submit, range(2)))
        self.assertEqual(results.count(200), 1)
        self.assertTrue(all(status in [200, 400, 409] for status in results))
        self.auth.update_user.assert_called_once()

    def test_disabled_account_and_changed_uid_are_rejected(self):
        self.user.disabled = True
        self.assertEqual(self.client.post('/api/forgot-password', json={'email': self.email}).status_code, 403)
        self.user.disabled = False
        code = self.request_code()
        self.backend.reset_account = lambda email: types.SimpleNamespace(uid='different-uid')
        self.assertEqual(self.reset(code).status_code, 409)
        self.auth.update_user.assert_not_called()

    def test_password_validation_and_missing_auth_account(self):
        code = self.request_code()
        for value in ['weak', '12345678', 'Valid Secret7!', 'personSecret7!']:
            self.assertEqual(self.reset(code, value).status_code, 400)
        self.user = None
        self.assertEqual(self.client.post('/api/forgot-password', json={'email': self.email}).status_code, 400)

    def test_archived_profile_stays_archived_after_password_reset(self):
        self.db.records['users/u1']['archived'] = True
        code = self.request_code()
        self.assertEqual(self.reset(code).status_code, 200)
        self.assertEqual(self.client.post('/api/login', json={'email': self.email, 'password': 'ValidSecret7!'}).status_code, 403)

    def test_web_and_mobile_use_same_firebase_password_and_role(self):
        self.email = self.user.email = 'admin@gmail.com'
        self.db.records['users/u1'].update(email=self.email, role='admin')
        code = self.request_code()
        self.assertEqual(self.reset(code).status_code, 200)
        response = self.client.post('/api/login', json={'email': self.email, 'password': 'ValidSecret7!'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['redirectUrl'], '/admin')
        self.assertEqual(response.json['user']['uid'], 'u1')
        self.assertEqual(self.client.post('/api/login', json={'email': self.email, 'password': 'OldPassword7!'}).status_code, 400)

    def test_profile_quota_reports_password_acceptance_without_starting_session(self):
        code = self.request_code()
        self.assertEqual(self.reset(code).status_code, 200)
        self.backend.customer_identity = Mock(side_effect=self.backend.ResourceExhausted('Quota exceeded.'))
        response = self.client.post('/api/login', json={'email': self.email, 'password': 'ValidSecret7!'})
        self.assertEqual(response.status_code, 503)
        self.assertIn('Firestore quota', response.json['message'])
        with self.client.session_transaction() as state:
            self.assertNotIn('role', state)
