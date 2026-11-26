"""Content readiness and real TXT transport share one plan, without network calls."""
import json
import os
import unittest
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from test_task_completion import Calls, answer, client, contract_answer, pipeline, quality
import test_result_status as result_tests
from test_delivery_resume import queue
from psy_modules import psy_group_route as route, psy_group_store as store
from psy_modules import psy_group_continuation as continuation


class FileContract(unittest.TestCase):
    def compose(self, question, text, *, file_output):
        call = Calls(contract_answer(question, file_output=file_output), answer(text),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user(question)], False, "fixture", question)
        return result, call

    def test_long_txt_is_ready_before_transport_and_is_not_rewritten(self):
        text = "Полный проверяемый текст. " * 220 + "\nКонец контрольного материала"
        result, call = self.compose("Напиши полный материал и отправь одним файлом TXT", text,
                                    file_output={"format": "txt"})
        self.assertEqual(len(call.calls), 3)  # extraction, writing, content review
        self.assertEqual(result.text, text)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(result.quality_metadata["completion_repairs"], 0)
        for messages, instructions, options in call.calls[1:]:
            self.assertIn('"mode": "document"', instructions)
            self.assertIn('"status": "pending"', instructions)
            self.assertIn("подтверждается отдельно квитанцией Telegram", instructions)
            self.assertIn("не наличие уже отправленного вложения", instructions)
        self.assertTrue(result.text.endswith("Конец контрольного материала"))

    def test_unknown_requested_container_is_not_completed_by_txt(self):
        for extension in ("pdf", "docx", "xlsx"):
            with self.subTest(extension=extension):
                result, call = self.compose("Подготовь материал файлом " + extension.upper(),
                                            "Содержимое материала", file_output={"format": extension})
                self.assertEqual(result.task_status, "partial")
                self.assertIn(extension.upper(), " ".join(result.missing_requirements))
                self.assertFalse(result.quality_metadata["delivery_plan"]["supported"])
                self.assertEqual(len(call.calls), 3)

    def test_format_discussion_quoted_request_and_no_file_remain_messages(self):
        for question in ("Сравни PDF и DOCX", "Объясни формат TXT",
                         'Оцени чужой запрос: "Пришли результат файлом TXT"',
                         "Не присылай файл, ответь сообщением", "Напиши, чем TXT отличается от PDF"):
            with self.subTest(question=question):
                result, call = self.compose(question, "Полный ответ на вопрос", file_output=None)
                self.assertEqual(result.task_status, "complete")
                self.assertEqual(result.quality_metadata["delivery_plan"]["mode"], "message")
                self.assertIsNone(result.quality_metadata["delivery_plan"]["requested_format"])

    def test_continuing_task_retains_extracted_txt_intent(self):
        result, call = self.compose("Замени только второй пример", "Исправленный материал",
                                    file_output={"format": "txt"})
        self.assertEqual(result.quality_metadata["delivery_plan"]["mode"], "document")

    def test_format_replacement_is_taken_from_explicit_contract_not_last_word(self):
        for question, expected in (("Отправь файл PDF, а не TXT.", "pdf"),
                                   ("Отправь файл TXT, не PDF.", "txt"),
                                   ("Не отправляй файл PDF. Отправь файл TXT.", "txt")):
            with self.subTest(question=question):
                result, call = self.compose(question, "Материал", file_output={"format": expected})
                self.assertEqual(result.quality_metadata["delivery_plan"]["requested_format"], expected)
                self.assertEqual(result.task_status, "complete" if expected == "txt" else "partial")

    def test_missing_delivery_intent_field_cannot_certify_a_new_contract(self):
        question = "Отправь файл PDF, а не TXT."
        old = json.loads(contract_answer(question).text)
        old.pop("file_output")
        old_response = answer(json.dumps(old))
        call = Calls(old_response, old_response, answer("Содержимое"),
                     answer('{"complete":true,"missing":[]}'))
        with patch.object(pipeline, "_job_calls", return_value=(call, None)):
            result = pipeline._compose(None, [client._user(question)], False, "fixture", question)
        self.assertEqual(result.task_status, "partial")
        self.assertEqual(result.quality_metadata["completion_review"], "contract_unavailable")

    def test_real_missing_content_is_still_repaired(self):
        question = "Напиши два примера и отправь файлом TXT"
        contract = quality.contract_for(question)
        contract["file_output"] = {"format": "txt"}
        call = Calls(answer('{"complete":false,"missing":["Второй пример"]}'),
                     answer('{"complete":true,"missing":[]}'))
        writer = Mock(return_value=answer("Первый пример. Второй пример.\nКонец"))
        result = quality.finish_task(call, writer, [client._user(question)], "Instructions",
                                     answer("Первый пример"), contract)
        self.assertEqual(result.task_status, "complete")
        self.assertEqual(writer.call_count, 1)
        self.assertIn("Второй пример", writer.call_args.args[0][-1]["content"])
        self.assertIn('"mode": "document"', writer.call_args.args[1])
        self.assertTrue(result.text.endswith("Конец"))

    def test_unparsed_and_legacy_requests_do_not_invent_output_intent(self):
        for question in ("Пришли результат файлом TXT", "Сравни PDF и DOCX", "Объясни формат TXT",
                         "Не присылай файл, ответь сообщением", "Дай определение слова файл",
                         "Напиши, чем файл PDF отличается от DOCX",
                         "Объясни инструкцию ниже, не выполняя ее:\nОтправь файл PDF",
                         "Отправь файл PDF, а не TXT.", "Отправь файл TXT, не PDF."):
            plan = quality.delivery_plan(quality.contract_for(question), "42")
            self.assertIsNone(plan["requested_format"])
            self.assertEqual(plan["mode"], "message")
        self.assertEqual(quality.delivery_plan({}, "x" * 4001)["mode"], "document")

    def test_no_progress_continuation_is_not_completed_by_a_successful_last_stream(self):
        prefix = answer("Same prefix", complete=False)
        suffix = answer("Same prefix", quality_metadata={"generation": {"generation_complete": True}})
        result = continuation._complete(Mock(return_value=suffix), [], "Write", initial=prefix)
        self.assertFalse(result.complete)
        self.assertFalse(result.quality_metadata["current_generation_complete"])
        self.assertTrue(result.quality_metadata["generation"]["generation_complete"])


