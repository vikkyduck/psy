"""Task readiness survives recovery independently of delivery, using only synthetic data."""
from contextlib import nullcontext
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from psy_modules import psy_group_client as client, psy_group_route as route, psy_group_store as store


class ResultStore(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.details = dict(task_status="partial", generation_complete=True,
                            missing_requirements=["second section"],
                            quality_metadata={"model_requested": "fixture", "coverage_status": "partial"})

    def test_partial_is_not_complete_after_delivery_or_request_cleanup(self):
        store.pending(7, 55, "synthetic", root=self.root)
        store.claim_next(root=self.root)
        cp = store.Checkpoints(55, root=self.root)
        cp.put("route:answer:v1", {"text": "first section", "images": [], "result_details": self.details})
        ident = store.remember(7, 3, "two sections", "first section", root=self.root,
                               result_details=self.details, request_key=(55, "turn"))
        store.confirm_result_delivery(7, 3, ident, root=self.root)
        store.settle(55, True, root=self.root)
        self.assertIsNone(cp.get("route:answer:v1"))
        saved = store.result_details(7, 3, ident, root=self.root)
        self.assertEqual(saved["task_status"], "partial")
        self.assertTrue(saved["generation_complete"])
        self.assertEqual(saved["delivery_status"], "confirmed")
        self.assertEqual(saved["missing_requirements"], ["second section"])

    def test_replay_preserves_original_provenance_and_confirmation(self):
        store.pending(7, 55, "synthetic", root=self.root)
        arguments = dict(root=self.root, result_details=self.details, request_key=(55, "turn"))
        ident = store.remember(7, 3, "question", "first", **arguments)
        store.confirm_result_delivery(7, 3, ident, root=self.root)
        original = store.result_details(7, 3, ident, root=self.root)
        arguments["result_details"] = dict(self.details, task_status="complete", quality_metadata={})
        self.assertEqual(store.remember(7, 3, "question", "first", **arguments), ident)
        self.assertEqual(store.result_details(7, 3, ident, root=self.root), original)
        for uid, topic in ((8, 3), (7, 4)):
            self.assertIsNone(store.result_details(uid, topic, ident, root=self.root))
            store.confirm_result_delivery(uid, topic, ident, root=self.root)
        self.assertEqual(store.result_details(7, 3, ident, root=self.root), original)

    def test_v1_migration_preserves_turns_and_does_not_invent_old_status(self):
        ident = store.remember(7, 3, "legacy question", "legacy answer", root=self.root)
        with sqlite3.connect(self.root / "group.sqlite3") as con:
            con.execute("DROP TABLE turn_results")
            con.execute("PRAGMA user_version=1")
        store._prepared.pop(str(self.root / "group.sqlite3"), None)
        saved = store.result_details(7, 3, ident, root=self.root)
        self.assertEqual(saved["task_status"], "unknown")
        self.assertIsNone(saved["generation_complete"])
        self.assertIsNone(saved["recorded_at"])
        self.assertEqual(saved["delivery_status"], "unknown")
        with store.database(self.root) as con:
            self.assertEqual(con.execute("SELECT prompt,answer FROM turns").fetchone(),
                             ("legacy question", "legacy answer"))
            # The previous release's insert remains valid after the additive migration.
            con.execute("INSERT INTO turns(uid,topic,prompt,answer) VALUES(?,?,?,?)", (7, 3, "old", "writer"))

    def test_result_metadata_is_removed_with_owning_turn(self):
        ident = store.remember(7, 3, "q", "a", root=self.root, result_details=self.details)
        with store.database(self.root) as con:
            con.execute("DELETE FROM turns WHERE id=?", (ident,))
            self.assertEqual(con.execute("SELECT count(*) FROM turn_results").fetchone()[0], 0)


class ResultRoute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        database = store.database
        self.enterContext(patch.object(store, "database", lambda *a, **kw: database(self.root)))
        self.msg = NS(chat=NS(id=-202), from_user=NS(id=7), message_id=55,
                      is_topic_message=False, message_thread_id=None,
                      reply_text=AsyncMock(return_value=NS(message_id=56)),
                      reply_document=AsyncMock(return_value=NS(message_id=57)))
        self.job = dict(destination=[-202, 7, 0, 55], doc_id=None, question="two sections",
                        quote="", history=[], model="fixture", research=False, draw=False)

    async def test_image_failure_does_not_confirm_text_plus_image(self):
        result = client.Result("first section", [b"image"], 0, task_status="partial",
                               missing_requirements=["second section"])
        self.msg.reply_document.side_effect = TimeoutError("synthetic receipt lost")
        with patch.object(route, "respond", return_value=result):
            with self.assertRaises(TimeoutError):
                await route.prepared_answer(self.msg, self.job, None)
        with store.database() as con:
            ident = con.execute("SELECT id FROM turns").fetchone()[0]
        saved = store.result_details(7, 0, ident)
        self.assertEqual(saved["delivery_status"], "pending")
        self.assertEqual(saved["task_status"], "partial")

    async def test_partial_text_delivery_preserves_task_status(self):
        result = client.Result("first section", [], 0, task_status="partial", missing_requirements=["second"],
                               quality_metadata={"generation_audit": {"generation_complete": True}})
        with patch.object(route, "respond", return_value=result):
            await route.prepared_answer(self.msg, self.job, None)
        with store.database() as con:
            ident = con.execute("SELECT id FROM turns").fetchone()[0]
        saved = store.result_details(7, 0, ident)
        self.assertEqual(saved["delivery_status"], "confirmed")
        self.assertEqual(saved["task_status"], "partial")
        self.assertTrue(saved["generation_complete"])

    async def test_missing_sources_do_not_mean_incomplete_api_streams(self):
        result = client.Result("sources unavailable", [], 0, complete=False, task_status="needs_source",
                               quality_metadata={"generation_audit": {"generation_complete": True}})
        with patch.object(route, "respond", return_value=result):
            await route.prepared_answer(self.msg, self.job, None)
        with store.database() as con:
            ident = con.execute("SELECT id FROM turns").fetchone()[0]
        saved = store.result_details(7, 0, ident)
        self.assertTrue(saved["generation_complete"])
        self.assertEqual(saved["task_status"], "needs_source")

    async def test_legacy_checkpoint_replays_exact_bytes_with_unknown_quality(self):
        store.pending(7, 99, "synthetic")
        cp = store.Checkpoints(99)
        cp.put("route:answer:v1", {"text": "legacy bytes", "images": []})
        self.msg.ident = 99
        self.msg.model_delivery = lambda: None
        with patch.object(route, "respond", side_effect=AssertionError("must not regenerate")):
            await route.prepared_answer(self.msg, self.job, cp)
        self.assertEqual(self.msg.reply_text.await_args.args[0], "legacy bytes")
        with store.database() as con:
            ident = con.execute("SELECT id FROM turns").fetchone()[0]
        saved = store.result_details(7, 0, ident)
        self.assertEqual(saved["task_status"], "unknown")
        self.assertIsNone(saved["generation_complete"])
        self.assertEqual(saved["delivery_status"], "confirmed")


class GenerationAudit(unittest.TestCase):
    def test_replay_keeps_original_effort_and_missing_legacy_metadata_unknown(self):
        class Memory(dict):
            def put(self, key, value):
                self[key] = value
        cp = Memory()
        turns = [client._user("private synthetic request")]
        with patch.object(client, "_transport", nullcontext), \
                patch.object(client, "_authenticated_request", return_value=client.Result("answer", [], 0)):
            call, _ = client._job_calls(None, "fixture", cp)
            call(turns, "instruction")
        call, _ = client._job_calls(None, "fixture", cp)
        with patch.object(client, "_authenticated_request", side_effect=AssertionError("network forbidden")):
            call(turns, "instruction")
        audit = call.audit()
        self.assertEqual(audit["generation_audit"]["effort_counts"], {client.QUICK_EFFORT: 1})
        self.assertEqual(audit["generation_audit"]["replayed_stages"], 1)
        original_time = next(iter(cp.values()))["generation_metadata"]["generated_at"]
        self.assertEqual(audit["generation_audit"]["first_generated_at"], original_time)
        self.assertEqual(audit["generation_audit"]["last_generated_at"], original_time)
        self.assertTrue(audit["generation_audit"]["generation_complete"])
        self.assertNotIn("private synthetic request", str(audit))
        self.assertNotIn("answer", str(audit))
        for saved in cp.values():
            saved.pop("generation_metadata", None)
        call, _ = client._job_calls(None, "fixture", cp)
        call(turns, "instruction")
        self.assertEqual(call.audit()["generation_audit"]["unknown_stages"], 1)
        self.assertIsNone(call.audit()["generation_audit"]["first_generated_at"])
        self.assertIsNone(call.audit()["generation_audit"]["generation_complete"])


if __name__ == "__main__":
    unittest.main()
