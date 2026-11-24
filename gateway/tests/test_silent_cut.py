"""A deep call cut off after a long silence makes the rest of the job think at the fallback effort.
Synthetic streams only."""
import json
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from psy_modules import psy_group_client as codex, psy_group_store as store


def event(**fields):
    return "data: " + json.dumps(fields)


class SilentCut(unittest.TestCase):
    def setUp(self):
        httpx = types.ModuleType("httpx")
        httpx.RequestError = type("RequestError", (Exception,), {})
        httpx.TimeoutException = type("TimeoutException", (httpx.RequestError,), {})
        self.httpx = httpx
        for patcher in (patch.dict(sys.modules, {"httpx": httpx}),
                        patch.object(codex, "_connection", return_value=("fixture", {}, "fixture-generation"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        store.pending(5, 1, "synthetic", root=self.root)
        store.claim_next(root=self.root)
        self.clock = [0]
        self.efforts = []
        self.stages = []

    def client(self, *streams):
        """One synthetic SSE stream per request; records the effort each request asked for."""
        pending = list(streams)
        def open_stream(*args, **kwargs):
            self.efforts.append(kwargs["json"].get("reasoning", {}).get("effort"))
            return nullcontext(Mock(status_code=200, iter_lines=pending.pop(0)))
        return Mock(stream=Mock(side_effect=open_stream))

    def stage(self):
        with store.database(self.root) as db:
            return db.execute("select stage from job_progress where request_id=1").fetchone()[0]

    def silent(self, seconds, *, visible_first=False, fragment=False, cut=None):
        def lines():
            self.stages.append(self.stage())
            if visible_first:
                yield event(type="response.reasoning_summary_text.delta", delta="thinking")
            for _ in range(seconds // 60):
                self.clock[0] += 60
                yield event(type="response.output_item.done",
                            item={"type": "web_search_call", "status": "completed", "action": {}})
            if fragment:
                # A whole message item without deltas, as the provider sends between searches.
                yield event(type="response.output_item.done",
                            item={"id": "m", "type": "message", "content": [{"type": "output_text", "text": "{\"claims\": ["}]})
            if cut:
                raise cut
        return lines

    def answer(self):
        def lines():
            yield event(type="response.output_item.done",
                        item={"type": "web_search_call", "status": "completed", "action": {}})
            yield event(type="response.output_text.delta", item_id="a", delta="готово")
            yield event(type="response.completed")
        return lines

    def run_stage(self, client, prompt="вопрос", *, web=False):
        call, _ = codex._job_calls(client, "test", store.Checkpoints(1, root=self.root))
        call.deep = True
        with patch.object(codex.time, "monotonic", side_effect=lambda: self.clock[0]):
            return call([codex._user(prompt)], "instr", prose=not web, web=web)

    def test_silent_deep_stage_is_retried_at_fallback_effort(self):
        # The client's own silence limit (DEEP_IDLE_SECONDS) ends the first attempt.
        with self.assertRaises(codex._ResumeModelJob) as deferred:
            self.run_stage(self.client(self.silent(codex.DEEP_IDLE_SECONDS + 60)))
        self.assertEqual(deferred.exception.issue.kind, "timeout")
        result = self.run_stage(self.client(self.answer()))
        self.assertEqual(result.text, "готово")
        self.assertEqual(self.efforts, [codex.DEEP_EFFORT, codex.FALLBACK_EFFORT])
        self.assertEqual(self.stages, [codex.DEEP_PROGRESS_STAGE])

    def test_provider_closing_a_silent_stream_counts_as_a_silent_cut(self):
        # 2026-11-24: the provider closed a silent max-effort stream exactly 15 minutes in.
        closed = self.httpx.RequestError("closed")
        with self.assertRaises(codex._ResumeModelJob) as deferred:
            self.run_stage(self.client(self.silent(840, cut=closed)))
        self.assertEqual((deferred.exception.issue.kind, deferred.exception.issue.stage), ("connection", "stream_read"))
        self.run_stage(self.client(self.answer()))
        self.assertEqual(self.efforts, [codex.DEEP_EFFORT, codex.FALLBACK_EFFORT])

    def test_fragment_from_a_cut_source_check_lowers_the_next_differently_worded_check(self):
        # The source check keeps the fragment as an incomplete result and asks again with an
        # extended prompt, so the mark must hold for the job, not for one prompt.
        closed = self.httpx.RequestError("closed")
        fragment = self.run_stage(self.client(self.silent(840, fragment=True, cut=closed)), web=True)
        self.assertFalse(fragment.complete)
        self.run_stage(self.client(self.answer()), "вопрос\nДополни проверку", web=True)
        self.assertEqual(self.efforts, [codex.DEEP_EFFORT, codex.FALLBACK_EFFORT])

    def test_short_drop_or_visible_thinking_keeps_the_deep_effort(self):
        closed = self.httpx.RequestError("closed")
        for stream in (self.silent(120, cut=closed), self.silent(840, visible_first=True, cut=closed)):
            with self.assertRaises(codex._ResumeModelJob):
                self.run_stage(self.client(stream))
        self.run_stage(self.client(self.answer()))
        self.assertEqual(self.efforts, [codex.DEEP_EFFORT] * 3)

    def test_mark_is_dropped_with_the_model_stages_once_the_answer_is_saved(self):
        with self.assertRaises(codex._ResumeModelJob):
            self.run_stage(self.client(self.silent(codex.DEEP_IDLE_SECONDS + 60)))
        checkpoints = store.Checkpoints(1, root=self.root)
        self.assertEqual(checkpoints.get(codex.EFFORT_MARK), {"silent_cuts": 1})
        checkpoints.put(store.ANSWER, {"text": "ответ"})
        self.assertIsNone(checkpoints.get(codex.EFFORT_MARK))


if __name__ == "__main__":
    unittest.main()
