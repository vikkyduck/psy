"""Baseline uses the live gateway's interpreter, never executes it as root, and refuses drift."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capture_state as capture


class LiveIdentity(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.uid = os.getuid()
        self.pid = 42
        process = self.root / str(self.pid)
        process.mkdir()
        (process / "status").write_text("Name:\tpython3.14\nUid:\t" + "\t".join([str(self.uid)] * 4) + "\n")
        (process / "stat").write_text("42 (python worker) S " + " ".join(["0"] * 18 + ["123456", "0"]))
        self.executable = self.root / "python3.14"
        self.executable.write_bytes(b"Synthetic interpreter, never executed")
        self.executable.chmod(0o700)
        (process / "exe").symlink_to(self.executable)
        self.shown = ("MainPID=42\nUser=hermes\nActiveState=active\nSubState=running\n"
                      "FragmentPath=/etc/systemd/system/hermes-gateway.service\n"
                      "InvocationID=" + "a" * 32 + "\nExecMainStartTimestampMonotonic=98765\n")
        self.enterContext(patch.object(capture, "PROC", self.root))

    def state(self):
        with patch.object(capture, "run", return_value=NS(stdout=self.shown)):
            return capture.service_state(self.uid)

    def test_live_proc_interpreter_uid_start_time_and_unit_are_recorded(self):
        value = self.state()
        self.assertEqual(value["main_pid"], 42)
        self.assertEqual(value["executable"], str(self.executable))
        self.assertEqual(value["uid"], self.uid)
        self.assertEqual(value["start_ticks"], 123456)
        self.assertEqual(value["unit"], "hermes-gateway.service")
        self.assertEqual(value["invocation_id"], "a" * 32)

    def test_wrong_process_uid_is_refused(self):
        (self.root / "42/status").write_text("Uid:\t0\t0\t0\t0\n" if self.uid else "Uid:\t997\t997\t997\t997\n")
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.state()

    def test_nonrunning_or_deleted_interpreter_is_refused(self):
        self.shown = self.shown.replace("SubState=running", "SubState=dead")
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.state()
        self.shown = self.shown.replace("SubState=dead", "SubState=running")
        with patch.object(capture.os, "readlink", return_value=str(self.executable) + " (deleted)"):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.state()

    def test_user_writable_interpreter_is_launched_only_through_runuser(self):
        identity = self.state()
        account_uid = self.uid or 997
        identity["uid"] = account_uid
        profile = {"snapshot_id": "b" * 64, "snapshot_verified": True,
                   "effective": {"model": "gpt-6.1-sol", "provider": "openai-codex"},
                   "capture_runtime": {"python": [3, 14, 0], "executable": str(self.executable),
                                       "uid": account_uid, "effective_uid": account_uid}}
        calls = []
        def command(argv, **options):
            calls.append((argv, options))
            if argv[0] == "runuser":
                self.assertEqual(argv, ["runuser", "-u", "hermes", "--", str(self.executable), "-I", "-"])
                return NS(stdout=json.dumps(profile))
            self.assertEqual(argv, ["systemctl", "cat", capture.UNIT])
            return NS(stdout=b"[Service]\nEnvironment=SECRET_EXAMPLE=private-value\n")
        with patch.object(capture.os, "geteuid", return_value=0), \
                patch.object(capture.pwd, "getpwnam", return_value=NS(pw_uid=account_uid)), \
                patch.object(capture, "service_state", return_value=identity) as verify, \
                patch.object(capture, "run", side_effect=command), \
                patch.object(capture, "SNAPSHOTS", self.root), patch.object(capture, "ROOT_FILES", ()):
            report = capture.capture()
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(report["gateway_runtime"], identity)
        self.assertEqual(report["profile"]["capture_runtime"]["python"], [3, 14, 0])
        self.assertNotIn("private-value", json.dumps(report))
        self.assertTrue(all(argv[0] != str(self.executable) for argv, _ in calls))
        saved = Path(report["root_snapshot"])
        self.assertEqual(stat.S_IMODE((saved / "manifest.json").stat().st_mode), 0o600)
        self.assertIn(b"private-value", (saved / "hermes-gateway.unit").read_bytes())

    def test_pid_or_identity_change_refuses_verified_report(self):
        identity = self.state()
        account_uid = self.uid or 997
        identity["uid"] = account_uid
        profile = {"snapshot_verified": True, "capture_runtime": {"executable": str(self.executable),
                    "uid": account_uid, "effective_uid": account_uid}}
        for changed in ({**identity, "main_pid": 43}, {**identity, "start_ticks": 123457}):
            with self.subTest(changed=changed["main_pid"]), patch.object(capture.os, "geteuid", return_value=0), \
                    patch.object(capture.pwd, "getpwnam", return_value=NS(pw_uid=account_uid)), \
                    patch.object(capture, "service_state", side_effect=[identity, changed]), \
                    patch.object(capture, "run", return_value=NS(stdout=json.dumps(profile))), \
                    patch.object(capture, "SNAPSHOTS", self.root):
                with self.assertRaisesRegex(RuntimeError, "Gateway changed"):
                    capture.capture()
        self.assertEqual(list(self.root.glob("baseline-*")), [])


class HelperSafety(unittest.TestCase):
    def test_root_alias_account_is_rejected_before_launch(self):
        with patch.object(capture.os, "geteuid", return_value=0), \
                patch.object(capture.pwd, "getpwnam", return_value=NS(pw_uid=0)), \
                patch.object(capture, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "non-root"):
                capture.capture()
        run.assert_not_called()

    def test_helper_failure_does_not_expose_stderr_stdout_or_credentials(self):
        error = subprocess.CalledProcessError(1, ["runuser"], output="private stdout", stderr="private credential")
        with patch.object(capture.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                capture.run(["runuser"])
        self.assertEqual(str(caught.exception), "Baseline command failed")

    def test_configured_model_is_used_and_extra_config_is_not_printed(self):
        pm = ModuleType("pm")
        pm.__path__ = []
        environments = ModuleType("pm.environments")
        environments.committed_venv = lambda checkout: Path("/synthetic/venv")
        environments.site_packages = lambda environment: environment / "site-packages"
        environments.venv_python_version = lambda environment: tuple(sys.version_info[:2])
        cli = ModuleType("hermes_cli")
        cli.__path__ = []
        config = ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {"model": {"default": "gpt-6.1-sol", "provider": "openai-codex"},
                                              "api_key": "private credential"}
        implementation = ('import json\ndef capture_runtime(model, **options):\n'
                          '    return {"model": model, "policy": options["policy"]}\n')
        output = io.StringIO()
        with patch.dict(sys.modules, {"pm": pm, "pm.environments": environments, "hermes_cli": cli,
                                      "hermes_cli.config": config}), patch.dict(os.environ), \
                patch.object(sys, "path", list(sys.path)), contextlib.redirect_stdout(output):
            exec(compile(capture.profile_code(implementation, os.getuid()), "synthetic-helper", "exec"), {})
        result = json.loads(output.getvalue())
        self.assertEqual(result["model"], "gpt-6.1-sol")
        self.assertEqual(result["policy"]["model_source"], "profile_config")
        self.assertEqual(result["capture_runtime"]["dependency_source"], "committed_venv")
        self.assertNotIn("private credential", output.getvalue())
        self.assertNotIn("see-profile", output.getvalue())


if __name__ == "__main__":
    unittest.main()
