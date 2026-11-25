"""Fast guards for the real quality harness. No credentials, network or model calls."""
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import group_example
import quality_compare as quality


class QualityNetworkTests(unittest.TestCase):
    def test_codex_is_allowed_but_no_telegram_loopback_or_undeclared_source(self):
        guard = quality.QualityNetworkGuard(["example.org"])
        guard.allowed_url("https://chatgpt.com/backend-api/codex/responses")
        guard.allowed_url("https://example.org/original.html")
        self.assertEqual(guard.codex_http, 1)
        for url in ("https://api.telegram.org/bot/sendMessage", "http://127.0.0.1:43210/",
                    "https://elsewhere.example/source", "http://example.org/source",
                    "https://example.org:8443/source", "https://secret@example.org/source"):
            with self.subTest(url=url), self.assertRaises(group_example.IsolationViolation):
                guard.allowed_url(url)

    def test_explicit_source_list_cannot_enable_telegram_or_local_hosts(self):
        for host in ("api.telegram.org", "telegram.org", "t.me", "sub.t.me", "localhost",
                     "127.0.0.1", "192.168.1.1", "source.internal", "source.local",
                     "https://example.org", "example.org:443", "example..org"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                quality.source_host(host)
        self.assertEqual(quality.source_host(" EXAMPLE.ORG. "), "example.org")

    def test_allowlisted_hostname_must_resolve_to_public_addresses(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch.object(socket, "getaddrinfo", return_value=private):
            guard = quality.QualityNetworkGuard(["example.org"])
            with self.assertRaises(group_example.IsolationViolation):
                guard.real_getaddrinfo("example.org", 443)
        self.assertEqual(guard.denied, ["non_public_peer"])

    def test_mixed_public_private_dns_answer_is_rejected(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
                     for host in ("93.184.216.34", "10.0.0.1")]
        with patch.object(socket, "getaddrinfo", return_value=addresses):
            guard = quality.QualityNetworkGuard(["example.org"])
            with self.assertRaises(group_example.IsolationViolation):
                guard.real_getaddrinfo("example.org", 443)


class SourceRestrictionTests(unittest.TestCase):
    def fixture(self, payload):
        class ProductionConnection:
            def __init__(self, host, *args, **kwargs):
                self.host = host
        return SimpleNamespace(PublicConnection=ProductionConnection, retrieve=lambda url: payload)

    def test_declared_html_uses_production_retrieval_and_keeps_payload(self):
        body = ("text/html", "utf-8", b"<p>synthetic primary excerpt</p>")
        module = self.fixture(body)
        original = module.retrieve
        with ExitStack() as stack:
            exclusions = quality.restrict_source_reader(module, {"example.org"}, stack)
            self.assertEqual(module.PublicConnection("example.org").host, "example.org")
            self.assertEqual(module.retrieve("https://example.org/work"), body)
            self.assertEqual(exclusions, {"host": 0, "pdf": 0})
        self.assertIs(module.retrieve, original)

    def test_redirect_to_unlisted_host_is_stopped_before_connection(self):
        module = self.fixture(("text/html", "utf-8", b"synthetic"))
        with ExitStack() as stack:
            exclusions = quality.restrict_source_reader(module, {"example.org"}, stack)
            with self.assertRaises(ValueError):
                module.PublicConnection("redirected.example")
            self.assertEqual(exclusions["host"], 1)

    def test_pdf_never_reaches_child_process(self):
        for payload in (("application/pdf", "utf-8", b"document"),
                        ("application/octet-stream", "utf-8", b"%PDF-synthetic")):
            module = self.fixture(payload)
            with self.subTest(payload=payload), ExitStack() as stack:
                exclusions = quality.restrict_source_reader(module, {"example.org"}, stack)
                with self.assertRaises(ValueError):
                    module.retrieve("https://example.org/work")
                self.assertEqual(exclusions["pdf"], 1)

    def test_opted_in_pdf_retains_the_production_payload(self):
        body = ("application/pdf", "utf-8", b"%PDF-synthetic")
        module = self.fixture(body)
        with ExitStack() as stack:
            exclusions = quality.restrict_source_reader(module, {"example.org"}, stack, allow_pdf=True)
            self.assertEqual(module.retrieve("https://example.org/work"), body)
            self.assertEqual(exclusions, {"host": 0, "pdf": 0})

    def test_all_public_mode_authorizes_only_hosts_requested_by_the_reader(self):
        guard = quality.QualityNetworkGuard([], all_public_sources=True)
        module = self.fixture(("text/html", "utf-8", b"synthetic"))
        self.assertNotIn("primary.example.org", guard.HOSTS)
        with self.assertRaises(group_example.IsolationViolation):
            guard.allowed_url("https://primary.example.org/work")
        with ExitStack() as stack:
            excluded = quality.restrict_source_reader(module, guard.source_hosts, stack,
                authorize_source=guard.authorize_source)
            connection = module.PublicConnection("primary.example.org")
            self.assertEqual(connection.host, "primary.example.org")
            guard.allowed_url("https://primary.example.org/work")
            guard.allowed_url("https://chatgpt.com/backend-api/codex/responses")
            self.assertEqual(guard.dynamic_source_hosts, {"primary.example.org"})
            self.assertEqual(excluded, {"host": 0, "pdf": 0})
            for url in ("https://unvisited.example.org/work", "https://sub.primary.example.org/work"):
                with self.assertRaises(group_example.IsolationViolation):
                    guard.allowed_url(url)

    def test_all_public_reader_cannot_authorize_telegram_local_names_or_ip_literals(self):
        guard = quality.QualityNetworkGuard([], all_public_sources=True)
        module = self.fixture(("text/html", "utf-8", b"synthetic"))
        with ExitStack() as stack:
            excluded = quality.restrict_source_reader(module, guard.source_hosts, stack,
                authorize_source=guard.authorize_source)
            hosts = ("api.telegram.org", "t.me", "localhost", "archive.internal", "archive.local",
                     "127.0.0.1", "93.184.216.34", "::1", "private@public.example.org")
            for host in hosts:
                with self.subTest(host=host), self.assertRaises(ValueError):
                    module.PublicConnection(host)
            self.assertEqual(excluded["host"], len(hosts))
            self.assertEqual(guard.dynamic_source_hosts, set())
            self.assertEqual(guard.HOSTS, {"chatgpt.com", "auth.openai.com"})

    def test_dynamic_source_still_rejects_private_or_mixed_dns_answers(self):
        for addresses in (("127.0.0.1",), ("93.184.216.34", "10.0.0.1")):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443)) for host in addresses]
            with self.subTest(addresses=addresses), patch.object(socket, "getaddrinfo", return_value=rows):
                guard = quality.QualityNetworkGuard([], all_public_sources=True)
                module = self.fixture(("text/html", "utf-8", b"synthetic"))
                with ExitStack() as stack:
                    quality.restrict_source_reader(module, guard.source_hosts, stack,
                        authorize_source=guard.authorize_source)
                    module.PublicConnection("primary.example.org")
                    with self.assertRaises(group_example.IsolationViolation):
                        guard.real_getaddrinfo("primary.example.org", 443)
                self.assertEqual(guard.denied, ["non_public_peer"])

    def test_fixed_default_does_not_expand_hosts_even_with_authorizer_wired(self):
        guard = quality.QualityNetworkGuard(["primary.example.org"])
        module = self.fixture(("text/html", "utf-8", b"synthetic"))
        with ExitStack() as stack:
            quality.restrict_source_reader(module, guard.source_hosts, stack,
                authorize_source=guard.authorize_source)
            module.PublicConnection("primary.example.org")
            with self.assertRaises(ValueError):
                module.PublicConnection("different.example.org")
        self.assertEqual(guard.dynamic_source_hosts, set())
        self.assertNotIn("different.example.org", guard.HOSTS)

    def test_real_reader_preserves_https_credential_and_port_checks(self):
        path = Path(__file__).resolve().parents[2] / "gateway/plugins/platforms/telegram/psy_source_evidence.py"
        spec = importlib.util.spec_from_file_location("quality_dynamic_reader", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        guard = quality.QualityNetworkGuard([], all_public_sources=True)
        with ExitStack() as stack:
            quality.restrict_source_reader(module, guard.source_hosts, stack,
                authorize_source=guard.authorize_source)
            for url in ("http://primary.example.org/work", "https://secret@primary.example.org/work",
                        "https://primary.example.org:8443/work"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    module.retrieve(url)
        self.assertEqual(guard.dynamic_source_hosts, set())


class BootstrapIsolationTests(unittest.TestCase):
    def test_real_transport_import_suppresses_only_startup_maintenance(self):
        names = ("restore_interrupted_pull", "prepare_launch", "recover_if_needed", "activate_dependencies")
        originals = {name: Mock(side_effect=AssertionError("maintenance attempted")) for name in names}
        recovery = SimpleNamespace(**{name: originals[name] for name in names if "recover" in name or "restore" in name})
        launch = SimpleNamespace(prepare_launch=originals["prepare_launch"])
        environments = SimpleNamespace(activate_dependencies=originals["activate_dependencies"])
        transport = SimpleNamespace(enable_happy_eyeballs_on_client=lambda client: client)
        modules = {"hermes_cli._early_recovery": recovery, "hermes_cli.venv_sync": launch,
                   "pm.environments": environments, "agent.process_bootstrap": transport}

        def importing(name):
            if name == "agent.process_bootstrap":
                self.assertEqual(os.environ["HERMES_DISABLE_LAZY_INSTALLS"], "1")
                recovery.restore_interrupted_pull("synthetic")
                launch.prepare_launch("synthetic")
                recovery.recover_if_needed("synthetic")
                environments.activate_dependencies("synthetic")
            return modules[name]

        with patch.dict(sys.modules), patch.object(sys, "path", ["/synthetic/dependencies"]), \
                patch.dict(os.environ, {"HERMES_DISABLE_LAZY_INSTALLS": "previous"}), \
                patch.object(quality.importlib, "import_module", side_effect=importing):
            sys.modules.pop("hermes_bootstrap", None)
            sys.modules.pop("agent.process_bootstrap", None)
            result = quality.initialize_transport_without_startup(Path("/synthetic/dependencies"))
            self.assertEqual(result["suppressed_calls"], {name: 1 for name in names})
            self.assertEqual(os.environ["HERMES_DISABLE_LAZY_INSTALLS"], "previous")
        self.assertIs(recovery.restore_interrupted_pull, originals["restore_interrupted_pull"])
        self.assertIs(environments.activate_dependencies, originals["activate_dependencies"])
        for original in originals.values():
            original.assert_not_called()

    def test_preloaded_bootstrap_is_rejected(self):
        with patch.dict(sys.modules, {"hermes_bootstrap": SimpleNamespace()}):
            with self.assertRaises(group_example.IsolationViolation):
                quality.initialize_transport_without_startup(Path("/synthetic/dependencies"))


class PdfProcessGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psy-pdf-guards-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.reader = self.root / "psy_source_evidence.py"
        self.reader.write_text("# Synthetic decoder path. Never executed.\n")
        self.command = ["/usr/bin/python3", "-I", str(self.reader), "--pdf"]
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    @contextmanager
    def installed(self, *, enabled=True):
        hooks = []
        calls = []

        def original_run(argv, **kwargs):
            calls.append(list(argv))
            hooks[0]("subprocess.Popen", (argv[0], list(argv), kwargs.get("cwd"), kwargs.get("env")))
            kwargs["stdout"].write(b"synthetic page one\fsynthetic page two")
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(sys, "addaudithook", side_effect=hooks.append), \
                patch.object(subprocess, "run", side_effect=original_run), \
                tempfile.NamedTemporaryFile(dir=self.root) as output:
            audit = group_example.install_storage_audit(self.root, self.root / "home", self.root,
                pdf_reader=self.reader if enabled else None)
            # /proc is available on production Linux. The local guard test can
            # run on macOS without weakening production descriptor validation.
            original_readlink = os.readlink
            def readlink(path, *args, **kwargs):
                if str(path).startswith("/proc/self/fd/"):
                    return output.name + " (deleted)"
                return original_readlink(path, *args, **kwargs)
            with patch.object(os, "readlink", side_effect=readlink):
                kwargs = {"input": b"%PDF-synthetic", "stdout": output, "stderr": subprocess.DEVNULL,
                          "timeout": 32, "env": dict(self.env), "check": True}
                yield SimpleNamespace(audit=audit, hook=hooks[0], calls=calls, kwargs=kwargs)

    def test_exact_opted_in_production_command_is_allowed_once(self):
        with self.installed() as fixture:
            result = subprocess.run(self.command, **fixture.kwargs)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(fixture.audit, {"pdf_subprocesses": 1, "denials": 0})
            with self.assertRaises(group_example.IsolationViolation):
                fixture.hook("subprocess.Popen", (self.command[0], self.command, None, self.env))
            self.assertEqual(fixture.audit["denials"], 1)

    def test_existing_default_does_not_allow_pdf(self):
        with self.installed(enabled=False) as fixture:
            with self.assertRaises(group_example.IsolationViolation):
                subprocess.run(self.command, **fixture.kwargs)
            self.assertEqual(fixture.calls, [])

    def test_modified_command_or_unbounded_options_are_rejected(self):
        with self.installed() as fixture:
            changes = [{"input": b"not pdf"}, {"input": b"%PDF-" + b"x" * (8 * 1024 * 1024)},
                       {"timeout": 33}, {"check": False}, {"cwd": str(self.root)}, {"shell": True},
                       {"env": {**self.env, "PYTHONPATH": str(self.root)}}, {"stderr": subprocess.PIPE}]
            for change in changes:
                with self.subTest(change=next(iter(change))), self.assertRaises(group_example.IsolationViolation):
                    subprocess.run(self.command, **{**fixture.kwargs, **change})
            with self.assertRaises(group_example.IsolationViolation):
                subprocess.run(["/usr/bin/python3", "-c", "print(1)"], **fixture.kwargs)
            self.assertEqual(fixture.calls, [])

    def test_output_must_be_private_and_in_the_fixture(self):
        with self.installed() as fixture:
            os.fchmod(fixture.kwargs["stdout"].fileno(), 0o644)
            with self.assertRaises(group_example.IsolationViolation):
                subprocess.run(self.command, **fixture.kwargs)
            os.fchmod(fixture.kwargs["stdout"].fileno(), 0o600)
            with patch.object(os, "readlink", return_value="/outside/output"):
                with self.assertRaises(group_example.IsolationViolation):
                    subprocess.run(self.command, **fixture.kwargs)
            self.assertEqual(fixture.calls, [])

    def test_reader_changed_after_selection_is_rejected(self):
        with self.installed() as fixture:
            self.reader.write_text("# Changed after audit initialization.\n")
            with self.assertRaises(group_example.IsolationViolation):
                subprocess.run(self.command, **fixture.kwargs)
            self.assertEqual(fixture.calls, [])

    def test_actual_production_passages_invocation_matches_the_guard(self):
        self.reader = Path(__file__).resolve().parents[2] / "gateway/plugins/platforms/telegram/psy_source_evidence.py"
        self.command = ["/usr/bin/python3", "-I", str(self.reader), "--pdf"]
        spec = importlib.util.spec_from_file_location("quality_fixture_reader", self.reader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.installed() as fixture, \
                patch.object(module, "retrieve", return_value=("application/pdf", "utf-8", b"%PDF-synthetic")), \
                patch.object(module.tempfile, "TemporaryFile", return_value=fixture.kwargs["stdout"]):
            result = module.passages("https://example.org/synthetic.pdf")
            self.assertEqual(result, ("PDF", ["synthetic page one", "synthetic page two"]))
            self.assertEqual(fixture.audit, {"pdf_subprocesses": 1, "denials": 0})


class QualityOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psy-quality-guards-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_output_is_private_owned_and_outside_production(self):
        self.assertEqual(quality.private_output(self.root, (self.root / "production",)), self.root)
        with self.assertRaises(ValueError):
            quality.private_output(self.root, (self.root,))
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            quality.private_output(self.root, ())
        self.root.chmod(0o700)

    def test_existing_or_symlink_output_is_never_overwritten(self):
        target = self.root / "answer-random.txt"
        quality.write_private(target, "synthetic answer")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            quality.write_private(target, "changed")
        link = self.root / "alias.txt"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            quality.write_private(link, "changed")
        self.assertEqual(target.read_text(), "synthetic answer")

    def test_symlink_output_directory_is_not_accepted(self):
        link = self.root / "alias"
        link.symlink_to(self.root)
        with self.assertRaises(ValueError):
            quality.private_output(link, ())

    def test_cases_are_fixed_and_long_local_case_carries_only_synthetic_material(self):
        case = quality.CASES["lecture180_local"]
        self.assertIn("3 часа", case["question"])
        turns = quality.case_turns(case)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(len(turns[0]["content"]), 2)
        self.assertIn("полностью вымышленный", turns[0]["content"][1]["text"])
        self.assertNotIn("/home/hermes", str(turns))
        self.assertFalse(any("prompt_path" in case or "input_file" in case for case in quality.CASES.values()))

    def test_api_generation_state_does_not_reuse_pipeline_complete(self):
        self.assertIsNone(quality.generation_complete(SimpleNamespace(complete=True)))
        self.assertIsNone(quality.generation_complete(SimpleNamespace(complete=False, quality_metadata={})))
        for value in (True, False):
            for key in ("generation", "generation_audit"):
                result = SimpleNamespace(complete=not value, quality_metadata={key: {"generation_complete": value}})
                self.assertIs(quality.generation_complete(result), value)
        result = SimpleNamespace(quality_metadata={"generation_audit": {"generation_complete": False},
                                                    "generation": {"generation_complete": True}})
        self.assertIs(quality.generation_complete(result), False)
        result.quality_metadata["generation_audit"]["generation_complete"] = None
        self.assertIsNone(quality.generation_complete(result))

    def test_temporary_job_is_claimed_before_deep_admission(self):
        path = Path(__file__).resolve().parents[2] / "gateway/plugins/platforms/telegram/psy_group_store.py"
        spec = importlib.util.spec_from_file_location("quality_fixture_store", path)
        store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(store)
        checkpoints = quality.fixture_job(store, self.root)
        self.assertEqual(store.state(group_example.UPDATE, root=self.root), "running")
        checkpoints.admit_long()
        with store.database(self.root) as connection:
            self.assertEqual(connection.execute("SELECT long_job FROM schedule WHERE request_id=?",
                                               (group_example.UPDATE,)).fetchone(), (1,))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="psy-quality-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = self.root / "fixture"
        self.fixture.mkdir(mode=0o700)
        self.output = self.root / "output"
        self.output.mkdir(mode=0o700)
        (self.output / "recovery").mkdir(mode=0o700)
        self.modules = Path(__file__).resolve().parents[2] / "gateway/plugins/platforms/telegram"
        spec = importlib.util.spec_from_file_location("quality_recovery_store", self.modules / "psy_group_store.py")
        self.store = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.store)
        self.checkpoints = quality.fixture_job(self.store, self.fixture)
        self.args = SimpleNamespace(case="concepts", variant="candidate", module_dir=self.modules)

    def snapshot(self):
        self.checkpoints.put("model:effort:v1", {"silent_cuts": 2})
        self.checkpoints.put("model:synthetic-stage", {"text": "synthetic checkpoint", "complete": True,
            "generation_metadata": {"effort": "max", "generation_complete": True, "generated_at": 1000}})
        original = self.store.database
        with patch.object(self.store, "database", lambda *args, **kwargs: original(self.fixture)):
            path = quality.export_recovery(self.store, self.fixture, self.output, "answer-synthetic", self.args,
                                           "synthetic-model", [{"kind": "timeout"}], None)
        self.args.resume_state = Path(path)
        return Path(path)

    def restore(self):
        destination = self.root / "restored"
        destination.mkdir(mode=0o700)
        return destination, quality.restore_recovery(self.args, destination, self.output)

    def test_snapshot_restores_actual_effort_mark_and_stage_provenance(self):
        snapshot = self.snapshot()
        destination, provenance = self.restore()
        self.assertEqual(provenance["checkpoint_count"], 2)
        self.assertEqual(provenance["snapshot"], str(snapshot))
        restored = quality.fixture_job(self.store, destination, resumed=True)
        self.assertEqual(restored.get("model:effort:v1"), {"silent_cuts": 2})
        self.assertEqual(restored.get("model:synthetic-stage")["generation_metadata"]["effort"], "max")
        self.assertEqual(provenance["manifest"]["model_requested"], "synthetic-model")
        self.assertEqual((snapshot / "group.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_snapshot_wrong_variant_or_case_is_rejected(self):
        self.snapshot()
        self.args.variant = "baseline"
        with self.assertRaises(ValueError):
            self.restore()

    def test_snapshot_with_other_participant_is_rejected(self):
        snapshot = self.snapshot()
        with sqlite3.connect(snapshot / "group.sqlite3") as connection:
            connection.execute("UPDATE requests SET uid=?", (group_example.OTHER_MEMBER,))
        with self.assertRaises(ValueError):
            self.restore()

    def test_snapshot_with_business_history_is_rejected(self):
        snapshot = self.snapshot()
        with sqlite3.connect(snapshot / "group.sqlite3") as connection:
            connection.execute("INSERT INTO turns(uid,topic,prompt,answer) VALUES(?,1,'synthetic','synthetic')",
                               (group_example.MEMBER,))
        with self.assertRaises(ValueError):
            self.restore()

    def test_snapshot_with_trigger_is_rejected(self):
        snapshot = self.snapshot()
        with sqlite3.connect(snapshot / "group.sqlite3") as connection:
            connection.execute("CREATE TRIGGER synthetic_trigger AFTER INSERT ON checkpoints BEGIN SELECT 1; END")
        with self.assertRaises(ValueError):
            self.restore()

    def test_snapshot_with_public_file_or_wal_is_rejected(self):
        snapshot = self.snapshot()
        (snapshot / "group.sqlite3").chmod(0o644)
        with self.assertRaises(ValueError):
            self.restore()
        (self.root / "restored").rmdir()
        (snapshot / "group.sqlite3").chmod(0o600)
        (snapshot / "group.sqlite3-wal").write_bytes(b"synthetic sidecar")
        with self.assertRaises(ValueError):
            self.restore()

    def test_snapshot_outside_recovery_or_symlinked_database_is_rejected(self):
        snapshot = self.snapshot()
        self.args.resume_state = self.fixture
        with self.assertRaises(ValueError):
            self.restore()
        (self.root / "restored").rmdir()
        self.args.resume_state = snapshot
        (snapshot / "group.sqlite3").unlink()
        (snapshot / "group.sqlite3").symlink_to(self.fixture / "group.sqlite3")
        with self.assertRaises(OSError):
            self.restore()

    def test_retry_uses_same_checkpoints_and_production_delay(self):
        class ModelIssue(Exception):
            kind, stage, reset, retryable = "timeout", "stream_idle", None, True
        calls = []
        def respond(_turns, **kwargs):
            calls.append(kwargs["checkpoints"])
            if len(calls) == 1:
                kwargs["checkpoints"].put("model:effort:v1", {"silent_cuts": 2})
                raise ModelIssue()
            self.assertEqual(kwargs["checkpoints"].get("model:effort:v1"), {"silent_cuts": 2})
            return "synthetic success"
        codex = SimpleNamespace(ModelIssue=ModelIssue, respond=respond)
        clock = [1000.0]
        waits, history = [], []
        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds
        with patch.object(quality.time, "time", side_effect=lambda: clock[0]), \
                patch.object(quality.time, "sleep", side_effect=sleep), patch("builtins.print"):
            result = quality.compose_with_retries(codex, self.store, self.fixture, self.checkpoints,
                quality.CASES["concepts"], "synthetic-model", 3, history, lambda: "synthetic-snapshot")
        self.assertEqual(result, "synthetic success")
        self.assertEqual(calls, [self.checkpoints, self.checkpoints])
        self.assertEqual(sum(waits), 60)
        self.assertEqual(history[0]["retry_delay_seconds"], 60)
        self.assertEqual(self.store.state(group_example.UPDATE, root=self.fixture), "running")

    def test_retry_limit_preserves_queued_job_instead_of_looping(self):
        class ModelIssue(Exception):
            kind, stage, reset, retryable = "timeout", "stream_idle", None, True
        respond = Mock(side_effect=ModelIssue)
        codex = SimpleNamespace(ModelIssue=ModelIssue, respond=respond)
        clock, history = [1000.0], []
        with patch.object(quality.time, "time", side_effect=lambda: clock[0]), \
                patch.object(quality.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch("builtins.print"), self.assertRaises(ModelIssue):
            quality.compose_with_retries(codex, self.store, self.fixture, self.checkpoints,
                quality.CASES["concepts"], "synthetic-model", 3, history, lambda: "synthetic-snapshot")
        self.assertEqual(respond.call_count, 3)
        self.assertEqual(len(history), 3)
        self.assertEqual(self.store.state(group_example.UPDATE, root=self.fixture), "queued")

    def test_nonretryable_failure_is_exported_but_not_retried(self):
        class ModelIssue(Exception):
            kind, stage, reset, retryable = "context", "source_missing", None, False
        respond, snapshot = Mock(side_effect=ModelIssue), Mock(return_value="synthetic-snapshot")
        codex = SimpleNamespace(ModelIssue=ModelIssue, respond=respond)
        with patch("builtins.print"), self.assertRaises(ModelIssue):
            quality.compose_with_retries(codex, self.store, self.fixture, self.checkpoints,
                quality.CASES["concepts"], "synthetic-model", 3, [], snapshot)
        self.assertEqual(respond.call_count, 1)
        snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
