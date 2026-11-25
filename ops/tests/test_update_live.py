import contextlib
import io
import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        # The shared module goes first, where install-runtime puts it, and its digest is in the release.
        entries = live.system_entries(997)
        self.assertEqual((str(entries[0].target), entries[0].source),
                         ("/usr/local/lib/psy-common/psy_common.py", "common/psy_common.py"))
        self.assertIn('"$repo/common/psy_common.py" /usr/local/lib/psy-common/psy_common.py', script)
        self.assertEqual(live.release_sums()[str(entries[0].target)], live.sha256(entries[0].data))

    def test_shared_module_restarts_the_services_that_load_it(self):
        self.assertEqual(live.units_for(live.COMMON / "psy_common.py"),
                         {"psy-upload", "psy-upload-worker", "hermes-gateway"})

    def test_platform_bridge_restarts_through_its_socket(self):
        socket = "psy-platform-bridge.socket"
        self.assertEqual(live.units_for(live.CHECKOUT / "hermes_cli/psy_platform_bridge.py"), {"hermes-gateway", socket})
        self.assertEqual(live.units_for(live.CHECKOUT / "hermes_cli/auth.py"), {"hermes-gateway", socket})
        self.assertEqual(live.units_for(live.SYSTEMD / "psy-platform-bridge.service"), {socket})
        self.assertEqual(live.units_for(live.SYSTEMD / socket), {socket})
        targets = {str(e.target) for e in live.system_entries(997)}
        self.assertIn("/etc/systemd/system/psy-platform-bridge.service", targets)
        self.assertNotIn("/etc/systemd/system/psy-platform-bridge.service.d/owner-alert.conf", targets)

    def test_owner_alerts_use_one_instance_per_failing_unit(self):
        entries = {str(e.target): e.data for e in live.system_entries(997)}
        alerted = [n for n, u in live.INSTALLED.items() if u.get("alert")]
        self.assertIn("psy-backup.service", alerted)
        for name in alerted:
            self.assertEqual(entries[f"/etc/systemd/system/{name}.d/owner-alert.conf"],
                             b"[Unit]\nOnFailure=psy-alert@%n.service\n")

    def test_enabled_backup_needs_its_destination_file(self):
        timer = lambda code: (lambda *cmd, **kw: subprocess.CompletedProcess(cmd, code, "", ""))
        with patch.object(live, "read_plain", return_value=None):
            with patch.object(live, "run", side_effect=timer(0)):     # psy-backup.timer enabled
                with self.assertRaisesRegex(ValueError, "destination.env"):
                    live.check_backup_config()
            with patch.object(live, "run", side_effect=timer(1)):     # no backup configured here
                live.check_backup_config()

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

    def test_new_code_folder_is_readable_by_the_services_under_any_umask(self):
        import pwd
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "lib/psy-common/psy_common.py"
            target.parent.parent.mkdir()
            previous = os.umask(0o077)
            try:
                live.write(target, b"code", pwd.getpwuid(os.geteuid()).pw_name, 0o644)
            finally:
                os.umask(previous)
            self.assertEqual((target.parent.stat().st_mode & 0o777, target.stat().st_mode & 0o777), (0o755, 0o644))

    def test_plan_lists_only_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            same, changed, missing = (Path(tmp) / n for n in ("same", "changed", "missing"))
            same.write_bytes(b"x")
            changed.write_bytes(b"old")
            entries = [live.Entry(same, b"x", "s"), live.Entry(changed, b"new", "c"), live.Entry(missing, b"m", "m")]
            self.assertEqual([(k, e.target.name) for k, e in live.plan(entries)], [("update", "changed"), ("new", "missing")])


