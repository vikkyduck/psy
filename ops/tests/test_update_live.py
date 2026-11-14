import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_live as live


class ReleaseInventory(unittest.TestCase):
    def test_group_manifest_matches_release_manifest(self):
        live.check_group_manifest(live.release_sums())

    def test_group_manifest_mismatch_is_refused(self):
        sums = dict(live.release_sums())
        sums[live.RELEASE.TELEGRAM + "psy_instance.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "psy_instance.py"):
            live.check_group_manifest(sums)

    def test_system_entries_follow_install_runtime(self):
        targets = {str(e.target) for e in live.system_entries(997)}
        script = (live.REPO / "ops/install-runtime").read_text()
        for name in live.MEDIA:
            self.assertIn(name, script)
            self.assertIn(f"/opt/psy-upload/{name}.py", targets)
        for src, dst in live.HELPERS.items():
            self.assertIn(f"{src}\" /usr/local/libexec/{dst}", script)
        self.assertIn("/etc/systemd/system/hermes-gateway.service.d/user-session.conf", targets)
        self.assertNotIn("/etc/systemd/system/psy-audio-priority.service", targets)
        self.assertFalse(any("SOUL.md" in t or "/skills/" in t or t.endswith("instance.env") for t in targets))


class RedundantDropins(unittest.TestCase):
    UNIT = ("[Unit]\nAfter=network-online.target\n[Service]\nUser=psy-deliver\n"
            "LoadCredential=recording-signing:/etc/psy-upload/recording-signing\n"
            "EnvironmentFile=/etc/psy/instance.env\nReadWritePaths=/srv/psy-queue /var/lib/psy-recording-receipts\n")

    def test_settings_already_in_the_unit(self):
        self.assertTrue(live.redundant("[Service]\nEnvironmentFile=/etc/psy/instance.env\n", self.UNIT))
        self.assertTrue(live.redundant("[Service]\nReadWritePaths=/var/lib/psy-recording-receipts\n"
                                       "LoadCredential=recording-signing:/etc/psy-upload/recording-signing\n", self.UNIT))

    def test_local_overrides_are_kept(self):
        self.assertFalse(live.redundant("[Service]\nUser=psy-upload\n", self.UNIT))
        self.assertFalse(live.redundant("[Unit]\nUser=psy-deliver\n", self.UNIT))  # other section
        self.assertFalse(live.redundant("[Service]\nMemoryMax=1G\n", self.UNIT))
        self.assertFalse(live.redundant("# only a comment\n", self.UNIT))

    def test_retired_only_beside_shipped_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            systemd = Path(tmp)
            (systemd / "psy-upload.service.d").mkdir()
            (systemd / "psy-upload.service.d/public-origin.conf").write_text("[Service]\nEnvironmentFile=/etc/psy/instance.env\n")
            (systemd / "psy-upload.service.d/limits.conf").write_text("[Service]\nMemoryMax=1G\n")
            (systemd / "psy-backup.service.d").mkdir()
            (systemd / "psy-backup.service.d/public-origin.conf").write_text("[Service]\nEnvironmentFile=/etc/psy/instance.env\n")
            unit = live.Entry(systemd / "psy-upload.service", self.UNIT.encode(), "ops/systemd/psy-upload.service")
            retire, kept = live.retired_dropins([unit], systemd)
            self.assertEqual([p.name for p in retire], ["public-origin.conf"])
            self.assertEqual([p.name for p in kept], ["limits.conf"])


class BackupAndRestore(unittest.TestCase):
    def test_restore_returns_changed_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / "srv/a.py", root / "srv/b.py"
            old.parent.mkdir()
            old.write_text("before")
            os.chmod(old, 0o640)
            with patch.object(live, "BACKUPS", root / "backups"):
                backup = live.Backup("test")
                backup.keep(old)
                backup.keep(new)
                old.write_text("after")
                new.write_text("added")
                backup.restore()
            self.assertEqual(old.read_text(), "before")
            self.assertEqual(old.stat().st_mode & 0o777, 0o640)
            self.assertFalse(new.exists())

    def test_plan_lists_only_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            same, changed, missing = (Path(tmp) / n for n in ("same", "changed", "missing"))
            same.write_bytes(b"x")
            changed.write_bytes(b"old")
            entries = [live.Entry(same, b"x", "s"), live.Entry(changed, b"new", "c"), live.Entry(missing, b"m", "m")]
            self.assertEqual([(k, e.target.name) for k, e in live.plan(entries)], [("update", "changed"), ("new", "missing")])


if __name__ == "__main__":
    unittest.main()
