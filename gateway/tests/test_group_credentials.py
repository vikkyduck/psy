import hashlib
import importlib
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from unittest.mock import ANY, Mock, patch
import json

from psy_modules import psy_group_codex as adapter


class OAuthError(Exception):
    def __init__(self, code, status):
        self.code = code
        self.oauth_refresh_status = status
        self.relogin_required = True


class TestPreferredCredentials(unittest.TestCase):
    def setUp(self):
        dirs = tempfile.TemporaryDirectory()
        self.addCleanup(dirs.cleanup)
        self.markers = Path(dirs.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(adapter.codex_policy, "MARKERS", self.markers))
        self.primary = dict(id="first", priority=0, auth_type="oauth", source="manual:device_code",
                            access_token="live", refresh_token="first-grant")
        self.other = dict(self.primary, id="other", priority=1, access_token="other", refresh_token="alternate-grant")
        self.rows = [self.other, self.primary]
        self.store = {"credential_pool": {"openai-codex": self.rows}}
        self.modules = {k: ModuleType(k) for k in ("hermes_cli", "hermes_cli.auth", "hermes_cli.auth_codex",
                                                   "hermes_cli.auth_constants", "httpx")}
        self.auth = self.modules["hermes_cli.auth"]
        self.codex = self.modules["hermes_cli.auth_codex"]
        constants = self.modules["hermes_cli.auth_constants"]
        constants.AuthError = OAuthError
        constants._decode_jwt_claims = lambda token: {"exp": time.time() + {"old":-30, "soon":60}.get(token, 6000)}
        self.modules["httpx"].RequestError = type("RequestError", (Exception,), {})
        constants.httpx = self.modules["httpx"]
        for name in ("PoolTimeout", "ConnectTimeout", "ConnectError", "ReadTimeout", "WriteError"):
            setattr(constants.httpx, name, type(name, (constants.httpx.RequestError,), {}))
        stack.enter_context(patch.object(adapter.codex_policy, "_incident"))
        self.auth._auth_file_path = lambda: self.markers / "test-auth.json"
        self.auth._load_auth_store = lambda: deepcopy(self.store)
        self.auth._save_auth_store = Mock(side_effect=self._save)
        self.auth._auth_store_lock = self._lock
        self.codex._codex_pool_row_available = lambda row, sole: not row.get("last_error_reset_at")
        self.codex._codex_pool_route_base_url = lambda base: base
        self.codex.refresh_codex_oauth_pure = Mock(side_effect=self._refresh)
        stack.enter_context(patch.dict(sys.modules, self.modules))
        self.locked = False
        self.on_lock = None

    @contextmanager
    def _lock(self, **kwargs):
        self.assertFalse(self.locked)
        self.locked = True
        try:
            if self.on_lock:
                self.on_lock()
            yield
        finally:
            self.locked = False

    def _refresh(self, access, grant, *, timeout_seconds=20.0, on_received=None):
        self.assertTrue(self.locked)
        self.assertEqual((access, grant), (self.primary["access_token"], self.primary["refresh_token"]))
        return dict(access_token="renewed", refresh_token="rotated", last_refresh="now")

    def _save(self, new_store):
        self.assertTrue(self.locked)
        self.store = deepcopy(new_store)

    def issue(self, kind):
        with self.assertRaises(adapter.CredentialIssue) as caught:
            adapter.get_credentials()
        self.assertEqual(caught.exception.kind, kind)

    def cooldown(self, reason):
        self.primary.update(last_status="exhausted", last_error_reason=reason, last_error_code=429,
                            last_error_reset_at=int(time.time())+86400)

    def test_primary_only_on_happy_path_and_subscription_cooldown(self):
        for reason in (None, "usage_limit_reached", "insufficient_quota"):
            if reason:
                self.cooldown(reason)
            self.assertEqual(adapter.get_credentials(), {"provider":"openai-codex",
                             "base_url":"https://chatgpt.com/backend-api/codex", "api_key":"live",
                             "generation":adapter.codex_policy.generation(self.primary)})
        self.auth._save_auth_store.assert_not_called()
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_traffic_auth_and_unexplained_cooldowns(self):
        for code, reason in ((429,None), (429,"rate_limit_exceeded"), (401,"usage_limit_reached"), (503,"backend_down")):
            self.cooldown(reason)
            self.primary["last_error_code"] = code
            self.issue("access_wait")
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_opaque_weekly_429_gets_a_real_attempt_after_backoff(self):
        self.cooldown(None)
        now = time.time()
        self.primary.update(last_status_at=now-30, last_error_message="Error code: 429")
        self.issue("access_wait")
        self.primary["last_status_at"] = now-301
        self.assertEqual(adapter.get_credentials()["api_key"], "live")
        self.primary["last_error_reset_at"] = now+90
        self.issue("access_wait")
        self.primary.update(last_error_reset_at=now+86400, last_error_message="Requests per minute exceeded")
        self.issue("access_wait")
        self.auth._save_auth_store.assert_not_called()
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_semantic_quota_message(self):
        self.cooldown(None)
        self.primary["last_error_message"] = "You've hit your usage limit."
        self.assertEqual(adapter.get_credentials()["api_key"], "live")

    def test_wrong_or_ambiguous_primary_is_rejected(self):
        for field, value in (("source","device_code"), ("base_url","https://test.invalid"), ("last_status","dead"),
                             ("refresh_token",""), ("auth_type","api_key"), ("id","")):
            with self.subTest(field=field), patch.dict(self.primary,{field:value}):
                self.issue("signin")
        with patch.dict(self.other,priority=0):
            self.issue("signin")
        self.codex._codex_pool_route_base_url = lambda base: "https://test.invalid"
        self.issue("signin")
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_both_monitor_refusal_markers(self):
        for name, field in (("rejected","refresh_token"),("account-denied","access_token")):
            path = self.markers / name
            path.write_text(hashlib.sha256(self.primary[field].encode()).hexdigest())
            self.issue("signin")
            path.unlink()
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def install_grant_guard(self):
        # Keep the public plugin, Hermes grant guard and store transaction real, replace
        # only the underlying HTTP exchange, filesystem root and interprocess lock.
        policy = adapter.codex_policy
        self.network = self.codex.refresh_codex_oauth_pure
        self.auth._auth_file_path = lambda: self.markers / "test-auth.json"
        self.auth._global_auth_file_path = lambda: None
        self.auth._save_private_json = lambda path, data, **options: path.write_text(json.dumps(data))
        self.auth._auth_store_lock = lambda **kwargs: nullcontext() if "target_path" in kwargs else self._lock()
        pool = ModuleType("agent.credential_pool")
        pool._profile_owns_pool_provider = lambda _: True
        self.enterContext(patch.dict(sys.modules, {"agent.credential_pool": pool}))
        def block(code):
            error = OAuthError(code, None)
            error.relogin_required = False
            return error
        self.enterContext(patch.object(policy, "block_error", block))
        self.codex.refresh_codex_oauth_pure = lambda access, grant, **options: policy.guarded_exchange(
            grant, lambda: self.network(access, grant), on_received=options.get("on_received"))

    def test_three_questions_preconnect_failure_share_backoff_and_restart_recovers(self):
        self.install_grant_guard()
        self.network.side_effect = self.modules["httpx"].ConnectError()
        model = Mock(side_effect=lambda token: 401 if token == "live" else 200)
        for _ in range(3):
            with self.assertRaises(adapter.ModelIssue) as issue:
                self.group_question(model)
            self.assertEqual(issue.exception.kind, "access_wait")
        self.network.assert_called_once()
        model.assert_called_once()
        self.network.side_effect = self._refresh
        with patch("time.time", return_value=time.time()+65):
            self.assertEqual(self.group_question(model).text, "42")
        self.assertEqual(self.network.call_count, 2)

    def test_lost_rotation_response_is_never_replayed_by_fresh_process(self):
        self.install_grant_guard()
        self.network.side_effect = self.modules["httpx"].ReadTimeout()
        model = Mock(return_value=401)
        for _ in range(3):
            with self.assertRaises(adapter.ModelIssue) as issue:
                self.group_question(model)
            self.assertEqual(issue.exception.kind, "signin")
        with patch("time.time", return_value=time.time()+4*86400):
            with self.assertRaises(OAuthError) as issue:
                self.codex.refresh_codex_oauth_pure("live", "first-grant")
            self.assertEqual(issue.exception.code, "psy_refresh_uncertain")
        self.network.assert_called_once()
        model.assert_called_once()
        # A replacement login ignores the journal entry for a different grant.
        row = self.store["credential_pool"]["openai-codex"][1]
        row.update(access_token="signed-in", refresh_token="fresh-login")
        self.assertEqual(self.group_question(lambda _: 200).text, "42")

    def test_incomplete_server_response_never_replays_grant(self):
        self.install_grant_guard()
        for status in (200, 502, 503, 504):
            grant = "invalid-response-fixture-" + str(status)
            self.network.side_effect = OAuthError("invalid_response", status)
            for _ in range(2):
                with self.assertRaises(OAuthError) as issue:
                    self.codex.refresh_codex_oauth_pure("live", grant)
                self.assertEqual(issue.exception.code, "psy_refresh_uncertain")
            self.network.assert_called_once()
            self.network.reset_mock()

    def test_token_endpoint_429_respects_server_retry_after_across_callers(self):
        self.install_grant_guard()
        error = OAuthError("rate_limit", 429)
        error.retry_after = 115
        self.network.side_effect = error
        self.primary["access_token"] = "old"
        self.issue("access_wait")
        with patch("time.time", return_value=time.time()+90):
            self.issue("access_wait")
        self.network.assert_called_once()
        self.network.side_effect = self._refresh
        with patch("time.time", return_value=time.time()+120):
            self.assertEqual(adapter.get_credentials()["api_key"], "renewed")
        self.assertEqual(self.network.call_count, 2)

    def test_conflicting_account_claim_cannot_receive_a_rotated_grant(self):
        constants = self.modules["hermes_cli.auth_constants"]
        constants._decode_jwt_claims = lambda token: {"sub": token, "exp": time.time()+60}
        self.rows.append(dict(self.primary, id="alias", priority=2, access_token="different-account"))
        self.issue("access_wait")
        self.auth._save_auth_store.assert_not_called()
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_conflicting_alias_owners_with_partial_primary_do_not_receive_tokens(self):
        self.primary["access_token"] = "soon"
        accounts = {"first-alias": "X", "second-alias": "Y"}
        self.modules["hermes_cli.auth_constants"]._decode_jwt_claims = lambda token: {
            "exp": time.time() + 60, "sub": "A",
            "https://api.openai.com/auth": {"chatgpt_account_id": accounts.get(token)},
        }
        for i, key in enumerate(accounts, 2):
            self.rows.append(dict(self.primary, id=key, priority=i, access_token=key))
        self.issue("access_wait")
        self.codex.refresh_codex_oauth_pure.assert_not_called()
        self.auth._save_auth_store.assert_not_called()

    def test_an_exchange_returning_another_owner_quarantines_only_its_grant(self):
        self.primary["access_token"] = "soon"
        self.rows.append(dict(self.primary, id="alias", priority=2))
        self.modules["hermes_cli.auth_constants"]._decode_jwt_claims = lambda token: {
            "exp": time.time() + 60, "sub": "B" if token == "renewed" else "A",
        }
        for _ in range(2):
            self.issue("signin")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        rows = self.store["credential_pool"]["openai-codex"]
        self.assertEqual(rows[0], self.other)
        self.assertTrue(all(r["access_token"] == "soon" and r["last_status"] == "dead" for r in rows[1:]))
        self.assertNotIn("renewed", repr(self.store))

    def test_corrupt_marker_fails_closed_without_redeeming_grant(self):
        self.install_grant_guard()
        directory = self.markers / "psy-codex-renewals"
        directory.mkdir()
        path = directory / (adapter.codex_policy.fingerprint("first-grant") + ".json")
        for damaged in ("{", "null", "{}", '{"state":"wait"}'):
            path.write_text(damaged)
            with self.assertRaises(OAuthError) as issue:
                self.codex.refresh_codex_oauth_pure("live", "first-grant")
            self.assertEqual(issue.exception.code, "psy_refresh_uncertain")
        self.network.assert_not_called()

    def test_process_stop_during_exchange_never_resends_pending_grant(self):
        self.install_grant_guard()
        self.network.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.codex.refresh_codex_oauth_pure("live", "first-grant")
        with self.assertRaises(OAuthError) as issue:
            self.codex.refresh_codex_oauth_pure("live", "first-grant")
        self.assertEqual(issue.exception.code, "psy_refresh_uncertain")
        self.network.assert_called_once()

    def test_failed_proactive_renewal_can_use_unrejected_bearer_until_expiry(self):
        self.install_grant_guard()
        self.primary["access_token"] = "soon"
        self.network.side_effect = self.modules["httpx"].ReadTimeout()
        for _ in range(2):
            self.assertEqual(self.group_question(lambda _: 200).text, "42")
        self.network.assert_called_once()

    def test_legacy_alias_renewed_in_same_transaction_and_independent_login_preserved(self):
        self.primary["access_token"] = "old"
        self.rows.append(dict(self.primary, id="alias", priority=2, label="alias", last_error_code=429))
        self.assertEqual(adapter.get_credentials()["api_key"], "renewed")
        rows = self.store["credential_pool"]["openai-codex"]
        self.assertEqual(rows[0], self.other)
        self.assertEqual([r["refresh_token"] for r in rows[1:]], ["rotated", "rotated"])
        self.assertEqual(rows[2]["last_error_code"], 429)
        self.auth._save_auth_store.assert_called_once()
        self.codex.refresh_codex_oauth_pure.assert_called_once()

    def test_conflicting_alias_cannot_receive_tokens(self):
        self.primary["access_token"] = "soon"
        self.rows.append(dict(self.primary, id="alias", priority=2, base_url="https://test.invalid"))
        self.issue("access_wait")
        self.auth._save_auth_store.assert_not_called()
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_near_expiry_and_quota_are_renewed_once_with_status_preserved(self):
        self.cooldown("usage_limit_exceeded")
        self.primary["access_token"] = "soon"
        old_status = {k:v for k,v in self.primary.items() if k.startswith("last_error") or k=="last_status"}
        for i in range(2):
            self.assertEqual(adapter.get_credentials()["api_key"], "renewed")
        rows = self.store["credential_pool"]["openai-codex"]
        self.assertEqual(rows[0], self.other)
        self.assertEqual({k:rows[1][k] for k in old_status}, old_status)
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.auth._save_auth_store.assert_called_once()

    def test_concurrent_rotation_and_replacement(self):
        self.primary["access_token"] = "old"
        self.on_lock = lambda: self.primary.update(access_token="peer-live",refresh_token="peer-grant")
        self.assertEqual(adapter.get_credentials()["api_key"], "peer-live")
        self.primary["access_token"] = "old"
        self.on_lock = lambda: self.primary.update(id="different",access_token="different-login")
        self.issue("access_wait")
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_rejected_generation_not_retried(self):
        self.primary["access_token"] = "old"
        self.codex.refresh_codex_oauth_pure.side_effect = OAuthError("refresh_token_expired", 400)
        for i in range(2):
            self.issue("signin")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.assertEqual(self.store["credential_pool"]["openai-codex"][1]["last_status"],"dead")
        self.assertEqual(self.store["credential_pool"]["openai-codex"][0],self.other)

    def test_challenge_does_not_quarantine(self):
        self.install_grant_guard()
        self.primary["access_token"] = "old"
        self.network.side_effect = OAuthError("http_error",403)
        self.issue("access_wait")
        self.issue("access_wait")
        self.network.assert_called_once()
        self.assertNotEqual(self.store["credential_pool"]["openai-codex"][1].get("last_status"), "dead")
        self.network.side_effect = self._refresh
        with patch("time.time", return_value=time.time()+65):
            self.assertEqual(adapter.get_credentials()["api_key"], "renewed")

    def group_question(self, reply):
        headers = ModuleType("agent.codex_headers")
        headers.codex_cloudflare_headers = lambda token: {}
        self.modules["httpx"].TimeoutException = type("TimeoutException", (self.modules["httpx"].RequestError,), {})
        def stream(method, url, *, headers, **kwargs):
            token = headers["Authorization"].removeprefix("Bearer ")
            self.assertEqual(url, adapter.ENDPOINT + "/responses")
            if reply(token) == 401:
                response = Mock(status_code=401, iter_bytes=lambda *_: [b'{"error":{"code":"token_expired"}}'])
            else:
                response = Mock(status_code=200, iter_lines=lambda: [
                    'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"42"}]}]}}'])
            return nullcontext(response)
        with patch.dict(sys.modules, {"agent.codex_headers": headers}):
            return adapter._compose(Mock(stream=stream), [adapter._user("6 * 7?")], False, "test", "6 * 7?")

    def test_model_401_before_local_expiry_rotates_once_and_completes_question(self):
        reply = Mock(side_effect=lambda token: 401 if token == "live" else 200)
        self.assertEqual(self.group_question(reply).text, "42")
        self.assertEqual([call.args[0] for call in reply.call_args_list], ["live", "renewed"])
        self.codex.refresh_codex_oauth_pure.assert_called_once_with("live", "first-grant", timeout_seconds=20.0, on_received=ANY)

    def test_rejected_refresh_on_model_401_blocks_later_questions_until_owner_login(self):
        self.codex.refresh_codex_oauth_pure.side_effect = OAuthError("refresh_token_expired", 401)
        reply = Mock(return_value=401)
        for _ in range(2):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual((caught.exception.kind, caught.exception.stage), ("signin", "credentials"))
        reply.assert_called_once_with("live")
        self.codex.refresh_codex_oauth_pure.assert_called_once_with("live", "first-grant", timeout_seconds=20.0, on_received=ANY)
        self.assertEqual(self.store["credential_pool"]["openai-codex"][0], self.other)

    def test_second_model_401_does_not_keep_rotating_a_fresh_grant(self):
        reply = Mock(return_value=401)
        for _ in range(3):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual(caught.exception.kind, "signin")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.assertEqual(self.store["credential_pool"]["openai-codex"][1]["refresh_token"], "rotated")
        self.assertEqual([item.args[0] for item in reply.call_args_list], ["live", "renewed"])
        self.store["credential_pool"]["openai-codex"][1].update(access_token="next-login", refresh_token="new-grant")
        self.assertEqual(self.group_question(lambda token: 200 if token == "next-login" else 401).text, "42")

    def test_late_model_401_does_not_mark_new_login_sharing_only_the_bearer(self):
        def respond(token):
            if token == "renewed":
                self.store["credential_pool"]["openai-codex"][1]["refresh_token"] = "peer-grant"
            return 401
        with self.assertRaises(adapter.ModelIssue):
            self.group_question(respond)
        self.assertEqual(self.group_question(lambda _: 200).text, "42")
        self.codex.refresh_codex_oauth_pure.assert_called_once()

    def test_401_on_peer_with_same_bearer_does_not_quarantine_that_login(self):
        def rotate_grant(_):
            self.store["credential_pool"]["openai-codex"][1]["refresh_token"] = "peer-grant"
            return 401
        with self.assertRaises(adapter.ModelIssue):
            self.group_question(rotate_grant)
        self.assertEqual(self.group_question(lambda _: 200).text, "42")
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_model_401_adopts_a_token_already_rotated_by_peer(self):
        self.on_lock = lambda: self.primary.update(access_token="peer-live", refresh_token="peer-grant")
        reply = Mock(side_effect=lambda token: 401 if token == "live" else 200)
        self.assertEqual(self.group_question(reply).text, "42")
        self.assertEqual([call.args[0] for call in reply.call_args_list], ["live", "peer-live"])
        self.codex.refresh_codex_oauth_pure.assert_not_called()

    def test_model_401_renewal_temporarily_unavailable_then_recovers(self):
        self.codex.refresh_codex_oauth_pure.side_effect = OAuthError("http_error", 503)
        reply = Mock(side_effect=lambda token: 401 if token == "live" else 200)
        for _ in range(2):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual(caught.exception.kind, "access_wait")
        reply.assert_called_once_with("live")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.codex.refresh_codex_oauth_pure.side_effect = self._refresh
        with patch("time.time", return_value=time.time()+65):
            self.assertEqual(self.group_question(reply).text, "42")

    def test_identical_access_token_from_refresh_is_never_replayed(self):
        self.codex.refresh_codex_oauth_pure.side_effect = None
        self.codex.refresh_codex_oauth_pure.return_value = dict(access_token="live", refresh_token="rotated", last_refresh="now")
        reply = Mock(return_value=401)
        for _ in range(3):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual(caught.exception.kind, "signin")
        reply.assert_called_once()
        self.codex.refresh_codex_oauth_pure.assert_called_once()

    def test_image_401_recovers_without_publishing_provider_detail(self):
        import base64
        headers = ModuleType("agent.codex_headers")
        headers.codex_cloudflare_headers = lambda token: {}
        bootstrap = ModuleType("agent.process_bootstrap")
        bootstrap.enable_happy_eyeballs_on_client = lambda client: None
        plugin = Mock()
        plugin._resolve_model.return_value = ("image-fixture", {"quality": "medium"})
        plugin._post_image_request.side_effect = [RuntimeError("Codex images API returned HTTP 401: private fixture"),
                                                 {"data": [{"b64_json": base64.b64encode(b"fake image").decode()}]}]
        modules = {"agent.codex_headers": headers, "agent.process_bootstrap": bootstrap, "plugins.image_gen.openai-codex": plugin}
        with patch.dict(sys.modules, modules):
            turns = [{"role": "user", "content": [{"type": "input_text", "text": "Нарисуй круг"}]}]
            result = adapter._respond(turns, draw=True, model="test")
        self.assertEqual(result.images, [b"fake image"])
        self.assertEqual([call.args[0] for call in plugin._post_image_request.call_args_list], ["live", "renewed"])


if __name__ == "__main__":
    unittest.main()
