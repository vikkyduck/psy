"""Task-fulfilment regressions with synthetic responses, no network or private data."""
import importlib
import json
import os
import unittest
from unittest.mock import Mock, patch


def plugin_module(name):
    if os.environ.get("PSY_GROUP_TEST_INSTALLED"):
        return importlib.import_module("plugins.platforms.telegram." + name)
    try:
        return importlib.import_module("psy_modules." + name)
    except ModuleNotFoundError:
        return importlib.import_module(name)


client, pipeline, quality, lecture = map(plugin_module, (
    "psy_group_client", "psy_group_pipeline", "psy_group_quality", "psy_group_lecture"))


def answer(text, **kwargs):
    return client.Result(text, [], 0, **kwargs)


class Calls:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.deep = False

    def __call__(self, messages, instructions, **options):
        self.calls.append((messages, instructions, options))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class Contracts(unittest.TestCase):
    def test_explicit_durations_are_normalized_without_a_default(self):
        for phrase, duration in (("3 часа", 180), ("три часа", 180), ("трехчасовую", 180),
                                 ("3-часовую", 180), ("2 академических часа", 90),
                                 ("1 час 30 минут", 90), ("1,5 часа", 90), ("30 минут", 30)):
            with self.subTest(phrase=phrase):
                self.assertEqual(quality.contract_for("Напиши полный текст лекции на " + phrase)["minutes"], duration)
        self.assertIsNone(quality.contract_for("Напиши лекцию")["minutes"])

    def test_outline_is_not_silently_upgraded_to_a_manuscript(self):
        contract = quality.contract_for("Составь план лекции на 3 часа", {"minutes": 180})
        self.assertTrue(contract["outline_only"])
        self.assertEqual(contract["kind"], "answer")
        self.assertIsNone(contract["minutes"])
        self.assertFalse(contract["review_required"])

    def test_a_question_about_a_lecture_is_not_a_lecture_order(self):
        self.assertEqual(quality.contract_for("Почему лекция длилась 3 часа?")["kind"], "answer")

    def test_local_only_request_is_preserved(self):
        self.assertTrue(quality.contract_for("Напиши текст лекции на 3 часа только по приложенному материалу")["local_only"])

    def test_only_first_part_does_not_order_the_entire_three_hour_manuscript(self):
        contract = quality.contract_for("Напиши только первую часть полного текста лекции на 3 часа", {"minutes": 180})
        self.assertEqual(contract["kind"], "manuscript")
        self.assertIsNone(contract["minutes"])
        self.assertTrue(contract["limited_scope"])


