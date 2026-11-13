"""Recover a pre-upgrade, unfinished chapter supplement from only its v1 request hashes."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from psy_modules import psy_group_codex as model, psy_group_store as store


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
    save_v1(checkpoints, turns, model.SYSTEM + model.DISPATCH,
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
        model._lecture(editor, turns, 30, write_prose=draft)
    except ReviewSaved:
        pass
    else:
        raise AssertionError("Fixture did not reach the chapter review")
    assert review_turns[-1] == {"role": "assistant", "content": CHAPTER}
    # In v1 every next request hashed this entire chapter and every prior addition.
    prompt = [*review_turns, model._user(OLD_SUPPLEMENT + MISSING)]
    instructions = model.SYSTEM + "\n" + model.ATTRIBUTION
    save_v1(checkpoints, prompt, instructions, parts[0], prose=True, complete=False)
    for index in range(1, len(parts)):
        prior = "\n".join(parts[:index])
        messages = [*prompt, {"role": "assistant", "content": prior}, model._user(OLD_CONTINUE)]
        save_v1(checkpoints, messages, instructions, parts[index], prose=True, complete=False)


class LegacyContinuation(unittest.TestCase):
    def test_recovers_each_v1_supplement_without_resending_the_chapter(self):
        question = "Write a lecture"
        turns = [model._user(question)]
        parts = [f"Supplement {i}|" + str(i) * 3600 + f"|end {i}." for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store.pending(1, 17, "{}", root=root)
            self.assertIsNotNone(store.claim_next(root=root))
            v1_lecture(store.Checkpoints(17, root=root), turns, parts)
            store.recover(root=root)
            self.assertIsNotNone(store.claim_next(root=root))
            sent = []

            def endpoint(client, headers, messages, web, selected, *, instructions, **kwargs):
                sent.append(messages)
                return model.Result("A new ending.", [], 0)

            with patch.object(model, "_transport", nullcontext), \
                    patch.object(model, "_authenticated_request", side_effect=lambda send: send("fixture", {})), \
                    patch.object(model, "_stream", side_effect=endpoint):
                result = model._compose(None, turns, False, MODEL, question,
                                        checkpoints=store.Checkpoints(17, root=root))

        self.assertTrue(result.complete)
        self.assertIn(CHAPTER + "\n\n" + "\n".join(parts + ["A new ending."]), result.text)
        self.assertEqual(len(sent), 1)
        self.assertIn("|end 4.", str(sent[0]))
        self.assertNotIn("chapter-start|", str(sent[0]))
        self.assertNotIn("Supplement 1|", str(sent[0]))
