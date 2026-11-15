import hashlib
import types
import unittest
from unittest.mock import Mock, patch
from backend_test_support import load_backend
from test_admin_sync import Database, transactional


class MissingObject(Exception):
    status = 404


class Bucket:
    def __init__(self, storage, name):
        self.storage, self.name = storage, name

    def download(self, path):
        key = (self.name, path)
        if key not in self.storage.objects:
            raise MissingObject()
        value = self.storage.objects[key]
        return value + b'corrupted' if self.storage.corrupt_private and self.name == 'private' else value

    def upload(self, path, contents, options):
        self.storage.objects[self.name, path] = contents

    def remove(self, paths):
        if self.storage.fail_cleanup:
            self.storage.fail_cleanup = False
            raise RuntimeError('temporary cleanup failure')
        for path in paths:
            self.storage.objects.pop((self.name, path), None)


class Storage:
    def __init__(self):
        self.objects = {('public', 'uploads/original.pdf'): b'%PDF-synthetic-private-document'}
        self.corrupt_private = False
        self.fail_cleanup = False

    def from_(self, name):
        return Bucket(self, name)


class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.storage = Storage()
        self.url = 'https://storage.example.com/storage/v1/object/public/public/uploads/original.pdf'
        self.order_path = 'artifacts/printcraft-pro/public/data/orders/PRNT-123456'
        self.db.records = {'users/u1': {'role': 'customer'}, self.order_path: {'customerId': 'u1', 'fileUrl': self.url}}
        self.sc = types.SimpleNamespace(SUPABASE_URL='https://storage.example.com', BUCKET_NAME='public', PRIVATE_BUCKET_NAME='private',
            ensure_private_bucket=lambda: None, _get_client=lambda: types.SimpleNamespace(storage=self.storage),
            _sanitize_filename=lambda name: name, get_signed_url=lambda path, **kwargs: 'https://storage.example.com/storage/v1/object/sign/private/' + path + '?token=synthetic')
        self.backend = load_backend({'db': self.db, 'sc': self.sc,
            'firestore': types.SimpleNamespace(transactional=transactional, SERVER_TIMESTAMP='server-time')})

    def report(self):
        result = self.backend.file_migration_report(self.db)
        result['objects'][0]['approved'] = True
        return result

    def test_verified_copy_updates_reference_and_completed_retry_is_idempotent(self):
        report = self.report()
        self.assertTrue(report['objects'][0]['eligible'])
        self.assertEqual(self.backend.apply_file_report(self.db, report), 1)
        self.assertNotIn(('public', 'uploads/original.pdf'), self.storage.objects)
        self.assertIn('/sign/private/', self.db.records[self.order_path]['fileUrl'])
        self.assertEqual(self.backend.apply_file_report(self.db, report), 1)

    def test_corrupt_copy_preserves_public_object_and_order_reference(self):
        self.storage.corrupt_private = True
        with self.assertRaises(ValueError):
            self.backend.apply_file_report(self.db, self.report())
        self.assertIn(('public', 'uploads/original.pdf'), self.storage.objects)
        self.assertEqual(self.db.records[self.order_path]['fileUrl'], self.url)

    def test_interrupted_cleanup_retries_without_duplicate_private_files(self):
        self.storage.fail_cleanup = True
        report = self.report()
        with self.assertRaises(RuntimeError):
            self.backend.apply_file_report(self.db, report)
        self.assertEqual(self.backend.apply_file_report(self.db, report), 1)
        self.assertEqual(len([key for key in self.storage.objects if key[0] == 'private']), 1)

    def test_public_catalog_asset_and_changed_owner_require_review(self):
        self.db.records['products/p1'] = {'imageUrl': self.url}
        report = self.report()
        self.assertFalse(report['objects'][0]['eligible'])
        with self.assertRaises(ValueError):
            self.backend.apply_file_report(self.db, report)
        del self.db.records['products/p1']
        report = self.report()
        self.db.records[self.order_path]['customerId'] = 'other'
        with self.assertRaises(ValueError):
            self.backend.apply_file_report(self.db, report)
        self.assertIn(('public', 'uploads/original.pdf'), self.storage.objects)

    def test_expired_signed_url_refresh_uses_immutable_owner_metadata(self):
        path = 'orders/u1/document.pdf'
        key = hashlib.sha256(('private/' + path).encode()).hexdigest()
        self.db.records['file_ownership/' + key] = {'uid': 'u1', 'purpose': 'document', 'path': path}
        url = self.sc.get_signed_url(path)
        self.assertEqual(self.backend.upload_metadata(url, {'uid': 'u1'}, 'document')['path'], path)
        with self.assertRaises(PermissionError):
            self.backend.upload_metadata(url, {'uid': 'other'}, 'document')


class SignedLinkBucketTests(unittest.TestCase):
    def test_signing_uses_private_default_and_explicit_public_bucket(self):
        import supabase_config as storage_config
        client = Mock()
        client.storage.from_.return_value.create_signed_url.return_value = {'signedURL': 'https://example.com/signed'}
        with patch.object(storage_config, '_get_client', return_value=client):
            self.assertEqual(storage_config.get_signed_url('document.pdf'), 'https://example.com/signed')
            client.storage.from_.assert_called_with(storage_config.PRIVATE_BUCKET_NAME)
            storage_config.get_signed_url('catalog.png', bucket_name=storage_config.BUCKET_NAME)
            client.storage.from_.assert_called_with(storage_config.BUCKET_NAME)
