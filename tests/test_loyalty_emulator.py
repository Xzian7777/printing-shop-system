import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from backend_test_support import submit_order, apply_order_status, orders_ref
from backend_test_support import complete_sale


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class LoyaltyEmulatorTests(unittest.TestCase):
    def setUp(self):
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        self.db = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        self.addCleanup(self.db.close)
        suffix = uuid.uuid4().hex
        self.actor = {'uid':'loyalty_'+suffix, 'email':suffix+'@example.com', 'role':'customer', 'name':'Customer'}
        self.admin = {'uid':'admin1', 'email':'admin@example.com', 'role':'admin'}
        self.uid = self.actor['uid']
        self.product = 'loyalty_' + suffix
        self.db.collection('products').document(self.product).set({'name':'Notebook', 'price':100, 'stock':10})
        self.account = self.db.collection('loyalty_accounts').document(self.uid)
        self.account.set({'balance':100, 'reserved':0})
        self.db.collection('settings').document('loyalty_config').set({'enabled':True, 'epoch':'emulator-test', 'pointValueCents':100})
        self.payload = {'submissionId':suffix, 'items':[{'id':self.product,'qty':2}],
                        'total':200, 'pointsUsed':80, 'payment':'Cash on Pickup'}

    def parallel(self, functions):
        barrier = threading.Barrier(len(functions))
        def run(function):
            barrier.wait(timeout=10)
            try: return function()
            except ValueError: return None
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def test_concurrent_devices_cannot_double_spend(self):
        results = self.parallel([
            lambda: submit_order(self.db,self.actor,self.payload),
            lambda: submit_order(self.db,self.actor,{**self.payload,'submissionId':uuid.uuid4().hex}),
        ])
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(self.account.get().to_dict()['reserved'],80)

    def test_approval_competes_with_cancel_on_order_and_balance(self):
        order = submit_order(self.db,self.actor,self.payload)
        result = self.parallel([
            lambda: apply_order_status(self.db,order['id'],'Approved',self.admin),
            lambda: apply_order_status(self.db,order['id'],'Cancelled',self.actor),
        ])
        self.assertEqual(sum(value is not None for value in result),1)
        current = orders_ref(self.db).document(order['id']).get().to_dict()
        account = self.account.get().to_dict()
        stock = self.db.collection('products').document(self.product).get().to_dict()['stock']
        self.assertEqual(account['reserved'],0)
        self.assertEqual((account['balance'],stock), (20,8) if current['status']=='Approved' else (100,10))

    def test_completion_retries_award_once_and_preserve_amounts(self):
        order = submit_order(self.db,self.actor,self.payload)
        for target in ['Approved','Preparing','Ready for Pickup']:
            apply_order_status(self.db,order['id'],target,self.admin)
        self.parallel([lambda:apply_order_status(self.db,order['id'],'Completed',self.admin,payment_confirmed=True)]*2)
        self.assertEqual(self.account.get().to_dict()['balance'],21)
        current = orders_ref(self.db).document(order['id']).get().to_dict()
        self.assertEqual(current['total'],120)
        self.assertEqual(current['pointsEarned'],1)
        events = list(self.account.collection('events').stream())
        self.assertEqual(len([e for e in events if e.to_dict()['type']=='earn']),1)

    def test_pos_linked_guest_free_and_same_submission_retries(self):
        payload = {'submissionId':uuid.uuid4().hex,'buyerName':'Counter buyer',
            'items':[{'id':self.product,'kind':'product','qty':1,'unitPrice':100}],
            'expectedTotal':100,'payment':'Cash','cashReceived':50,'pointsUsed':50}
        sale = complete_sale(self.db,payload,'admin1',self.actor)
        again = complete_sale(self.db,payload,'admin1',self.actor)
        self.assertEqual(sale['id'],again['id'])
        self.assertEqual((sale['total'],sale['pointsEarned'],sale['change']),(50,1,0))
        self.assertEqual(self.account.get().to_dict()['balance'],51)
        guest = complete_sale(self.db,{**payload,'submissionId':uuid.uuid4().hex,'cashReceived':100,'pointsUsed':0},'admin1')
        self.assertEqual(guest['pointsEarned'],0)
        reward_id = uuid.uuid4().hex
        self.db.collection('loyalty_rewards').document(reward_id).set({'name':'Free notebook','kind':'product','catalogId':self.product,
            'quantity':1,'pointsCost':5,'active':True,'updatedAt':'v1'})
        reward = complete_sale(self.db,{'submissionId':uuid.uuid4().hex,'items':[],'rewardId':reward_id,'rewardVersion':'v1'},'admin1',self.actor)
        self.assertEqual((reward['total'],reward['payment'],reward['pointsEarned']),(0,'Points',0))
        self.assertEqual(self.account.get().to_dict()['balance'],46)
