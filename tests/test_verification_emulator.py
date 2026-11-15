import os
import re
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from backend_test_support import load_backend


@unittest.skipUnless(os.getenv('FIRESTORE_EMULATOR_HOST', '').startswith(('127.0.0.1:', 'localhost:')), 'Local Firestore Emulator required')
class VerificationWorkerTests(unittest.TestCase):
    def setUp(self):
        from google.cloud.firestore import Client
        from google.auth.credentials import AnonymousCredentials
        self.db = Client(project='demo-printshop-push', credentials=AnonymousCredentials())
        self.addCleanup(self.db.close)
        self.mail = Mock(return_value=True)
        self.workers = [load_backend({'db': self.db, 'send_smtp_email': self.mail}) for _ in range(2)]
        self.email = uuid.uuid4().hex + '@example.com'
        self.workers[0].issue_challenge('signup', self.email, 'synthetic-uid')
        self.code = re.search(r'<b>([0-9]{6})</b>', self.mail.call_args.args[2]).group(1)

    def test_concurrent_workers_claim_and_consume_exactly_once(self):
        barrier = threading.Barrier(2)
        def claim(worker):
            barrier.wait()
            try:
                return worker.verify_challenge('signup', self.email, self.code, claim=True, operation='same-request')
            except worker.PasswordResetError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, self.workers))
        successful = [result for result in results if result is not None]
        self.assertEqual(len(successful), 1)
        self.workers[1].finish_challenge('signup', self.email, successful[0], True)
        with self.assertRaises(self.workers[0].PasswordResetError):
            self.workers[0].verify_challenge('signup', self.email, self.code, claim=True, operation='same-request')
        saved = self.workers[0].challenge_ref('signup', self.email).get().to_dict()
        self.assertEqual(saved['state'], 'consumed')
        self.assertNotIn('otpHash', saved)

    def test_wrong_attempts_are_shared_between_workers(self):
        wrong = '000000' if self.code != '000000' else '111111'
        for attempt in range(5):
            worker = self.workers[attempt % 2]
            with self.assertRaises(worker.PasswordResetError):
                worker.verify_challenge('signup', self.email, wrong)
        with self.assertRaises(self.workers[1].PasswordResetError):
            self.workers[1].verify_challenge('signup', self.email, self.code)
        self.assertEqual(self.workers[0].challenge_ref('signup', self.email).get().to_dict()['attempts'], 5)
