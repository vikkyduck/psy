"""Count manuscript characters in actual Codex request payloads, without network access."""
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from psy_modules import psy_group_codex as model, psy_group_store as store


BLOCK_SIZE = 1024
WINDOW_BUDGET = 2048
# A different private-use Unicode character for each block. Static inputs cannot
# accidentally match. Inter-block newlines count as part of the manuscript.
PROSE = re.compile(r"[\ue000-\ue0ff]+(?:\n[\ue000-\ue0ff]+)*")
QUESTION = "Публичная тема: Синтетическая лекция"
PLAN_TITLE = "Неповторимый план"
SOURCE = "Выдержка из синтетического первоисточника"


def prose_fragments(value):
    if isinstance(value, str):
        yield from PROSE.findall(value)
    elif isinstance(value, list):
        for item in value:
            yield from prose_fragments(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from prose_fragments(item)


class LectureEndpoint:
    """Only the remote endpoint is synthetic. Inspect the JSON delivered to client.stream."""

    def __init__(self, block_count):
        self.blocks = [chr(0xe000 + i) * BLOCK_SIZE for i in range(block_count)]
        self.next_block = 0
        self.contexts = []
        self.inputs = []
        self.chapter_reviews = []
        self.source_reviews = []

    def stream(self, method, endpoint, *, headers, json, timeout):
        payload = json
        if method != "POST" or not endpoint.endswith("/responses") or payload["tools"]:
            raise AssertionError("Unexpected outbound request")
        text, complete = self.reply(payload)
        return events(text, complete)

    def reply(self, payload):
        instruction = payload["instructions"]
        complete = True
        if model.DISPATCH in instruction:
            text = 'PSY_TASK {"minutes":30,"public_topic":"Synthetic"}'
        elif "Сейчас нужен внутренний редакционный" in instruction:
            plan = {"title": PLAN_TITLE, "sections": [{"title": "Теория", "brief": "Тезис",
                    "minutes": 30, "activity_minutes": 0}]}
            text = encode(plan)
        elif "Напиши целиком только раздел" in instruction:
            self.contexts.append(list(prose_fragments(payload)))
            self.inputs.append(encode(payload["input"]))
            text = self.blocks[self.next_block]
            self.next_block += 1
            complete = self.next_block == len(self.blocks)
        elif "Проверь содержание раздела" in instruction:
            self.chapter_reviews.extend(prose_fragments(payload))
            text = '{"missing":""}'
        elif instruction == model.REVIEW:
            self.source_reviews.extend(prose_fragments(payload))
            text = '{"edits":[]}'
        else:
            raise AssertionError("Unknown stage of the synthetic lecture")
        return text, complete

    def compose(self, checkpoints=None):
        fake_http = types.ModuleType("httpx")
        fake_http.RequestError = type("RequestError", (Exception,), {})
        fake_http.TimeoutException = type("TimeoutException", (fake_http.RequestError,), {})
        source_cards = encode({"claims": [{"support": SOURCE}], "gaps": []})
        with patch.dict(sys.modules, {"httpx": fake_http}), patch.object(
                model, "_connection", return_value=("fixture", {}, "fixture-generation")), patch.object(
                model, "_primary_sources", return_value=model.Result(source_cards, [], 1)):
            return model._compose(self, [model._user(QUESTION)], False, "fixture", QUESTION,
                                  checkpoints=checkpoints)


class SupplementEndpoint(LectureEndpoint):
    def __init__(self):
        super().__init__(16)
        self.additions = [chr(0xe080 + i) * BLOCK_SIZE for i in range(6)]
        self.supplement_contexts = []
        self.supplement_inputs = []

    def reply(self, payload):
        if "Проверь содержание раздела" in payload["instructions"]:
            return '{"missing":"Сопоставить два случая."}', True
        if any("Сопоставить два случая." in str(turn) for turn in payload["input"]):
            self.supplement_contexts.append(list(prose_fragments(payload)))
            self.supplement_inputs.append(encode(payload["input"]))
            number = len(self.supplement_contexts)
            return self.additions[number - 1], number == len(self.additions)
        return super().reply(payload)


def events(text, complete):
    terminal = ({"type": "response.completed"} if complete else
                {"type": "response.incomplete", "response": {
                    "incomplete_details": {"reason": "max_output_tokens"}}})
    lines = ["data: " + encode({"type": "response.output_text.delta", "delta": text}),
             "data: " + encode(terminal)]
    return nullcontext(Mock(status_code=200, iter_lines=lambda: iter(lines)))


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class ContinuationCost(unittest.TestCase):
    def test_lecture_keeps_the_full_manuscript_but_resends_only_its_tail(self):
        totals = []
        for count in (64, 128):
            with self.subTest(block_count=count):
                endpoint = LectureEndpoint(count)
                result = endpoint.compose()
                manuscript = "\n".join(endpoint.blocks)
                self.assertTrue(result.complete)
                self.assertEqual(list(prose_fragments(result.text)), [manuscript])
                self.assertEqual(endpoint.chapter_reviews, [manuscript])
                self.assertEqual(endpoint.source_reviews, [manuscript])
                self.assertEqual(len(endpoint.contexts), count)
                self.assertEqual(endpoint.contexts[0], [])
                total = 0
                for index, context in enumerate(endpoint.contexts[1:], start=1):
                    self.assertEqual(len(context), 1)
                    tail = context[0]
                    self.assertTrue(tail.endswith(endpoint.blocks[index - 1]))
                    prior_end = index * (BLOCK_SIZE + 1) - 1
                    self.assertEqual(manuscript[prior_end - len(tail):prior_end], tail)
                    self.assertLessEqual(len(tail), WINDOW_BUDGET)
                    total += len(tail)
                for inputs in endpoint.inputs:
                    for expected in (QUESTION, PLAN_TITLE, SOURCE):
                        self.assertIn(expected, inputs)
                totals.append(total)
        if len(totals) == 2:
            self.assertLess(totals[1], 2.1 * totals[0])

    def test_supplement_does_not_repeat_its_parent_chapter_or_earlier_additions(self):
        endpoint = SupplementEndpoint()
        result = endpoint.compose()
        self.assertTrue(result.complete)
        self.assertEqual(list(prose_fragments(result.text)),
                         ["\n".join(endpoint.blocks), "\n".join(endpoint.additions)])
        self.assertEqual(len(endpoint.supplement_contexts), len(endpoint.additions))
        for index, parts in enumerate(endpoint.supplement_contexts):
            for expected in (QUESTION, PLAN_TITLE, SOURCE, "Сопоставить два случая."):
                self.assertIn(expected, endpoint.supplement_inputs[index])
            sent = "".join(parts)
            self.assertNotIn(endpoint.blocks[0], sent)
            self.assertLessEqual(sum(map(len, parts)), WINDOW_BUDGET)
            if index:
                self.assertIn(endpoint.additions[index - 1], sent)
            else:
                self.assertIn(endpoint.blocks[-1], sent)

    def test_restart_mid_chapter_reads_saved_parts_then_requests_only_the_remainder(self):
        interrupted = LectureEndpoint(12)
        answer = interrupted.reply

        def interrupt(payload):
            if interrupted.next_block == 7:
                raise model.ModelIssue("timeout", "stream_idle")
            return answer(payload)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(store.pending(7, 12, "{}", root=root), "queued")
            self.assertEqual(store.claim_next(root=root)[0], 12)
            with patch.object(interrupted, "reply", side_effect=interrupt):
                with self.assertRaises(model._ResumeModelJob):
                    interrupted.compose(store.Checkpoints(12, root=root))

            self.assertEqual(store.recover(root=root), 1)
            self.assertEqual(store.claim_next(root=root)[0], 12)
            resumed = LectureEndpoint(12)
            resumed.next_block = 7
            result = resumed.compose(store.Checkpoints(12, root=root))

        self.assertTrue(result.complete)
        self.assertEqual(list(prose_fragments(result.text)), ["\n".join(interrupted.blocks)])
        self.assertEqual(len(interrupted.contexts), 7)
        self.assertEqual(len(resumed.contexts), 5)
        self.assertIn(resumed.blocks[6], "".join(resumed.contexts[0]))
        self.assertNotIn(resumed.blocks[0], "".join(resumed.contexts[0]))
        for fragments in resumed.contexts:
            self.assertLessEqual(sum(map(len, fragments)), WINDOW_BUDGET)
