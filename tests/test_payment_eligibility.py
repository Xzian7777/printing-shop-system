import unittest
import json
from pathlib import Path
import shutil
import subprocess
import test_loyalty_api as support


class PaymentEligibilityTests(unittest.TestCase):
    setUp = support.LoyaltyApiTests.setUp

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_web_cash_controls_follow_server_eligibility(self):
        source = (Path(__file__).parents[1] / 'static/js/loyalty.js').read_text(encoding='utf-8').split('const Loyalty =', 1)[0]
        script = '''
const vm=require('vm');const assert=require('node:assert/strict');
const cash={name:'custPaymentMethod',checked:true,disabled:false,closest:()=>({classList:{toggle(){}}})};
const gcash={checked:false};const message={textContent:''};let selected;
const document={querySelectorAll:s=>s.startsWith('input')?[cash]:[message],querySelector:()=>gcash};
const env={document,togglePaymentSelection:value=>selected=value};
vm.createContext(env);vm.runInContext(SOURCE,env);
vm.runInContext('PaymentEligibility.apply({cashOnPickupAllowed:false})',env);
assert.equal(cash.disabled,true);assert.equal(cash.checked,false);assert.equal(gcash.checked,true);
assert.equal(selected,'gcash');assert.match(message.textContent,/first payment-verified, completed order/);
vm.runInContext('PaymentEligibility.apply({cashOnPickupAllowed:true})',env);
assert.equal(cash.disabled,false);assert.match(message.textContent,/available/);
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def lock(self):
        self.db.records['loyalty_accounts/u1'].pop('cashOnPickupUnlocked', None)

    def eligibility(self):
        response = self.client.get('/api/loyalty/me', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_new_customer_cash_is_denied_even_with_forged_flags(self):
        self.lock()
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
        self.assertFalse(self.eligibility()['hasOrders'])
        result = self.client.post('/api/orders', headers=self.headers, json={
            'submissionId': 'n' * 32, 'items': [{'id': 'p1', 'qty': 1}],
            'total': 10, 'payment': 'Cash on Pickup', 'cashOnPickupAllowed': True,
            'customerId': 'admin', 'pointsUsed': 0,
        })
        self.assertEqual(result.status_code, 409, result.json)
        self.assertIn('first payment-verified, completed order', result.json['message'])
        self.assertFalse(any('/orders/' in key for key in self.db.records))

    def test_only_owned_positive_paid_completed_orders_qualify(self):
        self.lock()
        key = 'artifacts/printcraft-pro/public/data/orders/history'
        for status, paid, total in [
            ('Approved', 'Paid', 10), ('Printing', 'Paid', 10),
            ('Ready for Pickup', 'Paid', 10), ('Cancelled', 'Paid', 10),
            ('Rejected', 'Paid', 10), ('Completed', 'Pending', 10),
            ('Completed', 'Paid with Points', 0), ('Completed', 'Paid', 0),
        ]:
            with self.subTest(status=status, paid=paid, total=total):
                self.db.records[key] = {'customerId': 'u1', 'status': status,
                                        'paymentStatus': paid, 'total': total}
                self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
                self.assertTrue(self.eligibility()['hasOrders'])
        self.db.records[key] = {'customerId': 'other', 'status': 'Completed',
                                'paymentStatus': 'Paid', 'total': 10}
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
        self.db.records[key].update(customerId='u1', loyaltyUid='other')
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
        self.db.records[key]['loyaltyUid'] = 'u1'
        self.assertTrue(self.eligibility()['cashOnPickupAllowed'])

    def test_completed_legacy_order_qualifies_without_a_balance_marker(self):
        self.lock()
        self.db.records['orders/legacy'] = {'customerId': 'u1', 'status': 'Completed',
                                           'paymentStatus': 'Paid', 'totalAmount': 10}
        self.assertTrue(self.eligibility()['cashOnPickupAllowed'])

    def test_admin_payment_confirmation_and_completion_unlock_atomically(self):
        self.lock()
        path = 'artifacts/printcraft-pro/public/data/orders/PRNT-111111'
        self.db.records[path] = {'id': 'PRNT-111111', 'customerId': 'u1', 'loyaltyUid': 'u1',
            'status': 'Ready for Pickup', 'payment': 'GCash', 'total': 10, 'pointsEarned': 0}
        admin = {'uid': 'admin', 'role': 'admin', 'email': 'admin@example.com'}
        with self.assertRaises(ValueError):
            self.env['apply_order_status'](self.db, 'PRNT-111111', 'Completed', admin)
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
        self.env['apply_order_status'](self.db, 'PRNT-111111', 'Completed', admin, payment_confirmed=True)
        self.assertTrue(self.eligibility()['cashOnPickupAllowed'])
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 11)
        self.env['apply_order_status'](self.db, 'PRNT-111111', 'Completed', admin, payment_confirmed=True)
        self.assertEqual(self.db.records['loyalty_accounts/u1']['balance'], 11)

    def test_new_customer_can_pay_with_gcash_and_free_claim_does_not_unlock(self):
        self.lock()
        self.db.records['settings/gcash_config'] = {'isActive': True, 'accountNumber': '09123456789'}
        response = self.client.post('/api/orders', headers=self.headers, json={
            'submissionId': 'g' * 32, 'items': [{'id': 'p1', 'qty': 1}], 'total': 10,
            'payment': 'GCash', 'gcashRef': '1234567890123', 'merchantAccountNumber': '09123456789',
        })
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
        self.db.records['artifacts/printcraft-pro/public/data/orders/free'] = {
            'id': 'free', 'customerId': 'u1', 'loyaltyUid': 'u1', 'status': 'Ready for Pickup',
            'total': 0, 'payment': 'Points', 'pointsEarned': 0}
        self.env['apply_order_status'](self.db, 'free', 'Completed',
                                      {'uid': 'admin', 'role': 'admin', 'email': 'admin@example.com'})
        self.assertFalse(self.eligibility()['cashOnPickupAllowed'])
