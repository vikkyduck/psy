"""Long scoped history and honest compaction, with synthetic messages only."""
import json
import hashlib
import os
from pathlib import Path
import stat
import sys
import sysconfig
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from psy_modules import psy_group_client as client, psy_group_context as context


def exchange(prompt, answer):
    return [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]


class LocalTokenizerBootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.release = Path(temporary.name).resolve()
        self.package = self.release / "venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        (self.package / "tiktoken").mkdir(parents=True)
        (self.package / "tiktoken" / "__init__.py").write_text("# Not imported by these tests\n")
        (self.release / "cache").mkdir()
        self.python = {"major": sys.version_info.major, "minor": sys.version_info.minor,
                       "implementation": sys.implementation.name, "soabi": sysconfig.get_config_var("SOABI")}
        (self.release / "manifest.json").write_text(json.dumps({"python": self.python}))

    def test_sidecar_appends_after_runtime_without_processing_pth_files(self):
        (self.package / "unsafe.pth").write_text("raise RuntimeError('must not execute')\n")
        with patch.object(context, "_sidecar_release", return_value=self.release), \
                patch.object(context, "_root_owned", return_value=True), patch.object(sys, "path", ["hermes", "dependencies"]), \
                patch.object(context.importlib, "invalidate_caches") as invalidated:
            self.assertTrue(context._attach_tokenizer_sidecar())
            self.assertEqual(sys.path, ["hermes", "dependencies", str(self.package)])
            self.assertTrue(context._attach_tokenizer_sidecar())
            self.assertEqual(sys.path.count(str(self.package)), 1)
            invalidated.assert_called_once()

    def test_mismatched_python_abi_never_enters_import_path(self):
        self.python["soabi"] = "incompatible-abi"
        (self.release / "manifest.json").write_text(json.dumps({"python": self.python}))
        with patch.object(context, "_sidecar_release", return_value=self.release), \
                patch.object(context, "_root_owned", return_value=True), patch.object(sys, "path", ["hermes"]):
            self.assertFalse(context._attach_tokenizer_sidecar())
            self.assertEqual(sys.path, ["hermes"])

    def test_explicit_cache_preference_including_disabled_cache_wins(self):
        for environment, expected in (({"TIKTOKEN_CACHE_DIR": "/explicit"}, Path("/explicit")),
                                      ({"TIKTOKEN_CACHE_DIR": ""}, None),
                                      ({"DATA_GYM_CACHE_DIR": "/legacy"}, Path("/legacy"))):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch.object(context, "_sidecar_release") as sidecar:
                self.assertEqual(context._tokenizer_cache(), expected)
                sidecar.assert_not_called()

    def test_verified_sidecar_cache_is_selected_without_overriding_explicit_settings(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(context, "_sidecar_release", return_value=self.release), \
                patch.object(context, "_root_owned", return_value=True):
            self.assertEqual(context._tokenizer_cache(), self.release / "cache")
            self.assertEqual(os.environ["TIKTOKEN_CACHE_DIR"], str(self.release / "cache"))

    def test_sidecar_entry_requires_root_owner_and_nonwritable_regular_files(self):
        for owner, mode, expected in ((0, stat.S_IFREG | 0o644, True), (501, stat.S_IFREG | 0o644, False),
                                      (0, stat.S_IFREG | 0o666, False), (0, stat.S_IFLNK | 0o777, False)):
            path = Mock()
            path.lstat.return_value = types.SimpleNamespace(st_uid=owner, st_mode=mode)
            with self.subTest(owner=owner, mode=mode):
                self.assertEqual(context._root_owned(path), expected)

    def test_current_pointer_must_resolve_to_an_immediate_immutable_release(self):
        root = self.release / "tokenizer"
        release = root / "releases" / "synthetic-lock-py312"
        release.mkdir(parents=True)
        pointer = root / "current"
        pointer.symlink_to(release)
        lstat = Path.lstat
        def root_stat(path):
            actual = lstat(path)
            return types.SimpleNamespace(st_uid=0, st_mode=actual.st_mode)
        with patch.object(context, "TOKENIZER_SIDECAR", root), patch.object(Path, "lstat", root_stat):
            self.assertEqual(context._sidecar_release(), release.resolve())
            pointer.unlink()
            pointer.symlink_to(self.release)
            self.assertIsNone(context._sidecar_release())

    def test_provenance_uses_loaded_release_and_runtime_regex_without_paths(self):
        root = self.release / "tokenizer"
        old = root / "releases" / "loaded-old"
        package = old / "venv/lib/python3.14/site-packages"
        (package / "tiktoken").mkdir(parents=True)
        module_file = package / "tiktoken/__init__.py"
        module_file.write_text("# synthetic loaded tokenizer")
        (old / "manifest.json").write_text('{"installed":"old"}')
        distribution = package / "tiktoken-0.12.0.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text("Name: tiktoken\nVersion: 0.12.0\n")
        runtime = self.release / "runtime"
        (runtime / "regex").mkdir(parents=True)
        regex_file = runtime / "regex/__init__.py"
        regex_file.write_text("# synthetic runtime regex")
        distribution = runtime / "regex-2025.1.1.dist-info"
        distribution.mkdir()
        (distribution / "METADATA").write_text("Name: regex\nVersion: 2025.1.1\n")
        modules = {"tiktoken": types.SimpleNamespace(__file__=str(module_file), __version__="0.12.0"),
                   "regex": types.SimpleNamespace(__file__=str(regex_file), __version__="2.5.1")}
        with patch.dict(sys.modules, modules), patch.object(context, "_encoding", return_value=object()), \
                patch.object(context, "TOKENIZER_SIDECAR", root), \
                patch.object(context, "_root_owned", return_value=True), \
                patch.object(context, "_sidecar_release", side_effect=AssertionError("Current release is not the loaded release")):
            actual = context.prepare_tokenizer()
        self.assertTrue(actual["available"])
        token = actual["packages"]["tiktoken"]
        self.assertEqual(token["distribution_version"], "0.12.0")
        self.assertEqual(token["source"], "root_owned_sidecar")
        self.assertEqual(token["sidecar_manifest_sha256"], hashlib.sha256((old / "manifest.json").read_bytes()).hexdigest())
        regex = actual["packages"]["regex"]
        self.assertEqual(regex["source"], "runtime")
        self.assertEqual(regex["distribution_version"], "2025.1.1")
        self.assertEqual(regex["module_version"], "2.5.1")
        self.assertNotIn(str(self.release), json.dumps(actual))


class ScopedContext(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(context, "_encoding", return_value=None))
        self.scope = [7, 3, 11]
        self.current = {"role": "user", "content": "Continue this task"}

    def prepare(self, history, call=None, **options):
        return context.prepare_context(history, self.current, call or Mock(), scope=self.scope,
                                       input_budget_tokens=options.pop("budget", 8000), **options)

    def test_answer_context_carries_actual_tokenizer_metadata(self):
        tokenizer = {"available": False, "estimator": "utf8_upper_bound",
                     "packages": {"regex": {"source": "runtime", "distribution_version": "synthetic"}}}
        with patch.object(context, "prepare_tokenizer", return_value=tokenizer):
            prepared = self.prepare([])
        self.assertEqual(prepared.metadata["tokenizer"], tokenizer)
        self.assertEqual(prepared.metadata["token_estimator"], tokenizer["estimator"])

    def test_full_eight_plus_turns_and_long_edits_remain_exact(self):
        history = exchange("Keep the table columns A and B", "Accepted")
        history += sum((exchange(f"unrelated clarification {i}", f"reply {i}") for i in range(9)), [])
        correction = "x" * 3500 + " Replace column B with C, and cancel the earlier title."
        history += exchange(correction, "y" * 4500)
        call = Mock()
        prepared = self.prepare(history, call, budget=20000)
        self.assertEqual(prepared.turns, [*history, self.current])
        self.assertIsNone(prepared.state)
        call.assert_not_called()
        self.assertIn(correction, [m["content"] for m in prepared.turns])

    def test_summary_keeps_changes_and_cancellation_with_user_provenance(self):
        history = exchange("Use label alpha", "First answer " * 160)
        history += exchange("Replace label alpha with beta", "Second answer " * 160)
        history += exchange("Cancel label beta", "Third answer " * 160)
        history += exchange("New topic: budget. Keep a separate table.", "Fourth answer " * 160)
        call = self.compactor()
        prepared = self.prepare(history, call, budget=2200)
        self.assertGreater(prepared.metadata["compacted_messages"], 0)
        memory = prepared.state["memory"]
        self.assertEqual(memory["requirements"][0]["status"], "cancelled")
        self.assertEqual(memory["requirements"][0]["quote"], "Cancel label beta")
        self.assertEqual(memory["requirements"][0]["source_message"], 5)
        self.assertEqual(memory["requirements"][0]["scope"], "labels")
        self.assertEqual(memory["closed_topics"], ["labels"])
        self.assertEqual(prepared.turns[-1], self.current)
        self.assertLessEqual(context.tokens(prepared.turns), 2200)

    @staticmethod
    def compactor():
        def compact(messages, instructions):
            payload = json.loads(messages[0]["content"])
            memory = payload["previous"] or {"summary": "Synthetic facts", "requirements": [],
                                              "closed_topics": [], "unresolved": []}
            for message in payload["messages"]:
                text = message["content"]
                if message["role"] == "user":
                    status = ("cancelled" if "Cancel label beta" in text else
                              "active" if "Use label alpha" in text or "Replace label alpha with beta" in text else None)
                    if status:
                        memory["requirements"] = [{"key": "label", "text": text, "scope": "labels",
                                                   "status": status, "source_message": message["message_index"], "quote": text}]
                    if "New topic" in text:
                        memory["closed_topics"] = ["labels"]
            return client.Result(json.dumps(memory), [], 0)
        return Mock(side_effect=compact)

    def test_replay_uses_saved_summary_without_recompacting_old_turns(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        initial = self.prepare(history, self.compactor(), budget=2200)
        later = history + exchange("Replace label alpha with beta", "Updated")
        call = Mock()
        replay = self.prepare(later, call, previous=initial.state, budget=2200)
        call.assert_not_called()
        self.assertEqual(replay.turns[-3:], [*later[-2:], self.current])
        self.assertEqual(replay.state, initial.state)

    def test_state_from_another_participant_topic_or_source_is_rejected(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        state = self.prepare(history, self.compactor(), budget=2200).state
        for other in ([8, 3, 11], [7, 4, 11], [7, 3, 12]):
            with self.subTest(scope=other), self.assertRaisesRegex(ValueError, "scope mismatch"):
                context.prepare_context(history, self.current, Mock(), scope=other,
                                        previous=state, input_budget_tokens=2200)

    def test_deleted_or_changed_history_rebuilds_state_from_originals(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        state = self.prepare(history, self.compactor(), budget=2200).state
        changed = exchange("Entirely new task", "Small answer")
        call = Mock()
        prepared = self.prepare(changed, call, previous=state)
        self.assertTrue(prepared.metadata["state_rebuilt"])
        self.assertIsNone(prepared.state)
        self.assertEqual(prepared.turns, [*changed, self.current])
        self.assertNotIn("alpha", json.dumps(prepared.turns))

    def test_invented_requirement_quote_does_not_become_memory(self):
        memory = {"summary": "Invented", "requirements": [{"key": "bad", "text": "Invented constraint",
                  "scope": "conversation", "status": "active", "source_message": 1, "quote": "Not in original"}],
                  "closed_topics": [], "unresolved": []}
        call = Mock(return_value=client.Result(json.dumps(memory), [], 0))
        with self.assertRaises(client.ModelIssue) as caught:
            self.prepare(exchange("Use label alpha", "Large answer " * 500), call, budget=2200)
        self.assertEqual(caught.exception.stage, "context_compaction_invalid")
        self.assertEqual(call.call_count, 2)
        audit = caught.exception.quality_metadata["context_audit"]
        self.assertEqual(audit["compaction_rejections"][0]["reason"], "requirement_quote_not_exact")
        self.assertNotIn("Not in original", json.dumps(audit))
        self.assertIn("requirement_quote_not_exact", call.call_args_list[1].args[0][-1]["content"])

    def test_valid_but_oversized_memory_is_classified_separately_from_invalid_json(self):
        marker = "SYNTHETIC_PRIVATE_MEMORY"
        memory = {"summary": marker * 300, "requirements": [], "closed_topics": [], "unresolved": []}
        call = Mock(return_value=client.Result(json.dumps(memory), [], 0))
        with self.assertRaises(client.ModelIssue) as caught:
            self.prepare(exchange("Use label alpha", "Large answer " * 500), call, budget=2200)
        self.assertEqual(caught.exception.kind, "context")
        self.assertEqual(caught.exception.stage, "context_memory_overflow")
        audit = caught.exception.quality_metadata["context_audit"]
        rejection = audit["compaction_rejections"][0]
        self.assertEqual(rejection["reason"], "memory_budget_exceeded")
        self.assertGreater(rejection["memory_size"], rejection["memory_budget"])
        self.assertNotIn(marker, json.dumps(audit))
        self.assertIn("байтов UTF-8", call.call_args_list[0].args[1])

    def test_repair_receives_specific_error_and_can_preserve_a_corrected_memory(self):
        invalid = {"summary": "Known", "requirements": [{"key": "label", "text": "Use label alpha",
                   "scope": "labels", "status": "active", "source_message": 1, "quote": "invented"}],
                   "closed_topics": [], "unresolved": []}
        correct = self.compactor()
        attempts = [0]
        def repairing(messages, prompt):
            attempts[0] += 1
            if attempts[0] == 1:
                return client.Result(json.dumps(invalid), [], 0)
            return correct(messages, prompt)
        call = Mock(side_effect=repairing)
        prepared = self.prepare(exchange("Use label alpha", "Large answer " * 500), call, budget=2200)
        self.assertEqual(prepared.state["memory"]["requirements"][0]["quote"], "Use label alpha")
        repair_input = call.call_args_list[1].args[0]
        self.assertEqual(json.loads(repair_input[-2]["content"]), invalid)
        self.assertIn("requirement_quote_not_exact", repair_input[-1]["content"])

    def test_existing_requirement_cannot_silently_disappear(self):
        previous = {"summary": "Known", "requirements": [{"key": "label", "text": "Use label alpha",
                    "scope": "labels", "status": "active", "source_message": 1, "quote": "Use label alpha"}],
                    "closed_topics": [], "unresolved": []}
        omitted = {"summary": "Known", "requirements": [], "closed_topics": [], "unresolved": []}
        self.assertIsNone(context._validate_memory(omitted, exchange("Use label alpha", "ok"), previous))

    def test_large_current_attachment_is_never_rewritten_here(self):
        current = {"role": "user", "content": [{"type": "input_text", "text": "Материал: demo\n" + "z" * 10000}]}
        prepared = context.prepare_context([], current, Mock(), scope=self.scope, input_budget_tokens=2200)
        self.assertIs(prepared.turns[-1], current)
        self.assertTrue(prepared.metadata["current_requires_mapping"])

    def test_all_characters_of_large_old_message_are_read_in_fragments(self):
        old_answer = "START " + "x" * 5000 + " END"
        call = self.compactor()
        self.prepare(exchange("Use label alpha", old_answer), call, budget=2200)
        fragments = [fragment for args in call.call_args_list
                     for fragment in json.loads(args.args[0][0]["content"])["messages"]]
        observed = "".join(item["content"] for item in fragments if item["message_index"] == 2)
        self.assertEqual(observed, old_answer)

    def test_context_budget_uses_server_catalog_and_reports_its_origin(self):
        module = types.ModuleType("agent.model_metadata")
        module._resolve_codex_oauth_context_length_with_source = Mock(return_value=(500000, "live"))
        credentials = {"api_key": "synthetic-secret", "base_url": "https://example.invalid"}
        with patch.dict(sys.modules, {"agent.model_metadata": module}), patch.object(context, "_credentials", return_value=credentials):
            budget, metadata = context.resolve_budget("chosen-model")
        self.assertEqual(budget, 400000)
        self.assertEqual(metadata, {"context_window_tokens": 500000, "context_window_origin": "live", "reserved_tokens": 100000})
        self.assertNotIn("synthetic-secret", json.dumps(metadata))
        module._resolve_codex_oauth_context_length_with_source.assert_called_once_with(
            "chosen-model", access_token="synthetic-secret", base_url="https://example.invalid")

    def test_unknown_window_is_not_guessed(self):
        module = types.ModuleType("agent.model_metadata")
        module._resolve_codex_oauth_context_length_with_source = Mock(return_value=(None, ""))
        with patch.dict(sys.modules, {"agent.model_metadata": module}), patch.object(context, "_credentials", return_value={"api_key": "x", "base_url": "url"}):
            with self.assertRaises(client.ModelIssue) as caught:
                context.resolve_budget("unknown-model")
        self.assertEqual(caught.exception.stage, "context_window_unknown")


if __name__ == "__main__":
    unittest.main()
