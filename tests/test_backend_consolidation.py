import argparse
import ast
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask
from backend_test_support import load_backend


ROOT = Path(__file__).parents[1]


class BackendConsolidationTests(unittest.TestCase):
    def test_public_route_contract_matches_before_consolidation(self):
        backend = load_backend()
        backend.app = Flask('backend_under_test', root_path=str(ROOT))
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        for node in tree.body:
            route = isinstance(node, ast.FunctionDef) and any(
                ast.unparse(decorator).startswith('app.') for decorator in node.decorator_list)
            registration = (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name)
                            and node.value.func.id == 'register_loyalty_routes')
            if route or registration:
                exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), backend.__dict__)
        actual = [{'rule': rule.rule, 'methods': sorted(rule.methods), 'endpoint': rule.endpoint}
                  for rule in backend.app.url_map.iter_rules()]
        expected = json.loads((ROOT / 'tests/routes_before_consolidation.json').read_text(encoding='utf-8'))
        self.assertEqual(sorted(actual, key=str), sorted(expected, key=str))

    def test_test_loader_does_not_initialize_firebase(self):
        import firebase_admin
        before = dict(firebase_admin._apps)
        backend = load_backend()
        self.assertTrue(backend.ServerValidators.validate_date('2099-01-01')[0])
        self.assertFalse(backend.ServerValidators.validate_date('2000-01-01')[0])
        self.assertEqual(firebase_admin._apps, before)

    def test_config_has_connection_setup_and_no_feature_functions(self):
        tree = ast.parse((ROOT / 'firebase_config.py').read_text(encoding='utf-8'))
        self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in tree.body))

    def test_worker_mode_and_normal_startup_are_separate(self):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        main = next(node for node in tree.body if isinstance(node, ast.If)
                    and '__name__' in ast.unparse(node.test))
        for arguments, worker in [(['app.py', '--push-worker'], True), (['app.py'], False)]:
            backend = load_backend()
            env = backend.__dict__.copy()
            sender, server, database = Mock(), Mock(), object()
            env.update(argparse=argparse, db=database, run=sender, app=server,
                       os=types.SimpleNamespace(environ={}))
            with patch.object(sys, 'argv', arguments), patch('builtins.print'):
                exec(compile(ast.Module(body=main.body, type_ignores=[]), 'app.py', 'exec'), env)
            if worker:
                sender.assert_called_once_with(database, env['send_fcm_push_notification'])
                server.run.assert_not_called()
            else:
                server.run.assert_called_once_with(debug=True, port=5000)
                sender.assert_not_called()

    def test_worker_start_is_idempotent(self):
        backend = load_backend()
        thread = Mock()
        thread.is_alive.return_value = True
        with patch.object(backend.threading, 'Thread', return_value=thread) as constructor:
            backend.start_push_worker(object(), Mock())
            backend.start_push_worker(object(), Mock())
        self.assertEqual(constructor.call_count, 1)
        thread.start.assert_called_once()
