import uuid
import unittest
import os
import test_push_rules as access


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST','').startswith(('127.0.0.1:','localhost:')), 'Local Firestore Emulator required')
class LoyaltyAccessTests(unittest.TestCase):
    setUp = access.PushRulesTests.setUp
    call = access.PushRulesTests.call

    def test_only_owner_or_admin_can_read_balances_and_events(self):
        for suffix in ['', '/events/e1']:
            path = '/loyalty_accounts/customer1' + suffix
            self.assertEqual(self.call('PATCH',path,{'fields':{'balance':{'integerValue':'10'}}},'owner'),200)
            for uid in ['customer1','admin1']:
                self.assertEqual(self.call('GET',path,bearer=access.token(uid)),200)
                self.assertEqual(self.call('PATCH',path,{'fields':{'balance':{'integerValue':'100'}}},access.token(uid)),403)
            self.assertEqual(self.call('GET',path,bearer=access.token('other')),403)
            self.assertEqual(self.call('DELETE',path,bearer=access.token('admin1')),403)

    def test_profile_and_catalog_direct_write_bypasses_are_denied(self):
        for uid in ['customer1','admin1']:
            for path in ['/users/customer1','/customers/customer1']:
                self.call('PATCH',path,{'fields':{'role':{'stringValue':'customer'},'points':{'integerValue':'0'}}},'owner')
                self.assertEqual(self.call('PATCH',path,{'fields':{'points':{'integerValue':'100'}}},access.token(uid)),403)
            for path in ['/loyalty_rewards/r1','/settings/loyalty_config','/loyalty_submissions/s1']:
                self.assertEqual(self.call('PATCH',path,{'fields':{'active':{'booleanValue':True}}},access.token(uid)),403)

    def test_customer_cannot_forge_discount_or_cancel_reserved_points_directly(self):
        path = '/artifacts/printcraft-pro/public/data/orders/' + uuid.uuid4().hex
        fields = {'customerId':{'stringValue':'customer1'}, 'status':{'stringValue':'Order Submitted'},'pointsUsed':{'integerValue':'5'}}
        self.assertEqual(self.call('PATCH',path,{'fields':fields},access.token('customer1')),403)
        self.assertEqual(self.call('PATCH',path,{'fields':fields},'owner'),200)
        for uid in ['customer1','admin1']:
            self.assertEqual(self.call('PATCH',path,{'fields':{'status':{'stringValue':'Cancelled'}}},access.token(uid)),403)
        self.assertEqual(self.call('PATCH',path,{'fields':{'status':{'stringValue':'Completed'}}},access.token('admin1')),403)

    def test_admin_can_create_zero_balance_mirror_with_mapping(self):
        key = 'mirror_' + uuid.uuid4().hex
        fields = {'email': {'stringValue': 'customer1@example.com'}, 'role': {'stringValue': 'customer'},
                  'points': {'integerValue': '0'}, 'loyaltyPoints': {'integerValue': '0'},
                  'loyaltyUid': {'stringValue': 'customer1'}}
        self.assertEqual(self.call('PATCH', '/customers/'+key, {'fields': fields}, access.token('admin1')), 200)
        self.assertEqual(self.call('PATCH', '/users/customer1-new', {'fields': fields}, access.token('customer1')), 403)
        self.assertEqual(self.call('PATCH', '/customers/'+key, {'fields': {'points': {'integerValue': '170'}}}, access.token('admin1')), 403)
