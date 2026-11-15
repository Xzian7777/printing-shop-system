import ast
import re
import types
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_password_recovery as recovery


class SignupSecurityTests(unittest.TestCase):
    def setUp(self):
        recovery.PasswordRecoveryTests.setUp(self)
        self.user = None
        self.email = 'newcustomer@gmail.com'
        def create_user(**data):
            self.user = types.SimpleNamespace(uid=data['uid'], email=data['email'], disabled=False,
                display_name=data['display_name'], provider_data=[], tokens_valid_after_timestamp=0)
            return self.user
        self.auth.create_user = Mock(side_effect=create_user)
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ['request_signup_otp', 'api_signup']:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)

    def signup_code(self):
        response = self.client.post('/api/request-signup-otp', json={'email': self.email})
        self.assertEqual(response.status_code, 200, response.json)
        return re.search(r'<b>([0-9]{6})</b>', self.backend.send_smtp_email.call_args.args[2]).group(1)

    def payload(self, code):
        return {'email': self.email, 'fullName': 'New Customer', 'contact': '09123456789',
                'password': 'ValidSecret7!', 'confirmPassword': 'ValidSecret7!', 'otp': code, 'role': 'admin'}

    def test_signup_profiles_are_atomic_customer_role_zero_balance_and_verified_email(self):
        code = self.signup_code()
        response = self.client.post('/api/signup', json=self.payload(code))
        self.assertEqual(response.status_code, 200, response.json)
        uid = self.user.uid
        self.assertEqual(self.db.records['users/' + uid]['role'], 'customer')
        self.assertEqual(self.db.records['customers/' + uid]['uid'], uid)
        self.assertEqual(self.db.records['loyalty_accounts/' + uid]['balance'], 0)
        self.assertNotIn('password', self.db.records['users/' + uid])
        self.assertNotIn('password', self.db.records['customers/' + uid])
        self.assertTrue(self.auth.create_user.call_args.kwargs['email_verified'])
        self.assertEqual(self.client.post('/api/signup', json=self.payload(code)).status_code, 400)
        self.auth.create_user.assert_called_once()

    def test_wrong_signup_code_and_changed_password_have_no_account_side_effect(self):
        code = self.signup_code()
        wrong = '000000' if code != '000000' else '111111'
        self.assertEqual(self.client.post('/api/signup', json=self.payload(wrong)).status_code, 400)
        self.auth.create_user.assert_not_called()
        record = self.backend.verify_challenge('signup', self.email, code, claim=True, operation='first-operation')
        self.backend.finish_challenge('signup', self.email, record, False)
        self.assertEqual(self.client.post('/api/signup', json=self.payload(code)).status_code, 409)


