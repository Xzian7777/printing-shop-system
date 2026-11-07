import ast
import copy
import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from loyalty_rules import discount_quote, service_total
from test_admin_sync import Database, Collection, Ref, Transaction, transactional


class LoyaltyRulesTests(unittest.TestCase):
    def test_whole_points_balance_and_half_subtotal_cap(self):
        self.assertEqual(discount_quote(1999, 9, 20, 100)['total'], 10.99)
        for value in [10, 0.5, -1, '1e2', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                discount_quote(1999, value, 20, 100)
        with self.assertRaises(ValueError):
            discount_quote(10000, 3, 2, 100)
        self.assertEqual(discount_quote(10000, 25, 100, 200)['total'], 50)

    def test_service_price_uses_catalog_and_validated_package(self):
        self.assertEqual(service_total('standardA4', 3, {'quantity': 2, 'pages': 3}), 1800)
        self.assertEqual(service_total('standardA4', 3, {'quantity': 2, 'pages': 3, 'paperStock': 'long'}), 2160)
        self.assertEqual(service_total('tarpaulins', 15, {'quantity': 1, 'details': {'dimensions': '3ft x 4ft'}}), 18000)
        with self.assertRaises(ValueError):
            service_total('tarpaulins', 15, {'details': {'dimensions': '1000ft x 2ft'}})


class LoyaltyTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.records['settings/loyalty_config'] = {'enabled': True, 'epoch': 'test', 'pointValueCents': 100}
        self.db.records['loyalty_accounts/u1'] = {'balance': 100, 'reserved': 0}
        self.db.records['products/p1'] = {'name': 'Paper', 'price': 10, 'stock': 10}
        self.actor = {'uid': 'u1', 'email': 'u1@example.com', 'name': 'Customer', 'role': 'customer'}
        self.admin = {'uid': 'admin1', 'email': 'admin@example.com', 'role': 'admin'}
        self.patch = patch.object(Transaction, 'create', Transaction.set, create=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        sdk = types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time')
        self.env = {'firestore': sdk, 'datetime': datetime}
        source = Path(__file__).parents[1] / 'loyalty_backend.py'
        tree = ast.parse(source.read_text())
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == 'firebase_admin')]
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        exec(compile(ast.Module(body=imports+functions, type_ignores=[]), str(source), 'exec'), self.env)
        self.payload = {'submissionId': 'a'*32, 'items': [{'id': 'p1', 'qty': 2}], 'total': 20,
                        'pointsUsed': 10, 'payment': 'Cash on Pickup'}

    def submit(self, payload=None):
        return self.env['submit_order'](self.db, self.actor, payload or self.payload)

    def status(self, order, target, actor=None, **kwargs):
        return self.env['apply_order_status'](self.db, order['id'], target, actor or self.admin, **kwargs)

    def test_pending_reserves_then_approval_spends_and_rejection_returns_once(self):
        order = self.submit()
        self.assertEqual(self.db.records['loyalty_accounts/u1'], {'balance':100, 'reserved':10, 'updatedAt':'server-time'})
        self.assertEqual(self.submit()['id'], order['id'])
        self.status(order, 'Approved')
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 90)
        self.assertEqual(self.db.records['products/p1']['stock'], 8)
        self.status(order, 'Approved')
        self.status(order, 'Rejected', reason='Unavailable')
        self.status(order, 'Rejected', reason='Unavailable')
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 100)
        self.assertEqual(self.db.records['products/p1']['stock'], 10)
        events = [p for p in self.db.records if '/events/' in p]
        self.assertEqual(len(events), 3)

    def test_cancel_release_cannot_be_approved_and_other_customer_cannot_cancel(self):
        order = self.submit()
        with self.assertRaises(PermissionError):
            self.status(order, 'Cancelled', actor={**self.actor, 'uid':'other', 'email':'other@example.com'})
        self.status(order, 'Cancelled', actor=self.actor)
        self.status(order, 'Cancelled', actor=self.actor)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['reserved'], 0)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 100)
        with self.assertRaises(ValueError):
            self.status(order, 'Approved')

    def test_approved_order_cannot_be_customer_cancelled(self):
        order = self.submit()
        self.status(order, 'Approved')
        with self.assertRaises(ValueError):
            self.status(order, 'Cancelled', actor=self.actor)

    def test_completed_paid_order_earns_exactly_one_point(self):
        order = self.submit()
        for target in ['Approved', 'Preparing', 'Ready for Pickup']:
            self.status(order, target)
        with self.assertRaises(ValueError):
            self.status(order, 'Completed')
        self.status(order, 'Completed', payment_confirmed=True)
        self.status(order, 'Completed', payment_confirmed=True)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 91)
        self.assertEqual(len([p for p in self.db.records if '/events/earn_' in p]), 1)

    def test_sequential_device_reservations_cannot_overspend(self):
        self.db.records['loyalty_accounts/u1']['balance'] = 10
        self.submit()
        with self.assertRaises(ValueError):
            self.submit({**self.payload, 'submissionId':'b'*32})

    def test_free_product_reward_is_zero_payment_and_earns_nothing(self):
        self.db.records['loyalty_rewards/r1'] = {'name':'Free paper', 'kind':'product', 'catalogId':'p1',
            'quantity':1, 'pointsCost':5, 'active':True, 'updatedAt':'v1'}
        order = self.submit({'submissionId':'c'*32, 'rewardId':'r1', 'rewardVersion':'v1'})
        self.assertEqual(order['total'], 0)
        self.assertEqual(order['payment'], 'Points')
        for target in ['Approved', 'Preparing', 'Ready for Pickup', 'Completed']:
            self.status(order, target)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 95)
        self.assertFalse(any('/events/earn_' in p for p in self.db.records))

    def test_invalid_price_rate_reward_and_out_of_stock_fail_before_reservation(self):
        for change in [{'total':1}, {'pointsUsed':11}, {'expectedDiscount':1}, {'items':[{'id':'p1','qty':20}]}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.submit({**self.payload, **change})
        self.assertEqual(self.db.records['loyalty_accounts/u1']['reserved'], 0)

