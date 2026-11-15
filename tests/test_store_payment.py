import ast
import base64
import datetime
from functools import wraps
from pathlib import Path
import re
import tempfile
import unittest
import uuid
from flask import Flask, jsonify, request, session
from backend_test_support import validate_payment, validate_merchant, validate_receipt, shared_qr_settings


class PaymentRulesTests(unittest.TestCase):
    def test_gcash_references_and_cash(self):
        for method in ['GCash', 'GCash Online']:
            self.assertEqual(validate_payment(method, ' 1234567890123 ')[1], '1234567890123')
            for value in ['', '1234567', '1'*17, 'abcdefgh12345678']:
                with self.assertRaises(ValueError): validate_payment(method, value)
        self.assertEqual(validate_payment('Cash on Pickup', 'old-ref')[1], '')
        with self.assertRaises(ValueError): validate_payment('Paymaya', '')

    def test_unavailable_and_changed_merchant_are_rejected(self):
        settings = {'accountNumber':'09123456789', 'isActive':True}
        self.assertEqual(validate_merchant(settings, '09123456789'), '09123456789')
        for data, expected in [({}, ''), ({**settings, 'isActive':False}, ''), (settings, '09999999999')]:
            with self.assertRaises(ValueError): validate_merchant(data, expected)

    def test_receipts_support_optional_images_and_links(self):
        self.assertEqual(validate_receipt(''), '')
        image = 'data:image/png;base64,' + base64.b64encode(b'image-bytes').decode()
        self.assertEqual(validate_receipt(image), image)
        self.assertEqual(validate_receipt('https://example.com/receipt.jpg'), 'https://example.com/receipt.jpg')
        for value in ['javascript:alert(1)', 'data:text/html;base64,PHNjcmlwdD4=', 'data:image/png;base64,###', 'x'*700001]:
            with self.assertRaises(ValueError): validate_receipt(value)

    def test_local_qr_is_portable_and_remote_changes_clear_stale_image(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'gcash.png').write_bytes(b'qr-image')
            original = {'qrUrl':'/static/images/gcash.png?v=1','accountNumber':'09123456789'}
            shared = shared_qr_settings(original, directory)
            self.assertTrue(shared['qrImageData'].startswith('data:image/png;base64,'))
            self.assertEqual(shared['accountNumber'], original['accountNumber'])
            self.assertNotIn('qrImageData', original)
            changed = shared_qr_settings({**shared,'qrUrl':'https://example.com/new.png'}, directory)
            self.assertEqual(changed['qrImageData'], '')
            escaped = shared_qr_settings({'qrUrl':'/static/images/../outside.png'}, directory)
            self.assertEqual(escaped['qrImageData'], '')


class StorePaymentEndpointTests(unittest.TestCase):
    def setUp(self):
        import types
        from backend_test_support import load_backend
        from test_admin_sync import Database, transactional, Transaction
        self.db = Database()
        self.merchant = {'accountNumber': '09123456789', 'isActive': True}
        self.db.records = {
            'settings/loyalty_config': {'enabled': True, 'epoch': 'test', 'pointValueCents': 100},
            'settings/gcash_config': self.merchant,
            'products/paper': {'price': 5, 'stock': 100, 'name': 'Paper'},
            'loyalty_accounts/owner-uid': {'balance': 0, 'reserved': 0, 'cashOnPickupUnlocked': True},
        }
        Transaction.create = Transaction.set
        actor = {'uid': 'owner-uid', 'email': 'customer@example.com', 'role': 'customer', 'name': 'Customer', 'authTime': 1}
        self.backend = load_backend({'db': self.db, 'identity': lambda **kwargs: actor,
            'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time')})
        self.backend.store_inline_receipt = lambda value, actor: 'https://storage.example.com/private-receipt'
        self.backend.upload_metadata = lambda value, actor, purpose=None: {'uid': actor['uid'], 'purpose': purpose, 'path': 'receipt.jpg'}
        self.backend.app = Flask(__name__)
        self.backend.app.secret_key = 'test-only'
        self.backend.app.logger.disabled = True
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'handle_orders')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.client = self.backend.app.test_client()
        self.payload = {'submissionId': 'a' * 32, 'items': [{'id': 'paper', 'qty': 2}], 'total': 10,
            'payment': 'GCash', 'gcashRef': '1234567890123', 'merchantAccountNumber': '09123456789'}

    @property
    def saved(self):
        return [value for path, value in self.db.records.items() if path.startswith('artifacts/printcraft-pro/public/data/orders/')]

    def test_store_gcash_saves_reference_and_receipt(self):
        receipt = 'data:image/png;base64,' + base64.b64encode(b'receipt').decode()
        response = self.client.post('/api/orders', json={**self.payload,'gcashReceiptUrl':receipt})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.saved[0]['gcashRef'],'1234567890123')
        self.assertEqual(self.saved[0]['gcashReceiptUrl'],'https://storage.example.com/private-receipt')
        self.assertEqual(self.saved[0]['merchantAccountNumber'],'09123456789')
        self.assertEqual(self.saved[0]['total'],10)

    def test_invalid_reference_paymaya_and_stale_merchant_fail_without_order(self):
        for change in [{'gcashRef':''},{'gcashRef':'x'*13},{'payment':'Paymaya'},{'merchantAccountNumber':'09999999999'}]:
            response = self.client.post('/api/orders',json={**self.payload,**change})
            self.assertEqual(response.status_code,409,response.json)
        self.assertEqual(self.saved,[])

    def test_cash_clears_gcash_fields(self):
        response = self.client.post('/api/orders',json={**self.payload,'payment':'Cash on Pickup','gcashReceiptUrl':'old-receipt'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.saved[0]['gcashRef'],'')
        self.assertEqual(self.saved[0]['gcashReceiptUrl'],'')

    def test_failed_save_reports_failure(self):
        from unittest.mock import Mock
        self.db.transaction = Mock(side_effect=RuntimeError('database unavailable'))
        response = self.client.post('/api/orders',json=self.payload)
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.json['success'])
