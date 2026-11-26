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


def contract_answer(question, **changes):
    contract = quality.contract_for(question)
    value = dict(task=question, audience=None, topics=[], examples=[], format=None,
                 deliverables=[], constraints=[], obligations=[question], continuation=False,
                 superseded_requirements=[], kind=contract["kind"], minutes=contract["minutes"],
                 outline_only=contract["outline_only"], limited_scope=contract["limited_scope"],
                 local_only=contract["local_only"], file_output=None, literal_constraints=[])
    value.update(changes)
    return answer(json.dumps(value, ensure_ascii=False))


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
        self.assertTrue(contract["review_required"])

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
        call = Calls(contract_answer(q), answer("Ниже план и первая часть. Продолжение - часть 2."),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(
                pipeline, "_lecture", return_value=answer("Полная учебная рукопись")) as write:
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertEqual(write.call_args.args[2], 180)
        self.assertEqual(result.text, "Полная учебная рукопись")
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_review"], "passed")
        self.assertTrue(call.deep)

    def test_ordinary_short_answer_gets_a_contract_and_semantic_check(self):
        call = Calls(contract_answer("17 + 25"), answer("42"), answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user("17 + 25")], False, "fixture", "17 + 25")
        self.assertEqual(len(call.calls), 3)
        self.assertEqual(result.text, "42")
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_review"], "passed")
        self.assertEqual(call.calls[0][1], quality._EXTRACT)
        self.assertTrue(call.calls[-1][1].startswith(quality._REVIEW))

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
        call = Calls(contract_answer(q), answer('PSY_TASK {"public_topic":"authors"}'),
                     answer('{"complete":true,"missing":[]}'))
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
        call = Calls(contract_answer(q), answer('PSY_TASK {"minutes":180,"public_topic":"Freud"}'),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(
                pipeline, "_lecture", return_value=answer("Рукопись по приложенному материалу")), patch.object(
                pipeline, "_primary_sources") as search:
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        search.assert_not_called()
        self.assertEqual(result.task_status, "complete")

    def test_partial_notice_is_idempotent_and_preserves_each_specific_gap(self):
        gaps = [str(i) + "x" * 1000 for i in range(10)]
        result = answer("Черновик", task_status="partial", missing_requirements=gaps)
        quality.mark_partial_text(result)
        first = result.text
        quality.mark_partial_text(result)
        self.assertEqual(result.text, first)
        self.assertTrue(all(gap in result.text for gap in gaps))

    def test_source_edits_that_remove_an_obligation_downgrade_task_status(self):
        contract = quality.contract_for("Напиши полный текст статьи с примером")
        result = answer("Статья после удаления неподтвержденного примера", task_status="complete")
        call = Calls(answer('{"complete":false,"missing":["Обязательный пример"]}'))
        checked = quality.recheck_edited_task(call, [], result, contract)
        self.assertEqual(checked.task_status, "partial")
        self.assertEqual(checked.quality_metadata["post_source_completion_review"], "requirements_missing")
        self.assertIn("Обязательный пример", checked.missing_requirements)


class UniversalOrders(unittest.TestCase):
    def test_lecture_refusal_never_enters_another_repair_stage(self):
        plan = answer(json.dumps({"title": "Lecture", "sections": [
            {"title": "One", "brief": "Example", "minutes": 30, "activity_minutes": 0}]}))
        for stage in ("planning", "writing", "review", "supplement"):
            with self.subTest(stage=stage):
                refusal = answer("Не могу выполнить этот запрос.", stop_reason="provider_refusal")
                if stage == "planning":
                    call, write = Calls(refusal), Mock()
                elif stage == "writing":
                    call, write = Calls(plan), Mock(return_value=refusal)
                elif stage == "review":
                    call, write = Calls(plan, refusal), Mock(return_value=answer("Initial chapter"))
                else:
                    call = Calls(plan, answer('{"missing":"Example"}'))
                    write = Mock(side_effect=[answer("Initial chapter"), refusal])
                result = lecture._lecture(call, [client._user("Write lecture")], 30, write_prose=write)
                self.assertEqual(result.stop_reason, "provider_refusal")
                self.assertEqual(result.task_status, "refused")
                self.assertEqual(result.quality_metadata["lecture_refusal_phase"], stage)
                self.assertEqual(result.text, "Не могу выполнить этот запрос.")

    def test_provider_refusal_is_recorded_without_repair_or_continuation(self):
        q = "Напиши полный текст лекции на 3 часа"
        call = Calls(contract_answer(q), answer("Не могу выполнить этот запрос.", stop_reason="provider_refusal"))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(pipeline, "_lecture") as lecture:
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        lecture.assert_not_called()
        self.assertEqual(len(call.calls), 2)
        self.assertEqual(result.task_status, "refused")
        self.assertEqual(result.stop_reason, "provider_refusal")
        self.assertEqual(result.text, "Не могу выполнить этот запрос.")

    def test_contract_precedes_draft_for_code_planning_and_short_questions(self):
        examples = (
            ("Напиши функцию Python и два примера вызова", ["Код функции", "Два примера вызова"], "Python"),
            ("Составь план переезда с бюджетом и ответственными", ["Этапы", "Бюджет", "Ответственные"], "таблица"),
            ("Чем отличаются круг и шар? Кратко", ["Различие круга и шара", "Краткий ответ"], None),
        )
        for q, obligations, form in examples:
            with self.subTest(question=q):
                call = Calls(contract_answer(q, obligations=obligations, format=form),
                             answer("Результат по текущему вопросу"), answer('{"complete":true,"missing":[]}'))
                with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(pipeline, "_lecture") as lecture:
                    result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
                lecture.assert_not_called()
                self.assertEqual(call.calls[0][1], quality._EXTRACT)
                self.assertIn(obligations[-1], str(call.calls[1][0]))
                self.assertEqual(result.quality_metadata["task_contract"]["obligations"], obligations)
                self.assertEqual(result.task_status, "complete")

    def test_followup_extraction_sees_history_and_uses_replacement_requirement(self):
        turns = [client._user("Пиши письмо директору на английском"), answer("Draft").__dict__,
                 client._user("Теперь то же письмо на русском, английский отменяю")]
        # Use the actual role/content history shape. No synthetic system memory is needed.
        turns[1] = {"role": "assistant", "content": "Draft"}
        q = turns[-1]["content"]
        call = Calls(contract_answer(q, continuation=True, constraints=["Русский язык"],
                     obligations=["То же письмо директору на русском"], superseded_requirements=["Английский язык"]),
                     answer("Письмо на русском"), answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, turns, False, "fixture", q)
        self.assertEqual(call.calls[0][0][:-1], turns)
        self.assertEqual(result.quality_metadata["task_contract"]["constraints"], ["Русский язык"])
        self.assertIn("Отмененные требования исключи", call.calls[0][1])

    def test_a_new_short_question_does_not_inherit_lecture_routing(self):
        q = "Сколько будет 2 + 2?"
        turns = [client._user("Напиши лекцию на 3 часа"), {"role": "assistant", "content": "Готовая лекция"}, client._user(q)]
        call = Calls(contract_answer(q), answer("4"), answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)), patch.object(pipeline, "_lecture") as lecture:
            result = pipeline._compose(None, turns, False, "fixture", q)
        lecture.assert_not_called()
        self.assertIsNone(result.quality_metadata["task_contract"]["minutes"])
        self.assertEqual(result.text, "4")

    def test_unavailable_contract_preserves_answer_but_does_not_certify_it(self):
        q = "Раздели 12 на 3"
        call = Calls(answer("not JSON"), answer("still not JSON"), answer("4"),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertTrue(result.text.startswith("4"))
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.quality_metadata["completion_review"], "contract_unavailable")

    def test_missing_second_question_in_short_answer_is_repaired(self):
        q = "Чему равны 2+2 и 3+3?"
        call = Calls(contract_answer(q, obligations=["2+2", "3+3"]), answer("2+2=4"),
                     answer('{"complete":false,"missing":["Ответ на 3+3"]}'), answer("2+2=4, 3+3=6"),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertEqual(result.text, "2+2=4, 3+3=6")
        self.assertEqual(result.quality_metadata["completion_repairs"], 1)

    def test_large_contract_is_not_clipped_to_twenty_four_requirements(self):
        q = "Разбери все 40 пунктов"
        obligations = ["Пункт " + str(i) for i in range(40)]
        value = quality.extract_contract(Calls(contract_answer(q, obligations=obligations)), [client._user(q)], q)
        self.assertEqual(value["obligations"], obligations)

    def test_scoped_full_history_reaches_extraction_after_more_than_eight_exchanges(self):
        q = "Продолжи в ранее выбранном формате"
        turns = [client._user("Требование в начале: используй CSV. " + "Подробность " * 400),
                 {"role": "assistant", "content": "Принято"}]
        for i in range(10):
            turns.extend([client._user(f"Дополнение {i}"), {"role": "assistant", "content": f"Ответ {i}"}])
        turns.append(client._user(q))
        call = Calls(contract_answer(q, format="CSV", continuation=True), answer("a,b\n1,2"),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, turns, False, "fixture", q, context_scope=[1, 2, None],
                                       input_budget_tokens=50000)
        self.assertEqual(call.calls[0][0][:-1], turns)
        self.assertEqual(result.quality_metadata["task_contract"]["format"], "CSV")
        self.assertIn("context_audit", result.quality_metadata)

    def test_terminal_error_carries_prepared_context_audit(self):
        q = "Рассчитай стоимость"
        call = Calls(contract_answer(q), client.ModelIssue("unavailable", "http"))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            with self.assertRaises(client.ModelIssue) as caught:
                pipeline._compose(None, [client._user(q)], False, "fixture", q,
                                  context_scope=[1, 2, None], input_budget_tokens=50000)
        self.assertIn("context_audit", caught.exception.quality_metadata)


if __name__ == "__main__":
    unittest.main()
