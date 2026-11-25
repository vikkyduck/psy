"""Generic output ceilings preserve text, history and diagnostic provenance."""
import unittest
from unittest.mock import Mock

from psy_modules import psy_group_client as client, psy_group_continuation as continuation


def result(text, complete=True, **kwargs):
    return client.Result(text, [], 0, complete, **kwargs)


class UniversalContinuation(unittest.TestCase):
    def test_cut_inside_code_token_does_not_insert_newline(self):
        initial = result('def greet():\n    return "hel', False, stop_reason="max_output_tokens")
        call = Mock(return_value=result('lo"\n', stop_reason="completed"))
        output = continuation._complete(call, [client._user("Напиши функцию приветствия")], "instructions", initial=initial)
        self.assertEqual(output.text, 'def greet():\n    return "hello"\n')
        self.assertTrue(output.complete)
        self.assertEqual(output.stop_reason, "completed")
        self.assertEqual(output.quality_metadata["continuation_parts"], 2)

    def test_beginning_and_middle_are_available_after_multiple_provider_cuts(self):
        start = "Начальное определение. " + "a" * 5000
        middle = "\nУточненный расчет. " + "b" * 5000
        call = Mock(side_effect=[result(start, False), result(middle, False), result("\nОкончание")])
        output = continuation._complete(call, [client._user("Разбери все данные без сокращений")], "instructions")
        self.assertEqual(output.text, start + middle + "\nОкончание")
        self.assertEqual(call.call_args_list[2].args[0][-2]["content"], start + middle)

    def test_context_rejection_preserves_full_output_and_exact_reason(self):
        initial = result("Сохраненный материал" * 1000, False, stop_reason="max_output_tokens")
        call = Mock(side_effect=client.ModelIssue("context", "http", stop_reason="context_length_exceeded"))
        output = continuation._complete(call, [client._user("Продолжи работу")], "instructions", initial=initial)
        self.assertTrue(output.text.startswith(initial.text))
        self.assertFalse(output.complete)
        self.assertEqual(output.stop_reason, "context_length_exceeded")
        self.assertEqual(output.stop_stage, "http")

    def test_repeated_chunk_has_distinct_stop_reason(self):
        initial = result("Сохраненный текст", False, stop_reason="max_output_tokens")
        call = Mock(return_value=result("Сохраненный текст", False))
        output = continuation._complete(call, [], "instructions", initial=initial)
        self.assertEqual(output.text, initial.text)
        self.assertFalse(output.complete)
        self.assertEqual(output.stop_reason, "continuation_no_progress")

    def test_urls_and_model_identity_survive_assembly(self):
        first = result("Начало ", False, source_urls=("https://example.org/a",), model_actual="selected")
        call = Mock(return_value=result("конец", source_urls=("https://example.org/b",), model_actual="selected"))
        output = continuation._complete(call, [], "instructions", initial=first)
        self.assertEqual(output.source_urls, ("https://example.org/a", "https://example.org/b"))
        self.assertEqual(output.model_actual, "selected")


if __name__ == "__main__":
    unittest.main()
