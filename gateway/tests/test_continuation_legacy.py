"""Recover old v1 continuation blocks independently of changed task-extraction prompts."""
from contextlib import nullcontext
from functools import partial
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from psy_modules import psy_group_client as codex, psy_group_codex as model, psy_group_store as store
from psy_modules import psy_group_lecture as lecture, psy_group_pipeline as pipeline
from psy_modules import psy_group_prompts as prompts, psy_group_quality as quality
from psy_modules import psy_group_continuation as continuation


MODEL = "fixture"
CHAPTER = "chapter-start|" + "sample prose. " * 800 + "|chapter-end"
MISSING = "Explain the final distinction"
PLAN = {"title": "Lecture", "sections": [
    {"title": "Distinction", "minutes": 30, "activity_minutes": 0, "brief": MISSING}]}
OLD_CONTINUE = ("Продолжи точно с места обрыва, сохрани прежний жанр и задачу. "
                "Не повторяй написанное, заверши оставшуюся работу.")
OLD_SUPPLEMENT = ("Напиши только недостающий фрагмент в форме речи преподавателя. "
                  "Дополнение должно читаться сразу после готового текста, без повтора и служебных отметок. "
                  "Вопрос для раскрытия:\n")


def save_v1(checkpoints, turns, instructions, text, *, prose=False, complete=True):
    payload = json.dumps([MODEL, False, prose, instructions, turns],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    key = "model:v1:" + hashlib.sha256(payload).hexdigest()
    checkpoints.put(key, {"text": text, "complete": complete, "searches": 0, "source_urls": []})


def v1_lecture(checkpoints, turns, parts):
    """Use the plan and chapter prompts to seed the old per-call rows, ending inside the supplement."""
    save_v1(checkpoints, turns, prompts.SYSTEM + prompts.DISPATCH,
            'PSY_TASK {"minutes":30}', prose=True)
    review_turns = []

    class ReviewSaved(Exception):
        pass

    def editor(messages, instructions):
        if "Сейчас нужен внутренний редакционный" in instructions:
            result = json.dumps(PLAN)
            save_v1(checkpoints, messages, instructions, result)
            return model.Result(result, [], 0)
        if "Проверь содержание раздела" in instructions:
            save_v1(checkpoints, messages, instructions, json.dumps({"missing": MISSING}))
            review_turns.extend(messages)
            raise ReviewSaved()
        raise AssertionError("Unexpected setup-stage prompt")

    def draft(messages, instructions):
        save_v1(checkpoints, messages, instructions, CHAPTER, prose=True)
        return model.Result(CHAPTER, [], 0)

    try:
        lecture._lecture(editor, turns, 30, write_prose=draft)
    except ReviewSaved:
        pass
    else:
        raise AssertionError("Fixture did not reach the chapter review")
    assert review_turns[-1] == {"role": "assistant", "content": CHAPTER}
    # In v1 every next request hashed this entire chapter and every prior addition.
    prompt = [*review_turns, codex._user(OLD_SUPPLEMENT + MISSING)]
    instructions = prompts.SYSTEM + "\n" + prompts.ATTRIBUTION
    save_v1(checkpoints, prompt, instructions, parts[0], prose=True, complete=False)
    for index in range(1, len(parts)):
        prior = "\n".join(parts[:index])
        messages = [*prompt, {"role": "assistant", "content": prior}, codex._user(OLD_CONTINUE)]
        save_v1(checkpoints, messages, instructions, parts[index], prose=True, complete=False)


class LegacyContinuation(unittest.TestCase):
    def test_recovers_each_v1_supplement_with_full_context_for_new_work(self):
        question = "Write a lecture"
        turns = [codex._user(question)]
        parts = [f"Supplement {i}|" + str(i) * 3600 + f"|end {i}." for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store.pending(1, 17, "{}", root=root)
            self.assertIsNotNone(store.claim_next(root=root))
            v1_lecture(store.Checkpoints(17, root=root), turns, parts)
            store.recover(root=root)
            self.assertIsNotNone(store.claim_next(root=root))
            sent = []
            task_reviews = []

            def endpoint(client, headers, messages, web, selected, *, instructions, **kwargs):
                if instructions.startswith(quality._REVIEW):
                    task_reviews.append(messages)
                    return model.Result('{"complete":true,"missing":[]}', [], 0)
                sent.append(messages)
                return model.Result("A new ending.", [], 0)

            with patch.object(codex, "_transport", nullcontext), \
                    patch.object(codex, "_authenticated_request", side_effect=lambda send: send("fixture", {})), \
                    patch.object(codex, "_stream", side_effect=endpoint):
                call, continue_call = codex._job_calls(None, MODEL, store.Checkpoints(17, root=root))
                write = partial(continuation._complete, partial(call, prose=True), model=MODEL,
                                continuation_call=continue_call)
                result = lecture._lecture(call, turns, 30, write_prose=write)
                result = quality.finish_task(call, write, turns, prompts.SYSTEM, result, quality.contract_for(question))

        self.assertTrue(result.complete)
        self.assertEqual(result.task_status, "complete")
        self.assertIn(CHAPTER + "\n\n" + "\n".join(parts) + "A new ending.", result.text)
        self.assertEqual(len(sent), 1)
        self.assertIn("|end 4.", str(sent[0]))
        self.assertIn("chapter-start|", str(sent[0]))
        self.assertIn("Supplement 1|", str(sent[0]))
        # Historical blocks preserve their original joins. New continuation and
        # the separate semantic review both see the complete available text.
        self.assertEqual(len(task_reviews), 1)
        self.assertIn(CHAPTER, str(task_reviews[0]))
        self.assertIn("Supplement 1|", str(task_reviews[0]))
