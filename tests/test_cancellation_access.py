import os
import unittest
import uuid
import test_push_rules as access


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('localhost:', '127.0.0.1:')), 'Local Firestore Emulator required')
class CancellationAccessTests(unittest.TestCase):
    setUp = access.PushRulesTests.setUp
    call = access.PushRulesTests.call

    def seed(self, root=False, **changes):
        collection = 'orders' if root else 'artifacts/printcraft-pro/public/data/orders'
        self.path = '/' + collection + '/' + uuid.uuid4().hex
        fields = {'customerId': {'stringValue': 'customer1'}, 'email': {'stringValue': 'customer1@example.com'},
                  'status': {'stringValue': 'Order Submitted'}, 'total': {'integerValue': '100'}}
        fields.update(changes)
        self.assertEqual(self.call('PATCH', self.path, {'fields': fields}, 'owner'), 200)

    def cancel(self, uid='customer1', reason='Changed plans', extra=None):
        fields = {'status': {'stringValue': 'Cancelled'}, 'cancelledBy': {'stringValue': uid},
                  'cancelledByEmail': {'stringValue': uid + '@example.com'},
                  'cancellationReason': {'stringValue': reason}, **(extra or {})}
        return self.call('POST', ':commit', {'writes': [{
            'update': {'name': self.name + self.path, 'fields': fields},
            'updateMask': {'fieldPaths': list(fields)},
            'updateTransforms': [{'fieldPath': 'cancelledAt', 'setToServerValue': 'REQUEST_TIME'}],
        }]}, access.token(uid))

    def test_pending_owner_can_cancel_both_shared_and_root_orders(self):
        for root in [False, True]:
            self.seed(root=root)
            self.assertEqual(self.cancel(), 200)
            self.assertEqual(self.call('GET', self.path, bearer=access.token('customer1')), 200)

    def test_approved_flags_and_non_owner_block_cancellation(self):
        self.seed(); self.assertEqual(self.cancel(uid='other'), 403)
        for status in ['Approved', 'Printing', 'Preparing', 'Ready for Pickup', 'Completed', 'Rejected']:
            self.seed(status={'stringValue': status}); self.assertEqual(self.cancel(), 403)
        self.seed(stockDeducted={'booleanValue': True}); self.assertEqual(self.cancel(), 403)
        self.seed(approvedAt={'timestampValue': '2026-11-07T00:00:00Z'}); self.assertEqual(self.cancel(), 403)

    def test_direct_bypasses_and_hard_deletion_are_denied(self):
        self.seed()
        for field in ['total', 'items', 'customerId', 'stockDeducted', 'approvedAt']:
            value = {'integerValue': '1'} if field == 'total' else {'stringValue': 'tampered'}
            self.assertEqual(self.cancel(extra={field: value}), 403)
        self.assertEqual(self.cancel(reason='x' * 501), 403)
        self.assertEqual(self.call('DELETE', self.path, bearer=access.token('customer1')), 403)
        self.assertEqual(self.call('PATCH', self.path + '?updateMask.fieldPaths=status', {'fields': {'status': {'stringValue': 'Approved'}}}, access.token('customer1')), 403)
        self.assertEqual(self.cancel(), 200)
        self.assertEqual(self.call('PATCH', self.path + '?updateMask.fieldPaths=status', {'fields': {'status': {'stringValue': 'Approved'}}}, access.token('admin1')), 403)

    def test_legacy_email_ownership_and_sibling_artifact_access_remain_supported(self):
        self.seed(customerId={'stringValue': 'legacy-customer-id'})
        self.assertEqual(self.cancel(), 200)
        self.assertEqual(self.call('PATCH', '/artifacts/printcraft-pro/public/data/notification_tokens/legacy',
            {'fields': {'token': {'stringValue': 'test'}}}, access.token('customer1')), 200)


if __name__ == '__main__': unittest.main()
