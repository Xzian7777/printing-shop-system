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
        app = Flask(__name__); app.secret_key = 'test-only'
        self.saved = []
        self.merchant = {'accountNumber':'09123456789','isActive':True}
        self.save_ok = True
        def save(order):
            if self.save_ok: self.saved.append(order)
            return self.save_ok
        env = {'app':app,'wraps':wraps,'jsonify':jsonify,'request':request,'session':session,
            'uuid':uuid,'datetime':datetime,'re':re,'validate_payment':validate_payment,
            'validate_merchant':validate_merchant,'validate_receipt':validate_receipt,
            'get_gcash_settings':lambda:self.merchant,'create_order':save,'get_services':lambda:[],
            'format_proper_order_info':lambda order:order}
        source = Path(__file__).parents[1] / 'app.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ['login_required','handle_orders']]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),env)
        self.client = app.test_client()
        with self.client.session_transaction() as state:
            state.update(email='customer@example.com',uid='owner-uid',role='customer',username='Customer')
        self.payload = {'file':'Store_Receipt.pdf','service':'In-Store: Paper (2x)', 'paperSize':'Store Supply',
            'items':[{'id':'paper','qty':2,'price':5}], 'copies':2,'pages':1,'total':10,
            'payment':'GCash','gcashRef':'1234567890123','merchantAccountNumber':'09123456789'}

    def test_store_gcash_saves_reference_and_receipt(self):
        receipt = 'data:image/png;base64,' + base64.b64encode(b'receipt').decode()
        response = self.client.post('/api/orders', json={**self.payload,'gcashReceiptUrl':receipt})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.saved[0]['gcashRef'],'1234567890123')
        self.assertEqual(self.saved[0]['gcashReceiptUrl'],receipt)
        self.assertEqual(self.saved[0]['merchantAccountNumber'],'09123456789')
        self.assertEqual(self.saved[0]['total'],10)

    def test_invalid_reference_paymaya_and_stale_merchant_fail_without_order(self):
        for change in [{'gcashRef':''},{'gcashRef':'x'*13},{'payment':'Paymaya'},{'merchantAccountNumber':'09999999999'}]:
            response = self.client.post('/api/orders',json={**self.payload,**change})
            self.assertEqual(response.status_code,400,response.json)
        self.assertEqual(self.saved,[])

    def test_cash_clears_gcash_fields(self):
        response = self.client.post('/api/orders',json={**self.payload,'payment':'Cash on Pickup','gcashReceiptUrl':'old-receipt'})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.saved[0]['gcashRef'],'')
        self.assertEqual(self.saved[0]['gcashReceiptUrl'],'')

    def test_failed_save_reports_failure(self):
        self.save_ok = False
        response = self.client.post('/api/orders',json=self.payload)
        self.assertEqual(response.status_code,503)
        self.assertFalse(response.json['success'])
