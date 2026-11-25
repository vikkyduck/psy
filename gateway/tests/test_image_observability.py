"""Native image provenance with synthetic payloads and no external generation."""
import base64
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from psy_modules import psy_group_codex as adapter, psy_group_observability as observation
from psy_modules import psy_group_route as route, psy_group_store as store


PRIVATE = "PRIVATE participant image instructions"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (1536).to_bytes(4, "big") + (1024).to_bytes(4, "big")


class ImageAudit(unittest.TestCase):
    def setUp(self):
        self.plugin = ModuleType("plugins.image_gen.openai-codex")
        self.plugin.API_MODEL = "gpt-image-2"
        self.plugin._resolve_model = Mock(return_value=("gpt-image-2-high", {"quality": "high"}))
        self.plugin._data_url_to_input_image_url = Mock(side_effect=lambda value: value)
        self.plugin._post_image_request = Mock(return_value={"data": [{"b64_json": base64.b64encode(PNG).decode(),
                                                                      "revised_prompt": PRIVATE}]})
        self.turns = [{"role": "user", "content": [{"type": "input_text", "text": PRIVATE}]}]
        self.enterContext(patch.dict(sys.modules, {self.plugin.__name__: self.plugin}))
        self.auth = self.enterContext(patch.object(adapter.codex, "_authenticated_request", side_effect=lambda fn: fn("SECRET", {})))

    def generate(self):
        return adapter._image_response(self.turns, "chat-model")

    def test_requested_parameters_do_not_become_unreported_provider_facts(self):
        result = self.generate()
        stage = result.quality_metadata["generation_audit"]["stage_records"][0]
        self.assertEqual(stage["model_requested"], "gpt-image-2")
        self.assertEqual(stage["configured_chat_model"], "chat-model")
        self.assertEqual(stage["configured_image_tier"], "gpt-image-2-high")
        self.assertEqual(stage["requested_parameters"], {"size": "1024x1024", "quality": "high"})
        self.assertIsNone(result.model_actual)
        for key in ("reported_quality", "reported_size", "imagegen_request_id", "provider_stop_reason"):
            self.assertIsNone(stage[key])
        self.assertEqual(stage["pixel_size"], "1536x1024")
        self.assertEqual(stage["image_sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(result.stop_reason, "image_received")
        self.assertEqual(result.task_status, "unknown")
        self.assertTrue(result.quality_metadata["generation_audit"]["generation_complete"])
        self.assertEqual(result.images, [PNG])
        self.assertEqual(self.plugin._post_image_request.call_count, 1)
        self.assertEqual(self.plugin._post_image_request.call_args.kwargs["quality"], "high")
        self.assertNotIn(PRIVATE, json.dumps(result.quality_metadata))
        self.assertNotIn("SECRET", json.dumps(result.quality_metadata))
        self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(result.quality_metadata))

    def test_provider_facts_are_distinct_from_requested_settings(self):
        self.plugin._post_image_request.return_value.update(model="actual-image-2026", quality="medium",
                                                           size="1536x1024", imagegen_request_id="image-request-42")
        result = self.generate()
        stage = result.quality_metadata["generation_audit"]["stage_records"][0]
        self.assertEqual(result.model_actual, "actual-image-2026")
        self.assertEqual(stage["reported_quality"], "medium")
        self.assertEqual(stage["reported_size"], "1536x1024")
        self.assertEqual(stage["imagegen_request_id"], "image-request-42")
        self.assertEqual(stage["requested_parameters"]["quality"], "high")

    def test_reference_images_affect_fingerprint_without_publishing_payload(self):
        first = self.generate().quality_metadata["generation_audit"]["stage_records"][0]
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        self.turns[-1]["content"].append({"type": "input_image", "image_url": url})
        second = self.generate().quality_metadata["generation_audit"]["stage_records"][0]
        self.assertNotEqual(first["request_input_sha256"], second["request_input_sha256"])
        self.assertEqual(first["prompt_sha256"], second["prompt_sha256"])
        self.assertEqual(second["input_image_count"], 1)
        self.assertEqual(second["endpoint_path"], "images/edits")
        self.assertNotIn(url, json.dumps(second))

    def test_http_failure_is_auditable_and_does_not_add_retries(self):
        for status in (400, 403, 429, 500):
            with self.subTest(status=status):
                self.plugin._post_image_request.reset_mock()
                self.plugin._post_image_request.side_effect = RuntimeError(f"Codex images API returned HTTP {status}: {PRIVATE}")
                with self.assertRaises(adapter.ModelIssue) as caught:
                    self.generate()
                issue = caught.exception
                self.assertFalse(issue.retryable)
                self.assertEqual(issue.http_status, status)
                self.assertEqual(issue.stop_reason, "http_error")
                audit = issue.quality_metadata["generation_audit"]
                self.assertFalse(audit["generation_complete"])
                self.assertEqual(audit["attempts"][0]["http_status"], status)
                self.assertNotIn(PRIVATE, json.dumps(issue.__dict__))
                self.plugin._post_image_request.assert_called_once()

    def test_existing_401_renewal_keeps_both_attempts(self):
        def renew(fn):
            try:
                return fn("old-secret", {})
            except adapter.ModelIssue as exc:
                self.assertEqual((exc.kind, exc.stage), ("signin", "http"))
                return fn("new-secret", {})
        self.auth.side_effect = renew
        self.plugin._post_image_request.side_effect = [RuntimeError("Codex images API returned HTTP 401: " + PRIVATE),
                                                       self.plugin._post_image_request.return_value]
        audit = self.generate().quality_metadata["generation_audit"]
        self.assertEqual(len(audit["attempts"]), 2)
        self.assertEqual(audit["attempts"][0]["http_status"], 401)
        self.assertTrue(audit["attempts"][1]["response_received"])
        self.assertNotIn("secret", json.dumps(audit))

    def test_invalid_response_and_transport_failure_are_safely_explained(self):
        for payload in (None, {}, {"data": []}, {"data": [{"b64_json": "not base64"}]}):
            with self.subTest(payload=payload):
                self.plugin._post_image_request.return_value = payload
                with self.assertRaises(adapter.ModelIssue) as caught:
                    self.generate()
                self.assertEqual(caught.exception.stop_reason, "invalid_image_response")
                self.assertFalse(caught.exception.retryable)
                self.assertFalse(caught.exception.quality_metadata["generation_audit"]["generation_complete"])
        self.plugin._post_image_request.side_effect = RuntimeError(PRIVATE)
        with self.assertRaises(adapter.ModelIssue) as caught:
            self.generate()
        self.assertEqual(caught.exception.stage, "image_request")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.quality_metadata["generation_audit"]["attempts"][0]["error_class"], "RuntimeError")
        self.assertNotIn(PRIVATE, json.dumps(caught.exception.__dict__))

    def test_os_failures_keep_the_existing_transient_classification(self):
        self.plugin._post_image_request.side_effect = TimeoutError(PRIVATE)
        with self.assertRaises(adapter.ModelIssue) as caught:
            self.generate()
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.stop_reason, "timeout")
        self.plugin._post_image_request.assert_called_once()
        self.assertNotIn(PRIVATE, json.dumps(caught.exception.__dict__))

    def test_response_prose_cannot_leak_through_operational_fields(self):
        self.plugin._post_image_request.return_value.update(model=PRIVATE, quality=PRIVATE,
                                                           size=PRIVATE, imagegen_request_id=PRIVATE)
        result = self.generate()
        self.assertIsNone(result.model_actual)
        self.assertNotIn(PRIVATE, json.dumps(result.quality_metadata))


