import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock

import requests
import test_deployment_security as security
from tools.rfid_bridge import PersistentQueue, deliver_next


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class RfidConcurrencyTests(unittest.TestCase):
    def test_concurrent_duplicate_taps_credit_exactly_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        from backend_test_support import load_backend
        import threading
        import uuid
        database = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        self.addCleanup(database.close)
        owner = uuid.uuid4().hex
        reader = uuid.uuid4().hex[:12].upper()
        card = uuid.uuid4().hex[:8].upper()
        database.document('users/' + owner).set({'role': 'customer', 'email': owner + '@example.com'})
        database.document('rfid_readers/' + reader).set({'active': True, 'keyHash': 'synthetic'})
        database.document('rfid_cards/' + card).set({'uid': owner, 'active': True, 'version': 'one'})
        database.document('settings/loyalty_config').set({'enabled': True, 'epoch': 'test', 'pointValueCents': 100})
        database.document('loyalty_accounts/' + owner).set({'balance': 10, 'reserved': 2})
        backend = load_backend({'db': database})
        backend.customer_identity = lambda **kwargs: {'uid': owner, 'role': 'customer'}
        barrier = threading.Barrier(2)
        payload = {'eventId': reader + '-1', 'cardUid': card, 'bindingVersion': 'one'}
        def tap(_):
            barrier.wait()
            return backend.process_rfid_event(database, reader, 'synthetic', payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(tap, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['pointsAdded'], 1)
        account = database.document('loyalty_accounts/' + owner).get().to_dict()
        self.assertEqual((account['balance'], account['reserved']), (11, 2))
        self.assertEqual(len(list(database.collection('loyalty_accounts').document(owner).collection('events').stream())), 1)


class RfidReaderApiTests(unittest.TestCase):
    def setUp(self):
        security.DeploymentSecurityTests.setUp(self)
        self.backend.register_rfid_routes(self.app)
        self.admin = {'Authorization': 'Bearer admin'}
        self.reader = 'AABBCCDDEEFF'
        response = self.client.post('/api/admin/rfid/readers', headers=self.admin,
                                    json={'readerId': self.reader, 'name': 'Test counter'})
        self.assertEqual(response.status_code, 200, response.json)
        self.key = response.json['readerKey']
        self.device = {'X-RFID-Key': self.key}

    def state(self, revision=''):
        return self.client.post('/api/iot/rfid/state', headers=self.device,
            json={'readerId': self.reader, 'bindingsVersion': revision})

    def event(self, sequence, **extra):
        return {'readerId': self.reader, 'eventId': f'{self.reader}-{sequence}',
                'cardUid': 'A1B2C3D4', 'bindingVersion': '', 'captureId': '', **extra}

    def tap(self, payload):
        return self.client.post('/api/iot/rfid/taps', headers=self.device, json=payload)

    def enroll(self):
        capture = self.client.post('/api/admin/rfid/capture', headers=self.admin, json={'readerId': self.reader})
        self.assertEqual(capture.status_code, 200, capture.json)
        capture_id = capture.json['captureId']
        captured = self.tap(self.event(1, captureId=capture_id))
        self.assertEqual(captured.json['outcome'], 'captured')
        self.assertEqual(captured.json['pointsAdded'], 0)
        response = self.client.post('/api/admin/rfid/cards', headers=self.admin,
            json={'uid': 'customer', 'readerId': self.reader, 'captureId': capture_id})
        self.assertEqual(response.status_code, 200, response.json)
        return self.db.records['rfid_cards/A1B2C3D4']['version'], capture_id

    def test_registration_is_admin_only_and_keys_are_redacted(self):
        self.assertEqual(self.client.post('/api/admin/rfid/readers', headers=self.headers,
            json={'readerId': '112233445566'}).status_code, 403)
        response = self.client.get('/api/admin/rfid/readers', headers=self.admin)
        self.assertNotIn('keyHash', response.json['readers'][0])
        self.assertNotIn(self.key, json.dumps(response.json))
        self.assertEqual(self.client.post('/api/iot/rfid/state', json={'readerId': self.reader}).status_code, 401)
        with self.client.session_transaction() as state:
            state.clear()
        self.assertEqual(self.client.get('/api/auth/me', headers=self.device).status_code, 401)

    def test_enrollment_then_new_taps_add_one_point_and_retry_once(self):
        version, capture_id = self.enroll()
        self.db.records['loyalty_accounts/customer'] = {'balance': 10, 'reserved': 4}
        payload = self.event(2, bindingVersion=version, pointsAdded=1000, customerUid='admin')
        first = self.tap(payload)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(first.json['pointsAdded'], 1)
        self.assertEqual(self.tap(payload).json, first.json)
        self.assertEqual(self.db.records['loyalty_accounts/customer']['balance'], 11)
        self.assertEqual(self.db.records['loyalty_accounts/customer']['reserved'], 4)
        self.assertEqual(self.tap(self.event(3, bindingVersion=version)).json['pointsAdded'], 1)
        self.assertEqual(self.db.records['loyalty_accounts/customer']['balance'], 12)
        events = [value for path, value in self.db.records.items() if path.startswith('loyalty_accounts/customer/events/rfid_')]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event['source'] == 'rfid' and event['type'] == 'earn' for event in events))
        self.assertEqual(self.db.records['users/customer']['physicalRfidUid'], 'A1B2C3D4')
        duplicate = self.client.post('/api/admin/rfid/cards', headers=self.admin,
            json={'uid': 'customer', 'readerId': self.reader, 'captureId': capture_id})
        self.assertEqual(duplicate.status_code, 200)

    def test_unknown_stale_mapping_archived_account_and_reused_event_payload(self):
        self.assertEqual(self.tap(self.event(10)).json['outcome'], 'unassigned')
        version, _ = self.enroll()
        self.assertEqual(self.tap(self.event(2, bindingVersion='old')).json['outcome'], 'mapping_changed')
        self.db.records['users/customer']['archived'] = True
        self.assertEqual(self.tap(self.event(3, bindingVersion=version)).json['outcome'], 'account_unavailable')
        self.assertNotIn('loyalty_accounts/customer', self.db.records)
        self.assertEqual(self.tap(self.event(3, bindingVersion=version, cardUid='11223344')).status_code, 409)

    def test_key_rotation_rejects_the_old_bridge_key(self):
        response = self.client.post('/api/admin/rfid/readers', headers=self.admin,
            json={'readerId': self.reader, 'name': 'Rotated', 'rotateKey': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state().status_code, 401)
        self.device = {'X-RFID-Key': response.json['readerKey']}
        self.assertEqual(self.state().status_code, 200)


class BridgeQueueTests(unittest.TestCase):
    def test_offline_restart_retry_and_duplicate_frames_preserve_one_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.sqlite3'
            queue = PersistentQueue(path)
            queue.update_settings({'bindingsVersion': 'one', 'bindings': {'A1B2C3D4': 'original'}, 'captureId': ''})
            packet = {'readerId': 'AABBCCDDEEFF', 'eventId': 'AABBCCDDEEFF-1', 'cardUid': 'A1B2C3D4'}
            self.assertTrue(queue.enqueue(packet))
            original = copy.deepcopy(queue.next_event())
            config = {'server': 'http://127.0.0.1:5000', 'readerKey': 'synthetic'}
            offline = Mock()
            offline.post.side_effect = requests.ConnectionError('offline')
            with self.assertRaises(requests.ConnectionError):
                deliver_next(config, queue, offline)
            restarted = PersistentQueue(path)
            restarted.update_settings({'bindingsVersion': 'two', 'bindings': {'A1B2C3D4': 'reassigned'}, 'captureId': ''})
            self.assertFalse(restarted.enqueue(packet))
            self.assertEqual(restarted.next_event(), original)
            online = Mock()
            online.post.return_value = types.SimpleNamespace(status_code=200,
                json=lambda: {'success': True, 'outcome': 'credited', 'pointsAdded': 1})
            self.assertTrue(deliver_next(config, restarted, online))
            self.assertEqual(restarted.pending_count(), 0)
            self.assertFalse(restarted.enqueue(packet))
            self.assertEqual(online.post.call_args.kwargs['json'], original)

    def test_frame_validation_and_id_collision_do_not_replace_queued_data(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = PersistentQueue(Path(directory) / 'queue.sqlite3')
            packet = {'readerId': 'AABBCCDDEEFF', 'eventId': 'AABBCCDDEEFF-1', 'cardUid': 'A1B2C3D4'}
            queue.enqueue(packet)
            for changes in [{'cardUid': '11223344'}, {'eventId': 'invalid'}, {'cardUid': 'bad'}]:
                with self.assertRaises(ValueError):
                    queue.enqueue({**packet, **changes})
            self.assertEqual(queue.pending_count(), 1)
