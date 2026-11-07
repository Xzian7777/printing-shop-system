import ast
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask

from test_admin_sync import Database, Collection, Transaction, transactional


class MissingUser(Exception):
    pass


class LoyaltyApiTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        for uid, role in [('u1','customer'),('other','customer'),('admin','admin')]:
            self.db.records['users/'+uid] = {'email':uid+'@example.com','role':role,'fullName':uid}
        self.db.records['settings/loyalty_config'] = {'enabled':True,'epoch':'test','pointValueCents':100}
        self.db.records['loyalty_accounts/u1'] = {'balance':10,'reserved':0}
        self.db.records['products/p1'] = {'name':'Paper','stock':5,'price':10}
        def user(uid):
            if 'users/'+uid not in self.db.records:
                raise MissingUser()
            return types.SimpleNamespace(uid=uid,email=uid+'@example.com',disabled=False,display_name=uid)
        def verify(value, **kwargs):
            if value == 'expired':
                raise ValueError('expired')
            return {'uid':user(value).uid}
        fake_auth = types.SimpleNamespace(get_user=user,get_user_by_email=lambda email:user(email.split('@')[0]),
            verify_id_token=verify, UserNotFoundError=MissingUser)
        self.env = {'__name__':__name__, 'auth':fake_auth,'firebase':types.SimpleNamespace(db=self.db),
            'firestore':types.SimpleNamespace(Query=types.SimpleNamespace(DESCENDING='desc'))}
        tree = ast.parse((Path(__file__).parents[1]/'loyalty_api.py').read_text())
        nodes = [n for n in tree.body if not isinstance(n,(ast.Import,ast.ImportFrom)) or
                 (isinstance(n,ast.ImportFrom) and n.module not in ['firebase_admin']) or
                 (isinstance(n,ast.Import) and n.names[0].name != 'firebase_config')]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'loyalty_api.py','exec'),self.env)
        app = Flask(__name__)
        app.secret_key = 'loyalty-api-test'
        self.env['register_loyalty_routes'](app)
        self.client = app.test_client()
        self.headers = {'Authorization':'Bearer u1'}
        for name, method in [('get',Collection.stream),('order_by',lambda self,*a,**kw:self),('limit',lambda self,*a,**kw:self)]:
            patcher=patch.object(Collection,name,method,create=True);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(Transaction,'create',Transaction.set,create=True);patcher.start();self.addCleanup(patcher.stop)
        import loyalty_backend
        patcher=patch.object(loyalty_backend.firestore,'transactional',transactional);patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(loyalty_backend.firestore,'SERVER_TIMESTAMP','server-time');patcher.start();self.addCleanup(patcher.stop)

    def test_session_and_token_authentication_and_forged_admin(self):
        self.assertEqual(self.client.get('/api/loyalty/me').status_code,401)
        self.assertEqual(self.client.get('/api/loyalty/me',headers={'Authorization':'Bearer expired'}).status_code,401)
        self.assertEqual(self.client.get('/api/loyalty/me',headers=self.headers).status_code,200)
        with self.client.session_transaction() as session:
            session.update(email='u1@example.com',role='admin')
        self.assertEqual(self.client.get('/api/admin/loyalty/rewards').status_code,403)
        self.assertEqual(self.client.get('/api/loyalty/me').json['uid'],'u1')

    def test_customer_ownership_is_from_token_and_repeat_submission_is_safe(self):
        data={'submissionId':'a'*32,'items':[{'id':'p1','qty':2}], 'total':20,'pointsUsed':5,
              'customerId':'other','email':'other@example.com','payment':'Cash on Pickup'}
        first=self.client.post('/api/orders',json=data,headers=self.headers)
        self.assertEqual(first.status_code,200,first.json)
        self.assertEqual(first.json['order']['customerId'],'u1')
        repeat=self.client.post('/api/orders',json=data,headers=self.headers)
        self.assertEqual(repeat.json['orderId'],first.json['orderId'])
        foreign=self.client.post('/api/orders/'+first.json['orderId']+'/cancel',json={},headers={'Authorization':'Bearer other'})
        self.assertEqual(foreign.status_code,403)
        self.assertEqual(self.client.get('/api/loyalty/me',headers=self.headers).json['available'],5)

    def test_only_admin_can_manage_catalog_and_conversion(self):
        payload={'name':'Free paper','kind':'product','catalogId':'p1','pointsCost':5,'quantity':1,'active':True}
        self.assertEqual(self.client.post('/api/admin/loyalty/rewards',json=payload,headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/admin/loyalty/rewards',json=payload,headers={'Authorization':'Bearer admin'}).status_code,200)
        bad=self.client.put('/api/admin/loyalty/settings',json={'pointValue':'NaN'},headers={'Authorization':'Bearer admin'})
        self.assertEqual(bad.status_code,409)

