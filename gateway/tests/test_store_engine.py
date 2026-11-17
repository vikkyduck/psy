"""SQLite settings of the group vault: WAL, long busy waits and one-time schema setup."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from psy_modules import psy_group_store as store


class StoreEngine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "vault"

    def test_wal_journal_and_busy_timeout_on_every_connection(self):
        with store.database(self.root) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertGreaterEqual(con.execute("PRAGMA busy_timeout").fetchone()[0], 30_000)
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        # WAL is a property of the file, visible to an independent reader such as a backup.
        with sqlite3.connect(self.root / "group.sqlite3") as raw:
            self.assertEqual(raw.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "group.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_schema_is_prepared_once_per_file_and_again_after_replacement(self):
        prepare = store._prepare
        with patch.object(store, "_prepare", side_effect=prepare) as prepared:
            for ident in range(1, 4):
                store.pending(7, ident, f"synthetic {ident}", root=self.root)
                store.state(ident, root=self.root)
            self.assertEqual(prepared.call_count, 1)
            for name in os.listdir(self.root):
                if name.startswith("group.sqlite3"):
                    os.unlink(self.root / name)
            self.assertIsNone(store.state(1, root=self.root))
            self.assertEqual(prepared.call_count, 2)
        self.assertEqual((self.root / "group.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_symlinked_database_is_refused(self):
        self.root.mkdir()
        (self.root / "group.sqlite3").symlink_to(self.root / "elsewhere.sqlite3")
        with self.assertRaises(OSError):
            with store.database(self.root):
                pass
        self.assertFalse((self.root / "elsewhere.sqlite3").exists())

    def test_existing_binding_is_confirmed_without_the_write_lock(self):
        store.bind_instance(303, -202, root=self.root)
        holder = sqlite3.connect(self.root / "group.sqlite3", timeout=0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        with patch.object(store, "BUSY_SECONDS", 0.2):
            store.bind_instance(303, -202, root=self.root)
            with self.assertRaises(RuntimeError):
                store.bind_instance(304, -202, root=self.root, adopt_legacy=True)
            # The lock is real: a writer in the same situation waits and then fails.
            with self.assertRaises(sqlite3.OperationalError):
                store.archive(7, 0, "x", "text", b"x", root=self.root)
        holder.rollback()

    def test_root_follows_hermes_home_with_the_previous_default(self):
        with patch.dict(os.environ, {"HERMES_HOME": "/srv/fixture-home"}):
            self.assertEqual(store._hermes_home(), Path("/srv/fixture-home"))
        with patch.dict(os.environ, {"HERMES_HOME": ""}):
            self.assertEqual(store._hermes_home(), Path("/home/hermes/.hermes"))
        self.assertEqual(store.ROOT.parts[-2:], ("vault", "group-psy"))


if __name__ == "__main__":
    unittest.main()
