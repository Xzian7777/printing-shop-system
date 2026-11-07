"""Run against a local Firestore Emulator using demo-printshop-push only."""
import base64
import json
import os
import time
import unittest
import urllib.error
import urllib.request
import uuid

HOST = os.getenv('FIRESTORE_EMULATOR_HOST', '')
PROJECT = 'demo-printshop-push'


def token(uid, email=None):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'none', 'typ': 'JWT'}) + '.' + encode({
        'aud': PROJECT, 'iss': f'https://securetoken.google.com/{PROJECT}',
        'sub': uid, 'user_id': uid, 'iat': int(time.time()), 'exp': int(time.time()) + 3600,
        'email': email or uid + '@example.com',
        'firebase': {'sign_in_provider': 'custom'},
    }) + '.'


@unittest.skipUnless(HOST.startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class PushRulesTests(unittest.TestCase):
    def setUp(self):
        self.base = f'http://{HOST}/v1/projects/{PROJECT}/databases/(default)/documents'
        self.name = f'projects/{PROJECT}/databases/(default)/documents'
        self.job_id = uuid.uuid4().hex
        for uid, role in [('admin1', 'admin'), ('customer1', 'customer')]:
            self.call('PATCH', f'/users/{uid}', {'fields': {'role': {'stringValue': role}}}, 'owner')

    def call(self, method, path, data=None, bearer=None):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + bearer} if bearer else {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            return code

    def create(self, uid='admin1', extra=None, kind='order_status'):
        fields = {'type': {'stringValue': kind}, 'orderId': {'stringValue': 'o1'},
                  'status': {'stringValue': 'Ready for Pickup'}, 'state': {'stringValue': 'pending'}}
        fields.update(extra or {})
        return self.call('POST', ':commit', {'writes': [{
            'update': {'name': f'{self.name}/push_jobs/{self.job_id}', 'fields': fields},
            'updateTransforms': [{'fieldPath': 'createdAt', 'setToServerValue': 'REQUEST_TIME'}],
            'currentDocument': {'exists': False},
        }]}, token(uid) if uid else None)

    def test_admin_can_enqueue_but_customer_and_anonymous_cannot(self):
        self.assertEqual(self.create(), 200)
        self.job_id = uuid.uuid4().hex
        self.assertEqual(self.create('customer1'), 403)
        self.assertEqual(self.create(None), 403)

    def test_clients_cannot_read_change_or_delete_delivery_state(self):
        self.assertEqual(self.create(), 200)
        path = '/push_jobs/' + self.job_id
        self.assertEqual(self.call('GET', path, bearer=token('admin1')), 403)
        self.assertEqual(self.call('PATCH', path, {'fields': {'state': {'stringValue': 'sent'}}}, token('admin1')), 403)
        self.assertEqual(self.call('DELETE', path, bearer=token('admin1')), 403)

    def test_client_cannot_supply_recipient_tokens_or_arbitrary_job_type(self):
        self.assertEqual(self.create(extra={'recipient_email': {'stringValue': 'other@example.com'}}), 403)
        self.assertEqual(self.create(extra={'deliveredTokens': {'arrayValue': {'values': []}}}), 403)
        self.assertEqual(self.create(kind='broadcast'), 403)


if __name__ == '__main__':
    unittest.main()
