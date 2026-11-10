"""Offline push tests with fake Firestore. No credentials or network calls."""
import ast
import datetime
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

from test_admin_sync import Database


def transactional(function):
    def commit(transaction, *args):
        result = function(transaction, *args)
        transaction.commit()
        return result
    return commit


class PushDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and not any(ast.unparse(d).startswith('app.') for d in n.decorator_list)]
        for function in functions:
            function.decorator_list = []
        self.env = {'datetime': datetime, 'uuid': uuid, 'log': Mock(),
                    'auth': types.SimpleNamespace(get_user=Mock(), UserNotFoundError=LookupError)}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'app.py', 'exec'), self.env)
        for name in ['claim_job', 'finish_job']:
            self.env[name] = transactional(self.env[name])
        self.job = self.db.collection('push_jobs').document('event1')
        self.now = self.env['utc_now']()
        self.job.set({'type': 'order_status', 'orderId': 'o1', 'status': 'Ready for Pickup',
                      'state': 'pending', 'createdAt': self.now})
        self.db.document('artifacts/printcraft-pro/public/data/orders/o1').set({
            'customerEmail': 'Customer@example.com', 'service': 'Document Printing',
        })

    def deliver(self, sender):
        self.env['deliver_job'](self.db, self.job, sender)

    def test_ready_notification_targets_saved_customer_and_is_not_repeated(self):
        send = Mock(return_value={'success': True, 'delivered_tokens': ['phone1']})
        self.deliver(send)
        self.deliver(send)
        send.assert_called_once()
        payload = send.call_args.kwargs
        self.assertEqual(payload['recipient_email'], 'customer@example.com')
        self.assertEqual(payload['data_payload']['type'], 'order_status')
        self.assertIn('Ready for Pickup', payload['title'])
        self.assertEqual(self.job.get().to_dict()['state'], 'sent')

    def test_partial_delivery_retries_only_remaining_devices(self):
        send = Mock(return_value={'success': False, 'delivered_tokens': ['phone1']})
        self.deliver(send)
        self.assertEqual(self.job.get().to_dict()['state'], 'retry')
        self.deliver(send)
        send.assert_called_once()  # Backoff has not elapsed.
        self.job.set({'availableAt': self.now}, merge=True)
        send.return_value = {'success': True, 'delivered_tokens': ['phone2']}
        self.deliver(send)
        self.assertEqual(send.call_args.kwargs['data_payload']['_exclude_tokens'], ['phone1'])
        self.assertCountEqual(self.job.get().to_dict()['deliveredTokens'], ['phone1', 'phone2'])

    def test_active_lease_blocks_another_worker_and_expired_lease_can_recover(self):
        self.job.set({'state': 'sending', 'leaseUntil': self.now + datetime.timedelta(minutes=1)}, merge=True)
        send = Mock(return_value={'success': True})
        self.deliver(send)
        send.assert_not_called()
        self.job.set({'leaseUntil': self.now - datetime.timedelta(seconds=1)}, merge=True)
        self.deliver(send)
        send.assert_called_once()

    def test_delivery_error_leaves_retryable_job(self):
        self.deliver(Mock(side_effect=RuntimeError('network offline')))
        self.assertEqual(self.job.get().to_dict()['state'], 'retry')

    def test_missing_recipient_never_broadcasts_and_old_jobs_expire(self):
        self.db.document('artifacts/printcraft-pro/public/data/orders/o1').set({})
        send = Mock()
        self.deliver(send)
        send.assert_not_called()
        self.assertEqual(self.job.get().to_dict()['state'], 'unresolved')
        self.job.set({'state': 'pending', 'createdAt': self.now - datetime.timedelta(days=2)})
        self.deliver(send)
        self.assertEqual(self.job.get().to_dict()['state'], 'expired')

    def test_chat_reads_actual_message_and_conversation_recipient(self):
        conv = self.db.collection('conversations').document('c1')
        conv.set({'customerEmail': 'owner@example.com'})
        conv.collection('messages').document('m1').set({'senderRole': 'admin', 'text': 'Ready na po.'})
        self.job.set({'type': 'chat', 'conversationId': 'c1', 'messageId': 'm1',
                      'state': 'pending', 'createdAt': self.now, 'recipient_email': 'wrong@example.com'})
        send = Mock(return_value={'success': True})
        self.deliver(send)
        self.assertEqual(send.call_args.kwargs['recipient_email'], 'owner@example.com')
        self.assertEqual(send.call_args.kwargs['body'], 'Ready na po.')

    def test_stale_worker_cannot_finish_another_workers_lease(self):
        self.job.set({'lease': 'new-worker'}, merge=True)
        self.env['finish_job'](self.db.transaction(), self.job, 'old-worker', {'state': 'sent'})
        self.assertEqual(self.job.get().to_dict()['state'], 'pending')


class PushPayloadTests(unittest.TestCase):
    def test_android_payload_and_partial_failure_bookkeeping(self):
        import json
        from firebase_admin import messaging

        class Query:
            def __init__(self, docs):
                self.docs = docs
            def where(self, *args):
                return self
            def stream(self):
                return self.docs

        db = Database()
        for token in ['good', 'failed', 'already-sent']:
            db.collection('fcm_tokens').document(token).set({'token': token})
        docs = db.collection('fcm_tokens').stream()
        fake_db = types.SimpleNamespace(collection=lambda _: Query(docs))
        sent = []
        def send(message):
            sent.append(message)
            if message.token == 'failed':
                raise RuntimeError('temporary outage')
            return 'message-id'
        facade = types.SimpleNamespace(**{
            name: getattr(messaging, name) for name in [
                'Message', 'Notification', 'WebpushConfig', 'WebpushNotification',
                'WebpushFCMOptions', 'AndroidConfig', 'AndroidNotification', 'UnregisteredError',
            ]})
        facade.send = send
        env = {'db': fake_db, 'datetime': datetime, 'messaging': facade}
        tree = ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'send_fcm_push_notification')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), env)
        result = env['send_fcm_push_notification'](
            'customer', 'owner@example.com', 'Order Ready for Pickup', 'Ready na po.',
            '/customer?tab=myOrders', {'type': 'order_status', 'event_id': 'event1', '_exclude_tokens': ['already-sent']},
        )
        self.assertFalse(result['success'])
        self.assertEqual(result['delivered_tokens'], ['good'])
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(len(sent), 2)
        # The real Admin SDK validates and encodes the message locally.
        encoded = json.loads(str(sent[0]))
        self.assertEqual(encoded['android']['priority'], 'high')
        self.assertEqual(encoded['android']['notification']['channel_id'], 'kcz_chat_channel')
        self.assertEqual(encoded['android']['notification']['tag'], 'event1')
        self.assertEqual(encoded['data']['recipient_email'], 'owner@example.com')
        self.assertNotIn('_exclude_tokens', encoded['data'])
        self.assertNotIn('fcm_options', encoded.get('webpush', {}))


if __name__ == '__main__':
    unittest.main()
