"""Member deletion, checkpoint pruning and the operator CLI for jobs that need a decision."""
import asyncio
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_delivery_resume as harness
from test_delivery_resume import queue, route, store
from psy_modules import psy_group_admin as admin


def message(text, *, uid=10, topic=1, mid=50):
    return NS(chat=NS(id=route.GROUP, type="supergroup"), from_user=NS(id=uid, is_bot=False), text=text,
              caption=None, business_connection_id=None, sender_chat=None, is_topic_message=bool(topic),
              message_thread_id=topic or None, message_id=mid, document=None, photo=(), reply_to_message=None,
              reply_text=AsyncMock(return_value=NS(message_id=mid + 1000)))


class MemberDelete(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        database = store.database
        self.enterContext(patch.object(store, "database", lambda *a, **kw: database(self.root)))

    async def test_member_deletes_only_their_own_file_and_its_bytes(self):
        marker = b"synthetic-private-marker-" + os.urandom(8).hex().encode()
        data = marker + b"\n" + b"z" * 12000 + marker
        doc = store.archive(10, 1, "notes.txt", "text", data, message_id=40)
        store.set_current(10, 1, doc, 40)
        store.remember(10, 1, "what is in it?", "an answer", source_id=doc, message_id=41)
        other = store.archive(11, 1, "other.txt", "text", b"other member", message_id=42)

        foreign = message(f"/delete {other}")
        await route.group_message(foreign)
        foreign.reply_text.assert_awaited_once_with("Материал с этим номером в Вашем кабинете не найден.")
        self.assertIsNotNone(store.document(11, 1, other))

        own = message(f"/delete@{route.BOT_USERNAME} {doc}")
        await route.group_message(own)
        own.reply_text.assert_awaited_once_with(f"Материал {doc} удален из Ваших материалов.")
        self.assertIsNone(store.document(10, 1, doc))
        self.assertEqual(store.listing(10, 1), [])
        self.assertEqual(store.message_source(10, 1, 40)[0], None)
        self.assertEqual(store.choices(10, 1), (None, []))
        # The member's own discussion stays, but no later file can inherit it.
        self.assertEqual([t["content"] for t in store.history(10, 1)], ["what is in it?", "an answer"])
        self.assertEqual(store.history(10, 1, source_id=doc), [])
        for name in os.listdir(self.root):
            if name.startswith("group.sqlite3"):
                self.assertNotIn(marker, (self.root / name).read_bytes(), name)

        for text in ("/delete", "/delete abc", "/delete 0"):
            bad = message(text)
            await route.group_message(bad)
            bad.reply_text.assert_awaited_once_with("Укажите номер из /files.")

    def test_replayed_deletion_reports_the_same_result(self):
        doc = store.archive(10, 1, "notes.txt", "text", b"text")
        self.assertEqual(store.pending(10, 5, "synthetic request"), "queued")
        self.assertTrue(store.forget(10, 1, doc, request_key=(5, "delete")))
        self.assertTrue(store.forget(10, 1, doc, request_key=(5, "delete")))
        self.assertFalse(store.forget(10, 1, doc))
        with self.assertRaises(ValueError):
            store.forget(99, 1, doc, request_key=(5, "delete"))


class CheckpointPruning(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def job(self, ident):
        store.pending(7, ident, f"request {ident}", root=self.root)
        store.claim_next(root=self.root)
        return store.Checkpoints(ident, root=self.root)

    def keys(self, ident):
        with store.database(self.root) as con:
            return {k for (k,) in con.execute("SELECT key FROM checkpoints WHERE request_id=?", (ident,))}

    def test_final_answer_replaces_model_stages(self):
        cp = self.job(1)
        for key in ("route:input:v1", "model:v1:a", "model:v1:b", "prose:v2:c"):
            cp.put(key, {"text": "x" * 1000})
        cp.put("route:answer:v1", {"text": "final", "images": []})
        self.assertEqual(self.keys(1), {"route:input:v1", "route:answer:v1"})

    def test_uncertain_job_keeps_only_what_a_requeue_needs(self):
        cp = self.job(1)
        cp.put("route:input:v1", {})
        cp.put("route:answer:v1", {"text": "final", "images": []})
        with store.database(self.root) as con:  # A job written by the previous release.
            con.execute("INSERT INTO checkpoints VALUES(1,'model:v1:old','{}')")
        store.settle(1, False, root=self.root)
        self.assertEqual(self.keys(1), {"route:input:v1", "route:answer:v1"})
        cp2 = self.job(2)
        cp2.put("model:v1:draft", {"text": "partial"})
        store.settle(2, False, root=self.root)
        self.assertEqual(self.keys(2), {"model:v1:draft"})


class OperatorCli(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def run_cli(self, *args):
        out = io.StringIO()
        code = admin.main(["--root", str(self.root), *args], out=out)
        return code, out.getvalue()

    def prepare(self):
        for ident in (1, 2, 3, 4):
            store.pending(ident, ident, f"request {ident}", root=self.root)
        for _ in range(4):
            store.claim_next(root=self.root)
        # 1: an answer part was confirmed and the next one may have reached the group.
        store.Checkpoints(1, root=self.root).put("route:answer:v1", {"text": "t", "images": []})
        store.begin_delivery(1, 0, "a", root=self.root)
        store.confirm_delivery(1, 0, 501, root=self.root)
        store.begin_delivery(1, 1, "b", root=self.root)
        # 2: an unexpected error was reported to the member.
        store.Checkpoints(2, root=self.root).put("outbound.single_text", {"text": "error"})
        store.begin_delivery(2, 0, "error", root=self.root)
        store.confirm_delivery(2, 0, 502, root=self.root)
        store.fail(2, root=self.root)
        # 3: waits for a model retry. 4 stays running.
        store.wait_for_model(3, "busy", root=self.root, now=1000)
        store.recover(root=self.root)  # 1 becomes uncertain; 4 is requeued.
        store.claim_next(root=self.root, now=1000)

    def test_list_shows_metadata_only(self):
        self.prepare()
        code, text = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("1  uncertain", text)
        self.assertIn("2  failed", text)
        self.assertIn("3  queued", text)
        self.assertIn("retry 1 (busy)", text)
        self.assertIn("answer saved", text)
        self.assertNotRegex(text, r"(?m)^4  ")
        self.assertNotIn("request 1", text)
        self.assertIn("4  running", self.run_cli("list", "--all")[1])

    def test_requeue_uncertain_resends_only_the_ambiguous_part(self):
        self.prepare()
        self.assertEqual(self.run_cli("resolve", "1", "requeue"), (0, "1: requeue\n"))
        self.assertEqual(store.state(1, root=self.root), "queued")
        self.assertEqual(store.claim_next(root=self.root), (1, "request 1"))
        self.assertEqual(store.begin_delivery(1, 0, "a", root=self.root), ("confirmed", 501))
        self.assertEqual(store.begin_delivery(1, 1, "b", root=self.root)[0], "send")

    def test_requeue_failed_job_replaces_the_error_notice(self):
        self.prepare()
        self.run_cli("resolve", "2", "requeue")
        self.assertEqual(store.claim_next(root=self.root), (2, "request 2"))
        self.assertIsNone(store.Checkpoints(2, root=self.root).get("outbound.single_text"))
        self.assertEqual(store.begin_delivery(2, 0, "answer", root=self.root)[0], "send")

    def test_delivered_and_drop_finish_jobs_and_block_redelivery(self):
        self.prepare()
        self.run_cli("resolve", "1", "delivered")
        self.run_cli("resolve", "3", "drop")
        for ident in (1, 3):
            self.assertIsNone(store.state(ident, root=self.root))
            self.assertEqual(store.pending(ident, ident, "redelivered", root=self.root), "existing")

    def test_running_and_unknown_jobs_and_lost_text_are_refused(self):
        self.prepare()
        for args in (("4", "drop"), ("3", "requeue"), ("3", "delivered"), ("99", "drop")):
            with self.assertRaises(SystemExit) as stopped, patch("sys.stderr", io.StringIO()):
                self.run_cli("resolve", *args)
            self.assertEqual(stopped.exception.code, 1)
        with store.database(self.root) as con:
            con.execute("UPDATE requests SET body='' WHERE id=1")
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            self.run_cli("resolve", "1", "requeue")
        self.assertEqual(store.state(4, root=self.root), "running")

    def test_refuses_root_and_a_missing_vault(self):
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_cli("list")
            self.prepare()
            with patch.object(admin.os, "geteuid", return_value=0), self.assertRaises(SystemExit):
                self.run_cli("list")

    def test_runs_by_path_without_the_package(self):
        self.prepare()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        done = subprocess.run([sys.executable, "-I", admin.__file__, "--root", str(self.root), "list"],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("2  failed", done.stdout)


class RequeueThroughQueue(unittest.IsolatedAsyncioTestCase):
    setUp = harness.Delivery.setUp

    async def test_operator_requeue_delivers_the_real_answer_after_a_defect(self):
        sent = []

        async def telegram(text, **kwargs):
            sent.append(text)
            return NS(message_id=900 + len(sent))

        self.message.reply_text.side_effect = telegram
        defect = True

        async def group(out):
            if defect:
                raise KeyError("synthetic defect")
            await out.reply_text("real answer")

        with patch.object(route, "group_message", group):
            await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
            self.assertEqual(store.state(self.ident), "failed")
            defect = False
            store.resolve(self.ident, "requeue")
            await queue.run_job(self.app, asyncio.Event(), *store.claim_next())
        self.assertEqual(sent, ["Не удалось завершить запрос. Попробуйте еще раз.", "real answer"])
        self.assertIsNone(store.state(self.ident))


if __name__ == "__main__":
    unittest.main()
