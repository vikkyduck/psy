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
from unittest.mock import Mock, patch

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
        stack.enter_context(patch.object(adapter, "_GROUP_AUTH_MARKERS", dirs.name))
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

    def _refresh(self, access, grant):
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
                             "base_url":"https://chatgpt.com/backend-api/codex", "api_key":"live"})
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
        self.primary["access_token"] = "old"
        self.codex.refresh_codex_oauth_pure.side_effect = OAuthError("http_error",403)
        self.issue("access_wait")
        self.issue("access_wait")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.assertNotEqual(self.store["credential_pool"]["openai-codex"][1].get("last_status"), "dead")
        self.codex.refresh_codex_oauth_pure.side_effect = self._refresh
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
        self.codex.refresh_codex_oauth_pure.assert_called_once_with("live", "first-grant")

    def test_rejected_refresh_on_model_401_blocks_later_questions_until_owner_login(self):
        self.codex.refresh_codex_oauth_pure.side_effect = OAuthError("refresh_token_expired", 401)
        reply = Mock(return_value=401)
        for _ in range(2):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual((caught.exception.kind, caught.exception.stage), ("signin", "credentials"))
        reply.assert_called_once_with("live")
        self.codex.refresh_codex_oauth_pure.assert_called_once_with("live", "first-grant")
        self.assertEqual(self.store["credential_pool"]["openai-codex"][0], self.other)

    def test_second_model_401_does_not_keep_rotating_a_fresh_grant(self):
        reply = Mock(return_value=401)
        for _ in range(2):
            with self.assertRaises(adapter.ModelIssue) as caught:
                self.group_question(reply)
            self.assertEqual(caught.exception.kind, "signin")
        self.codex.refresh_codex_oauth_pure.assert_called_once()
        self.assertEqual(self.store["credential_pool"]["openai-codex"][1]["refresh_token"], "rotated")

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
        with self.assertRaises(adapter.ModelIssue) as caught:
            self.group_question(reply)
        self.assertEqual(caught.exception.kind, "signin")
        reply.assert_called_once()

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
