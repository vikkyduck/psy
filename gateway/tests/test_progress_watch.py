import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from psy_modules import psy_group_client as codex, psy_group_codex as model, psy_group_store as store


class ProgressWatch(unittest.TestCase):
    def test_commentary_alone_cannot_extend_generation(self):
        clock = [0]
        def lines():
            yield 'data: ' + json.dumps({"type":"response.output_item.added", "item":{"id":"c", "phase":"commentary"}})
            for _ in range(41):
                clock[0] += 45
                yield 'data: ' + json.dumps({"type":"response.output_text.delta", "item_id":"c", "delta":"working"})
        client = Mock(stream=Mock(return_value=nullcontext(Mock(status_code=200, iter_lines=lines))))
        with patch.object(codex.time, "monotonic", side_effect=lambda:clock[0]):
            with self.assertRaises(model.ModelIssue):
                codex._stream(client, {}, [], False, "test")
        self.assertLessEqual(clock[0], 315)

    def test_keepalive_cannot_hold_a_stage_forever(self):
        clock = [0]
        def lines():
            for _ in range(41):
                clock[0] += 45
                yield ": keepalive"
            yield 'data: ' + json.dumps({"type":"response.completed"})
        client = Mock(stream=Mock(return_value=nullcontext(Mock(status_code=200, iter_lines=lines))))
        with patch.object(codex.time, "monotonic", side_effect=lambda:clock[0]):
            with self.assertRaises(model.ModelIssue) as error:
                codex._stream(client, {}, [], False, "test")
        self.assertEqual(error.exception.kind, "timeout")
        self.assertLessEqual(clock[0], 315)

    def test_real_progress_can_continue_for_more_than_a_day(self):
        clock = [0]
        def lines():
            for _ in range(1500):
                clock[0] += 60
                yield 'data: ' + json.dumps({"type":"response.reasoning_summary_text.delta", "delta":"progress"})
            yield 'data: ' + json.dumps({"type":"response.output_text.delta", "delta":"finished"})
            yield 'data: ' + json.dumps({"type":"response.completed"})
        report = Mock()
        client = Mock(stream=Mock(return_value=nullcontext(Mock(status_code=200, iter_lines=lines))))
        with patch.object(codex.time, "monotonic", side_effect=lambda:clock[0]):
            result = codex._stream(client, {}, [], False, "test", on_progress=report)
        self.assertEqual(result.text, "finished")
        self.assertGreater(clock[0], 86400)
        self.assertGreater(report.call_count, 1)

    def test_chapters_preserved_when_stalled_stage_is_requeued(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store.pending(5, 1, "synthetic", root=root)
            store.claim_next(root=root)
            checkpoint = store.Checkpoints(1, root=root)
            for i in range(3):
                checkpoint.put(f"chapter.{i}", {"text":f"completed {i}"})
            checkpoint.progress(now=1234)
            store.wait_for_model(1, "timeout", root=root, now=1300)
            self.assertIsNone(store.claim_next(root=root, now=1359))
            store.recover(root=root)
            self.assertEqual(store.claim_next(root=root, now=1360)[0], 1)
            for i in range(3):
                self.assertEqual(store.Checkpoints(1, root=root).get(f"chapter.{i}"), {"text":f"completed {i}"})
            with store.database(root) as db:
                self.assertEqual(db.execute("select stage,updated from job_progress").fetchone(), ("started",1360))
