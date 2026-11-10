"""Offline regression tests. Never initialize Firebase or read credentials."""
import ast
import copy
import datetime
import types
import unittest
import uuid
from pathlib import Path
from backend_test_support import stock_plan, valid_transition


class Snapshot:
    def __init__(self, ref):
        self.reference = ref
        self.id = ref.id
        self.exists = ref.path in ref.db.records
        self.value = copy.deepcopy(ref.db.records.get(ref.path))

    def to_dict(self): return self.value


class Ref:
    def __init__(self, db, path):
        self.db, self.path = db, path
        self.id = path.split('/')[-1]
        self.parent = types.SimpleNamespace(id=path.split('/')[-2])

    def collection(self, name): return Collection(self.db, self.path + '/' + name)
    def get(self, transaction=None): return Snapshot(self)
    def set(self, value, merge=False):
        self.db.records[self.path] = {**(self.db.records.get(self.path, {}) if merge else {}), **copy.deepcopy(value)}
        self.db.records[self.path] = {k: v for k, v in self.db.records[self.path].items() if v != '__delete_field__'}


class Collection:
    def __init__(self, db, path): self.db, self.path = db, path
    def document(self, key=None): return Ref(self.db, self.path + '/' + (key or uuid.uuid4().hex))
    def stream(self, transaction=None):
        return [Snapshot(Ref(self.db, p)) for p in self.db.records if p.rsplit('/', 1)[0] == self.path]
    def where(self, field, op, value):
        return types.SimpleNamespace(stream=lambda: [s for s in self.stream() if s.to_dict().get(field) == value])


class Transaction:
    def __init__(self, db): self.db, self.writes = db, []
    def set(self, ref, data, merge=False): self.writes.append(('set', ref, data, merge))
    def update(self, ref, data): self.set(ref, data, True)
    def delete(self, ref): self.writes.append(('delete', ref, None, False))
    def commit(self):
        for action, ref, data, merge in self.writes:
            if action == 'delete': self.db.records.pop(ref.path, None)
            else: ref.set(data, merge)


class Database:
    def __init__(self): self.records = {}
    def collection(self, name): return Collection(self, name)
    def document(self, path): return Ref(self, path)
    def transaction(self): return Transaction(self)
    def batch(self): return Transaction(self)


def transactional(fn):
    def run(tx):
        result = fn(tx)
        tx.commit()
        return result
    return run


class AdminSyncTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.env = {'db': self.db, 'datetime': datetime, 'uuid': uuid,
                    'stock_plan': stock_plan, 'valid_transition': valid_transition,
                    'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time', DELETE_FIELD='__delete_field__')}
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        # Load only function definitions, bypassing SDK initialization entirely.
        functions = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and not any(ast.unparse(d).startswith('app.') for d in n.decorator_list)], type_ignores=[])
        exec(compile(functions, 'app.py', 'exec'), self.env)
        self.legacy = 'artifacts/printcraft-pro/public/data/store_products/p1'
        self.db.records['products/p1'] = {'id': 'p1', 'name': 'Paper', 'stock': 10, 'price': 3}
        self.db.records[self.legacy] = copy.deepcopy(self.db.records['products/p1'])

    def test_archive_restore_both_product_collections(self):
        self.assertTrue(self.env['archive_item']('store_product', 'p1'))
        self.assertTrue(self.db.records['products/p1']['archived'])
        self.assertTrue(self.db.records[self.legacy]['archived'])
        self.assertEqual(self.env['get_store_products'](), [])
        self.assertTrue(self.env['restore_archived_item']('store_product_p1')[0])
        self.assertFalse(self.db.records['products/p1']['archived'])
        self.assertNotIn('archivedAt', self.db.records[self.legacy])
        self.assertNotIn('archives/store_product_p1', self.db.records)

    def test_web_permanent_delete_removes_mobile_tombstones(self):
        self.env['archive_item']('store_product', 'p1')
        self.assertTrue(self.env['permanently_delete_archived_item']('store_product_p1'))
        self.assertNotIn('products/p1', self.db.records)
        self.assertNotIn(self.legacy, self.db.records)

    def test_stale_save_cannot_resurrect_archived_product(self):
        old = copy.deepcopy(self.db.records['products/p1'])
        self.env['archive_item']('store_product', 'p1')
        self.assertFalse(self.env['save_store_products']([old]))
        self.assertTrue(self.db.records['products/p1']['archived'])

    def test_failed_archive_does_not_write_partial_records(self):
        before = copy.deepcopy(self.db.records)
        self.assertFalse(self.env['archive_item']('store_product', 'missing'))
        self.assertEqual(before, self.db.records)

    def test_restore_conflict_keeps_archive_and_active_product(self):
        self.env['archive_item']('store_product', 'p1')
        self.db.records['products/p1']['archived'] = False
        before = copy.deepcopy(self.db.records)
        self.assertFalse(self.env['restore_archived_item']('store_product_p1')[0])
        self.assertEqual(before, self.db.records)

    def test_user_archive_resolves_both_existing_profile_ids(self):
        self.db.records['customers/CUST-1'] = {'email': 'a@gmail.com', 'fullName': 'Alice', 'uid': 'firebase-uid'}
        self.db.records['users/firebase-uid'] = {'email': 'a@gmail.com', 'full_name': 'Alice'}
        self.assertTrue(self.env['archive_item']('user', 'CUST-1'))
        self.assertTrue(self.db.records['users/firebase-uid']['archived'])
        self.assertTrue(self.env['restore_archived_item']('user_CUST-1')[0])
        self.assertFalse(self.db.records['users/firebase-uid']['isArchived'])

    def test_legacy_user_restore_preserves_existing_active_profile_fields(self):
        self.db.records['users/u1'] = {'email': 'a@gmail.com', 'full_name': 'Current name', 'uid': 'u1'}
        self.db.records['archives/user_c1'] = {'originalId': 'c1', 'itemType': 'user', 'data': {'email': 'a@gmail.com', 'fullName': 'Archived name'}}
        self.assertTrue(self.env['restore_archived_item']('user_c1')[0])
        self.assertEqual(self.db.records['users/u1']['full_name'], 'Current name')
        self.assertEqual(self.db.records['users/u1']['uid'], 'u1')
        self.assertFalse(self.db.records['customers/c1']['archived'])

    def test_restock_uses_current_stock_and_updates_both_copies(self):
        self.db.records['products/p1']['stock'] = 7
        self.assertTrue(self.env['save_store_products']([{'id': 'p1', 'stockDelta': 5}]))
        self.assertEqual(self.db.records['products/p1']['stock'], 12)
        self.assertEqual(self.db.records[self.legacy]['stock'], 12)

    def test_structured_and_legacy_stock_rules(self):
        products = {'p1': {'name': 'Paper', 'stock': 10}}
        order = {'items': [{'id': 'p1', 'qty': 2}, {'id': 'p1', 'qty': 3}]}
        self.assertEqual(stock_plan(order, products), {'p1': 5})
        self.assertEqual(stock_plan({'service': 'In-Store: Paper (3x)'}, products), {'p1': 7})
        with self.assertRaises(ValueError): stock_plan({'items': [{'id': 'p1', 'qty': 11}]}, products)
        self.assertTrue(valid_transition('Order Submitted', 'Approved'))
        self.assertFalse(valid_transition('Order Submitted', 'Printing'))

    def test_stock_deducted_once_across_approval_and_completion(self):
        self.env['create_notification'] = lambda *_: True
        self.env['send_fcm_push_notification'] = lambda **_: True
        path = 'artifacts/printcraft-pro/public/data/orders/o1'
        self.db.records[path] = {'status': 'Order Submitted', 'items': [{'id': 'p1', 'qty': 2}]}
        for status in ['Approved', 'Printing', 'Ready for Pickup', 'Completed']:
            self.assertTrue(self.env['update_order_status']('o1', status))
        self.assertEqual(self.db.records['products/p1']['stock'], 8)
        self.assertEqual(self.db.records[self.legacy]['stock'], 8)
        self.assertFalse(self.env['update_order_status']('o1', 'Approved'))
        self.assertEqual(self.db.records['products/p1']['stock'], 8)

    def test_rejection_restores_stock_once(self):
        self.env['create_notification'] = lambda *_: True
        self.env['send_fcm_push_notification'] = lambda **_: True
        path = 'artifacts/printcraft-pro/public/data/orders/o1'
        self.db.records[path] = {'status': 'Order Submitted', 'items': [{'id': 'p1', 'qty': 2}]}
        self.assertTrue(self.env['update_order_status']('o1', 'Approved'))
        self.assertTrue(self.env['update_order_status']('o1', 'Rejected: Bad file'))
        self.assertEqual(self.db.records['products/p1']['stock'], 10)

    def test_restore_keeps_stock_returned_after_archival(self):
        self.env['create_notification'] = lambda *_: True
        self.env['send_fcm_push_notification'] = lambda **_: True
        path = 'artifacts/printcraft-pro/public/data/orders/o1'
        self.db.records[path] = {'status': 'Order Submitted', 'items': [{'id': 'p1', 'qty': 2}]}
        self.assertTrue(self.env['update_order_status']('o1', 'Approved'))
        self.assertTrue(self.env['archive_item']('store_product', 'p1'))
        self.assertTrue(self.env['update_order_status']('o1', 'Rejected: Bad file'))
        self.assertTrue(self.env['restore_archived_item']('store_product_p1')[0])
        self.assertEqual(self.db.records['products/p1']['stock'], 10)
        self.assertFalse(self.db.records[path]['stockDeducted'])
        self.assertFalse(self.env['update_order_status']('o1', 'Rejected: Bad file'))
        self.assertEqual(self.db.records['products/p1']['stock'], 10)

    def test_archived_catalog_is_not_reseeded(self):
        self.db.records['services/custom'] = {'name': 'Custom', 'sortOrder': 1}
        self.assertTrue(self.env['archive_item']('service', 'custom'))
        self.assertEqual(self.env['get_services'](), [])
        self.assertTrue(self.env['restore_archived_item']('service_custom')[0])
        self.assertEqual(len(self.env['get_services']()), 1)

    def test_rfid_identifies_card_without_awarding_purchase_points(self):
        self.db.records['customers/c1'] = {'email': 'a@gmail.com', 'rfidUid': 'card1', 'points': 20}
        self.db.records['users/u1'] = {'email': 'a@gmail.com', 'rfidUid': 'card1', 'points': 20}
        self.assertTrue(self.env['log_rfid_tap']('card1'))
        self.assertEqual(self.db.records['customers/c1']['points'], 20)
        self.assertEqual(self.db.records['users/u1']['points'], 20)
        self.assertEqual(len(self.db.collection('rfid_logs').stream()), 1)
        self.assertFalse(self.env['log_rfid_tap']('missing'))

    def test_rfid_preserves_legacy_balance_when_web_profile_has_no_balance(self):
        self.db.records['customers/c1'] = {'email': 'a@gmail.com', 'rfidUid': 'card1'}
        self.db.records['users/u1'] = {'email': 'a@gmail.com', 'points': 80}
        self.assertTrue(self.env['log_rfid_tap']('card1'))
        self.assertNotIn('points', self.db.records['customers/c1'])
        self.assertEqual(self.db.records['users/u1']['points'], 80)


class LoyaltyRestoreTests(unittest.TestCase):
    setUp = AdminSyncTests.setUp
    def test_restore_preserves_current_mirror_fields_and_zeros_recreated_mirror(self):
        self.db.records['users/u1'] = {'email': 'a@gmail.com', 'points': 0, 'loyaltyPoints': 0, 'loyaltyUid': 'u1', 'archived': True}
        self.db.records['archives/user_u1'] = {'originalId': 'u1', 'itemType': 'user', 'data': {
            'email': 'a@gmail.com', 'uid': 'u1', 'customerId': 'c1', 'points': 170, 'loyaltyPoints': 170}}
        self.assertTrue(self.env['restore_archived_item']('user_u1')[0])
        self.assertEqual(self.db.records['users/u1']['points'], 0)
        self.assertEqual(self.db.records['users/u1']['loyaltyUid'], 'u1')
        self.assertEqual(self.db.records['customers/c1']['points'], 0)
        self.assertFalse(self.db.records['users/u1']['archived'])


if __name__ == '__main__': unittest.main()
