"""Long scoped history and honest compaction, with synthetic messages only."""
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from psy_modules import psy_group_client as client, psy_group_context as context


def exchange(prompt, answer):
    return [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]


class ScopedContext(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(context, "_encoding", return_value=None))
        self.scope = [7, 3, 11]
        self.current = {"role": "user", "content": "Continue this task"}

    def prepare(self, history, call=None, **options):
        return context.prepare_context(history, self.current, call or Mock(), scope=self.scope,
                                       input_budget_tokens=options.pop("budget", 8000), **options)

    def test_full_eight_plus_turns_and_long_edits_remain_exact(self):
        history = exchange("Keep the table columns A and B", "Accepted")
        history += sum((exchange(f"unrelated clarification {i}", f"reply {i}") for i in range(9)), [])
        correction = "x" * 3500 + " Replace column B with C, and cancel the earlier title."
        history += exchange(correction, "y" * 4500)
        call = Mock()
        prepared = self.prepare(history, call, budget=20000)
        self.assertEqual(prepared.turns, [*history, self.current])
        self.assertIsNone(prepared.state)
        call.assert_not_called()
        self.assertIn(correction, [m["content"] for m in prepared.turns])

    def test_summary_keeps_changes_and_cancellation_with_user_provenance(self):
        history = exchange("Use label alpha", "First answer " * 160)
        history += exchange("Replace label alpha with beta", "Second answer " * 160)
        history += exchange("Cancel label beta", "Third answer " * 160)
        history += exchange("New topic: budget. Keep a separate table.", "Fourth answer " * 160)
        call = self.compactor()
        prepared = self.prepare(history, call, budget=2200)
        self.assertGreater(prepared.metadata["compacted_messages"], 0)
        memory = prepared.state["memory"]
        self.assertEqual(memory["requirements"][0]["status"], "cancelled")
        self.assertEqual(memory["requirements"][0]["quote"], "Cancel label beta")
        self.assertEqual(memory["requirements"][0]["source_message"], 5)
        self.assertEqual(memory["requirements"][0]["scope"], "labels")
        self.assertEqual(memory["closed_topics"], ["labels"])
        self.assertEqual(prepared.turns[-1], self.current)
        self.assertLessEqual(context.tokens(prepared.turns), 2200)

    @staticmethod
    def compactor():
        def compact(messages, instructions):
            payload = json.loads(messages[0]["content"])
            memory = payload["previous"] or {"summary": "Synthetic facts", "requirements": [],
                                              "closed_topics": [], "unresolved": []}
            for message in payload["messages"]:
                text = message["content"]
                if message["role"] == "user":
                    status = ("cancelled" if "Cancel label beta" in text else
                              "active" if "Use label alpha" in text or "Replace label alpha with beta" in text else None)
                    if status:
                        memory["requirements"] = [{"key": "label", "text": text, "scope": "labels",
                                                   "status": status, "source_message": message["message_index"], "quote": text}]
                    if "New topic" in text:
                        memory["closed_topics"] = ["labels"]
            return client.Result(json.dumps(memory), [], 0)
        return Mock(side_effect=compact)

    def test_replay_uses_saved_summary_without_recompacting_old_turns(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        initial = self.prepare(history, self.compactor(), budget=2200)
        later = history + exchange("Replace label alpha with beta", "Updated")
        call = Mock()
        replay = self.prepare(later, call, previous=initial.state, budget=2200)
        call.assert_not_called()
        self.assertEqual(replay.turns[-3:], [*later[-2:], self.current])
        self.assertEqual(replay.state, initial.state)

    def test_state_from_another_participant_topic_or_source_is_rejected(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        state = self.prepare(history, self.compactor(), budget=2200).state
        for other in ([8, 3, 11], [7, 4, 11], [7, 3, 12]):
            with self.subTest(scope=other), self.assertRaisesRegex(ValueError, "scope mismatch"):
                context.prepare_context(history, self.current, Mock(), scope=other,
                                        previous=state, input_budget_tokens=2200)

    def test_deleted_or_changed_history_rebuilds_state_from_originals(self):
        history = exchange("Use label alpha", "Large answer " * 500)
        state = self.prepare(history, self.compactor(), budget=2200).state
        changed = exchange("Entirely new task", "Small answer")
        call = Mock()
        prepared = self.prepare(changed, call, previous=state)
        self.assertTrue(prepared.metadata["state_rebuilt"])
        self.assertIsNone(prepared.state)
        self.assertEqual(prepared.turns, [*changed, self.current])
        self.assertNotIn("alpha", json.dumps(prepared.turns))

    def test_invented_requirement_quote_does_not_become_memory(self):
        memory = {"summary": "Invented", "requirements": [{"key": "bad", "text": "Invented constraint",
                  "scope": "conversation", "status": "active", "source_message": 1, "quote": "Not in original"}],
                  "closed_topics": [], "unresolved": []}
        call = Mock(return_value=client.Result(json.dumps(memory), [], 0))
        with self.assertRaises(client.ModelIssue) as caught:
            self.prepare(exchange("Use label alpha", "Large answer " * 500), call, budget=2200)
        self.assertEqual(caught.exception.stage, "context_compaction_invalid")
        self.assertEqual(call.call_count, 2)

    def test_existing_requirement_cannot_silently_disappear(self):
        previous = {"summary": "Known", "requirements": [{"key": "label", "text": "Use label alpha",
                    "scope": "labels", "status": "active", "source_message": 1, "quote": "Use label alpha"}],
                    "closed_topics": [], "unresolved": []}
        omitted = {"summary": "Known", "requirements": [], "closed_topics": [], "unresolved": []}
        self.assertIsNone(context._validate_memory(omitted, exchange("Use label alpha", "ok"), previous))

    def test_large_current_attachment_is_never_rewritten_here(self):
        current = {"role": "user", "content": [{"type": "input_text", "text": "Материал: demo\n" + "z" * 10000}]}
        prepared = context.prepare_context([], current, Mock(), scope=self.scope, input_budget_tokens=2200)
        self.assertIs(prepared.turns[-1], current)
        self.assertTrue(prepared.metadata["current_requires_mapping"])

    def test_all_characters_of_large_old_message_are_read_in_fragments(self):
        old_answer = "START " + "x" * 5000 + " END"
        call = self.compactor()
        self.prepare(exchange("Use label alpha", old_answer), call, budget=2200)
        fragments = [fragment for args in call.call_args_list
                     for fragment in json.loads(args.args[0][0]["content"])["messages"]]
        observed = "".join(item["content"] for item in fragments if item["message_index"] == 2)
        self.assertEqual(observed, old_answer)

    def test_context_budget_uses_server_catalog_and_reports_its_origin(self):
        module = types.ModuleType("agent.model_metadata")
        module._resolve_codex_oauth_context_length_with_source = Mock(return_value=(500000, "live"))
        credentials = {"api_key": "synthetic-secret", "base_url": "https://example.invalid"}
        with patch.dict(sys.modules, {"agent.model_metadata": module}), patch.object(context, "_credentials", return_value=credentials):
            budget, metadata = context.resolve_budget("chosen-model")
        self.assertEqual(budget, 400000)
        self.assertEqual(metadata, {"context_window_tokens": 500000, "context_window_origin": "live", "reserved_tokens": 100000})
        self.assertNotIn("synthetic-secret", json.dumps(metadata))
        module._resolve_codex_oauth_context_length_with_source.assert_called_once_with(
            "chosen-model", access_token="synthetic-secret", base_url="https://example.invalid")

    def test_unknown_window_is_not_guessed(self):
        module = types.ModuleType("agent.model_metadata")
        module._resolve_codex_oauth_context_length_with_source = Mock(return_value=(None, ""))
        with patch.dict(sys.modules, {"agent.model_metadata": module}), patch.object(context, "_credentials", return_value={"api_key": "x", "base_url": "url"}):
            with self.assertRaises(client.ModelIssue) as caught:
                context.resolve_budget("unknown-model")
        self.assertEqual(caught.exception.stage, "context_window_unknown")


if __name__ == "__main__":
    unittest.main()
