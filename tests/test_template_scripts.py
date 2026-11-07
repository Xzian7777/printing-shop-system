"""Check browser script syntax without starting Flask or accessing Firebase."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TemplateSyntaxTests(unittest.TestCase):
    def test_modified_pages_render(self):
        from flask import Flask, render_template, session
        root = Path(__file__).parents[1]
        app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
        app.secret_key = 'template-test-only'
        with app.test_request_context('/'):
            session.update(username='Admin', email='admin@example.com', role='admin')
            for filename in ['admin.html', 'customer.html', 'store.html']:
                with self.subTest(template=filename):
                    self.assertIn('<html', render_template(filename))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser script syntax checks')
    def test_modified_browser_scripts_parse(self):
        root = Path(__file__).parents[1] / 'templates'
        for filename in ['base.html', 'admin.html', 'store.html', 'order.html', 'customer.html']:
            html = (root / filename).read_text(encoding='utf-8')
            for index, script in enumerate(re.findall(r'<script\b[^>]*>(.*?)</script>', html, re.S | re.I)):
                if not script.strip(): continue
                script = re.sub(r'\{\{.*?\}\}', 'null', script, flags=re.S)
                script = re.sub(r'\{%.*?%\}', '', script, flags=re.S)
                with self.subTest(template=filename, script=index):
                    result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True, encoding='utf-8')
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
