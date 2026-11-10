import re
import unittest
from pathlib import Path


class EnglishUiTests(unittest.TestCase):
    def test_templates_do_not_contain_tagalog_application_copy(self):
        pattern = re.compile(r'\b(kailangan|hindi|kulang|pakilagay|paki|ilagay|inyong|iyong|mangyaring|walang|maaaring|sumasang|matagumpay|babayarang|piliin|magbayad|pindutin)\b', re.I)
        for path in (Path(__file__).parents[1] / 'templates').glob('*.html'):
            text = re.sub(r'<!--.*?-->|/\*.*?\*/', '', path.read_text(encoding='utf-8'), flags=re.S)
            text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('//'))
            with self.subTest(template=path.name):
                self.assertIsNone(pattern.search(text))
