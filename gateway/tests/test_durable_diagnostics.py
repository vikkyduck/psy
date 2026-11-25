"""Failure provenance, private snapshots and full history use synthetic data only."""
import asyncio
import json
import sqlite3
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from psy_modules import psy_group_store as store, psy_group_route as route
from psy_modules import psy_group_observability as observation
from psy_modules.psy_group_client import ModelIssue


class DurableDiagnostics(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_terminal_failure_survives_queue_cleanup_and_is_scoped(self):
        store.pending(7, 11, json.dumps({"message": {"message_id": 22, "is_topic_message": True,
                      "message_thread_id": 4, "text": "private request body"}}), root=self.root)
        store.audit_start('update:11', 7, 4, 22, {'snapshot_id': 'hash'}, root=self.root)
        store.audit_event('update:11', 'model_failure', {'kind': 'context', 'stage': 'stream'},
                          state='failed', root=self.root)
        store.settle(11, True, root=self.root)
        report = store.explain_request('update:11', uid=7, topic=4, root=self.root)
        self.assertEqual(report['state'], 'failed')
        self.assertEqual(report['events'][0]['details']['stage'], 'stream')
        self.assertNotIn('private request body', json.dumps(report))
        self.assertIsNone(store.explain_request('update:11', uid=8, topic=4, root=self.root))

    def test_export_excludes_private_contract_and_memory(self):
        details = {'task_status': 'partial', 'missing_requirements': ['secret requirement'],
                   'quality_metadata': {'task_contract': {'request': 'secret request'},
                    'context_state': {'summary': 'secret summary'}, 'source_gaps': ['secret source'],
                    'source_reason_codes': ['text_unavailable'], 'stop_reason': 'max_output_tokens'}}
        report = observation.result_audit(details)
        self.assertNotIn('secret', json.dumps(report))
        self.assertEqual(report['metadata']['stop_reason'], 'max_output_tokens')
        self.assertEqual(report['missing_requirements_count'], 1)

    def test_full_history_retains_long_correction_after_twelve_exchanges(self):
        instruction = 'Keep this constraint: ' + 'x' * 6000 + ' final exact condition'
        store.remember(7, 4, instruction, 'initial answer', root=self.root, message_id=1)
        for number in range(2, 14):
            store.remember(7, 4, f'next {number}', 'a' * 4500, root=self.root, message_id=number)
        history = store.history(7, 4, source_id=None, root=self.root)
        self.assertEqual(len(history), 26)
        self.assertEqual(history[0]['content'], instruction)
        self.assertEqual(history[-1]['content'], 'a' * 4500)
        self.assertEqual(store.history(8, 4, source_id=None, root=self.root), [])

    def test_saved_compaction_never_crosses_member_topic_source_or_future(self):
        state = {'memory': {'summary': 'private scoped summary'}}
        store.save_context(7, 4, 9, state, message_id=10, root=self.root)
        self.assertEqual(store.get_context(7, 4, 9, before_message_id=11, root=self.root), state)
        for args in ((8, 4, 9), (7, 5, 9), (7, 4, 10)):
            self.assertIsNone(store.get_context(*args, root=self.root))
        self.assertIsNone(store.get_context(7, 4, 9, before_message_id=10, root=self.root))
        store.save_context(7, 4, 9, {'older': True}, message_id=9, root=self.root)
        self.assertEqual(store.get_context(7, 4, 9, root=self.root), state)

    def test_snapshot_copies_full_private_state_and_detects_tampering(self):
        profile, code = self.root / 'profile', self.root / 'code'
        profile.mkdir()
        code.mkdir()
        (profile / 'config.yaml').write_text('synthetic-secret-setting')
        (profile / 'SOUL.md').write_text('synthetic private instruction')
        (code / 'psy_example.py').write_text('VALUE = 1\n')
        snapshot_root = self.root / 'snapshots'
        result = observation.capture_runtime('fixture', profile=profile, code_dir=code, root=snapshot_root)
        self.assertNotIn('synthetic-secret', json.dumps(result))
        snapshot = snapshot_root / result['snapshot_id']
        self.assertEqual((snapshot / 'profile/config.yaml').read_text(), 'synthetic-secret-setting')
        self.assertEqual(stat.S_IMODE((snapshot / 'profile/config.yaml').stat().st_mode), 0o600)
        self.assertEqual(result, observation.capture_runtime('fixture', profile=profile, code_dir=code,
                                                             root=snapshot_root))
        (snapshot / 'code/psy_example.py').write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            observation.capture_runtime('fixture', profile=profile, code_dir=code, root=snapshot_root)


class RouteFailureDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_issue_retained_without_transcript_or_private_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = store.database
            with patch.object(store, 'database', lambda *a, **kw: database(Path(tmp))), \
                    patch.object(route, 'runtime_snapshot', return_value={'snapshot_id': 'synthetic'}):
                msg = NS(chat=NS(id=-202), from_user=NS(id=7), message_id=33,
                         is_topic_message=False, reply_text=AsyncMock(return_value=NS(message_id=34)))
                issue = ModelIssue('context', 'request')
                issue.quality_metadata = {'context_state': {'summary': 'private conversation'},
                                          'generation_audit': {'effort_policy': 'maximum'}}
                with patch.object(route, 'group_message', AsyncMock(side_effect=issue)):
                    await route.process_group(msg)
                report = store.explain_request(route.audit_key(msg))
                self.assertEqual(report['state'], 'failed')
                self.assertNotIn('private conversation', json.dumps(report))
                self.assertTrue(any(e['event'] == 'model_failure' for e in report['events']))
                self.assertTrue(any(e['event'] == 'error_delivery' for e in report['events']))


class QueueDiagnosticRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_failure_audit_retry_survives_queue_cleanup(self):
        await self.terminal_failure_case("model_failure")

    async def test_error_delivery_receipt_survives_failed_audit_and_single_text_replay(self):
        await self.terminal_failure_case("error_delivery")

    async def terminal_failure_case(self, blocked_event):
        from test_delivery_resume import queue
        with tempfile.TemporaryDirectory() as tmp:
            database = store.database
            clock = [1_800_000_000.0]
            msg = NS(chat=NS(id=-202, type="supergroup"), from_user=NS(id=7, is_bot=False),
                     message_id=44, is_topic_message=False, message_thread_id=None,
                     sender_chat=None, business_connection_id=None,
                     reply_text=AsyncMock(return_value=NS(message_id=145)))
            body = json.dumps({"update_id": 55, "message": {"message_id": 44,
                    "chat": {"id": -202, "type": "supergroup"}, "from": {"id": 7},
                    "text": "private synthetic request"}})
            app = NS(running=True, bot=NS(id=route.BOT))
            issue = ModelIssue("context", "contract_input", stop_reason="context")
            issue.quality_metadata = {"generation_audit": {"failed_attempts": [
                {"error_kind": "context", "stop_stage": "contract_input", "effort": "max"}]},
                "context_state": {"summary": "private memory"}}
            actual_audit = store.audit_event
            failures = [0]
            def busy_once(key, event, *args, **kwargs):
                if event == blocked_event and not failures[0]:
                    failures[0] += 1
                    raise sqlite3.OperationalError("database is locked")
                return actual_audit(key, event, *args, **kwargs)
            with patch.object(store, "database", lambda *a, **kw: database(Path(tmp))), \
                    patch.object(store, "time", NS(time=lambda: clock[0])), \
                    patch.object(queue, "time", NS(time=lambda: clock[0])), \
                    patch.object(queue.Update, "de_json", return_value=NS(message=msg, update_id=55)), \
                    patch.object(queue, "typing", AsyncMock()), \
                    patch.object(route, "runtime_snapshot", return_value={"snapshot_id": "fixture"}), \
                    patch.object(route, "group_message", AsyncMock(side_effect=issue)) as model_work, \
                    patch.object(store, "audit_event", side_effect=busy_once):
                store.pending(7, 55, body)
                await queue.run_job(app, asyncio.Event(), *store.claim_next())
                self.assertEqual(store.state(55), "queued")
                pending = store.Checkpoints(55).get("route:diagnostic:v1")
                if blocked_event == "model_failure":
                    self.assertEqual(pending["details"]["stage"], "contract_input")
                    self.assertNotIn("private memory", json.dumps(pending))
                    msg.reply_text.assert_not_awaited()
                else:
                    self.assertIsNone(pending)
                    msg.reply_text.assert_awaited_once()
                # Reopen the durable queue as a new worker after the backoff.
                store.recover()
                clock[0] += store.next_retry_delay() + 1
                await queue.run_job(app, asyncio.Event(), *store.claim_next())
                model_work.assert_awaited_once()
                self.assertIsNone(store.state(55))
                with store.database() as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM checkpoints").fetchone()[0], 0)
                report = store.explain_request("update:55", uid=7, topic=0)
                self.assertEqual(report["state"], "failed")
                completed = [x for x in report["events"] if x["event"] == "queue_completed"]
                self.assertEqual(completed[-1]["details"], {"confirmed_parts": 1, "pending_parts": 0,
                                                           "delivery_confirmed": True})
                failures = [x for x in report["events"] if x["event"] == "model_failure"]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0]["details"]["stage"], "contract_input")
                self.assertEqual(failures[0]["details"]["stop_reason"], "context")
                self.assertNotIn("private synthetic request", json.dumps(report))
                self.assertNotIn("private memory", json.dumps(report))
                msg.reply_text.assert_awaited_once()

    async def test_early_command_finishes_audit_after_queue_cleanup(self):
        from test_delivery_resume import queue
        with tempfile.TemporaryDirectory() as tmp:
            database = store.database
            msg = NS(chat=NS(id=-202, type="supergroup"), from_user=NS(id=7, is_bot=False),
                     message_id=44, is_topic_message=False, message_thread_id=None,
                     sender_chat=None, business_connection_id=None,
                     reply_text=AsyncMock(return_value=NS(message_id=145)))
            async def command(out):
                await out.reply_text("Synthetic command result")
            body = json.dumps({"update_id": 55, "message": {"message_id": 44, "text": "/files"}})
            app = NS(running=True, bot=NS(id=route.BOT))
            with patch.object(store, "database", lambda *a, **kw: database(Path(tmp))), \
                    patch.object(queue.Update, "de_json", return_value=NS(message=msg, update_id=55)), \
                    patch.object(queue, "typing", AsyncMock()), \
                    patch.object(route, "runtime_snapshot", return_value={"snapshot_id": "fixture"}), \
                    patch.object(route, "group_message", side_effect=command):
                store.pending(7, 55, body)
                await queue.run_job(app, asyncio.Event(), *store.claim_next())
                self.assertIsNone(store.state(55))
                report = store.explain_request("update:55")
                self.assertEqual(report["state"], "finished")
                self.assertTrue(any(x["event"] == "action_completed" for x in report["events"]))
