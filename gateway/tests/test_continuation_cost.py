"""Verify complete continuation context in actual Codex payloads without network access."""
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from psy_modules import psy_group_client as codex, psy_group_codex as model, psy_group_store as store
from psy_modules import psy_group_pipeline as pipeline, psy_group_prompts as prompts
from psy_modules import psy_group_quality as quality


BLOCK_SIZE = 1024
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
        self.task_reviews = []

    def stream(self, method, endpoint, *, headers, json, timeout):
        payload = json
        if method != "POST" or not endpoint.endswith("/responses") or payload["tools"]:
            raise AssertionError("Unexpected outbound request")
        text, complete = self.reply(payload)
        return events(text, complete)

    def reply(self, payload):
        instruction = payload["instructions"]
        complete = True
        if prompts.DISPATCH in instruction:
            text = 'PSY_TASK {"minutes":30,"public_topic":"Synthetic"}'
        elif instruction.startswith(quality._EXTRACT):
            text = encode(dict(task=QUESTION, audience=None, topics=[], examples=[], format=None,
                               deliverables=[], constraints=[], obligations=[QUESTION], continuation=False,
                               superseded_requirements=[], kind="lecture", minutes=30,
                               outline_only=False, limited_scope=False, local_only=False, file_output=None))
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
        elif instruction == prompts.REVIEW:
            self.source_reviews.extend(prose_fragments(payload))
            text = '{"edits":[]}'
        elif instruction.startswith(quality._REVIEW):
            self.task_reviews.extend(prose_fragments(payload))
            text = '{"complete":true,"missing":[]}'
        else:
            raise AssertionError("Unknown stage of the synthetic lecture")
        return text, complete

    def compose(self, checkpoints=None):
        fake_http = types.ModuleType("httpx")
        fake_http.RequestError = type("RequestError", (Exception,), {})
        fake_http.TimeoutException = type("TimeoutException", (fake_http.RequestError,), {})
        source_cards = encode({"claims": [{"support": SOURCE}], "gaps": []})
        with patch.dict(sys.modules, {"httpx": fake_http}), patch.object(
                codex, "_connection", return_value=("fixture", {}, "fixture-generation")), patch.object(
                pipeline, "_primary_sources", return_value=model.Result(source_cards, [], 1)):
            return pipeline._compose(self, [codex._user(QUESTION)], False, "fixture", QUESTION,
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


class ContinuationContext(unittest.TestCase):
    def test_lecture_keeps_the_full_manuscript_and_all_prior_context(self):
        for count in (16, 32):
            with self.subTest(block_count=count):
                endpoint = LectureEndpoint(count)
                result = endpoint.compose()
                manuscript = "".join(endpoint.blocks)
                self.assertTrue(result.complete)
                self.assertEqual(result.task_status, "complete")
                self.assertEqual(list(prose_fragments(result.text)), [manuscript])
                self.assertEqual(endpoint.chapter_reviews, [manuscript])
                self.assertEqual(endpoint.source_reviews, [manuscript])
                self.assertEqual(endpoint.task_reviews, [manuscript])
                self.assertEqual(len(endpoint.contexts), count)
                self.assertEqual(endpoint.contexts[0], [])
                for index, context in enumerate(endpoint.contexts[1:], start=1):
                    self.assertEqual(len(context), 1)
                    self.assertEqual(context[0], "".join(endpoint.blocks[:index]))
                for inputs in endpoint.inputs:
                    for expected in (QUESTION, PLAN_TITLE, SOURCE):
                        self.assertIn(expected, inputs)

    def test_supplement_sees_parent_chapter_and_all_earlier_additions(self):
        endpoint = SupplementEndpoint()
        result = endpoint.compose()
        self.assertTrue(result.complete)
        self.assertEqual(list(prose_fragments(result.text)),
                         ["".join(endpoint.blocks), "".join(endpoint.additions)])
        self.assertEqual(len(endpoint.supplement_contexts), len(endpoint.additions))
        for index, parts in enumerate(endpoint.supplement_contexts):
            for expected in (QUESTION, PLAN_TITLE, SOURCE, "Сопоставить два случая."):
                self.assertIn(expected, endpoint.supplement_inputs[index])
            sent = "".join(parts)
            self.assertIn("".join(endpoint.blocks), sent)
            if index:
                self.assertIn("".join(endpoint.additions[:index]), sent)
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
                with self.assertRaises(codex._ResumeModelJob):
                    interrupted.compose(store.Checkpoints(12, root=root))

            self.assertEqual(store.recover(root=root), 1)
            self.assertEqual(store.claim_next(root=root)[0], 12)
            resumed = LectureEndpoint(12)
            resumed.next_block = 7
            result = resumed.compose(store.Checkpoints(12, root=root))

        self.assertTrue(result.complete)
        self.assertEqual(list(prose_fragments(result.text)), ["".join(interrupted.blocks)])
        self.assertEqual(len(interrupted.contexts), 7)
        self.assertEqual(len(resumed.contexts), 5)
        self.assertIn(resumed.blocks[6], "".join(resumed.contexts[0]))
        self.assertIn(resumed.blocks[0], "".join(resumed.contexts[0]))
        for index, fragments in enumerate(resumed.contexts, start=7):
            self.assertEqual("".join(fragments), "".join(resumed.blocks[:index]))