class TaskCompletion(unittest.TestCase):
    def test_three_hour_request_cannot_exit_with_first_part_prose(self):
        q = "Составь полный текст лекции на 3 часа по синтетической теме"
        call = Calls(answer("Ниже план и первая часть. Продолжение - часть 2."),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(
                pipeline, "_lecture", return_value=answer("Полная учебная рукопись")) as write:
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertEqual(write.call_args.args[2], 180)
        self.assertEqual(result.text, "Полная учебная рукопись")
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_review"], "passed")
        self.assertTrue(call.deep)

    def test_ordinary_short_answer_has_no_added_model_call(self):
        call = Calls(answer("42"))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user("17 + 25")], False, "fixture", "17 + 25")
        self.assertEqual(len(call.calls), 1)
        self.assertEqual(result.text, "42")
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_review"], "not_required")

    def test_missing_obligation_is_repaired_and_checked_again(self):
        contract = quality.contract_for("Напиши полный текст статьи с двумя примерами")
        call = Calls(answer('{"complete":false,"missing":["Второй пример"]}'),
                     answer('{"complete":true,"missing":[]}'))
        write = Mock(return_value=answer("Статья с первым и вторым примерами"))
        result = quality.finish_task(call, write, [client._user(contract["request"])], "instructions",
                                     answer("Статья только с первым примером"), contract)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_repairs"], 1)
        self.assertIn("Второй пример", write.call_args.args[0][-1]["content"])
        self.assertIn("Статья с первым и вторым примерами", call.calls[-1][0][-1]["content"])

    def test_completed_stream_with_unresolved_deferral_is_honestly_partial(self):
        q = "Напиши полный текст статьи"
        bad = "Ниже первая часть. Продолжение - часть 2."
        call = Calls(answer('{"complete":true,"missing":[]}'), answer('{"complete":true,"missing":[]}'))
        write = Mock(return_value=answer(bad))
        result = quality.finish_task(call, write, [client._user(q)], "instructions", answer(bad), quality.contract_for(q))
        quality.mark_partial_text(result)
        self.assertTrue(result.complete)  # API finished successfully, the task did not.
        self.assertEqual(result.task_status, "partial")
        self.assertTrue(result.missing_requirements)
        self.assertIn("Материал пока частичный.", result.text)
        self.assertEqual(write.call_count, 1)

    def test_review_unavailable_preserves_draft_without_claiming_success(self):
        q = "Напиши полный текст статьи"
        call = Calls(client.ModelIssue("context", "quality"))
        write = Mock()
        result = quality.finish_task(call, write, [client._user(q)], "instructions",
                                     answer("Сохраненный черновик"), quality.contract_for(q))
        self.assertEqual(result.text, "Сохраненный черновик")
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.quality_metadata["completion_review"], "unavailable")
        write.assert_not_called()

    def test_broken_review_json_does_not_certify_completion(self):
        call = Calls(answer('{"complete":true,"missing":["Пример"]}'))
        result = quality.finish_task(call, Mock(), [], "instructions", answer("Черновик"),
                                     quality.contract_for("Напиши полный текст статьи"))
        self.assertEqual(result.task_status, "partial")

    def test_failed_repair_preserves_prior_obligations(self):
        call = Calls(answer('{"complete":false,"missing":["Заключение"]}'))
        write = Mock(return_value=answer("Начало заключения", complete=False))
        result = quality.finish_task(call, write, [], "instructions", answer("Готовая основная часть"),
                                     quality.contract_for("Напиши полный текст статьи"))
        self.assertEqual(result.task_status, "partial")
        self.assertIn("Заключение", result.missing_requirements)
        self.assertEqual(result.text, "Готовая основная часть")

    def test_requested_first_part_is_not_a_false_deferral_failure(self):
        q = "Напиши только первую часть полного текста лекции"
        call = Calls(answer('{"complete":true,"missing":[]}'))
        write = Mock()
        result = quality.finish_task(call, write, [], "instructions", answer("Ниже первая часть."), quality.contract_for(q))
        self.assertEqual(result.task_status, "complete")
        write.assert_not_called()

    def test_source_coverage_gap_survives_successful_writing_and_review(self):
        q = "Сравни Фрейда и Лакана"
        call = Calls(answer('PSY_TASK {"public_topic":"authors"}'))
        evidence = answer('{"claims":[],"gaps":["Вторая сторона"]}', task_status="partial",
                          missing_requirements=["Не подтверждена вторая сторона сравнения."],
                          quality_metadata={"coverage_status": "partial"})
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(
                pipeline, "_primary_sources", return_value=evidence), patch.object(
                pipeline, "_complete", return_value=answer("Подтвержденная часть ответа")), patch.object(
                pipeline, "_verify_draft", side_effect=lambda call, result, evidence: result):
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertTrue(result.complete)
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.quality_metadata["coverage_status"], "partial")
        self.assertIn("Не подтверждена вторая сторона", result.text)

    def test_local_lecture_does_not_start_web_search_even_when_router_requests_it(self):
        q = "Напиши полный текст лекции на 3 часа только по приложенному тексту Фрейда"
        call = Calls(answer('PSY_TASK {"minutes":180,"public_topic":"Freud"}'),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(
                pipeline, "_lecture", return_value=answer("Рукопись по приложенному материалу")), patch.object(
                pipeline, "_primary_sources") as search:
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        search.assert_not_called()
        self.assertEqual(result.task_status, "complete")

    def test_partial_notice_is_idempotent_and_bounded(self):
        result = answer("Черновик", task_status="partial", missing_requirements=["x" * 1000] * 10)
        quality.mark_partial_text(result)
        first = result.text
        quality.mark_partial_text(result)
        self.assertEqual(result.text, first)
        self.assertLess(len(result.text), 2000)

    def test_source_edits_that_remove_an_obligation_downgrade_task_status(self):
        contract = quality.contract_for("Напиши полный текст статьи с примером")
        result = answer("Статья после удаления неподтвержденного примера", task_status="complete")
        call = Calls(answer('{"complete":false,"missing":["Обязательный пример"]}'))
        checked = quality.recheck_edited_task(call, [], result, contract)
        self.assertEqual(checked.task_status, "partial")
        self.assertEqual(checked.quality_metadata["post_source_completion_review"], "requirements_missing")
        self.assertIn("Обязательный пример", checked.missing_requirements)


if __name__ == "__main__":
    unittest.main()
