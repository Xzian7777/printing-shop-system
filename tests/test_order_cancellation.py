import ast
import datetime
import os
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import flask
from backend_test_support import backend as order_cancellation
from backend_test_support import stock_plan, valid_transition
from test_admin_sync import Database, transactional


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.ref = self.db.document('artifacts/printcraft-pro/public/data/orders/o1')
        self.ref.set({'status': 'Order Submitted', 'email': 'owner@example.com', 'customerId': 'legacy-owner', 'total': 100})
        self.patch = patch.object(order_cancellation.firestore, 'transactional', transactional)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def cancel(self, **changes):
        return order_cancellation.cancel_customer_order(self.db, changes.get('id', 'o1'), changes.get('uid', 'owner-uid'), changes.get('email', 'owner@example.com'), changes.get('reason', 'Changed plans'))

    def test_owner_cancels_without_deleting_order_or_changing_amount_and_repeat_is_idempotent(self):
        self.assertEqual(self.cancel()['status'], 'Cancelled')
        before = self.ref.get().to_dict()
        self.cancel(reason='Different reason')
        after = self.ref.get().to_dict()
        self.assertEqual(after['cancellationReason'], before['cancellationReason'])
        self.assertEqual(after['total'], 100)
        self.assertEqual(after['cancelledBy'], 'owner-uid')
        self.assertEqual(after['status'], 'Cancelled')

    def test_approved_and_later_orders_and_stock_or_approval_flags_block_cancellation(self):
        for change in [
            {'status': 'Approved'}, {'status': 'Printing'}, {'status': 'Preparing'},
            {'status': 'Ready for Pickup'}, {'status': 'Completed'}, {'status': 'Rejected'},
            {'status': 'Order Submitted', 'stockDeducted': True},
            {'status': 'Order Submitted', 'approvedAt': 'approved'},
            {'status': 'Order Submitted', 'source': 'POS'},
        ]:
            with self.subTest(change=change):
                self.ref.set({'email': 'owner@example.com', 'status': 'Order Submitted', **change})
                with self.assertRaises(ValueError): self.cancel()

    def test_owner_missing_order_and_reason_limits(self):
        with self.assertRaises(PermissionError): self.cancel(email='other@example.com', uid='other')
        with self.assertRaises(FileNotFoundError): self.cancel(id='missing')
        for reason in ['x' * 501, []]:
            with self.assertRaises(TypeError): self.cancel(reason=reason)
        self.ref.set({'customerId': 'owner-uid', 'status': 'For Approval', 'stockDeducted': False})
        self.assertEqual(self.cancel(email='', reason='')['status'], 'Cancelled')

    def test_cancelled_order_has_no_admin_production_transition(self):
        for status in ['Approved', 'Printing', 'Preparing', 'Ready for Pickup', 'Completed', 'Rejected']:
            self.assertFalse(valid_transition('Cancelled', status))


class CancellationApiTests(unittest.TestCase):
    def test_status_codes(self):
        app = flask.Flask(__name__); app.secret_key = 'test-only'
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'cancel_order_endpoint')
        env = {'cancel_customer_order': lambda *args, **kwargs: order_cancellation.cancel_customer_order(*args, **kwargs), 'app': app, 'db': object(), 'session': flask.session, 'request': flask.request, 'jsonify': flask.jsonify}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), env)
        client = app.test_client()
        self.assertEqual(client.post('/api/orders/o1/cancel', json={}).status_code, 401)
        with client.session_transaction() as state: state.update(username='Owner', uid='owner-uid', email='owner@example.com')
        for error, code in [(FileNotFoundError('missing'), 404), (PermissionError('owner'), 403), (TypeError('reason'), 400), (ValueError('approved'), 409)]:
            with patch.object(order_cancellation, 'cancel_customer_order', side_effect=error):
                self.assertEqual(client.post('/api/orders/o1/cancel', json={}).status_code, code)
        with patch.object(order_cancellation, 'cancel_customer_order', return_value={'id': 'o1', 'status': 'Cancelled'}):
            self.assertEqual(client.post('/api/orders/o1/cancel', json={}).status_code, 200)


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class CancellationRaceTests(unittest.TestCase):
    def test_approval_and_cancellation_compete_on_the_same_order(self):
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        from firebase_admin import firestore
        db = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        env = {'db': db, 'firestore': firestore, 'datetime': datetime, 'uuid': uuid,
               'valid_transition': valid_transition, 'stock_plan': stock_plan}
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and not any(ast.unparse(d).startswith('app.') for d in n.decorator_list)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'app.py', 'exec'), env)
        env['create_notification'] = lambda *_: True
        env['send_fcm_push_notification'] = lambda **_: True
        for _ in range(3):
            key = uuid.uuid4().hex
            db.collection('products').document(key).set({'name': 'Race product', 'stock': 5, 'price': 10})
            order = db.document('artifacts/printcraft-pro/public/data/orders/' + key)
            order.set({'status': 'Order Submitted', 'email': 'owner@example.com', 'customerId': 'owner',
                       'items': [{'id': key, 'qty': 1}], 'total': 10})
            def cancel():
                try: order_cancellation.cancel_customer_order(db, key, 'owner', 'owner@example.com'); return True
                except ValueError: return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(env['update_order_status'], key, 'Approved')
                b = pool.submit(cancel)
                approved, cancelled = a.result(), b.result()
            self.assertEqual(int(approved) + int(cancelled), 1)
            status = order.get().to_dict()['status']
            self.assertIn(status, ['Approved', 'Cancelled'])
            self.assertEqual(db.collection('products').document(key).get().to_dict()['stock'], 4 if approved else 5)
        db.close()


if __name__ == '__main__': unittest.main()
