from pathlib import Path
import unittest

from flask import Flask, render_template


ROOT = Path(__file__).resolve().parents[1]


class TermsContentTests(unittest.TestCase):
    def test_shared_modal_renders_the_expanded_policy(self):
        app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
        app.secret_key = 'terms-render-test-only'
        with app.test_request_context('/login'):
            page = render_template('login.html')
        self.assertIn('Version 2026-11-14', page)
        terms = page.split('id="termsContent"', 1)[1].split('<!-- Modal Footer -->', 1)[0]
        self.assertEqual(terms.count('<section>'), 14)
        for text in ['before admin approval', '30 calendar days', '1–2 business days',
                     '50%', 'Needs Review', 'National Privacy Commission',
                     'Department of Trade and Industry', 'statutory rights']:
            self.assertIn(text, terms)
        self.assertNotIn('without refund eligibility', terms)
        self.assertNotIn('Non-Refund Policy', terms)

    def test_modal_close_preserves_explicit_form_acceptance(self):
        base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        close = base.split('function closeTermsModal()', 1)[1].split('function toggleNotifDropdown()', 1)[0]
        self.assertNotIn('.checked', close)
        login = (ROOT / 'templates/login.html').read_text(encoding='utf-8')
        self.assertNotIn('loginTerms.checked = true', login)
