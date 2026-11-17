import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import psy_vault_archive as archive


class VaultArchive(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name)
        self.vault = self.home / 'vault'
        self.generated = self.home / 'cache/generated'
        self.generated.mkdir(parents=True)
        self.patches = [patch.object(archive, 'HOME', self.home), patch.object(archive, 'VAULT', self.vault)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.folder.cleanup()

    def hashed(self):
        calls = []
        real = archive.hashlib.sha256
        def counting(*args):
            calls.append(1)
            return real(*args)
        return calls, patch.object(archive.hashlib, 'sha256', side_effect=counting)

    def test_unchanged_files_are_not_read_again(self):
        image = self.generated / 'day/picture.png'
        image.parent.mkdir()
        image.write_bytes(b'synthetic image')
        self.assertEqual(archive.archive_media(), 1)
        calls, counting = self.hashed()
        with counting:
            self.assertEqual(archive.archive_media(), 0)
        self.assertEqual(calls, [])
        cache = json.loads((self.vault / archive.MEDIA_CACHE).read_text())
        self.assertEqual(list(cache), ['day/picture.png'])
        image.write_bytes(b'synthetic image, edited')
        os.utime(image, ns=(1, 2_000_000_000))
        self.assertEqual(archive.archive_media(), 1)
        self.assertEqual(len(list((self.vault / 'generated/day').iterdir())), 2)

    def test_lost_archive_copy_is_written_again_and_vanished_sources_leave_the_cache(self):
        image = self.generated / 'picture.png'
        image.write_bytes(b'synthetic image')
        archive.archive_media()
        for copy in (self.vault / 'generated').iterdir():
            copy.unlink()
        self.assertEqual(archive.archive_media(), 1)
        image.unlink()
        archive.archive_media()
        self.assertEqual(json.loads((self.vault / archive.MEDIA_CACHE).read_text()), {})

    def test_checkpointed_wal_state_read_from_read_only_profile(self):
        database = self.home / 'state.db'
        db = sqlite3.connect(database)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE messages(id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, tool_calls TEXT)')
        db.execute("INSERT INTO messages VALUES (1, 's', 'user', 'synthetic', 1.0, NULL)")
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.close()
        for side in ('-wal', '-shm'):
            Path(str(database) + side).unlink(missing_ok=True)
        self.vault.mkdir()
        self.home.chmod(0o500)   # like ProtectHome=read-only with only the vault writable
        try:
            self.assertEqual(archive.archive_messages(), 1)
            self.assertFalse(Path(str(database) + '-wal').exists())
        finally:
            self.home.chmod(0o700)


if __name__ == '__main__':
    unittest.main()
