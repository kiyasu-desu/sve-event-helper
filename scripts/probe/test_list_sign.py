"""Offline CLI regression tests using only synthetic signing samples and keys."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ListSignTest(unittest.TestCase):
    def test_all_candidate_groups_use_the_supplied_query(self):
        raw_query = ('_page=3&_size=7&area=synthetic%20region%2B%E6%B5%8B%E8%AF%95'
                     '&is_sv2=1&empty=&filter=a%3Db')
        secret = 'TESTSECRET'
        device = 'synthetic-device'
        decoded = '_page=3_size=7area=synthetic region+测试empty=filter=a=bis_sv2=1'
        quoted = ('_page=3_size=7area=synthetic%20region%2B%E6%B5%8B%E8%AF%95'
                  'empty=filter=a=bis_sv2=1')
        encoded = ('_page=3&_size=7&area=synthetic+region%2B%E6%B5%8B%E8%AF%95'
                   '&is_sv2=1&empty=&filter=a%3Db')
        candidates = {
            'A1': raw_query,
            'B1': decoded + device + '=' + secret,
            'B2': decoded + 'udid=' + secret,
            'C1': encoded + device + '=' + secret,
            'C2': quoted + device + '=' + secret,
        }
        script = Path(__file__).with_name('list_sign.py')
        with tempfile.TemporaryDirectory(prefix='sve-list-sign-test-') as directory:
            config = Path(directory) / 'samples.json'
            env = dict(os.environ, SVE_APP_KEY=secret,
                       SVE_SIGNING_SAMPLES_FILE=str(config), PYTHONIOENCODING='utf-8')
            for candidate, text in candidates.items():
                with self.subTest(candidate=candidate):
                    target = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    config.write_text(json.dumps({'list_sign': {
                        'UDID': device, 'TARGET': target, 'RAW_QUERY': raw_query,
                    }}), encoding='utf-8')
                    result = subprocess.run([sys.executable, '-B', str(script)],
                                            env=env, capture_output=True, text=True,
                                            encoding='utf-8', timeout=10)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn('命中: ' + candidate + ' ', result.stdout)
                    self.assertNotIn(secret, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
