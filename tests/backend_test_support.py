"""Load backend definitions without initializing connections or starting Flask."""
import ast
import sys
import types
from pathlib import Path


def load_backend(overrides=None):
    path = Path(__file__).parents[1] / 'app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
            if module not in ['firebase_config', 'supabase_config']:
                nodes.append(node)
        elif isinstance(node, ast.FunctionDef):
            if not any(ast.unparse(decorator).startswith('app.') for decorator in node.decorator_list):
                nodes.append(node)
        elif isinstance(node, ast.ClassDef):
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) for target in node.targets):
            if all(target.id != 'app' for target in node.targets):
                nodes.append(node)
    module = types.ModuleType('backend_under_test')
    module.__file__ = str(path)
    module.db = None
    sys.modules[module.__name__] = module
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), module.__dict__)
    if overrides:
        module.__dict__.update(overrides)
    return module


backend = load_backend()


def __getattr__(name):
    return getattr(backend, name)
