"""The same trusted-looking search citation must fail for a fabricated excerpt."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import socket
import threading
import unittest
from unittest.mock import patch

from psy_modules import psy_group_client as codex, psy_group_codex as model
from psy_modules import psy_group_pipeline as pipeline, psy_group_prompts as prompts
from psy_modules import psy_group_sources as sources
from psy_modules import psy_source_evidence as evidence


URL = "https://archive.example/actual"
BODY = b"<title>Untrusted page label</title><h1>Repression</h1><p>Sigmund Freud</p><p>Observed wording from the original publication.</p><script>Private fake quote</script>"


class Evidence(unittest.TestCase):
    def test_search_hit_with_plausible_invented_metadata_is_insufficient(self):
        reader = evidence.EvidenceReader()
        card = dict(author="Sigmund Freud", work="Repression", date="1915", location="p. 999",
                    support="This invented passage sounds entirely plausible", claim="Unverified conclusion",
                    url=URL, access="full_text")
        with patch.object(evidence, "retrieve", return_value=("text/html","utf-8",BODY)) as get:
            self.assertIsNone(sources._source_card(card, {URL}, reader))
            self.assertIsNone(sources._source_card(dict(card,support="Private fake quote"), {URL}, reader))
            real = dict(card,support="Observed wording from the original publication.")
            self.assertIsNone(sources._source_card(dict(real,author="Other Writer"), {URL}, reader))
            self.assertIsNone(sources._source_card(dict(real,work="Different publication"), {URL}, reader))
            result = sources._source_card(real, {URL}, reader)
            self.assertEqual((result["access"],result["location"]), ("excerpt","HTML, абзац 4"))
            get.assert_called_once()

    def test_wrong_pdf_page_is_rejected(self):
        reader = evidence.EvidenceReader()
        with patch.object(evidence, "passages",return_value=("PDF",["Sigmund Freud Repression","three actual source words"])):
            for page, expected in ((1,False),(2,True)):
                found = reader.locate(URL + "#page=" + str(page),"three actual source words","unknown","Sigmund Freud","Repression")
                self.assertEqual(bool(found), expected)

    def test_cannot_connect_to_private_or_mixed_peer(self):
        for ip in ("127.0.0.1","169.254.169.254","::1","224.0.0.1"):
            with patch.object(socket,"getaddrinfo",return_value=[(2,1,6,"",("8.8.8.8",443)),(2,1,6,"",(ip,443))]), patch.object(socket, "socket") as dial:
                with self.assertRaises(ValueError):
                    evidence.PublicConnection("public.example").connect()
                dial.assert_not_called()

    def test_single_turn_model_topic_never_escapes(self):
        question = "Сравни случай Условный Маркер Сосна с вытеснением у Фрейда"
        def stream(client, headers, turns, web, model_id, *, instructions, **kw):
            if prompts.DISPATCH in instructions:
                return model.Result('PSY_TASK {"public_topic":"Условный Маркер Сосна, вытеснение"}', [], 0)
            return model.Result("Ответ", [], 0)
        def find_sources(call, topic, *, read_main):
            self.assertEqual(topic, "Freud Repression 1915")
            return model.Result("Проверка",[],0,False)
        with patch.object(codex,"_transport",nullcontext),patch.object(codex,"_stream",stream),patch.object(codex,"_connection",return_value=("fixture",{}, "fixture-generation")),patch.object(pipeline,"_primary_sources",side_effect=find_sources) as lookup:
            pipeline._compose(None,[codex._user(question)],False,"fixture",question)
            lookup.assert_called_once()
        self.assertEqual(evidence.public_topic("Разбери Условный Маркер Сосна"), "")
        self.assertEqual(evidence.public_topic("Публичная тема: RFC 9110\nУсловный Маркер Сосна"),"RFC 9110")
        self.assertEqual(evidence.public_topic("Разбери запись\nПубличная тема: Условный Маркер Сосна"),"")

    def test_public_subjects_preserve_the_requested_authors_without_case_text(self):
        topic = evidence.public_topic("Сравни Винникотта и Мелани Кляйн, переходный объект, пациент Условный Маркер")
        self.assertIn("Winnicott",topic)
        self.assertIn("Melanie Klein",topic)
        self.assertIn("Transitional Objects",topic)
        self.assertNotIn("Маркер",topic)
        topic = evidence.public_topic("Вытеснение у Фрейда, Лапланш и Понталис")
        self.assertIn("Laplanche",topic)
        self.assertIn("Pontalis",topic)

    def test_unknown_requested_research_asks_for_release_instead_of_inventing_sources(self):
        question = "Разбери Условный Маркер"
        stream = lambda *args,**kwargs: model.Result('PSY_TASK {"public_topic":"Условный Маркер"}', [], 0)
        with (patch.object(codex,"_transport",nullcontext),patch.object(codex,"_stream",stream),
              patch.object(codex,"_connection",return_value=("fixture",{}, "fixture-generation")),
              patch.object(pipeline,"_primary_sources") as search):
            result=pipeline._compose(None,[codex._user(question)],False,"fixture",question)
        self.assertIn("Публичная тема",result.text)
        search.assert_not_called()

    def test_total_cache_is_bounded_across_distinct_large_sources(self):
        reader=evidence.EvidenceReader()
        text="On test content by one Author. " * 50
        with patch.object(evidence,"CACHE_LIMIT",2200), patch.object(evidence,"passages",return_value=("HTML",[text])):
            self.assertIsNotNone(reader.locate(URL,"On test content","p.2","Author","On test content"))
            self.assertIsNone(reader.locate(URL+"/2","On test content","p.2","Author","On test content"))
        self.assertLessEqual(reader.cache_bytes,2200)

    def test_slow_page_does_not_block_other_pages_and_is_fetched_once(self):
        reader = evidence.EvidenceReader()
        release, started = threading.Event(), threading.Event()
        calls = Counter()
        def passages(base):
            calls[base] += 1
            if base.endswith("/slow"):
                started.set()
                self.assertTrue(release.wait(5))
            return ("HTML", ["Sigmund Freud, Repression", "three actual source words"])
        check = ("three actual source words", "p. 1", "Sigmund Freud", "Repression")
        with patch.object(evidence, "passages", passages), ThreadPoolExecutor(3) as pool:
            slow = [pool.submit(reader.locate, URL + "/slow", *check) for _ in range(2)]
            self.assertTrue(started.wait(5))
            self.assertIsNotNone(reader.locate(URL + "/fast", *check))
            self.assertFalse(any(future.done() for future in slow))
            release.set()
            self.assertTrue(all(future.result(5) for future in slow))
        self.assertEqual(calls, Counter({URL + "/slow": 1, URL + "/fast": 1}))

    def test_no_page_count_limit_within_the_memory_bound(self):
        reader = evidence.EvidenceReader()
        with patch.object(evidence, "passages", return_value=("HTML", ["Sigmund Freud, Repression", "three actual source words"])):
            found = [reader.locate(f"{URL}/{i}", "three actual source words", "p. 1", "Sigmund Freud", "Repression")
                     for i in range(40)]
        self.assertTrue(all(found))
