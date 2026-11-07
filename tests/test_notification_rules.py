import uuid
import test_push_rules as push
from test_push_rules import token


class NotificationRulesTests(push.PushRulesTests):
    def test_recipient_reads_and_owner_only_state(self):
        key = uuid.uuid4().hex
        for suffix, fields in [
            ('uid', {'userId': 'customer1'}), ('email', {'userId': 'customer1@example.com'}),
            ('legacy', {'userId': 'CUST1', 'recipientEmail': 'customer1@example.com'}),
            ('admin', {'userId': 'ADMIN'}), ('all', {'userId': 'ALL'}),
        ]:
            path = '/notifications/' + key + suffix
            self.assertEqual(self.call('PATCH', path, {'fields': {k: {'stringValue': v} for k, v in fields.items()}}, 'owner'), 200)
            self.assertEqual(self.call('GET', path, bearer=token('customer1')), 403 if suffix == 'admin' else 200)
        path = f'{self.name}/notification_states/customer1/items/{key}'
        data = {'writes': [{'update': {'name': path, 'fields': {'read': {'booleanValue': True}, 'dismissed': {'booleanValue': True}}},
                            'updateTransforms': [{'fieldPath': 'updatedAt', 'setToServerValue': 'REQUEST_TIME'}]}]}
        self.assertEqual(self.call('POST', ':commit', data, token('customer1')), 200)
        self.assertEqual(self.call('POST', ':commit', data, token('other')), 403)
        self.assertEqual(self.call('GET', '/notification_states/customer1/items/' + key, bearer=token('other')), 403)
        self.assertEqual(self.call('PATCH', '/notifications/' + key + 'uid', {'fields': {'status': {'stringValue': 'Read'}}}, token('customer1')), 403)

    def test_customer_queries_are_authorized(self):
        for field, op, value in [
            ('userId', 'IN', {'arrayValue': {'values': [{'stringValue': v} for v in ['customer1', 'customer1@example.com', 'ALL']]}}),
            ('recipientEmail', 'EQUAL', {'stringValue': 'customer1@example.com'}),
        ]:
            query = {'structuredQuery': {'from': [{'collectionId': 'notifications'}], 'where': {'fieldFilter': {
                'field': {'fieldPath': field}, 'op': op, 'value': value}}}}
            self.assertEqual(self.call('POST', ':runQuery', query, token('customer1')), 200)
        query = {'structuredQuery': {'from': [{'collectionId': 'notifications'}]}}
        self.assertEqual(self.call('POST', ':runQuery', query, token('customer1')), 403)

    def test_new_order_notification_requires_same_owned_order_commit(self):
        key = uuid.uuid4().hex
        data = {'id': 'order_created_' + key, 'userId': 'ADMIN', 'title': 'New order', 'message': 'Submitted',
                'type': 'new_order', 'link': '/admin', 'status': 'Unread', 'orderId': key}
        write = {'update': {'name': self.name + '/notifications/order_created_' + key,
                           'fields': {k: {'stringValue': v} for k, v in data.items()}},
                 'updateTransforms': [{'fieldPath': 'createdAt', 'setToServerValue': 'REQUEST_TIME'}]}
        self.assertEqual(self.call('POST', ':commit', {'writes': [write]}, token('customer1')), 403)
        order = {'update': {'name': self.name + '/artifacts/printcraft-pro/public/data/orders/' + key,
                            'fields': {'customerId': {'stringValue': 'customer1'}, 'status': {'stringValue': 'Order Submitted'}}}}
        self.assertEqual(self.call('POST', ':commit', {'writes': [order, write]}, token('customer1')), 200)
        self.assertEqual(self.call('POST', ':commit', {'writes': [write]}, token('customer1')), 403)

    def test_customer_chat_notification_requires_same_message_commit(self):
        key = uuid.uuid4().hex
        data = {'id': 'chat_' + key, 'userId': 'ADMIN', 'title': 'New message', 'message': 'Hello',
                'type': 'chat', 'link': '/admin?tab=liveChat&convId=' + key, 'status': 'Unread',
                'conversationId': key, 'messageId': key}
        write = {'update': {'name': self.name + '/notifications/chat_' + key,
                           'fields': {k: {'stringValue': v} for k, v in data.items()}},
                 'updateTransforms': [{'fieldPath': 'createdAt', 'setToServerValue': 'REQUEST_TIME'}]}
        conversation = {'update': {'name': self.name + '/conversations/' + key,
                                  'fields': {'customerEmail': {'stringValue': 'customer1@example.com'}}}}
        message = {'update': {'name': self.name + '/conversations/' + key + '/messages/' + key,
                             'fields': {'senderRole': {'stringValue': 'customer'}, 'senderId': {'stringValue': 'customer1@example.com'}}}}
        self.assertEqual(self.call('POST', ':commit', {'writes': [write]}, token('customer1')), 403)
        self.assertEqual(self.call('POST', ':commit', {'writes': [conversation, message, write]}, token('customer1')), 200)