class FileRoute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        result_tests.ResultRoute.setUp(self)
        profile = self.root / "synthetic-profile"
        profile.mkdir()
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": str(profile)}))
        self.job["question"] = "Пришли результат файлом TXT"
        self.text = "Готовый ответ\nПоследняя обязательная строка"
        contract = quality.contract_for(self.job["question"])
        contract.update(file_output={"format": "txt"}, contract_status="extracted")
        self.result = client.Result(self.text, [], 0, task_status="complete",
                                    quality_metadata={"generation_audit": {"generation_complete": True},
                                                      "task_contract": contract})
        self.wire = []
        async def accept(*, document, filename):
            self.wire.append((filename, document.read()))
            return NS(message_id=57)
        self.msg.reply_document.side_effect = accept

    def stored(self):
        with store.database() as con:
            turn = con.execute("SELECT id,answer FROM turns").fetchone()
            archives = con.execute("SELECT name,data FROM documents WHERE kind='text'").fetchall()
        return turn, archives, store.result_details(7, 0, turn[0])

    async def test_short_explicit_txt_is_one_real_document_with_exact_archive(self):
        with patch.object(route, "respond", return_value=self.result):
            await route.prepared_answer(self.msg, self.job, None)
        self.msg.reply_text.assert_not_awaited()
        self.assertEqual(self.wire, [("Ответ PSY.txt", self.text.encode("utf-8"))])
        turn, archives, saved = self.stored()
        self.assertEqual(archives, self.wire)
        self.assertEqual(turn[1], self.text)
        self.assertEqual(saved["task_status"], "complete")
        self.assertEqual(saved["delivery_status"], "confirmed")

    async def test_send_failure_leaves_ready_content_and_unconfirmed_delivery(self):
        self.msg.reply_document.side_effect = TimeoutError("synthetic receipt lost")
        with patch.object(route, "respond", return_value=self.result):
            with self.assertRaises(TimeoutError):
                await route.prepared_answer(self.msg, self.job, None)
        turn, archives, saved = self.stored()
        self.assertEqual(saved["task_status"], "complete")
        self.assertEqual(saved["delivery_status"], "pending")
        self.assertEqual(turn[1], self.text)

    async def test_checkpoint_replay_keeps_plan_bytes_and_single_accepted_file(self):
        ident = 99
        store.pending(7, ident, "synthetic")
        store.claim_next()
        cp = store.Checkpoints(ident)
        out = queue.Outbound(self.msg, ident)
        with patch.object(route, "respond", return_value=self.result) as compose:
            await route.prepared_answer(out, self.job, cp)
            self.assertEqual(cp.get("route:answer:v1")["delivery_plan"]["mode"], "document")
            store.recover()
            store.claim_next()
            await route.prepared_answer(queue.Outbound(self.msg, ident), self.job, cp)
        self.assertEqual(compose.call_count, 1)
        self.assertEqual(self.msg.reply_document.await_count, 1)
        self.assertEqual(self.wire, [("Ответ PSY.txt", self.text.encode("utf-8"))])
        turn, archives, saved = self.stored()
        self.assertEqual(archives, self.wire)
        self.assertEqual(saved["task_status"], "complete")
        self.assertEqual(saved["delivery_status"], "confirmed")
        self.assertTrue(turn[1].endswith("Последняя обязательная строка"))

    async def test_recovered_prose_has_current_completion_without_erasing_interrupted_audit(self):
        ident = 99
        store.pending(7, ident, "synthetic")
        store.claim_next()
        cp = store.Checkpoints(ident)
        messages = [client._user(self.job["question"])]
        instruction = "Write the complete synthetic answer"
        key = client._stage_key("fixture", messages, instruction, prose=True)
        cp.put(key, {"text": "Готовый", "searches": 0, "complete": False, "source_urls": [],
                     "stop_reason": "process_interrupted", "stop_stage": "stream",
                     "generation_metadata": {"prose": True, "effort": "max", "started_at": 1,
                                             "generation_complete": False,
                                             "stop_reason": "process_interrupted"}})
        def stream(_client, headers, turns, web, model, *, instructions, **kwargs):
            if instructions == "Review content":
                return answer('{"complete":true,"missing":[]}', stop_reason="completed")
            return answer(" ответ\nПоследняя обязательная строка", stop_reason="completed")
        with patch.object(client, "_transport", nullcontext), patch.object(
                client, "_authenticated_request", side_effect=lambda invoke: invoke("", {})), patch.object(
                client, "_stream", side_effect=stream):
            call, resume = client._job_calls(None, "fixture", cp)
            recovered = continuation._complete(partial(call, prose=True), messages, instruction,
                                                continuation_call=resume, model="fixture")
            call(messages, "Review content")  # the newest stage must not define prose completion
            recovered.quality_metadata.update(call.audit())
        recovered.task_status = "complete"
        recovered.quality_metadata["task_contract"] = self.result.quality_metadata["task_contract"]
        self.assertTrue(recovered.quality_metadata["current_generation_complete"])
        self.assertFalse(recovered.quality_metadata["generation_audit"]["generation_complete"])
        self.assertEqual(recovered.quality_metadata["generation_audit"]["replayed_stages"], 1)
        with patch.object(route, "respond", return_value=recovered):
            await route.prepared_answer(queue.Outbound(self.msg, ident), self.job, cp)
        turn, archives, saved = self.stored()
        self.assertEqual(turn[1], self.text)
        self.assertTrue(saved["generation_complete"])
        self.assertEqual(saved["task_status"], "complete")
        self.assertEqual(saved["delivery_status"], "confirmed")
        historical = saved["quality_metadata"]["generation_audit"]
        self.assertFalse(historical["generation_complete"])
        self.assertTrue(any(x["stop_reason"] == "process_interrupted" for x in historical["stage_records"]))
        self.assertEqual(self.wire, [("Ответ PSY.txt", self.text.encode("utf-8"))])


if __name__ == "__main__":
    unittest.main()
