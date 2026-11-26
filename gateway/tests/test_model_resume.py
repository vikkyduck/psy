"""Restart real model composition in fresh processes, using temporary SQLite and fake Codex."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

# The child loads the same restored plugin directory used by gateway/test_gateway.py.
if __name__ == "__main__":
    pkg = types.ModuleType("psy_modules")
    pkg.__path__ = [sys.argv[1]]
    sys.path.insert(0, str(Path(sys.argv[1]).parents[2]))
    sys.modules[pkg.__name__] = pkg
    sys.path.insert(0, str(Path(__file__).parent))

from psy_modules import psy_group_client as codex, psy_group_codex as model, psy_group_store as store
from psy_modules import psy_group_continuation as continuation, psy_group_pipeline as pipeline
from psy_modules import psy_group_prompts as prompts, psy_group_sources as sources
from psy_modules import psy_group_quality as quality
from task_contract_fixture import contract_json


class FakeHttp(types.ModuleType):
    class RequestError(Exception):
        pass
    class TimeoutException(RequestError):
        pass


def run_child(root, kind, interrupt):
    ident = 12
    if interrupt:
        store.pending(1, ident, '{}', root=root)
    else:
        store.recover(root=root)
    assert store.claim_next(root=root)[0] == ident
    source = "https://example.org/public-work"
    question = ("Напиши лекцию на 1 час только по приложенному материалу" if kind == "lecture"
                else "Публичная тема: Synthetic assignment")
    plan = {"title": "Lecture", "sections": [
        {"title": f"Chapter {i}", "brief": "An example", "minutes": 10, "activity_minutes": 0}
        for i in range(1, 7)
    ]}
    lock = threading.Lock()
    saved_reviews = set()

    class Checkpoints(store.Checkpoints):
        def put(self, key, value):
            super().put(key, value)
            # Exit from a worker only after SQLite has committed the third review.
            if kind == "lecture" and interrupt and value and '"checked_chapter"' in value.get("text", ""):
                chapter = json.loads(value["text"])["checked_chapter"]
                with lock:
                    saved_reviews.add(chapter)
                    if saved_reviews == {1, 2, 3}:
                        os._exit(83)

    def stream(client, headers, messages, web, selected_model, *, instructions, **kwargs):
        step = None
        if instructions.startswith(quality._EXTRACT):
            step = "contract"
            text = contract_json(question, kind="lecture" if kind == "lecture" else "answer",
                                 minutes=60 if kind == "lecture" else None, local_only=kind == "lecture")
        elif prompts.DISPATCH in instructions:
            step = "dispatch"
            task = {"minutes": 60} if kind == "lecture" else {"public_topic": "Synthetic public topic"}
            text = "PSY_TASK " + json.dumps(task)
        elif "Сейчас нужен внутренний редакционный" in instructions:
            step, text = "plan", json.dumps(plan)
        elif match := re.search(r"Напиши целиком только раздел (\d+)", instructions):
            index = int(match.group(1))
            if interrupt and index > 3:
                # Other workers are still committing the first three chapters.
                # A timeout here fails the fixture instead of hanging the test.
                if not threading.Event().wait(12):
                    raise AssertionError("first three sections did not checkpoint")
            step, text = f"draft-{index}", f"Original draft for chapter {index}."
        elif match := re.search(r'Проверь содержание раздела "Chapter (\d+)"', instructions):
            index = int(match.group(1))
            step, text = f"check-{index}", json.dumps({"missing": "", "checked_chapter": index})
        elif instructions == prompts.WEB_SYSTEM:
            step, text = "sources", "Public notes"
        elif instructions == prompts.SOURCE_CHECK:
            card = dict(author="A", work="W", date="2001", location="p.1", support="quotation",
                        claim="Original assertion", access="full_text", url=source)
            step, text = "source-check", json.dumps({"claims": [card], "gaps": []})
        elif instructions == prompts.REVIEW:
            step = "review"
            text = json.dumps({"edits": [{"before": "draft assertion", "after": "verified assertion"}]})
        elif instructions.startswith(quality._REVIEW):
            step, text = "task-review", '{"complete":true,"missing":[]}'
        else:
            assert kind == "analysis"
            step, text = "analysis-draft", "The draft assertion."
        with lock, (root/"calls").open("a") as out:
            out.write(step+"\n")
        if kind == "analysis" and interrupt and step == "review":
            os._exit(83)
        return model.Result(text, [], int(web), source_urls=(source,) if web else ())

    with patch.object(sources.EvidenceReader, "locate", return_value=("HTML, абзац 1", "fixture context")), patch.dict(sys.modules, {"httpx": FakeHttp("httpx")}), patch.object(
        codex, "_connection", return_value=("", {}, "fixture-generation")
    ), patch.object(codex, "_stream", stream):
        result = pipeline._compose(None, [codex._user(question)], kind == "analysis", "fixture", question,
                                   checkpoints=Checkpoints(ident, root=root))
    assert result.complete
    assert result.task_status == "complete"
    (root/"answer").write_text(result.text)
    store.settle(ident, True, root=root)


class ModelRestart(unittest.TestCase):
    def run_restart(self, name):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = [sys.executable, "-I", __file__, str(Path(model.__file__).parent), folder, name]
            stopped = subprocess.run([*command, "interrupt"], capture_output=True, timeout=25)
            self.assertEqual(stopped.returncode, 83, stopped.stderr.decode())
            before = (root/"calls").read_text().splitlines()
            resumed = subprocess.run([*command, "resume"], capture_output=True, timeout=25)
            self.assertEqual(resumed.returncode, 0, resumed.stderr.decode())
            self.assertFalse(store.state(12, root=root))
            after = (root/"calls").read_text().splitlines()[len(before):]
            return before, after, (root/"answer").read_text()

    def test_fresh_process_keeps_first_three_chapters_and_verified_reviews(self):
        before, after, text = self.run_restart("lecture")
        self.assertEqual(Counter(before), Counter(["contract", "dispatch", "plan", *[
            f"{phase}-{i}" for i in range(1, 4) for phase in ("draft", "check")]]))
        self.assertEqual(Counter(after), Counter(["task-review", *[
            f"{phase}-{i}" for i in range(4, 7) for phase in ("draft", "check")]]))
        for i in range(1, 7):
            self.assertEqual(text.count(f"Original draft for chapter {i}."), 1)

    def test_fresh_process_resumes_at_source_verification_with_saved_analysis_draft(self):
        before, after, text = self.run_restart("analysis")
        self.assertEqual(before, ["contract", "dispatch", "sources", "source-check", "analysis-draft", "task-review", "review"])
        self.assertEqual(after, ["review", "task-review"])
        self.assertEqual(text, "The verified assertion.")

    def test_stream_resumes_only_final_prose_after_process_interruption(self):
        class Interrupted(BaseException):
            pass
        class Memory(dict):
            def put(self, key, value):
                self[key] = value

        checkpoints = Memory()
        question = "A question"
        turns = [codex._user(question)]
        kept = "Original paragraph. " * 500
        hidden = "Private commentary. " * 500
        def events():
            for phase, text in (("commentary", hidden), ("final_answer", kept)):
                item = {"id": phase, "type": "message", "phase": phase}
                yield "data: "+json.dumps({"type": "response.output_item.added", "item": item})
                yield "data: "+json.dumps({"type": "response.output_text.delta", "item_id": phase,
                                           "delta": text})
            raise Interrupted()
        def restored(*args, instructions, **kwargs):
            if instructions.startswith(quality._EXTRACT):
                return model.Result(contract_json(question), [], 0)
            if instructions.startswith(quality._REVIEW):
                return model.Result('{"complete":true,"missing":[]}', [], 0)
            inputs = args[2]
            self.assertEqual(inputs[-2], {"role": "assistant", "content": kept})
            self.assertNotIn(hidden.strip(), str(inputs))
            return model.Result("Restored ending.", [], 0)
        client = Mock()
        def stream(*args, json, **kwargs):
            if json["instructions"].startswith(quality._EXTRACT):
                lines = ["data: " + __import__("json").dumps({"type": "response.output_text.delta", "delta": contract_json(question)}),
                         'data: {"type":"response.completed"}']
                return nullcontext(Mock(status_code=200, iter_lines=lambda: iter(lines)))
            return nullcontext(Mock(status_code=200, iter_lines=events))
        client.stream.side_effect = stream
        with patch.object(sources.EvidenceReader, "locate", return_value=("HTML, абзац 1", "fixture context")), patch.dict(sys.modules, {"httpx": FakeHttp("httpx")}), patch.object(codex, "_connection", return_value=("", {}, "fixture-generation")):
            with self.assertRaises(Interrupted):
                pipeline._compose(client, turns, False, "fixture", question, checkpoints=checkpoints)
            self.assertNotIn(hidden[:100], json.dumps(checkpoints))
            with patch.object(codex, "_stream", side_effect=restored) as provider:
                result = pipeline._compose(None, turns, False, "fixture", question, checkpoints=checkpoints)
                self.assertEqual(provider.call_count, 2)
            self.assertTrue(result.complete)
            self.assertEqual(result.text, kept + "Restored ending.")

            def different(*args, instructions, **kwargs):
                if instructions.startswith(quality._EXTRACT):
                    return model.Result(contract_json("Different question"), [], 0)
                if instructions.startswith(quality._REVIEW):
                    return model.Result('{"complete":true,"missing":[]}', [], 0)
                return model.Result("different answer", [], 0)
            with patch.object(codex, "_stream", side_effect=different) as provider:
                fresh = pipeline._compose(None, [codex._user("Different question")], False, "fixture",
                                          "Different question", checkpoints=checkpoints)
                self.assertEqual((provider.call_count, fresh.text), (3, "different answer"))


if __name__ == "__main__":
    run_child(Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "interrupt")
