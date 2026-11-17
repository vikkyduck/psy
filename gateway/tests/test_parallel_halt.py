"""A 429 in one parallel stage parks the job: no new section starts and nothing written is lost."""
import copy
import json
import re
import threading
import unittest
from collections import Counter
from contextlib import nullcontext
from unittest.mock import patch

from psy_modules import psy_group_codex as model


class Memory:
    """Checkpoint double with the same JSON round trip as the SQLite store."""

    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.data.get(key))

    def put(self, key, value):
        with self.lock:
            self.data[key] = json.loads(json.dumps(value))


PLAN = {"title": "Synthetic lecture", "sections": [
    {"title": f"Chapter {i}", "brief": "argument", "minutes": 15, "activity_minutes": 0} for i in range(1, 7)]}


class ParallelHalt(unittest.TestCase):
    def setUp(self):
        for name, value in (("_connection", lambda rejected=None: ("fixture", {}, "fixture-generation")),
                            ("_transport", nullcontext)):
            patcher = patch.object(model, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_lecture(self, stream, checkpoints):
        with patch.object(model, "_stream", stream):
            return model._compose(None, [model._user("synthetic lecture request")], False, "fixture",
                                  "synthetic lecture request", checkpoints=checkpoints)

    def test_rate_limit_stops_remaining_sections_and_resume_reuses_written_ones(self):
        calls = Counter()
        busy = True
        refused = threading.Event()
        first = threading.Event()

        def stream(client, headers, messages, web, selected, *, instructions, **kwargs):
            if model.DISPATCH in instructions:
                return model.Result('PSY_TASK {"minutes":90}', [], 0)
            if "Сейчас нужен внутренний редакционный" in instructions:
                return model.Result(json.dumps(PLAN), [], 0)
            if match := re.search(r"Напиши целиком только раздел (\d+)", instructions):
                index = int(match.group(1))
                calls[index] += 1
                if busy and index == 1:
                    first.set()
                if busy and index == 2:
                    self.assertTrue(first.wait(3))
                    refused.set()
                    raise model.ModelIssue("busy", "http")
                if busy:
                    # Sections already streaming when the 429 arrives still finish.
                    self.assertTrue(refused.wait(3))
                return model.Result(f"chapter {index} text. " * 20, [], 0)
            if "Проверь содержание раздела" in instructions:
                calls["review"] += 1
                return model.Result('{"missing":""}', [], 0)
            self.fail("unexpected stage")

        saved = Memory()
        with self.assertRaises(model._ResumeModelJob) as parked:
            self.run_lecture(stream, saved)
        self.assertEqual(parked.exception.issue.kind, "busy")
        # Section 3 may or may not have been picked up before the 429. Nothing later starts.
        self.assertEqual((calls[1], calls[2], calls[4], calls[5], calls[6], calls["review"]), (1, 1, 0, 0, 0, 0))

        busy = False
        result = self.run_lecture(stream, saved)
        self.assertTrue(result.complete)
        self.assertEqual(calls, Counter({1: 1, 2: 2, 3: 1, 4: 1, 5: 1, 6: 1, "review": 6}))
        for index in range(1, 7):
            self.assertIn(f"chapter {index} text.", result.text)

    def test_rate_limited_search_stops_the_independent_central_reader(self):
        reads = 0
        refused, reading = threading.Event(), threading.Event()

        def stream(client, headers, messages, web, selected, *, instructions, **kwargs):
            nonlocal reads
            if model.DISPATCH in instructions:
                return model.Result('PSY_TASK {"minutes":60,"public_topic":"Freud"}', [], 0)
            if instructions == model.CENTRAL_READING:
                reads += 1
                reading.set()
                self.assertTrue(refused.wait(3))
                # An unfinished first pass would normally ask the reader for a second one.
                return model.Result('{"scope":"partial","items":[],"gaps":["second pass needed"]}', [], 1)
            if instructions == model.WEB_SYSTEM:
                self.assertTrue(reading.wait(3))
                refused.set()
                raise model.ModelIssue("busy", "http")
            self.fail("unexpected stage")

        with patch.object(model, "_stream", stream), self.assertRaises(model._ResumeModelJob):
            model._compose(None, [model._user("synthetic request")], False, "fixture",
                           "Публичная тема: Sigmund Freud, Die Verdrängung", checkpoints=Memory())
        self.assertEqual(reads, 1)

    def test_without_checkpoints_a_failed_section_still_leaves_a_gap(self):
        def stream(client, headers, messages, web, selected, *, instructions, **kwargs):
            if model.DISPATCH in instructions:
                return model.Result('PSY_TASK {"minutes":90}', [], 0)
            if "Сейчас нужен внутренний редакционный" in instructions:
                return model.Result(json.dumps(PLAN), [], 0)
            if match := re.search(r"Напиши целиком только раздел (\d+)", instructions):
                if match.group(1) == "2":
                    raise model.ModelIssue("busy", "http")
                return model.Result("text. " * 20, [], 0)
            return model.Result('{"missing":""}', [], 0)

        result = self.run_lecture(stream, None)
        self.assertFalse(result.complete)
        self.assertIn("[Этот раздел не сформирован.]", result.text)


if __name__ == "__main__":
    unittest.main()
