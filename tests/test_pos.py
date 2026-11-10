import ast
import copy
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from backend_test_support import backend as pos_backend
from backend_test_support import cents, validate_sale
from test_admin_sync import Database, transactional


def request_data(**changes):
    return {'submissionId': uuid.uuid4().hex, 'buyerName': 'Maria Santos',
            'items': [{'id': 'p1', 'kind': 'product', 'qty': 2, 'unitPrice': 19}],
            'expectedTotal': 38, 'cashReceived': 100, 'payment': 'Cash', **changes}


class PosValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {('product', 'p1'): {'name': 'Notebook', 'price': 19, 'stock': 5},
                        ('service', 's1'): {'name': 'Printing', 'basePrice': 3}}

    def test_buyer_cash_change_and_mixed_service_quote(self):
        sale = validate_sale(request_data(), self.catalog)
        self.assertEqual((sale['buyerName'], sale['total'], sale['change']), ('Maria Santos', 38, 62))
        sale = validate_sale(request_data(items=[
            {'id': 'p1', 'kind': 'product', 'qty': 2, 'unitPrice': 19},
            {'id': 's1', 'kind': 'service', 'qty': 10, 'unitPrice': 4, 'note': 'A4 color'},
        ], expectedTotal=78), self.catalog)
        self.assertEqual((sale['productTotal'], sale['serviceTotal']), (38, 40))
        self.assertEqual(sale['items'][1]['pricingSource'], 'counter_quote')
        self.assertEqual(validate_sale(request_data(buyerName=''), self.catalog)['buyerName'], 'Walk-in Customer')

    def test_overselling_archived_changed_price_and_duplicates_are_rejected(self):
        for changes in [
            {'items': [{'id': 'p1', 'kind': 'product', 'qty': 6, 'unitPrice': 19}], 'expectedTotal': 114},
            {'items': [{'id': 'p1', 'kind': 'product', 'qty': 2, 'unitPrice': 1}]},
            {'cashReceived': 10}, {'buyerName': 'a' * 101}, {'expectedTotal': 1},
            {'items': [None]}, {'items': []},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_sale(request_data(**changes), self.catalog)
        line = request_data()['items'][0]
        with self.assertRaises(ValueError): validate_sale(request_data(items=[line, line]), self.catalog)
        self.catalog[('product', 'p1')]['archived'] = True
        with self.assertRaises(ValueError): validate_sale(request_data(), self.catalog)

    def test_quantity_and_amount_limits(self):
        for qty in [0, -1, 1.5, True, '9' * 100, 100001]:
            with self.subTest(qty=qty), self.assertRaises(ValueError):
                validate_sale(request_data(items=[{'id': 'p1', 'kind': 'product', 'qty': qty, 'unitPrice': 19}]), self.catalog)
        for amount in ['NaN', float('inf'), -1, 5000001, True]:
            with self.assertRaises(ValueError): cents(amount)
        self.assertEqual(cents(1.005), 101)
        self.assertEqual(cents(2.675), 268)

    def test_gcash_requires_reference_and_received_confirmation(self):
        valid = request_data(payment='GCash', gcashRef='1234567890123', paymentConfirmed=True, cashReceived=0)
        sale = validate_sale(valid, self.catalog)
        self.assertEqual((sale['cashReceived'], sale['change']), (38, 0))
        for changes in [{'gcashRef': 'abc'}, {'paymentConfirmed': False}, {'gcashRef': '1' * 17}, {'payment': 'PayMaya'}]:
            with self.assertRaises(ValueError): validate_sale({**valid, **changes}, self.catalog)


class PosTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.collection('products').document('p1').set({'name': 'Notebook', 'price': 19, 'stock': 5})
        self.patcher = patch.object(pos_backend.firestore, 'transactional', transactional)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_retry_creates_one_receipt_and_deducts_both_stock_copies_once(self):
        payload = request_data()
        first = pos_backend.complete_sale(self.db, payload, 'admin1')
        again = pos_backend.complete_sale(self.db, payload, 'admin1')
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(self.db.records['products/p1']['stock'], 3)
        self.assertEqual(self.db.records['artifacts/printcraft-pro/public/data/store_products/p1']['stock'], 3)
        order = self.db.records[f"artifacts/printcraft-pro/public/data/orders/{first['id']}"]
        self.assertEqual(order['status'], 'Completed')
        self.assertEqual(order['customerName'], 'Maria Santos')
        self.assertTrue(order['stockDeducted'])
        self.assertEqual(len([x for x in self.db.records if x.startswith('pos_sales/')]), 1)
        with self.assertRaises(PermissionError): pos_backend.complete_sale(self.db, payload, 'other-admin')

    def test_insufficient_stock_never_saves_partial_sale(self):
        before = copy.deepcopy(self.db.records)
        with self.assertRaises(ValueError):
            pos_backend.complete_sale(self.db, request_data(items=[{'kind': 'product', 'id': 'p1', 'qty': 6, 'unitPrice': 19}], expectedTotal=114), 'admin1')
        self.assertEqual(self.db.records, before)

    def test_reference_collision_and_legacy_only_product(self):
        self.db.records.pop('products/p1')
        self.db.document('artifacts/printcraft-pro/public/data/store_products/p1').set({'name': 'Legacy', 'price': 19, 'stock': 5})
        self.db.document('artifacts/printcraft-pro/public/data/orders/POS-111111').set({'id': 'existing'})
        with patch.object(pos_backend.secrets, 'randbelow', side_effect=[111111, 222222]):
            sale = pos_backend.complete_sale(self.db, request_data(), 'admin1')
        self.assertEqual(sale['id'], 'POS-222222')
        self.assertEqual(self.db.records['products/p1']['stock'], 3)


class PosApiTests(unittest.TestCase):
    def setUp(self):
        import flask
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'pos_sales')
        env = {'complete_sale': lambda *args, **kwargs: pos_backend.complete_sale(*args, **kwargs), 'app': self.app, 'session': flask.session, 'request': flask.request, 'jsonify': flask.jsonify, 'db': object()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), env)
        self.client = self.app.test_client()

    def test_authentication_and_admin_only_sales(self):
        self.assertEqual(self.client.post('/api/pos/sales', json=request_data()).status_code, 401)
        with self.client.session_transaction() as state: state.update(username='Customer', role='customer')
        self.assertEqual(self.client.post('/api/pos/sales', json=request_data()).status_code, 403)
        with self.client.session_transaction() as state: state.update(username='Admin', role='admin', email='admin@example.com')
        with patch.object(pos_backend, 'complete_sale', return_value={'id': 'POS-123456', 'buyerName': 'Maria'}) as complete:
            response = self.client.post('/api/pos/sales', json=request_data())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['sale']['buyerName'], 'Maria')
        self.assertEqual(complete.call_args.args[2], 'admin@example.com')


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('localhost:', '127.0.0.1:')), 'Local Firestore Emulator required')
class PosEmulatorTests(unittest.TestCase):
    def test_concurrent_counter_sales_cannot_oversell(self):
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        db = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        product = 'race_' + uuid.uuid4().hex
        db.collection('products').document(product).set({'name': 'Last notebook', 'price': 19, 'stock': 1})
        def sell(_):
            try:
                return pos_backend.complete_sale(db, request_data(items=[{'kind': 'product', 'id': product, 'qty': 1, 'unitPrice': 19}], expectedTotal=19), 'admin1')
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(sell, range(2)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertEqual(db.collection('products').document(product).get().to_dict()['stock'], 0)
        db.close()


if __name__ == '__main__': unittest.main()
