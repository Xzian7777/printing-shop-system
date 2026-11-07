"""Exercise the actual review renderer with printing and legacy/store fixtures."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js is required')
class ReviewRenderingTests(unittest.TestCase):
    def render_order(self, order):
        source = (Path(__file__).parents[1] / 'templates/admin.html').read_text(encoding='utf-8')
        start = source.index('function openReviewSpecsModal(')
        end = source.index('function closeSpecsModal(', start)
        script = """
const vm = require('node:vm');
const body = {innerHTML:'', querySelector:()=>null};
const context = {
  allAdminOrdersCache: [ORDER],
  document: {getElementById: id => id === 'specsModalBody' ? body : {classList:{remove:()=>{}}}},
  escapeHtml: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  formatInteger: value => String(value), formatCurrency: value => 'PHP ' + value,
};
vm.runInNewContext(SOURCE,context);
context.openReviewSpecsModal('test');
process.stdout.write(body.innerHTML);
""".replace('ORDER', json.dumps({'id': 'test', **order})).replace('SOURCE', json.dumps(source[start:end]))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_store_hides_print_fields_and_displays_units(self):
        html = self.render_order({'service':'In-Store: acer (1x)','paperSize':'Store Supply',
            'copies':1,'colorMode':'N/A','items':[{'name':'acer','qty':1}], 'total':15000})
        for label in ['Paper Specs','Copies / Quantity','Color Mode']:
            self.assertNotIn(f'>{label}</dt>', html)
        self.assertIn('Store items', html)
        self.assertIn('acer', html)
        self.assertIn('PHP 15000', html)

    def test_printing_preserves_specs_requirements_and_valid_attachment_link(self):
        html = self.render_order({'service':'Document Printing','paperSize':'A4','copies':5,'colorMode':'Black & White',
            'customRequirements':{'Binding':'Spiral'},'fileName':'test.pdf','fileUrl':'https://example.com/test.pdf'})
        for label in ['Paper Specs','Copies / Quantity','Color Mode','Binding']:
            self.assertIn(f'>{label}</dt>', html)
        self.assertIn('https://example.com/test.pdf', html)
        self.assertIn('Open File', html)

    def test_legacy_store_detection_and_notes_are_escaped(self):
        html = self.render_order({'service':'In-Store: pen (2x)', 'customerNote':'<script>bad()</script>', 'fileUrl':'javascript:alert(1)'})
        self.assertNotIn('Paper Specs', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn('File Info', html)
