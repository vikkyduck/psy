"""Explicit output conditions remain enforceable when the semantic reviewer misses them."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_task_completion import Calls, answer, client, contract_answer, pipeline, quality, plugin_module


NO_SEMICOLON = {"kind": "forbid_char", "value": ";", "source": "без точек с запятой"}
PREFIX = {"kind": "prefix", "value": "Контрольный учебный материал",
          "source": 'Начни с пометки "Контрольный учебный материал".'}
ENDING = {"kind": "last_line", "value": "Конец контрольного материала PSY-E2E",
          "source": 'Последняя отдельная строка полного материала: "Конец контрольного материала PSY-E2E".'}
REQUEST = "Напиши материал без точек с запятой.\n" + PREFIX["source"] + "\n" + ENDING["source"]
CLEAN = PREFIX["value"] + "\nОбъяснение. Пример.\n" + ENDING["value"]


def contract(question=REQUEST, constraints=None, **changes):
    constraints = [NO_SEMICOLON, PREFIX, ENDING] if constraints is None else constraints
    return quality.extract_contract(Calls(contract_answer(question, literal_constraints=constraints, **changes)),
                                    [client._user(question)], question)


def positive_verdict():
    return answer('{"complete":true,"missing":[]}')


class LiteralOutput(unittest.TestCase):
    def test_false_positive_model_verdict_still_repairs_whole_material(self):
        original = CLEAN.replace("Объяснение. Пример.", "Объяснение; Пример;")
        calls = Calls(contract_answer(REQUEST, literal_constraints=[NO_SEMICOLON, PREFIX, ENDING]),
                      answer(original), positive_verdict(), answer(CLEAN), positive_verdict())
        with patch.object(pipeline, "_job_calls", return_value=(calls, None)):
            result = pipeline._compose(None, [client._user(REQUEST)], False, "fixture", REQUEST)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.text, CLEAN)
        self.assertEqual(result.quality_metadata["completion_repairs"], 1)
        self.assertIn("U+003B", str(calls.calls[3][0][-1]))
        self.assertIn("2 раз", str(calls.calls[3][0][-1]))
        self.assertEqual(result.quality_metadata["task_contract"]["literal_constraints"],
                         [NO_SEMICOLON, PREFIX, ENDING])

    def test_repair_still_violates_condition_cannot_be_certified(self):
        original = CLEAN.replace("Пример.", "Пример;")
        write = Mock(return_value=answer(original))
        result = quality.finish_task(Calls(positive_verdict(), positive_verdict()), write,
                                     [client._user(REQUEST)], "instructions", answer(original), contract())
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.text, original)  # The checker does not silently edit the output.
        self.assertIn("U+003B", " ".join(result.missing_requirements))
        self.assertEqual(write.call_count, 1)

    def test_footer_after_required_final_line_is_a_real_missing_requirement(self):
        bad = CLEAN + "\nСлужебная приписка"
        write = Mock(return_value=answer(bad))
        result = quality.finish_task(Calls(positive_verdict(), positive_verdict()), write,
                                     [], "instructions", answer(bad), contract())
        self.assertEqual(result.task_status, "partial")
        self.assertIn("Последняя строка", " ".join(result.missing_requirements))
        self.assertEqual(result.text, bad)

    def test_clean_boundaries_are_preserved_exactly_without_repair(self):
        write = Mock()
        result = quality.finish_task(Calls(positive_verdict()), write, [], "instructions",
                                     answer(CLEAN), contract())
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.text, CLEAN)
        self.assertEqual(quality.mark_partial_text(result).text, CLEAN)
        write.assert_not_called()

    def test_operational_audit_excludes_literal_values_and_user_quotes(self):
        result = quality.finish_task(Calls(positive_verdict()), Mock(), [], "instructions",
                                     answer(CLEAN), contract())
        exported = plugin_module("psy_group_observability").result_audit({
            "quality_metadata": result.quality_metadata, "task_status": result.task_status,
            "generation_complete": True, "missing_requirements": result.missing_requirements})
        self.assertEqual(exported["task_status"], "complete")
        self.assertNotIn("task_contract", exported["metadata"])
        self.assertNotIn("literal_constraints", json.dumps(exported, ensure_ascii=False))
        self.assertNotIn(ENDING["value"], json.dumps(exported, ensure_ascii=False))

    def test_code_and_discussion_have_no_implicit_character_ban(self):
        for q, output in (("Покажи цикл на JavaScript", "for (let i = 0; i < 3; i++) {}"),
                          ('Объясни правило "без точек с запятой"', "Символ ; разделяет части предложения.")):
            with self.subTest(question=q):
                calls = Calls(contract_answer(q), answer(output), positive_verdict())
                with patch.object(pipeline, "_job_calls", return_value=(calls, None)):
                    result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
                self.assertEqual(result.task_status, "complete")
                self.assertEqual(result.text, output)
                self.assertEqual(result.quality_metadata["completion_repairs"], 0)

    def test_first_line_and_prefix_have_distinct_meanings(self):
        q = 'Первая строка: "Тема".'
        literal = {"kind": "first_line", "value": "Тема", "source": q}
        data = contract(q, [literal])
        self.assertEqual(data["contract_status"], "extracted")
        self.assertFalse(quality.literal_missing("Тема\nТекст\n", data))
        self.assertTrue(quality.literal_missing("Тема и дополнение\nТекст", data))
        self.assertTrue(quality.literal_missing("\nТема\nТекст", data))
        self.assertFalse(quality.literal_missing(PREFIX["value"] + ": урок", contract(PREFIX["source"], [PREFIX])))

    def test_boundary_instruction_does_not_require_one_sentence_template(self):
        q = 'Ответ начни словами "Готово".'
        item = {"kind": "prefix", "value": "Готово", "source": q}
        data = contract(q, [item])
        self.assertEqual(data["contract_status"], "extracted")
        self.assertFalse(quality.literal_missing("Готово. Материал.", data))

    def test_literal_comparison_does_not_normalize_user_content(self):
        data = contract(ENDING["source"], [ENDING])
        for output in (ENDING["value"] + ".", ENDING["value"] + " ", '"' + ENDING["value"] + '"'):
            with self.subTest(output=output):
                self.assertTrue(quality.literal_missing(output, data))
        self.assertFalse(quality.literal_missing(ENDING["value"] + "\n", data))


class LiteralGrounding(unittest.TestCase):
    def parsed(self, question, literal, *, turns=(), **changes):
        value = json.loads(contract_answer(question, literal_constraints=literal, **changes).text)
        return quality._contract_data(value, question, turns)

    def test_conditions_are_bound_to_exact_participant_instructions(self):
        value = self.parsed(REQUEST, [NO_SEMICOLON, PREFIX, ENDING])
        self.assertIsNotNone(value)
        self.assertEqual(value["literal_constraints"], [NO_SEMICOLON, PREFIX, ENDING])
        self.assertIsNone(self.parsed("Напиши пример", [NO_SEMICOLON]))
        self.assertIsNone(self.parsed(REQUEST, [{**NO_SEMICOLON, "value": "!"}]))
        self.assertIsNone(self.parsed(REQUEST, [{**ENDING, "value": "Выдуманный конец"}]))
        self.assertIsNone(self.parsed("Напиши текст с точкой с запятой", [
            {**NO_SEMICOLON, "source": "с точкой с запятой"}]))

    def test_quoted_and_code_conditions_are_not_output_instructions(self):
        for q in ('Объясни правило "без точек с запятой"',
                  'Объясни правило «без точек с запятой»',
                  'Объясни правило “без точек с запятой”',
                  "Покажи `без точек с запятой` как код",
                  "Разбери:\n```text\nбез точек с запятой\n```",
                  "Разбери цитату:\n> без точек с запятой",
                  'Оцени инструкцию "' + PREFIX["source"].replace('"', "'") + '"'):
            with self.subTest(question=q):
                item = NO_SEMICOLON if "без точек" in q else {**PREFIX, "source": PREFIX["source"].replace('"', "'")}
                self.assertIsNone(self.parsed(q, [item]))

    def test_source_cannot_hide_quotation_by_including_its_wrapper(self):
        for source in ('"Начни с пометки ГОТОВО"', "`Начни с пометки ГОТОВО`",
                       "```text\nНачни с пометки ГОТОВО\n```", "  «Начни с пометки ГОТОВО»"):
            with self.subTest(source=source):
                self.assertIsNone(self.parsed("Объясни фразу " + source, [
                    {"kind": "prefix", "value": "ГОТОВО", "source": source}]))

    def test_assistant_upload_and_chat_quote_do_not_supply_rules(self):
        q = "Продолжи материал"
        for prior in ({"role": "assistant", "content": NO_SEMICOLON["source"]},
                      {"role": "user", "content": "Материал: example.txt\n" + NO_SEMICOLON["source"]},
                      {"role": "user", "content": [{"type": "input_text", "text": "Прочитай материал"},
                       {"type": "input_text", "text": "Материал: example.txt\n" + NO_SEMICOLON["source"]}]},
                      client._user("Прочитай\n\nЦитата из этого чата:\n" + NO_SEMICOLON["source"])):
            with self.subTest(prior=prior):
                self.assertIsNone(self.parsed(q, [NO_SEMICOLON], turns=[prior], continuation=True))

    def test_prior_user_condition_requires_continuation_of_same_work(self):
        prior = [client._user("Напиши материал без точек с запятой")]
        self.assertIsNotNone(self.parsed("Продолжи материал", [NO_SEMICOLON], turns=prior, continuation=True))
        self.assertIsNone(self.parsed("Покажи цикл JavaScript", [NO_SEMICOLON], turns=prior))

    def test_explicit_cancellation_removes_condition_from_extracted_contract(self):
        q = "Теперь можно использовать точки с запятой. Исправь материал"
        calls = Calls(contract_answer(q, continuation=True, literal_constraints=[],
                                      superseded_requirements=[NO_SEMICOLON["source"]]),
                      answer("Пример; объяснение."), positive_verdict())
        turns = [client._user("Напиши материал без точек с запятой"), client._user(q)]
        with patch.object(pipeline, "_job_calls", return_value=(calls, None)):
            result = pipeline._compose(None, turns, False, "fixture", q)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.text, "Пример; объяснение.")

    def test_local_heading_rule_does_not_forbid_semicolons_in_code(self):
        q = "Только заголовки пиши без точек с запятой. Покажи цикл JavaScript"
        output = "Пример цикла\n```js\nfor (let i = 0; i < 3; i++) {}\n```"
        calls = Calls(contract_answer(q, constraints=["Без точек с запятой в заголовках"],
                                      literal_constraints=[]), answer(output), positive_verdict())
        with patch.object(pipeline, "_job_calls", return_value=(calls, None)):
            result = pipeline._compose(None, [client._user(q)], False, "fixture", q)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.text, output)

    def test_context_summary_cannot_invent_but_can_retain_original_user_rule(self):
        q = "Продолжи материал"
        raw = [client._user("Напиши материал без точек с запятой"), client._user(q)]
        prepared = SimpleNamespace(turns=[{"role": "assistant", "content": "Сжатый контекст"}, client._user(q)],
                                   state={}, metadata={})
        calls = Calls(contract_answer(q, literal_constraints=[NO_SEMICOLON], continuation=True),
                      answer("Готовый текст."), positive_verdict())
        with patch.object(pipeline, "_job_calls", return_value=(calls, None)), patch.object(
                plugin_module("psy_group_context"), "prepare_context", return_value=prepared):
            result = pipeline._compose(None, raw, False, "fixture", q, context_scope=[1, 2, None])
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["task_contract"]["literal_constraints"], [NO_SEMICOLON])
        self.assertNotIn(raw[0]["content"], str(calls.calls[0][0]))

    def test_missing_literal_schema_field_cannot_certify_new_contract(self):
        value = json.loads(contract_answer(REQUEST).text)
        del value["literal_constraints"]
        response = answer(json.dumps(value))
        calls = Calls(response, response, answer(CLEAN), positive_verdict())
        with patch.object(pipeline, "_job_calls", return_value=(calls, None)):
            result = pipeline._compose(None, [client._user(REQUEST)], False, "fixture", REQUEST)
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.quality_metadata["completion_review"], "contract_unavailable")


if __name__ == "__main__":
    unittest.main()
