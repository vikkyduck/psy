import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cas_fixture as release


class ReleaseInstallation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.backup = self.root / 'backup'
        self.backup.mkdir(mode=0o700)

    def item(self, name, old, new):
        path = self.root / name
        if old is not None:
            path.write_text(old)
            path.chmod(0o640)
        return {'path': str(path), 'content': new, 'group': 'gateway',
            'before_sha256': release.digest(old.encode()) if old is not None else None,
            'after_sha256': release.digest(new.encode())}

    def test_atomic_code_install_preserves_modes_and_is_idempotent(self):
        entry = self.item('source.py', 'value = 1\n', 'value = 2\n')
        package = {'targets': [entry]}
        changed = release.install(package, {'gateway'}, self.backup)
        self.assertEqual(len(changed), 1)
        target = Path(entry['path'])
        self.assertEqual(target.read_text(), 'value = 2\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(release.install(package, {'gateway'}, self.backup), [])
        release.restore(changed)
        self.assertEqual(target.read_text(), 'value = 1\n')

    def test_concurrent_source_edit_aborts_entire_preflight(self):
        first = self.item('first.py', 'v = 1\n', 'v = 2\n')
        second = self.item('second.py', 'v = 3\n', 'v = 4\n')
        Path(second['path']).write_text('independent = True\n')
        with self.assertRaisesRegex(RuntimeError, 'Concurrent'):
            release.install({'targets': [first, second]}, {'gateway'}, self.backup)
        self.assertEqual(Path(first['path']).read_text(), 'v = 1\n')
        self.assertEqual(Path(second['path']).read_text(), 'independent = True\n')

    def test_bad_syntax_fails_before_any_replacement(self):
        first = self.item('first.py', 'v = 1\n', 'v = 2\n')
        invalid = self.item('invalid.py', 'v = 3\n', 'def broken(\n')
        with self.assertRaises(SyntaxError):
            release.install({'targets': [first, invalid]}, {'gateway'}, self.backup)
        self.assertEqual(Path(first['path']).read_text(), 'v = 1\n')

    def test_failed_second_write_restores_first(self):
        entries = [self.item('first.py', 'v = 1\n', 'v = 2\n'),
                   self.item('second.py', 'v = 3\n', 'v = 4\n')]
        original = release.atomic
        calls = []
        def fail_second(*args):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('fixture')
            return original(*args)
        with patch.object(release, 'atomic', side_effect=fail_second):
            with self.assertRaises(OSError):
                release.install({'targets': entries}, {'gateway'}, self.backup)
        self.assertEqual(Path(entries[0]['path']).read_text(), 'v = 1\n')
        self.assertEqual(Path(entries[1]['path']).read_text(), 'v = 3\n')

    def test_rollback_cannot_overwrite_a_later_independent_edit(self):
        entry = self.item('source.py', 'v = 1\n', 'v = 2\n')
        changed = release.install({'targets': [entry]}, {'gateway'}, self.backup)
        Path(entry['path']).write_text('independent = True\n')
        with self.assertRaisesRegex(RuntimeError, 'independent'):
            release.restore(changed)
        self.assertEqual(Path(entry['path']).read_text(), 'independent = True\n')

    def test_new_helper_is_removed_by_rollback(self):
        entry = self.item('helper.py', None, 'value = 1\n')
        changed = release.install({'targets': [entry]}, {'gateway'}, self.backup)
        self.assertTrue(Path(entry['path']).is_file())
        release.restore(changed)
        self.assertFalse(Path(entry['path']).exists())

    def test_symlink_target_rejected_without_touching_destination(self):
        entry = self.item('source.py', 'v = 1\n', 'v = 2\n')
        link = self.root / 'link.py'
        link.symlink_to(entry['path'])
        entry['path'] = str(link)
        with self.assertRaisesRegex(RuntimeError, 'Symlinked'):
            release.install({'targets': [entry]}, {'gateway'}, self.backup)
        self.assertEqual(link.read_text(), 'v = 1\n')


if __name__ == '__main__':
    unittest.main()