class ImageSnapshot(unittest.TestCase):
    def test_native_plugin_shared_helpers_and_declaration_change_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            code = checkout / "plugins/platforms/telegram"
            code.mkdir(parents=True)
            (checkout / "agent").mkdir()
            (checkout / "agent/reasoning_effort.py").write_text("EFFORT = 1\n")
            (code / "psy_example.py").write_text("ROUTE = 1\n")
            relative = ["plugins/image_gen/_common.py", "plugins/image_gen/openai-codex/__init__.py",
                        "plugins/image_gen/openai-codex/plugin.yaml"]
            for path in relative:
                target = checkout / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original\n")
            def capture():
                return observation.capture_runtime("chat-model", profile=root / "profile", code_dir=code,
                                                   root=root / "snapshots", include_manifest=True)
            previous = capture()
            for path in relative:
                self.assertIn("runtime/" + path, previous["file_sha256"])
                (checkout / path).write_text("changed\n")
                current = capture()
                self.assertNotEqual(previous["snapshot_id"], current["snapshot_id"])
                self.assertNotEqual(previous["code_version"], current["code_version"])
                previous = current


class ImageDeliveryRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_answer_checkpoint_retains_audit_without_regeneration_and_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            database = store.database
            with patch.object(store, "database", lambda *args, **kwargs: database(Path(directory))), \
                    patch.object(route, "runtime_snapshot", return_value={"snapshot_id": "fixture"}):
                store.pending(7, 99, "synthetic")
                cp = store.Checkpoints(99)
                msg = NS(chat=NS(id=-202), from_user=NS(id=7), message_id=55, ident=99,
                         is_topic_message=False, message_thread_id=None, model_delivery=lambda: None)
                job = dict(destination=[-202, 7, 0, 55], doc_id=None, question=PRIVATE,
                           quote="", history=[], model="chat-model", research=False, draw=True)
                details = {"model_requested": "gpt-image-2", "generation_audit": {"generation_complete": True,
                           "stage_records": [{"model_actual": None, "requested_parameters": {"quality": "high"}}]}}
                result = adapter.Result("", [PNG], 0, task_status="unknown", quality_metadata=details,
                                        stop_reason="image_received", stop_stage="image_response")
                with patch.object(route, "respond", return_value=result) as generated, \
                        patch.object(route, "deliver", AsyncMock(side_effect=TimeoutError("fixture"))):
                    with self.assertRaises(TimeoutError):
                        await route.prepared_answer(msg, job, cp)
                    generated.assert_called_once()
                saved_checkpoint = cp.get("route:answer:v1")
                self.assertEqual(saved_checkpoint["result_details"]["quality_metadata"]["stop_reason"], "image_received")
                with patch.object(route, "respond", side_effect=AssertionError("must not regenerate")), \
                        patch.object(route, "deliver", AsyncMock(return_value=NS(message_id=56))) as delivered:
                    await route.prepared_answer(msg, job, cp)
                self.assertEqual(delivered.await_args.args[2], PNG)
                with store.database() as con:
                    ident = con.execute("SELECT id FROM turns").fetchone()[0]
                store.settle(99, True)
                self.assertIsNone(cp.get("route:answer:v1"))
                saved = store.result_details(7, 0, ident)
                self.assertTrue(saved["generation_complete"])
                self.assertEqual(saved["delivery_status"], "confirmed")
                self.assertEqual(saved["quality_metadata"]["generation_audit"], details["generation_audit"])
                self.assertNotIn(PRIVATE, json.dumps(observation.result_audit(saved)))


if __name__ == "__main__":
    unittest.main()
