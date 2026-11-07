import os
import unittest
import uuid
import test_push_rules as access


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('localhost:', '127.0.0.1:')), 'Local Firestore Emulator required')
class PosAccessTests(unittest.TestCase):
    setUp = access.PushRulesTests.setUp
    call = access.PushRulesTests.call

    def create(self, uid='admin1', cashier='admin1', mirror=True):
        self.sale_id = uuid.uuid4().hex
        order_id = 'POS-123456'
        fields = {'id': {'stringValue': order_id}, 'submissionId': {'stringValue': self.sale_id},
                  'cashier': {'stringValue': cashier}, 'source': {'stringValue': 'POS'},
                  'status': {'stringValue': 'Completed'}, 'buyerName': {'stringValue': 'Maria Santos'},
                  'total': {'doubleValue': 19}, 'items': {'arrayValue': {'values': [{'mapValue': {'fields': {'name': {'stringValue': 'Notebook'}}}}]}}}
        writes = [{'update': {'name': f'{self.name}/pos_sales/{self.sale_id}', 'fields': fields},
                   'updateTransforms': [{'fieldPath': 'recordedAt', 'setToServerValue': 'REQUEST_TIME'}],
                   'currentDocument': {'exists': False}}]
        if mirror: writes.append({'update': {'name': f'{self.name}/artifacts/printcraft-pro/public/data/orders/{order_id}', 'fields': fields}})
        return self.call('POST', ':commit', {'writes': writes}, access.token(uid))

    def test_admin_receipts_are_readable_and_immutable(self):
        self.assertEqual(self.create(), 403)
        self.assertEqual(self.call('PATCH', '/pos_sales/' + self.sale_id, {'fields': {'cashier': {'stringValue': 'admin1'}}}, 'owner'), 200)
        path = '/pos_sales/' + self.sale_id
        self.assertEqual(self.call('GET', path, bearer=access.token('admin1')), 200)
        self.assertEqual(self.call('GET', path, bearer=access.token('customer1')), 403)
        self.assertEqual(self.call('GET', path), 403)
        self.assertEqual(self.call('PATCH', path, {'fields': {'total': {'doubleValue': 1}}}, access.token('admin1')), 403)
        self.assertEqual(self.call('DELETE', path, bearer=access.token('admin1')), 403)

    def test_customer_spoofed_cashier_and_missing_mirror_are_denied(self):
        self.assertEqual(self.create(uid='customer1'), 403)
        self.assertEqual(self.create(cashier='customer1'), 403)
        self.assertEqual(self.create(mirror=False), 403)


if __name__ == '__main__': unittest.main()
