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
        # Every unit and timer the two installers stage, plus their code, comes from the inventory.
        backup_script = (live.REPO / "ops/install-backup").read_text()
        for name in live.INSTALLED:
            self.assertIn(f"/etc/systemd/system/{name}", targets)
        for path in ("/etc/needrestart/conf.d/50-psy.conf", "/usr/local/sbin/psy-backup",
                     "/usr/local/lib/psy-backup/sqlite_snapshot.py", "/usr/local/lib/psy-backup/verify_backup.py"):
            self.assertIn(path, targets)
            self.assertIn(path.rsplit("/", 1)[0], script + backup_script)
        self.assertIn("/etc/systemd/system/psy-restart-outdated.timer", targets)
        self.assertNotIn("/etc/psy-backup/destination.env", targets)

    def test_owner_alerts_use_one_instance_per_failing_unit(self):
        entries = {str(e.target): e.data for e in live.system_entries(997)}
        alerted = [n for n, u in live.INSTALLED.items() if u.get("alert")]
        self.assertIn("psy-backup.service", alerted)
        for name in alerted:
            self.assertEqual(entries[f"/etc/systemd/system/{name}.d/owner-alert.conf"],
                             b"[Unit]\nOnFailure=psy-alert@%n.service\n")

    def test_only_new_runtime_timers_are_enabled(self):
        systemd = live.SYSTEMD
        self.assertEqual(live.new_timers([systemd / "psy-restart-outdated.timer", systemd / "psy-backup.timer",
                                          systemd / "psy-alerts.service"]), ["psy-restart-outdated.timer"])


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


class OwnerSide(unittest.TestCase):
    """The checkout is written through owner_io; links planted there must not redirect anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        (self.checkout / "pkg").mkdir(parents=True)
        self.outside = self.root / "outside.py"
        self.outside.write_text("root-owned original")

    def tearDown(self):
        self.tmp.cleanup()

    def io(self, action, payload):
        return live.owner_io(action, payload, root=self.checkout, user=None)

    def write(self, path, data, mode=0o644):
        import base64
        return self.io("write", {"files": [{"path": str(path), "mode": mode, "data": base64.b64encode(data).decode()}]})

    def test_old_temporary_name_planted_as_link_is_not_followed(self):
        target = self.checkout / "pkg/auth.py"
        target.write_text("old")
        os.symlink(self.outside, self.checkout / "pkg/auth.py.psy-new")
        self.write(target, b"new")
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(self.outside.read_text(), "root-owned original")

    def test_link_at_target_or_in_a_parent_is_refused(self):
        os.symlink(self.outside, self.checkout / "pkg/linked.py")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.write(self.checkout / "pkg/linked.py", b"x")
        with self.assertRaisesRegex(ValueError, "refused"):
            self.io("read", {"paths": [str(self.checkout / "pkg/linked.py")]})
        os.symlink(self.root, self.checkout / "redirect")
        with self.assertRaisesRegex(ValueError, "not a plain directory"):
            self.write(self.checkout / "redirect/outside.py", b"x")
        with self.assertRaisesRegex(ValueError, "outside the checkout"):
            self.write(self.checkout / "../outside.py", b"x")
        self.assertEqual(self.outside.read_text(), "root-owned original")

    def test_write_keeps_mode_and_read_reports_missing(self):
        target = self.checkout / "pkg/kept.py"
        target.write_text("old")
        os.chmod(target, 0o600)
        self.write(target, b"new", mode=0o644)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        found = self.io("read", {"paths": [str(target), str(self.checkout / "pkg/none.py")]})
        self.assertIsNone(found[str(self.checkout / "pkg/none.py")])
        self.assertEqual(found[str(target)]["mode"], 0o600)


class RootSide(unittest.TestCase):
    def test_link_at_root_target_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside, target = Path(tmp) / "outside", Path(tmp) / "unit.service"
            outside.write_text("keep")
            os.symlink(outside, target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                live.write(target, b"x", "root", 0o644)
            self.assertEqual(outside.read_text(), "keep")

    def test_directory_others_can_change_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared"
            shared.mkdir()
            os.chmod(shared, 0o777)
            with self.assertRaisesRegex(ValueError, "another account"):
                live.write_plain(shared / "f", b"x", os.geteuid(), os.getegid(), 0o644)


class BackupAndRestore(unittest.TestCase):
    def test_restore_returns_changed_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "checkout"
            (checkout / "pkg").mkdir(parents=True)
            old, new = root / "srv/a.py", root / "srv/b.py"
            mine, added = checkout / "pkg/c.py", checkout / "pkg/d.py"
            old.parent.mkdir()
            old.write_text("before")
            os.chmod(old, 0o640)
            mine.write_text("checkout before")
            os.chmod(mine, 0o600)
            with patch.object(live, "BACKUPS", root / "backups"):
                backup = live.Backup("test", checkout=checkout, owner=None)
                backup.keep([old, new, mine, added])
                backup.note({"psy-upload"}, True)
                old.write_text("after")
                new.write_text("added")
                mine.write_text("checkout after")
                added.write_text("new in checkout")
                # A later `rollback` reads the same index from disk.
                again = live.Backup(root=backup.root, checkout=checkout, owner=None)
                self.assertEqual(again.index["units"], ["psy-upload"])
                again.restore()
            self.assertEqual(old.read_text(), "before")
            self.assertEqual(old.stat().st_mode & 0o777, 0o640)
            self.assertFalse(new.exists())
            self.assertEqual(mine.read_text(), "checkout before")
            self.assertEqual(mine.stat().st_mode & 0o777, 0o600)
            self.assertFalse(added.exists())

    def test_plan_lists_only_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            same, changed, missing = (Path(tmp) / n for n in ("same", "changed", "missing"))
            same.write_bytes(b"x")
            changed.write_bytes(b"old")
            entries = [live.Entry(same, b"x", "s"), live.Entry(changed, b"new", "c"), live.Entry(missing, b"m", "m")]
            self.assertEqual([(k, e.target.name) for k, e in live.plan(entries)], [("update", "changed"), ("new", "missing")])


if __name__ == "__main__":
    unittest.main()
