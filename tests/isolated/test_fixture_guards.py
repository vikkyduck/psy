"""Fast standard-library tests. No service, model, credentials or network is used."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import generate_reference
import group_example
import run_on_host


class FixtureDestinationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='psy-fixture-guards-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_new_direct_wav_in_private_temp_is_allowed(self):
        self.assertEqual(generate_reference.validate_destination(self.root, self.root / 'synthetic.wav'),
                         (self.root, self.root / 'synthetic.wav'))

    def test_production_directories_are_rejected(self):
        for root in ('/home/hermes/.hermes', '/opt/psy-upload', '/etc', '/var/lib', '/'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                generate_reference.validate_destination(Path(root), Path(root) / 'synthetic.wav')

    def test_temp_directory_itself_is_not_a_fixture_scope(self):
        root = Path(tempfile.gettempdir()).resolve()
        with self.assertRaises(ValueError):
            generate_reference.validate_destination(root, root / 'synthetic.wav')

    def test_inherited_tmpdir_cannot_allow_a_production_directory(self):
        root = Path('/home/hermes/.hermes/fixture')
        with patch.object(tempfile, 'gettempdir', return_value='/home/hermes/.hermes'):
            with self.assertRaises(ValueError):
                generate_reference.validate_destination(root, root / 'synthetic.wav')

    def test_sibling_and_relative_outputs_are_rejected(self):
        for target in (self.root.parent / 'synthetic.wav', Path('synthetic.wav'), self.root / 'nested/audio.wav'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, target)

    def test_existing_file_and_symlink_are_never_overwritten(self):
        existing = self.root / 'existing.wav'
        existing.write_bytes(b'synthetic fixture')
        link = self.root / 'link.wav'
        link.symlink_to(existing)
        for target in (existing, link):
            with self.subTest(target=target), self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, target)
        self.assertEqual(existing.read_bytes(), b'synthetic fixture')

    def test_symbolic_fixture_directory_is_rejected(self):
        link = self.root / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            generate_reference.validate_destination(link, link / 'synthetic.wav')

    def test_world_readable_fixture_directory_is_rejected(self):
        self.root.chmod(0o755)
        try:
            with self.assertRaises(ValueError):
                generate_reference.validate_destination(self.root, self.root / 'synthetic.wav')
        finally:
            self.root.chmod(0o700)

    def test_missing_tools_create_no_audio_or_directory(self):
        destination = self.root / 'unused'
        with patch.object(generate_reference, 'find_tools', side_effect=RuntimeError('not installed')):
            with self.assertRaises(RuntimeError):
                generate_reference.generate(destination, destination / 'synthetic.wav')
        self.assertFalse(destination.exists())

    def test_control_text_contains_only_synthetic_acceptance_phrase(self):
        phrase = generate_reference.REFERENCE.read_text(encoding='utf-8').casefold()
        for reference in ('synthetic test', 'green', 'train', 'london', 'nine', 'forty seven'):
            self.assertIn(reference, phrase)


class NetworkAndModeTests(unittest.TestCase):
    def test_stub_model_cannot_contact_real_codex(self):
        guard = group_example.NetworkGuard(43210, allow_model=False)
        guard.allowed_url('http://127.0.0.1:43210/bot-fixture')
        with self.assertRaises(group_example.IsolationViolation):
            guard.allowed_url('https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(guard.codex_http, 0)

    def test_real_mode_accepts_codex_but_rejects_real_telegram(self):
        guard = group_example.NetworkGuard(43210, allow_model=True)
        guard.allowed_url('https://chatgpt.com/backend-api/codex/responses')
        self.assertEqual(guard.codex_http, 1)
        for url in ('https://api.telegram.org', 'https://example.invalid', 'http://chatgpt.com',
                    'http://127.0.0.1:43211/bot-fixture'):
            with self.subTest(url=url), self.assertRaises(group_example.IsolationViolation):
                guard.allowed_url(url)

    def test_host_runner_starts_with_no_inherited_service_credentials(self):
        with patch.dict(os.environ, {'BOT_TOKEN': 'synthetic-value', 'CREDENTIALS_DIRECTORY': '/invalid-fixture'}):
            environment = run_on_host.clean_environment('/synthetic/home', '/synthetic/temp')
        self.assertEqual(set(environment), {'PATH', 'HOME', 'TMPDIR', 'LANG', 'PYTHONDONTWRITEBYTECODE'})
        self.assertEqual(environment['PYTHONDONTWRITEBYTECODE'], '1')


if __name__ == '__main__':
    unittest.main()
