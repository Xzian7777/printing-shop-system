import json
import tempfile
import types
import unittest
from pathlib import Path
from loyalty_rollout import rollout
from test_admin_sync import Database


class LoyaltyRolloutTests(unittest.TestCase):
    def test_backup_reset_preserves_orders_logs_and_existing_history(self):
        db=Database()
        db.records.update({'users/u1':{'email':'a@example.com','points':20},
            'customers/c1':{'email':'a@example.com','loyaltyPoints':80},
            'customers/unknown':{'email':'missing@example.com','points':50},
            'loyalty_accounts/u1':{'balance':99,'reserved':0},
            'loyalty_accounts/u1/events/old':{'points':99},
            'orders/o1':{'status':'Completed'},'rfid_logs/t1':{'pointsAdded':20}})
        auth=types.SimpleNamespace(list_users=lambda:types.SimpleNamespace(iterate_all=lambda:iter([
            types.SimpleNamespace(uid='u1',email='a@example.com')])) )
        with tempfile.TemporaryDirectory() as directory:
            backup=Path(directory)/'balances.json'
            report=rollout(db,auth,backup)
            self.assertFalse(report['applied'])
            self.assertEqual(db.records['users/u1']['points'],20)
            result=rollout(db,auth,backup,True)
            self.assertTrue(result['applied'])
            self.assertEqual(result['unmatched'],1)
            self.assertEqual(db.records['users/u1']['points'],0)
            self.assertEqual(db.records['customers/c1']['loyaltyPoints'],0)
            self.assertEqual(db.records['loyalty_accounts/u1']['balance'],0)
            self.assertIn('loyalty_accounts/u1/events/old',db.records)
            self.assertEqual(db.records['orders/o1']['status'],'Completed')
            self.assertEqual(db.records['rfid_logs/t1']['pointsAdded'],20)
            self.assertEqual(json.loads(backup.read_text(encoding='utf-8'))['profiles'][0]['points'],20)
            with self.assertRaises(ValueError):
                rollout(db,auth,backup,True)
