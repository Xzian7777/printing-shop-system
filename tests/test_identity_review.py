import copy
import types
import unittest

from backend_test_support import load_backend
from test_admin_sync import Database, transactional


class IdentityReviewTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db.records = {
            'users/u1': {'email': 'USER@example.com', 'role': 'customer', 'rfidUid': 'card'},
            'customers/ambiguous': {'email': 'DUPLICATE@example.com', 'uid': 'other', 'rfidUid': 'legacy'},
        }
        self.backend = load_backend({'db': self.db,
            'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time')})
        self.report = {'version': 1, 'identities': [], 'records': [
            {'path': path, 'data': copy.deepcopy(data), 'digest': self.backend.record_digest(data), 'approved': False}
            for path, data in self.db.records.items()]}

    def test_unreviewed_metadata_is_preserved_and_production_readiness_stays_pending(self):
        original = copy.deepcopy(self.db.records)
        self.backend.normalize_identity_records(self.db, self.report)
        for path, data in original.items():
            self.assertEqual(self.db.records[path], data)
        self.assertEqual(self.db.records['settings/security_config']['normalizedIdentityVersion'], 0)
        self.assertEqual(self.db.records['settings/security_config']['normalizationPending'], 2)

    def test_individual_metadata_review_preserves_identity_rfid_and_role_on_retry(self):
        for row in self.report['records']:
            row['approved'] = True
        self.backend.normalize_identity_records(self.db, self.report)
        self.backend.normalize_identity_records(self.db, self.report)
        self.assertEqual(self.db.records['users/u1']['emailNormalized'], 'user@example.com')
        self.assertEqual(self.db.records['customers/ambiguous']['uid'], 'other')
        self.assertEqual(self.db.records['customers/ambiguous']['rfidUid'], 'legacy')
        self.assertEqual(self.db.records['settings/security_config']['normalizedIdentityVersion'], 1)

    def test_changed_profile_requires_new_review(self):
        self.report['records'][0]['approved'] = True
        self.db.records['users/u1']['rfidUid'] = 'changed'
        with self.assertRaises(ValueError):
            self.backend.normalize_identity_records(self.db, self.report)
        self.assertNotIn('emailNormalized', self.db.records['users/u1'])
