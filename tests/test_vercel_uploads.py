import ast
import copy
import datetime
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import supabase_config as storage
import test_deployment_security as security


class DirectUploadTests(unittest.TestCase):
    def setUp(self):
        security.DeploymentSecurityTests.setUp(self)
        self.backend.sc = storage
        for node in ast.parse((Path(__file__).parents[1] / 'app.py').read_text(encoding='utf-8')).body:
            if isinstance(node, ast.FunctionDef) and node.name in ['prepare_upload_endpoint', 'finalize_upload_endpoint']:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), self.backend.__dict__)
        self.staging = patch.object(storage, 'signed_staging_upload', side_effect=lambda path: 'https://storage.example.com/' + path)
        self.staging.start()
        self.addCleanup(self.staging.stop)
        self.content = b'%PDF-synthetic-direct-upload'
        for name, value in [
            ('read_staged_upload', Mock(return_value=self.content)),
            ('publish_uploaded_bytes', Mock()),
            ('get_signed_url', Mock(side_effect=lambda path: 'https://storage.example.com/storage/v1/object/sign/private/' + path)),
        ]:
            replacement = patch.object(storage, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.payload = {'submissionId': 'synthetic-upload-0001', 'purpose': 'document',
                        'files': [{'name': 'test.pdf', 'size': len(self.content)}]}

    def prepare(self, payload=None, headers=None):
        return self.client.post('/api/uploads/prepare', json=payload or self.payload, headers=headers or self.headers)

    def finalize(self, file_id, headers=None):
        return self.client.post('/api/uploads/finalize', json={'fileId': file_id}, headers=headers or self.headers)

    def test_verified_upload_retry_preserves_owner_digest_and_single_object(self):
        prepared = self.prepare()
        self.assertEqual(prepared.status_code, 200, prepared.json)
        file_id = prepared.json['files'][0]['fileId']
        self.assertEqual(self.prepare().json['files'][0]['fileId'], file_id)
        result = self.finalize(file_id)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.finalize(file_id).json, result.json)
        storage.publish_uploaded_bytes.assert_called_once()
        records = [value for path, value in self.db.records.items() if path.startswith('file_ownership/')]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['uid'], 'customer')
        self.assertEqual(records[0]['sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertNotIn('staging/', records[0]['path'])
        self.assertIn('result', self.prepare().json['files'][0])

    def test_foreign_finalization_catalog_access_and_changed_request_are_rejected(self):
        file_id = self.prepare().json['files'][0]['fileId']
        self.assertEqual(self.finalize(file_id, {'Authorization': 'Bearer admin'}).status_code, 403)
        self.assertEqual(self.prepare({**self.payload, 'purpose': 'catalog'}).status_code, 403)
        changed = {**self.payload, 'files': [{'name': 'different.pdf', 'size': 2}]}
        self.assertEqual(self.prepare(changed).status_code, 409)
        storage.publish_uploaded_bytes.assert_not_called()

    def test_missing_login_and_incomplete_profile_are_rejected(self):
        self.assertEqual(self.client.post('/api/uploads/prepare', json=self.payload).status_code, 401)
        del self.db.records['users/customer']
        self.assertEqual(self.prepare().status_code, 403)

    def test_invalid_sizes_formats_and_combined_limit_are_rejected(self):
        for size in [0, -1, 10 * 1024 * 1024 + 1, 1.5, True, '5']:
            with self.subTest(size=size):
                self.assertEqual(self.prepare({**self.payload, 'files': [{'name': 'test.pdf', 'size': size}]}).status_code, 409)
        self.assertEqual(self.prepare({**self.payload, 'files': [{'name': 'bad.exe', 'size': 2}]}).status_code, 409)
        files = [{'name': 'test.pdf', 'size': 10 * 1024 * 1024}] * 3
        self.assertEqual(self.prepare({**self.payload, 'files': files}).status_code, 409)
        self.assertEqual(self.prepare({**self.payload, 'files': files * 2}).status_code, 409)

    def test_changed_bytes_failed_storage_and_expired_intents_keep_files_untrusted(self):
        file_id = self.prepare().json['files'][0]['fileId']
        storage.read_staged_upload.return_value = b'changed-size'
        self.assertEqual(self.finalize(file_id).status_code, 409)
        self.assertFalse(any(path.startswith('file_ownership/') for path in self.db.records))
        storage.read_staged_upload.return_value = self.content
        storage.publish_uploaded_bytes.side_effect = RuntimeError('synthetic failure')
        self.assertEqual(self.finalize(file_id).status_code, 503)
        storage.publish_uploaded_bytes.side_effect = None
        self.assertEqual(self.finalize(file_id).status_code, 200)
        self.payload['submissionId'] = 'synthetic-upload-0002'
        other = self.prepare().json['files'][0]['fileId']
        self.db.records['upload_intents/' + other]['expiresAt'] = self.backend.utc_now() - datetime.timedelta(seconds=1)
        self.assertEqual(self.finalize(other).status_code, 409)

    def test_active_finalization_lease_rejects_overlap_and_allows_later_retry(self):
        file_id = self.prepare().json['files'][0]['fileId']
        record = self.db.records['upload_intents/' + file_id]
        record['leaseUntil'] = self.backend.utc_now() + datetime.timedelta(minutes=1)
        self.assertEqual(self.finalize(file_id).status_code, 409)
        record['leaseUntil'] = self.backend.utc_now() - datetime.timedelta(seconds=1)
        self.assertEqual(self.finalize(file_id).status_code, 200)


class VercelConfigurationTests(unittest.TestCase):
    def test_firebase_credentials_load_from_private_environment(self):
        import firebase_admin
        from firebase_admin import credentials, firestore
        path = Path(__file__).parents[1] / 'firebase_config.py'
        account = {'project_id': 'synthetic-project', 'private_key': 'synthetic-private-key'}
        with patch.dict(os.environ, {'FIREBASE_SERVICE_ACCOUNT_JSON': json.dumps(account)}), \
                patch.object(firebase_admin, '_apps', {}), \
                patch.object(credentials, 'Certificate') as certificate, \
                patch.object(firebase_admin, 'initialize_app') as initialize, \
                patch.object(firestore, 'client', return_value='synthetic-database'):
            result = runpy.run_path(str(path))
        certificate.assert_called_once_with(account)
        initialize.assert_called_once_with(certificate.return_value)
        self.assertEqual(result['db'], 'synthetic-database')

    def test_config_excludes_credentials_and_uses_external_worker(self):
        root = Path(__file__).parents[1]
        config = json.loads((root / 'vercel.json').read_text())
        self.assertEqual(config['functions']['app.py']['maxDuration'], 120)
        self.assertEqual(config['daemons']['notifications']['instances'], {'sin1': 1})
        self.assertEqual(config['daemons']['notifications']['resources']['requests']['memory'], '1Gi')
        self.assertIn({'source': '/__/auth/:path*',
                       'destination': 'https://printingshop-7fd66.firebaseapp.com/__/auth/:path*'}, config['rewrites'])
        for name in ['.dockerignore', '.vercelignore']:
            ignored = (root / name).read_text()
            for secret in ['.env', 'firebase-service-account.json', 'security-backups']:
                self.assertIn(secret, ignored)

    def test_external_worker_mode_does_not_spawn_a_request_thread(self):
        from backend_test_support import load_backend
        backend = load_backend()
        with patch.dict(os.environ, {'PUSH_WORKER_MODE': 'external'}), patch.object(backend.threading, 'Thread') as thread:
            backend.start_push_worker(object(), Mock())
            thread.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for upload browser checks')
    def test_browser_upload_uses_signed_storage_and_retries_finalization(self):
        source = (Path(__file__).parents[1] / 'static/js/uploads.js').read_text(encoding='utf-8')
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const calls = [];
let finalize = 0;
class Xhr {
  constructor() { this.upload = {}; }
  open(method, url) { assert.equal(method, 'PUT'); assert.equal(url, 'https://storage.example.com/signed'); }
  send(form) { assert.equal(form.file.name, 'test.pdf'); this.status = 200; this.onload(); }
}
class Form { append(name, file) { this[name] = file; } }
const env = {window:{}, crypto:{randomUUID:()=> 'synthetic-upload-0001'},
  XMLHttpRequest:Xhr, FormData:Form, setTimeout:callback=>callback(),
  fetch:async (path, options)=> {
    calls.push([path, JSON.parse(options.body)]);
    if (path.endsWith('prepare')) return {ok:true,json:async()=>({success:true,files:[{fileId:'one',uploadUrl:'https://storage.example.com/signed'}]})};
    finalize++;
    return {ok:finalize>1,json:async()=>finalize>1 ? {success:true,fileUrl:'https://storage.example.com/private'} : {success:false,message:'Retry shortly'}};
  }};
vm.runInNewContext(SOURCE, env);
(async()=>{
  const progress=[];
  const result=await env.window.ShopUploads.upload({name:'test.pdf',size:5*1024*1024}, 'document', value=>progress.push(value));
  assert.equal(result.fileUrl,'https://storage.example.com/private');
  assert.equal(calls.length,3);
  assert.deepEqual(calls[1][1],calls[2][1]);
  assert.equal(progress.at(-1),100);
  await assert.rejects(env.window.ShopUploads.upload({name:'bad.pdf',size:11*1024*1024}),/10 MB/);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
