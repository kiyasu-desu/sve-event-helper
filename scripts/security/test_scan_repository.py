"""Offline gate tests, including real Git index/tree/range semantics in a temp repo."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import scan_repository as scan


class RulesTest(unittest.TestCase):
    def categories(self, text, known=()):
        return {category for _, category in scan.findings(text.encode(), list(known))}

    def test_credentials_and_personal_data(self):
        cases = [
            ('app_key = "' + 'Ab9z' * 8 + '"', 'credential-literal'),
            ('sessionid = "' + '12ab' * 16 + '"', 'credential-literal'),
            ('Cookie: "' + 'NTES_SESS=' + 'aB8' * 12 + '"', 'credential-literal'),
            ('token = "' + 'fake-but-sensitive-' + 'z7' * 10 + '"', 'credential-literal'),
            ('authcode = "' + 'a8Zx' * 4 + '"', 'credential-literal'),
            ('-----BEGIN ' + 'OPENSSH PRIVATE KEY-----', 'private-key'),
            ('person' + '@private.invalid', 'email'),
            ('138' + '12345678', 'phone'),
            ('u' + 'id: ' + '87654321', 'account-literal'),
            ('nickname: "' + 'private person' + '"', 'account-literal'),
            ('https://' + 'private.invalid', 'unreviewed-host'),
            ('10.' + '23.45.67', 'infrastructure-ip'),
            ('C:/Users/' + 'private-user/.ssh/key', 'personal-path'),
        ]
        for text, category in cases:
            with self.subTest(category=category):
                self.assertIn(category, self.categories(text))

    def test_official_endpoints_and_documented_synthetic_data(self):
        for text in ['https://api-sve.nie.netease.com/sve/api/v1',
                     'https://sve.example.com', 'person@example.com',
                     '127.0.0.1', '192.0.2.10', '10.0.2.2',
                     '{"uid": 10001, "nickname": "测试账号"}',
                     'sessionid = os.environ.get("SID")']:
            self.assertEqual(set(), self.categories(text))

    def test_known_private_overrides_placeholder(self):
        self.assertIn('known-private', self.categories('uid: 10001', ['10001']))

    def test_no_matched_value_in_results(self):
        private = 'sensitive-' + 'identifier'
        result = scan.findings(private.encode(), [private])
        self.assertEqual([(1, 'known-private')], result)
        self.assertNotIn(private, repr(result))

    def test_binary_fails_closed(self):
        self.assertEqual([(1, 'binary-review-required')], scan.findings(b'a\0b', []))


class GitModesTest(unittest.TestCase):
    def test_sensitive_paths_fail_in_all_modes_without_disclosure(self):
        private = 'private-' + 'customer-name'
        email = 'person' + '@private.invalid'
        cases = [
            ('.deployment-backups/test-snapshot/private.json', [], 'private-file'),
            ('.maintenance/test-snapshot/session.json.bak', [], 'private-file'),
            ('maintenance-backups/test-snapshot/private.png', [], 'private-file'),
            ('deployment-state.json', [], 'private-file'),
            ('runtime-migration.json', [], 'private-file'),
            ('watchdog/watchdog-state.json', [], 'private-file'),
            ('deployment-backups/test-snapshot/private.json', [], 'private-file'),
            ('legacy-config/other.json', [], 'private-file'),
            ('release.json', [], 'private-file'),
            (private + '.txt', [private], 'known-private'),
            (private + '.png', [private], 'known-private'),
            (private + '/fixture.txt', [private], 'known-private'),
            (email + '.txt', [], 'email'),
            (email + '.png', [], 'email'),
            ('person@example.com/fixture.txt', [], None),
            ('safe.png', [], None),
            ('identity-v2.json', [], 'private-file'),
            ('identity-migration.json', [], 'private-file'),
            ('identity-backups/fixture.json', [], 'private-file'),
            ('identity-backups/screenshot.png', [], 'private-file'),
        ]
        for name, known, category in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='sve-security-test-') as directory:
                root = Path(directory)

                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()

                git('init')
                git('config', 'user.name', 'Synthetic Test')
                git('config', 'user.email', 'test@example.com')
                # Identical blobs must still be checked under each distinct path.
                (root / 'safe.txt').write_text('safe\n', encoding='utf-8')
                git('add', 'safe.txt')
                git('commit', '-m', 'synthetic safe baseline')
                base = git('rev-parse', 'HEAD')
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('safe\n', encoding='utf-8')
                git('add', '--', name)
                git('commit', '-m', 'synthetic path fixture')
                config = root / 'local-config.json'
                config.write_text(json.dumps({'known_private': known}), encoding='utf-8')
                with patch.object(scan, 'ROOT', root):
                    self.addCleanup(scan.git.cache_clear)
                    for mode in ('working', 'index', 'tree', 'history', 'range'):
                        with self.subTest(mode=mode):
                            scan.git.cache_clear()
                            output = io.StringIO()
                            ref = base + '..HEAD' if mode == 'range' else 'HEAD'
                            with patch.object(sys, 'argv', ['scan', '--mode', mode, '--ref', ref,
                                                           '--private-config', str(config)]), contextlib.redirect_stdout(output):
                                code = scan.main()
                            self.assertEqual(1 if category else 0, code)
                            if category:
                                self.assertIn(':<path>: ' + category, output.getvalue())
                                self.assertNotIn(private if known else email, output.getvalue())

    def test_index_tree_and_history(self):
        with tempfile.TemporaryDirectory(prefix='sve-security-test-') as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()

            git('init')
            git('config', 'user.name', 'Synthetic Test')
            git('config', 'user.email', 'test@example.com')
            path = root / 'fixture.txt'
            secret = 'Q7bX' * 8
            path.write_text('token = "' + secret + '"\n', encoding='utf-8')
            git('add', 'fixture.txt')
            git('commit', '-m', 'synthetic unsafe fixture')
            base = git('rev-parse', 'HEAD')
            path.write_text('safe\n', encoding='utf-8')
            git('add', 'fixture.txt')
            git('commit', '-m', 'remove fixture')
            path.write_text('token = "' + secret + '"\n', encoding='utf-8')
            with patch.object(scan, 'ROOT', root):
                def run(mode, ref='HEAD'):
                    scan.git.cache_clear()
                    output = io.StringIO()
                    with patch.object(sys, 'argv', ['scan', '--mode', mode, '--ref', ref,
                                                   '--private-config', str(root / 'missing.json')]), contextlib.redirect_stdout(output):
                        code = scan.main()
                    self.assertNotIn(secret, output.getvalue())
                    return code
                self.assertEqual(1, run('working'))
                self.assertEqual(0, run('index'))
                self.assertEqual(0, run('tree'))
                self.assertEqual(1, run('history'))
                self.assertEqual(0, run('range', base + '..HEAD'))
                git('add', 'fixture.txt')
                self.assertEqual(1, run('index'))
                git('commit', '-m', 'intermediate unsafe synthetic fixture')
                path.write_text('safe again\n', encoding='utf-8')
                git('add', 'fixture.txt')
                git('commit', '-m', 'remove intermediate fixture')
                self.assertEqual(0, run('tree'))
                self.assertEqual(1, run('range', base + '..HEAD'))
            scan.git.cache_clear()


if __name__ == '__main__':
    unittest.main()