class DeploymentTransaction(unittest.TestCase):
    """Inject host failures into main while keeping all bytes in a synthetic local host."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.code = self.root / "installed.py"
        self.code.write_bytes(b"previous code")
        self.record = self.root / "deployed"
        self.previous = b"commit=previous\nbackup=/older/backup\n"
        self.record.write_bytes(self.previous)
        self.backups = self.root / "backups"
        self.output = io.StringIO()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        mocked = {
            "BACKUPS": self.backups, "RECORD": self.record,
            "release_sums": lambda: {}, "check_group_manifest": lambda sums: None,
            "system_entries": lambda uid: [live.Entry(self.code, b"candidate code", "fixture.py")],
            "gateway_entries": lambda sums: [], "check_instance": lambda: None,
            "check_backup_config": lambda: None, "retired_dropins": lambda entries: ([], []),
            "units_for": lambda path: {"hermes-gateway"}, "busy": lambda: [],
            "snapshot_databases": lambda backup: [],
            "check_root_dir": lambda path: self.assertTrue(Path(path).is_relative_to(self.root)),
        }
        for name, value in mocked.items():
            self.stack.enter_context(patch.object(live, name, value))
        self.stack.enter_context(patch.object(live.os, "geteuid", return_value=0))
        self.stack.enter_context(patch.object(live.os, "fchown"))
        self.stack.enter_context(patch.object(pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=1)))
        self.stack.enter_context(patch.object(live.signal, "signal"))
        self.run = self.stack.enter_context(patch.object(live, "run", side_effect=self.fake_run))
        self.restart = self.stack.enter_context(patch.object(live, "restart", return_value=[]))

    @staticmethod
    def fake_run(*cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def main(self, *arguments):
        with patch.object(sys, "argv", ["update_live.py", *arguments]), patch.object(sys, "stdout", self.output):
            try:
                return live.main()
            finally:
                if isinstance(sys.stdout, live.Log) and sys.stdout.file:
                    sys.stdout.file.close()

    def backup_path(self):
        return next(self.backups.iterdir())

    def assert_previous(self):
        self.assertEqual(self.code.read_bytes(), b"previous code")
        self.assertEqual(self.record.read_bytes(), self.previous)

    def test_preflight_timeout_restores_installed_bytes_and_deployment_record(self):
        def run(*cmd, **kwargs):
            if "preflight.py" in str(cmd):
                self.assertEqual(self.code.read_bytes(), b"candidate code")
                raise subprocess.TimeoutExpired(cmd, 120)
            return self.fake_run(*cmd, **kwargs)
        self.run.side_effect = run
        with self.assertRaisesRegex(SystemExit, "^rolled back$"):
            self.main("apply", "--commit", "candidate")
        self.assert_previous()
        self.restart.assert_called_once_with({"hermes-gateway"})

    def test_restart_timeout_restores_files_before_restarting_previous_code(self):
        def restart(units):
            if self.restart.call_count == 1:
                self.assertEqual(self.code.read_bytes(), b"candidate code")
                raise subprocess.TimeoutExpired(["systemctl", "restart"], 120)
            self.assert_previous()
            return []
        self.restart.side_effect = restart
        with self.assertRaisesRegex(SystemExit, "^rolled back$"):
            self.main("apply", "--commit", "candidate")
        self.assert_previous()
        self.assertEqual(self.restart.call_count, 2)

    def fail_marker(self, *, after_replace, broken_log=False):
        original = live.write_plain
        class BrokenLog:
            def __init__(self, handle):
                self.handle = handle
            def write(self, text):
                raise OSError("injected log failure")
            def close(self):
                self.handle.close()
        def write(path, data, uid, gid, mode):
            if Path(path) == self.record and data.startswith(b"commit=candidate\n"):
                self.assertEqual(self.code.read_bytes(), b"candidate code")
                if after_replace:
                    original(path, data, uid, gid, mode)
                if broken_log:
                    sys.stdout.file = BrokenLog(sys.stdout.file)
                raise OSError("injected deployment marker failure")
            return original(path, data, uid, gid, mode)
        with patch.object(live, "write_plain", side_effect=write):
            with self.assertRaisesRegex(SystemExit, "^rolled back$"):
                self.main("apply", "--commit", "candidate")
        self.assert_previous()
        self.assertEqual(self.restart.call_count, 2)

    def test_marker_write_failure_rolls_back_already_restarted_release(self):
        self.fail_marker(after_replace=False)

    def test_marker_failure_after_replace_restores_saved_predecessor(self):
        self.fail_marker(after_replace=True)

    def test_marker_and_log_failure_do_not_prevent_rollback(self):
        self.fail_marker(after_replace=False, broken_log=True)

    def test_manual_rollback_returns_success_and_restores_exact_previous_marker(self):
        self.assertIsNone(self.main("apply", "--commit", "candidate"))
        backup = self.backup_path()
        marker = self.record.read_text()
        self.assertIn("commit=candidate\n", marker)
        self.assertIn(f"backup={backup}\n", marker)
        index = json.loads((backup / "backup.json").read_text())
        saved = next(item for item in index["saved"] if item["target"] == str(self.record))
        self.assertEqual((backup / saved["copy"]).read_bytes(), self.previous)
        self.assertIsNone(self.main("rollback", str(backup)))
        self.assert_previous()
        self.assertIn(f"Restored {backup}.", self.output.getvalue())

    def test_first_managed_release_rollback_restores_absent_marker(self):
        self.record.unlink()
        self.main("apply", "--commit", "candidate")
        self.assertTrue(self.record.exists())
        self.assertIsNone(self.main("rollback", str(self.backup_path())))
        self.assertEqual(self.code.read_bytes(), b"previous code")
        self.assertFalse(self.record.exists())

    def test_legacy_backup_reports_unknown_commit_instead_of_retaining_reverted_commit(self):
        backup = live.Backup("legacy")
        backup.keep([self.code])  # Historical backups did not include /etc/psy/deployed.
        backup.note({"hermes-gateway"}, False)
        self.code.write_bytes(b"candidate code")
        self.record.write_text(f"commit=candidate\nbackup={backup.root}\n")
        self.assertIsNone(self.main("rollback", str(backup.root)))
        self.assertEqual(self.code.read_bytes(), b"previous code")
        self.assertEqual(self.record.read_text(), f"commit=unknown\nrestored_from={backup.root}\n")
        self.assertIn("restored commit is unknown", self.output.getvalue())

    def test_failure_during_rollback_is_reported_without_claiming_success(self):
        self.restart.side_effect = subprocess.TimeoutExpired(["systemctl", "restart"], 120)
        with self.assertRaisesRegex(SystemExit, "^rollback incomplete:"):
            self.main("apply", "--commit", "candidate")
        self.assert_previous()
        self.assertEqual(self.restart.call_count, 2)
        self.assertNotIn("Updated to", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
