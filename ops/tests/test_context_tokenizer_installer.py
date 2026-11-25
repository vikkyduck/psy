"""Publication/failure tests use synthetic releases, never installed bot data."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import hashlib
import io
import json
import zipfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'install-context-tokenizer'
loader = importlib.machinery.SourceFileLoader('context_tokenizer_installer', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
installer = importlib.util.module_from_spec(spec)
loader.exec_module(installer)
IDENTITY = {'implementation': 'cpython', 'major': 3, 'minor': 12,
            'version': '3.12.12', 'soabi': 'cpython-312-x86_64-linux-gnu'}


class TokenizerInstallation(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        (self.base / 'releases').mkdir()
        self.lock = self.base / 'requirements.lock.txt'
        self.lock.write_text('synthetic-lock')
        self.old = self.base / 'releases' / 'old'
        self.old.mkdir()
        (self.old / 'manifest.json').write_text('{}')
        (self.base / 'current').symlink_to('releases/old', target_is_directory=True)

    def install(self):
        return installer.install(self.base, self.lock, '/root-owned/python3.12', IDENTITY)

    def test_failed_build_retains_active_release_and_removes_partial(self):
        def fail(release, *args):
            (release / 'partial').write_text('partial')
            raise RuntimeError('Synthetic cache integrity failure')
        with patch.object(installer, 'build', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'integrity'):
                self.install()
        self.assertEqual((self.base / 'current').resolve(), self.old)
        self.assertEqual(list((self.base / 'releases').iterdir()), [self.old])

    def test_success_builds_at_final_path_then_reuses_verified_release(self):
        def build(release, *args):
            self.assertEqual((self.base / 'current').resolve(), self.old)
            (release / 'manifest.json').write_text('{}')
            (release / 'venv').mkdir()
            (release / 'venv' / 'final-path').write_text(str(release))
        with patch.object(installer, 'build', side_effect=build) as builder, \
             patch.object(installer, 'verify') as verify:
            release = self.install()
            self.assertEqual((self.base / 'current').resolve(), release)
            self.assertEqual((release / 'venv' / 'final-path').read_text(), str(release))
            self.assertEqual(self.install(), release)
        self.assertEqual(builder.call_count, 1)
        verify.assert_called_once_with(release, self.lock, IDENTITY, None)
        self.assertTrue(self.old.is_dir())

    def test_existing_invalid_release_never_replaces_active(self):
        with patch.object(installer, 'build', side_effect=lambda path, *a: (path / 'manifest.json').write_text('{}')):
            release = self.install()
        installer.publish(self.base, self.old)
        with patch.object(installer, 'verify', side_effect=RuntimeError('Invalid existing release')):
            with self.assertRaisesRegex(RuntimeError, 'Invalid'):
                self.install()
        self.assertEqual((self.base / 'current').resolve(), self.old)
        self.assertTrue(release.is_dir())

    def test_atomic_publication_error_preserves_old_link(self):
        candidate = self.base / 'releases' / 'candidate'
        candidate.mkdir()
        with patch.object(installer.os, 'replace', side_effect=OSError('Synthetic rename failure')):
            with self.assertRaises(OSError):
                installer.publish(self.base, candidate)
        self.assertEqual((self.base / 'current').resolve(), self.old)
        self.assertFalse(list(self.base.glob('.current-*')))

    def test_rejects_directory_at_active_path(self):
        (self.base / 'current').unlink()
        (self.base / 'current').mkdir()
        with self.assertRaisesRegex(RuntimeError, 'non-symlink'):
            installer.publish(self.base, self.old)
        self.assertTrue((self.base / 'current').is_dir())

    def test_runtime_python_is_invoked_only_after_privilege_drop(self):
        runtime = {'user': 'hermes', 'python': '/home/hermes/tools/python3.14'}
        command = installer.runtime_command('/usr/bin/python3.12', runtime)
        self.assertEqual(command, ['/usr/sbin/runuser', '-u', 'hermes', '--',
                                   '/home/hermes/tools/python3.14'])
        self.assertEqual(installer.runtime_command('/usr/bin/python3.12', None),
                         ['/usr/bin/python3.12'])

    def test_target_abi_changes_package_path_and_release_identity(self):
        identity = {**IDENTITY, 'minor': 14, 'version': '3.14.7',
                    'soabi': 'cpython-314-x86_64-linux-gnu'}
        path = installer.target_path(self.old, identity)
        self.assertTrue(str(path).endswith('/venv/lib/python3.14/site-packages'))
        flags = installer.cross_flags(identity)
        self.assertEqual(flags[flags.index('--abi') + 1], 'cp314')
        self.assertEqual(flags[flags.index('--python-version') + 1], '314')
        runtime = {'user': 'hermes', 'python': '/home/hermes/tools/python3.14'}
        with patch.object(installer, 'build', side_effect=lambda path, *a: (path / 'manifest.json').write_text('{}')):
            release = installer.install(self.base, self.lock, '/usr/bin/python3.12', identity, runtime)
        self.assertTrue(release.name.endswith('-py314'))
        self.assertTrue(self.old.exists())

    def test_rank_download_must_match_hash_inside_pinned_wheel(self):
        release = self.base / 'rank-test'
        (release / 'wheels').mkdir(parents=True)
        (release / 'cache').mkdir()
        digest = hashlib.sha256(b'expected ranks').hexdigest()
        source = ('def o200k_base():\n    return load_tiktoken_bpe(' + repr(installer.RANK_URL)
                  + ', expected_hash=' + repr(digest) + ')\n')
        with zipfile.ZipFile(release / 'wheels/tiktoken-synthetic.whl', 'w') as archive:
            archive.writestr('tiktoken_ext/openai_public.py', source)
        with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupted ranks')):
            with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                installer.warm_cache(release)
        self.assertEqual(list((release / 'cache').iterdir()), [])
        with patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'expected ranks')):
            encoding = installer.warm_cache(release)
        self.assertEqual(encoding['sha256'], digest)
        self.assertEqual((release / 'cache' / encoding['cache_key']).read_bytes(), b'expected ranks')

    def test_lock_has_exact_complete_dependency_set_and_sha256(self):
        versions = installer.pinned_versions(installer.LOCK)
        self.assertEqual(versions['tiktoken'], '0.12.0')
        entries = installer.LOCK.read_text().split('\n\n')
        for entry in entries:
            if '==' not in entry:
                continue
            self.assertRegex(entry, r'--hash=sha256:[0-9a-f]{64}')
        self.assertNotIn('http://', installer.LOCK.read_text())



if __name__ == '__main__':
    unittest.main()
