"""Terminal failure keeps the full scoped user turn across real SQLite queue cleanup."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from psy_modules import psy_group_route as route, psy_group_store as store
from psy_modules.psy_group_client import ModelIssue, Result
from test_delivery_resume import queue


class FailedTurnHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        database = store.database
        self.enterContext(patch.object(store, "database", lambda *a, **kw: database(self.root)))
        self.now = 1_800_000_000.0
        self.enterContext(patch.object(store, "time", NS(time=lambda: self.now)))
        self.enterContext(patch.object(queue, "time", NS(time=lambda: self.now)))
        self.enterContext(patch.object(route, "runtime_snapshot", return_value={"snapshot_id": "fixture"}))
        self.enterContext(patch.object(queue, "typing", AsyncMock()))
        self.question = "Новое уточнение: " + "x" * 7000 + ". Прежний формат отменен, используй CSV."
        self.uid, self.topic, self.mid, self.ident = 7, 4, 44, 55
        self.msg = NS(chat=NS(id=route.GROUP, type="supergroup"), from_user=NS(id=self.uid, is_bot=False),
                      message_id=self.mid, is_topic_message=True, message_thread_id=self.topic,
                      sender_chat=None, business_connection_id=None, text=self.question, caption=None,
                      reply_text=AsyncMock(return_value=NS(message_id=145)))
        self.enterContext(patch.object(queue.Update, "de_json", return_value=NS(message=self.msg, update_id=self.ident)))
        self.app = NS(running=True, bot=NS(id=route.BOT))
        body = json.dumps({"update_id": self.ident, "message": {
            "message_id": self.mid, "is_topic_message": True, "message_thread_id": self.topic,
            "chat": {"id": route.GROUP, "type": "supergroup"}, "from": {"id": self.uid},
            "text": self.question}})
        self.assertEqual(store.pending(self.uid, self.ident, body), "queued")
        self.checkpoints = store.Checkpoints(self.ident)

    def job(self, *, source_id=None, question=None, uid=None):
        value = {"destination": [route.GROUP, self.uid if uid is None else uid, self.topic, self.mid],
                 "question": self.question if question is None else question, "doc_id": source_id}
        self.checkpoints.put("route:input:v1", value)

    async def run_job(self):
        claimed = store.claim_next()
        self.assertIsNotNone(claimed)
        await queue.run_job(self.app, asyncio.Event(), *claimed)

    def recover(self):
        store.recover()
        self.now += (store.next_retry_delay() or 0) + 1

    def result(self):
        with store.database() as con:
            rows = con.execute("SELECT id,prompt,answer FROM turns WHERE uid=? AND topic=?",
                               (self.uid, self.topic)).fetchall()
        self.assertEqual(len(rows), 1)
        ident, question, answer = rows[0]
        return question, answer, store.result_details(self.uid, self.topic, ident)

    async def test_terminal_failure_keeps_full_checkpoint_question_and_source_scope(self):
        source_id = store.archive(self.uid, self.topic, "Synthetic source.txt", "text", b"Synthetic source")
        self.job(source_id=source_id)
        self.msg.text = "/read 1 short transport text"
        issue = ModelIssue("context", "task_contract", stop_reason="context_length_exceeded")
        with patch.object(route, "group_message", AsyncMock(side_effect=issue)):
            await self.run_job()
        self.assertIsNone(store.state(self.ident))
        prompt, answer, details = self.result()
        self.assertEqual(prompt, self.question)
        self.assertEqual(answer, issue.public())
        self.assertEqual(details["task_status"], "failed")
        self.assertEqual(details["quality_metadata"]["stop_reason"], "context_length_exceeded")
        self.assertEqual(details["delivery_status"], "confirmed")
        self.assertEqual(store.history(self.uid, self.topic, source_id=source_id)[0]["content"], self.question)
        for uid, topic, source in ((8, self.topic, source_id), (self.uid, 5, source_id),
                                   (self.uid, self.topic, None), (self.uid, self.topic, source_id + 1)):
            self.assertEqual(store.history(uid, topic, source_id=source), [])
        for number in range(1, 11):
            store.remember(self.uid, self.topic, "followup " + str(number), "response", source_id=source_id,
                           message_id=self.mid + number)
        self.assertEqual(store.history(self.uid, self.topic, source_id=source_id)[0]["content"], self.question)
        report = store.explain_request("update:55", uid=self.uid, topic=self.topic)
        self.assertNotIn(self.question, json.dumps(report))
        self.assertNotIn("Прежний формат", json.dumps(report, ensure_ascii=False))

    async def test_raw_message_is_retained_when_failure_precedes_prepared_input(self):
        with patch.object(route, "group_message", AsyncMock(side_effect=ModelIssue("context", "source_selection"))):
            await self.run_job()
        self.assertEqual(self.result()[0], self.question)

    async def test_foreign_prepared_input_never_enters_this_members_history(self):
        self.job(question="OTHER PRIVATE QUESTION", source_id=900, uid=8)
        self.checkpoints.put("route:answer:v1", {"text": "OTHER PRIVATE ANSWER", "images": []})
        with patch.object(route, "group_message", AsyncMock(side_effect=ModelIssue("context", "source_selection"))):
            await self.run_job()
        prompt, answer, _ = self.result()
        self.assertEqual(prompt, self.question)
        self.assertNotIn("OTHER PRIVATE", answer)
        self.assertEqual(store.history(self.uid, self.topic, source_id=900), [])

    async def test_crash_after_cause_commit_does_not_rerun_model_or_duplicate_cause(self):
        self.job()
        actual = store.audit_event
        interrupted = [False]
        def commit_then_interrupt(key, event, *args, **kwargs):
            result = actual(key, event, *args, **kwargs)
            if event == "model_failure" and not interrupted[0]:
                interrupted[0] = True
                raise asyncio.CancelledError()
            return result
        with patch.object(route, "group_message", AsyncMock(side_effect=ModelIssue("context", "input"))) as model:
            with patch.object(store, "audit_event", side_effect=commit_then_interrupt):
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_job()
            self.assertFalse(self.checkpoints.get("route:diagnostic:v1")["recorded"])
            self.msg.reply_text.assert_not_awaited()
            self.recover()
            await self.run_job()
            model.assert_awaited_once()
        self.assertEqual(self.result()[0], self.question)
        report = store.explain_request("update:55")
        self.assertEqual(sum(event["event"] == "model_failure" for event in report["events"]), 1)
        self.msg.reply_text.assert_awaited_once()

    async def test_crash_after_remember_replays_error_once_and_keeps_one_turn(self):
        self.job()
        with patch.object(route, "group_message", AsyncMock(side_effect=ModelIssue("context", "input"))) as model:
            with patch.object(route, "error_reply", AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_job()
            self.assertTrue(self.checkpoints.get("route:diagnostic:v1")["recorded"])
            self.assertEqual(self.result()[2]["delivery_status"], "pending")
            self.msg.reply_text.assert_not_awaited()
            self.recover()
            await self.run_job()
            model.assert_awaited_once()
        self.assertEqual(self.result()[2]["delivery_status"], "confirmed")
        self.msg.reply_text.assert_awaited_once()
        self.assertIsNone(store.state(self.ident))

    async def test_transient_park_never_adds_a_failed_turn_or_repeats_same_action(self):
        self.job()
        with patch.object(route, "group_message", AsyncMock(side_effect=[ModelIssue("busy", "http"), None])) as model:
            await self.run_job()
            self.assertEqual(store.state(self.ident), "queued")
            self.assertIsNone(self.checkpoints.get("route:diagnostic:v1"))
            self.assertEqual(store.history(self.uid, self.topic), [])
            self.recover()
            await self.run_job()
            self.assertEqual(model.await_count, 2)

    async def test_error_receipt_does_not_overwrite_or_confirm_a_generated_answer(self):
        self.job()
        details = {"task_status": "complete", "generation_complete": True,
                   "missing_requirements": [], "quality_metadata": {"completion_review": "passed"}}
        async def saved_then_failed(out):
            store.remember(self.uid, self.topic, self.question, "Complete generated answer",
                           source_id=None, message_id=self.mid, result_details=details,
                           request_key=(out.ident, "turn"))
            raise ModelIssue("context", "post_generation")
        with patch.object(route, "group_message", side_effect=saved_then_failed):
            await self.run_job()
        _, answer, saved = self.result()
        self.assertEqual(answer, "Complete generated answer")
        self.assertEqual(saved["task_status"], "complete")
        self.assertEqual(saved["delivery_status"], "pending")
        self.assertFalse(saved["quality_metadata"].get("terminal_failure"))

    async def test_legacy_generated_answer_does_not_gain_failed_or_delivered_status(self):
        self.job()
        async def saved_then_failed(out):
            store.remember(self.uid, self.topic, self.question, "Legacy generated answer",
                           source_id=None, message_id=self.mid, request_key=(out.ident, "turn"))
            raise ModelIssue("context", "post_generation")
        with patch.object(route, "group_message", side_effect=saved_then_failed):
            await self.run_job()
        _, answer, saved = self.result()
        self.assertEqual(answer, "Legacy generated answer")
        self.assertEqual(saved["task_status"], "unknown")
        self.assertNotEqual(saved["delivery_status"], "confirmed")
        self.assertFalse(saved["quality_metadata"].get("terminal_failure"))

    async def test_operator_retry_keeps_failure_and_stores_the_actual_successful_answer(self):
        job = {"destination": [route.GROUP, self.uid, self.topic, self.mid], "question": self.question,
               "doc_id": None, "history": [], "quote": "", "model": "fixture", "draw": False, "research": False}
        self.checkpoints.put("route:input:v1", job)
        with patch.object(route, "group_message", AsyncMock(side_effect=KeyError("synthetic private error"))):
            await self.run_job()
        self.assertEqual(store.state(self.ident), "failed")
        failed = store.recorded_turn(self.uid, self.topic, (self.ident, "failed_turn"))
        self.assertIsNotNone(failed)
        self.assertIsNone(store.recorded_turn(self.uid, self.topic, (self.ident, "turn")))
        for uid, topic in ((8, self.topic), (self.uid, 5)):
            with self.assertRaises(ValueError):
                store.recorded_turn(uid, topic, (self.ident, "failed_turn"))
        # Explicit operator retry: discard only the old error's transport receipt,
        # while retaining both private history and idempotent write receipts.
        with store.database() as con:
            con.execute("UPDATE requests SET state='queued' WHERE id=?", (self.ident,))
            con.execute("DELETE FROM checkpoints WHERE request_id=? AND key='outbound.single_text'", (self.ident,))
            con.execute("DELETE FROM delivery_parts WHERE request_id=?", (self.ident,))
        response = Result("Actual completed answer after operator retry", [], 0, task_status="complete",
                          quality_metadata={"generation_audit": {"generation_complete": True}}, stop_reason="completed")
        async def continue_work(out):
            await route.prepared_answer(out, job, out.checkpoints)
        with patch.object(route, "group_message", side_effect=continue_work), patch.object(route, "respond", return_value=response):
            await self.run_job()
        self.assertIsNone(store.state(self.ident))
        history = store.history(self.uid, self.topic, source_id=None)
        self.assertEqual([row["content"] for row in history[::2]], [self.question, self.question])
        self.assertEqual(history[-1]["content"], response.text)
        with store.database() as con:
            rows = con.execute("SELECT id FROM turns WHERE uid=? AND topic=? ORDER BY id", (self.uid, self.topic)).fetchall()
        self.assertEqual(len(rows), 2)
        first, second = [store.result_details(self.uid, self.topic, row[0]) for row in rows]
        self.assertEqual(first["task_status"], "failed")
        self.assertEqual(second["task_status"], "complete")
        self.assertEqual(second["delivery_status"], "confirmed")
        self.assertEqual(self.msg.reply_text.await_count, 2)
        self.assertEqual(self.msg.reply_text.await_args.args[0], response.text)


if __name__ == "__main__":
    unittest.main()
