"""Maximum-capability and durable stop provenance tests without external traffic."""
import importlib
import json
import os
import sqlite3
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch


def module(name):
    if os.environ.get("PSY_GROUP_TEST_INSTALLED"):
        return importlib.import_module("plugins.platforms.telegram." + name)
    try:
        return importlib.import_module("psy_modules." + name)
    except ModuleNotFoundError:
        return importlib.import_module(name)


client = module("psy_group_client")
MODEL = "gpt-6.1-sol"


class Memory(dict):
    def put(self, key, value):
        self[key] = value


class Provenance(unittest.TestCase):
    def stream(self, events, **options):
        response = Mock(status_code=200)
        response.iter_lines.return_value = ["data: " + json.dumps(event) for event in events]
        transport = Mock()
        transport.stream.return_value = nullcontext(response)
        return client._stream(transport, {}, [], False, MODEL, **options), transport

    def test_service_call_uses_maximum_and_records_provider_completion(self):
        result, transport = self.stream([
            {"type": "response.created", "response": {"model": "gpt-6.1-sol-2026-09-29"}},
            {"type": "response.output_text.delta", "delta": "Полный ответ"},
            {"type": "response.completed"},
        ])
        payload = transport.stream.call_args.kwargs["json"]
        self.assertEqual(payload["reasoning"], {"effort": "max"})
        self.assertEqual(payload["model"], MODEL)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.model_actual, "gpt-6.1-sol-2026-09-29")

    def test_stream_preserves_continuation_whitespace_for_code(self):
        text = "\n    return value  \n"
        prefix = [{"type": "response.output_text.delta", "delta": text}]
        for ending in ({"type": "response.completed"}, {"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "max_output_tokens"}}}):
            result, _ = self.stream(prefix + [ending])
            self.assertEqual(result.text, text)
        result, _ = self.stream(prefix)
        self.assertEqual(result.text, text)
        result, _ = self.stream([{"type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}]}}])
        self.assertEqual(result.text, text)

    def test_output_ceiling_differs_from_connection_end(self):
        prefix = [{"type": "response.output_text.delta", "delta": "Начало ответа"}]
        ceiling, _ = self.stream(prefix + [{"type": "response.incomplete", "response": {
            "incomplete_details": {"reason": "max_output_tokens"}}}])
        disconnected, _ = self.stream(prefix)
        self.assertEqual(ceiling.stop_reason, "max_output_tokens")
        self.assertEqual((disconnected.stop_reason, disconnected.stop_stage), ("connection", "stream_end"))
        self.assertFalse(ceiling.complete)
        self.assertFalse(disconnected.complete)
        self.assertEqual(ceiling.text, disconnected.text)

    def test_partial_timeout_keeps_distinct_stop_stage(self):
        response = Mock(status_code=200)
        transport = Mock()
        transport.stream.return_value = nullcontext(response)
        lines = ["data: " + json.dumps({"type": "response.output_text.delta", "delta": "Часть"}),
                 client.ModelIssue("timeout", "stream_idle")]
        with patch.object(client, "_lines", return_value=iter(lines)):
            result = client._stream(transport, {}, [], False, MODEL)
        self.assertEqual((result.stop_reason, result.stop_stage), ("timeout", "stream_idle"))

    def test_malformed_stream_retains_exact_generated_prefix(self):
        response = Mock(status_code=200)
        transport = Mock()
        transport.stream.return_value = nullcontext(response)
        prefix = "def f():\n    "
        response.iter_lines.return_value = ["data: " + json.dumps(
            {"type": "response.output_text.delta", "delta": prefix}), "data: {invalid"]
        result = client._stream(transport, {}, [], False, MODEL)
        self.assertEqual(result.text, prefix)
        self.assertFalse(result.complete)
        self.assertEqual((result.stop_reason, result.stop_stage), ("connection", "stream_format"))

    def test_content_filter_has_stable_reason_without_publishing_prefix(self):
        with self.assertRaises(client.ModelIssue) as caught:
            self.stream([
                {"type": "response.output_text.delta", "delta": "PRIVATE prefix"},
                {"type": "response.incomplete", "response": {"model": MODEL,
                    "incomplete_details": {"reason": "content_filter"}}},
            ])
        self.assertEqual(caught.exception.stop_reason, "content_filter")
        self.assertEqual(caught.exception.model_actual, MODEL)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("PRIVATE", str(caught.exception.__dict__))

    def test_completed_refusal_is_distinct_from_success(self):
        result, _ = self.stream([{"type": "response.completed", "response": {"output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "Ограничение провайдера"}]}]}}])
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "provider_refusal")

    def test_http_error_keeps_status_and_safe_code_without_provider_message(self):
        issue = client._failure(429, {"code": "usage_limit_reached", "message": "private error body"}, "http")
        self.assertEqual((issue.kind, issue.http_status, issue.provider_error_code),
                         ("quota", 429, "usage_limit_reached"))
        unknown = client._failure(400, {"code": "private-user-text", "message": "private error body"}, "http")
        self.assertIsNone(unknown.provider_error_code)
        self.assertNotIn("private", str(issue.__dict__) + str(unknown.__dict__))

    def test_capabilities_depend_on_selected_model_without_changing_it(self):
        self.assertEqual(client._maximum_effort(MODEL), "max")
        self.assertEqual(client._maximum_effort("gpt-5.3-codex"), "xhigh")
        self.assertNotEqual(client._maximum_effort(MODEL), "ultra")

    def test_old_silent_cut_marker_never_lowers_new_attempt(self):
        checkpoints = Memory({client.EFFORT_MARK: {"silent_cuts": 90}})
        observed = []
        def stream(_client, headers, messages, web, model, **kwargs):
            observed.append((model, kwargs["effort"], kwargs["deep"]))
            return client.Result("Полный ответ", [], 0, stop_reason="completed")
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", side_effect=lambda fn: fn("", {})), \
                patch.object(client, "_stream", side_effect=stream):
            call, _ = client._job_calls(None, MODEL, checkpoints)
            result = call([client._user("Вопрос")], "Инструкция")
        self.assertEqual(observed, [(MODEL, "max", True)])
        self.assertFalse(result.quality_metadata["generation"]["fallback_used"])
        self.assertEqual(call.audit()["generation_audit"]["effort_policy"], "maximum_supported_no_downgrade")

    def test_failure_audit_survives_resume_and_never_contains_user_text(self):
        checkpoints = Memory()
        private = "private participant statement"
        instructions = "private task instructions"
        issue = client.ModelIssue("timeout", "stream_idle")
        issue.silent = 901
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", side_effect=issue) as invoked:
            first, _ = client._job_calls(None, MODEL, checkpoints)
            with self.assertRaises(client._ResumeModelJob):
                first([client._user(private)], instructions)
            with self.assertRaises(client._ResumeModelJob):
                first([client._user("sibling")], instructions)
            self.assertEqual(invoked.call_count, 1)
        self.assertEqual(len(checkpoints[client.ATTEMPT_AUDIT]["failed_attempts"]), 1)
        self.assertEqual(issue.quality_metadata["generation_audit"]["failed_attempts"][0]["effort"], "max")
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", return_value=client.Result(
                    "saved answer", [], 0, stop_reason="completed", model_actual=MODEL)):
            resumed, _ = client._job_calls(None, MODEL, checkpoints)
            result = resumed([client._user(private)], instructions)
        audit = resumed.audit()["generation_audit"]
        self.assertEqual(len(audit["failed_attempts"]), 1)
        stage = audit["stage_records"][0]
        self.assertEqual(stage["stop_reason"], "completed")
        self.assertEqual(stage["model_actual"], MODEL)
        self.assertEqual(stage["effective_parameters"]["reasoning"], {"effort": "max"})
        self.assertNotIn(private, str(audit))
        self.assertNotIn(instructions, str(audit))
        self.assertNotIn("saved answer", str(audit))
        with patch.object(client, "_authenticated_request", side_effect=AssertionError("network forbidden")):
            replay, _ = client._job_calls(None, MODEL, checkpoints)
            cached = replay([client._user(private)], instructions)
        self.assertEqual(cached.stop_reason, result.stop_reason)
        self.assertEqual(cached.quality_metadata, result.quality_metadata)
        self.assertEqual(replay.audit()["generation_audit"]["failed_attempts"], audit["failed_attempts"])

    def test_audit_checkpoint_failure_preserves_original_provider_cause(self):
        class FailedAudit(Memory):
            def put(self, key, value):
                if key == client.ATTEMPT_AUDIT:
                    raise sqlite3.OperationalError("PRIVATE storage detail")
                super().put(key, value)
        for kind in ("timeout", "context"):
            issue = client.ModelIssue(kind, "stream_idle" if kind == "timeout" else "http")
            with self.subTest(kind=kind), patch.object(client, "_transport", nullcontext), \
                    patch.object(client, "_authenticated_request", side_effect=issue):
                call, _ = client._job_calls(None, MODEL, FailedAudit())
                expected = client._ResumeModelJob if issue.retryable else client.ModelIssue
                with self.assertRaises(expected) as caught:
                    call([client._user("PRIVATE question")], "PRIVATE instruction")
                original = caught.exception.issue if issue.retryable else caught.exception
            self.assertIs(original, issue)
            record = issue.quality_metadata["generation_audit"]["failed_attempts"][0]
            self.assertEqual(record["stop_reason"], kind)
            self.assertFalse(record["audit_checkpoint_persisted"])
            self.assertEqual(record["audit_checkpoint_error_class"], "OperationalError")
            self.assertNotIn("PRIVATE", str(issue.quality_metadata))

    def test_terminal_failure_attaches_audit_without_retry(self):
        issue = client.ModelIssue("context", "http")
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", side_effect=issue):
            call, _ = client._job_calls(None, MODEL, Memory())
            with self.assertRaises(client.ModelIssue):
                call([client._user("private")], "instruction")
        record = issue.quality_metadata["generation_audit"]["failed_attempts"][0]
        self.assertFalse(record["retryable"])
        self.assertEqual(record["stop_reason"], "context")
        self.assertEqual(record["stop_stage"], "http")
        self.assertEqual(len(record["request_sha256"]), 64)

    def test_request_fingerprint_covers_messages_and_options(self):
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", side_effect=lambda invoke: client.Result("answer", [], 0)):
            call, _ = client._job_calls(None, MODEL, None)
            a = call([client._user("one")], "instruction")
            b = call([client._user("two")], "instruction")
            c = call([client._user("one")], "instruction", web=True)
        hashes = [item.quality_metadata["generation"]["request_sha256"] for item in (a, b, c)]
        self.assertEqual(len(set(hashes)), 3)
        self.assertEqual(len(call.audit()["generation_audit"]["stage_records"]), 3)


if __name__ == "__main__":
    unittest.main()
