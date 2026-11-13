"""Offline renewal tests. The only credentials, clock and store here are fixtures."""
import copy
import sys
import tempfile
from io import StringIO
import unittest
from contextlib import contextmanager, nullcontext, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway"))
from hermes_cli import psy_codex_policy as policy
import psy_codex_auth as guard


class ProviderError(Exception):
    def __init__(self, code):
        self.code = code
        self.oauth_refresh_status = 400 if code == "invalid_grant" else None
        self.relogin_required = code in {"invalid_grant", "refresh_token_expired"}


class TestCodexMaintenance(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000
        self.expiry = {
            "initial": self.now + guard.RENEW_BEFORE * 5,
            "rotated": self.now + guard.RENEW_BEFORE * 10,
        }
        self.primary = {
            "id": "first", "label": "primary", "source": "manual:device_code",
            "auth_type": "oauth", "priority": 0, "access_token": "initial",
            "refresh_token": "first-grant", "last_refresh": "earlier",
        }
        secondary = dict(self.primary, priority=1, id="second", refresh_token="other-grant")
        self.store = {"credential_pool": {guard.PROVIDER: [secondary, self.primary]}}
        self.rotated = {"access_token": "rotated", "refresh_token": "next-grant", "last_refresh": "later"}
        self.locked = False
        self.peer_rotation = False

        @contextmanager
        def lock(**_kwargs):
            self.assertFalse(self.locked)
            if self.peer_rotation:
                self.primary.update(self.rotated)
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

        def save(store):
            self.assertTrue(self.locked)
            self.store = copy.deepcopy(store)

        def rotate(access, refresh):
            self.assertTrue(self.locked)
            self.assertEqual((access, refresh), ("initial", "first-grant"))
            return self.rotated.copy()

        self.refresh = Mock(side_effect=rotate)
        self.save = Mock(side_effect=save)
        modules = {key: ModuleType(key) for key in (
            "hermes_cli", "hermes_cli.auth", "hermes_cli.auth_constants", "hermes_cli.auth_codex",
        )}
        modules["hermes_cli"].psy_codex_policy = policy
        modules["hermes_cli.auth_codex"]._codex_pool_route_base_url = lambda base: base
        modules["hermes_cli.auth_constants"].httpx = SimpleNamespace(RequestError=ConnectionError)
        modules["hermes_cli.auth"]._auth_store_lock = lock
        modules["hermes_cli.auth"]._load_auth_store = lambda: copy.deepcopy(self.store)
        modules["hermes_cli.auth"]._save_auth_store = self.save
        modules["hermes_cli.auth_codex"].refresh_codex_oauth_pure = self.refresh
        # These fixtures use the numeric 429 form of Hermes' quota classification.
        modules["hermes_cli.auth_codex"]._entry_is_rate_limit_exhausted = lambda row: (
            row.get("last_status") == "exhausted" and row.get("last_error_code") == 429
        )
        modules["hermes_cli.auth_constants"].AuthError = ProviderError
        modules["hermes_cli.auth_constants"]._decode_jwt_claims = lambda token: {"exp": self.expiry[token]}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.available = guard.classify_account(200, {"rate_limit": {"allowed": True}})
        self.rejected = guard.classify_account(401)
        self.probe = Mock(return_value=self.available)
        for replacement in (
            patch.dict(sys.modules, modules),
            patch.object(guard, "time", SimpleNamespace(time=lambda: self.now)),
            patch.object(guard, "REJECTED", Path(directory.name) / "marker"),
            patch.object(guard, "check_provider", self.probe),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def due(self):
        self.expiry["initial"] = self.now + guard.RENEW_BEFORE // 2

    def test_fresh_login_is_checked_without_refresh(self):
        guard.maintain()
        self.probe.assert_called_once_with("initial")
        self.refresh.assert_not_called()
        self.save.assert_not_called()

    def test_early_refresh_preserves_cooldown_identity_and_other_rows(self):
        self.due()
        self.primary.update(last_status="exhausted", last_error_code=429,
                            last_error_reset_at=self.now + 3 * guard.RENEW_BEFORE)
        expected = copy.deepcopy(self.store)
        expected["credential_pool"][guard.PROVIDER][1].update(self.rotated)
        guard.maintain()
        self.assertEqual(self.store, expected)
        self.refresh.assert_called_once()
        self.probe.assert_called_once_with("rotated")

    def test_provider_rejection_retains_row_and_stops_replay(self):
        self.due()
        self.refresh.side_effect = ProviderError("invalid_grant")
        before = copy.deepcopy(self.store)
        for _ in range(2):
            with self.assertRaisesRegex(guard.CheckError, "owner login required"):
                guard.maintain()
        self.refresh.assert_called_once()
        self.save.assert_called_once()
        rows = self.store["credential_pool"][guard.PROVIDER]
        self.assertEqual(rows[0], before["credential_pool"][guard.PROVIDER][0])
        self.assertEqual(rows[1]["last_status"], "dead")

    def test_transient_failure_preserves_grant_for_next_run(self):
        self.due()
        self.refresh.side_effect = ProviderError("codex_refresh_failed")
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(guard.CheckError, "retry next check"):
            guard.maintain()
        self.assertEqual(self.store, before)
        self.save.assert_not_called()
        self.assertFalse(guard.REJECTED.exists())
        self.refresh.side_effect = None
        self.refresh.return_value = self.rotated.copy()
        guard.maintain()
        self.assertEqual(self.refresh.call_count, 2)
        self.probe.assert_called_once_with("rotated")

    def test_renewal_log_allows_only_status_and_known_class(self):
        self.due()
        error = ProviderError("untrusted remote text")
        error.oauth_refresh_status = 503
        error.oauth_refresh_category = "untrusted remote text"
        self.refresh.side_effect = error
        with self.assertRaises(guard.CheckError) as caught:
            guard.maintain()
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertNotIn("untrusted", str(caught.exception))
        error.oauth_refresh_category = "http_error"
        self.assertEqual(guard.renewal_detail(error), " (HTTP 503, http_error)")

    def test_scheduled_renewal_updates_exact_alias_but_not_independent_login(self):
        self.store["credential_pool"][guard.PROVIDER].append(dict(self.primary, id="legacy", priority=2))
        self.due()
        guard.maintain()
        rows = self.store["credential_pool"][guard.PROVIDER]
        self.assertEqual(rows[0]["refresh_token"], "other-grant")
        self.assertEqual([r["refresh_token"] for r in rows[1:]], ["next-grant", "next-grant"])
        self.refresh.assert_called_once()
        self.save.assert_called_once()

    def test_monitor_rejects_ambiguous_priorities_and_overridden_route(self):
        self.store["credential_pool"][guard.PROVIDER][0]["priority"] = 0
        with self.assertRaisesRegex(guard.CheckError, "ambiguous"):
            guard.maintain()
        self.store["credential_pool"][guard.PROVIDER][0]["priority"] = 1
        with patch.object(sys.modules["hermes_cli.auth_codex"], "_codex_pool_route_base_url", return_value="http://example.invalid"):
            with self.assertRaisesRegex(guard.CheckError, "owner login required"):
                guard.maintain()
        self.probe.assert_not_called()
        self.refresh.assert_not_called()

    def test_server_error_cannot_quarantine_a_renewable_grant(self):
        self.due()
        error = ProviderError("refresh_token_expired")
        error.oauth_refresh_status = 503
        self.refresh.side_effect = error
        with self.assertRaisesRegex(guard.CheckError, "retry next check"):
            guard.maintain()
        self.assertFalse(guard.REJECTED.exists())

    def test_peer_rotation_before_lock_is_reused_after_401(self):
        self.probe.side_effect = [self.rejected, self.available]
        self.peer_rotation = True
        guard.maintain()
        self.refresh.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual([c.args[0] for c in self.probe.call_args_list], ["initial", "rotated"])

    def test_peer_replaced_only_refresh_token(self):
        self.probe.side_effect = [self.rejected, self.available]
        self.peer_rotation = True
        self.rotated["access_token"] = "initial"
        guard.maintain()
        self.refresh.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual([c.args[0] for c in self.probe.call_args_list], ["initial", "initial"])
        self.assertEqual(self.primary["refresh_token"], "next-grant")

    def test_quota_row_in_gateways_refresh_window_defers_to_gateway(self):
        self.primary.update(last_status="exhausted", last_error_code=429)
        for seconds_left in (-1, 0, 119, 120):
            with self.subTest(seconds_left=seconds_left):
                self.expiry["initial"] = self.now + seconds_left
                with self.assertRaisesRegex(guard.CheckError, "expired in cooldown"):
                    guard.maintain()
        self.refresh.assert_not_called()
        self.save.assert_not_called()

    def test_quota_row_before_gateways_refresh_window_can_renew(self):
        self.primary.update(last_status="exhausted", last_error_code=429)
        self.expiry["initial"] = self.now + 121
        guard.maintain()
        self.refresh.assert_called_once()
        self.save.assert_called_once()

    def test_expired_generic_exhaustion_recovers_after_downtime(self):
        self.primary.update(last_status="exhausted", last_error_code=None, last_error_reason=None,
                            last_status_at=self.now - 2 * 86400, last_error_reset_at=self.now - 86400)
        self.expiry["initial"] = self.now - 86400
        expected = copy.deepcopy(self.store)
        expected["credential_pool"][guard.PROVIDER][1].update(self.rotated)
        guard.maintain()
        guard.maintain()
        self.refresh.assert_called_once()
        self.save.assert_called_once()
        self.assertEqual(self.store, expected)

    def test_persistent_account_401_does_not_rotate_hourly(self):
        self.probe.side_effect = [self.rejected, self.rejected, self.rejected, self.available, self.rejected, self.available]
        with self.assertRaises(guard.CheckError):
            guard.maintain()
        self.refresh.assert_called_once()
        with self.assertRaises(guard.CheckError):
            guard.maintain()
        self.refresh.assert_called_once()
        guard.maintain()  # Endpoint recovers for the very same bearer.
        self.refresh.side_effect = None
        self.refresh.return_value = self.rotated.copy()
        guard.maintain()  # A new 401 after recovery gets its own renewal attempt.
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.probe.call_count, 6)

    def test_persistent_account_401_allows_scheduled_renewal(self):
        self.probe.side_effect = [self.rejected, self.rejected, self.available]
        with self.assertRaises(guard.CheckError):
            guard.maintain()
        self.expiry["rotated"] = self.now + guard.RENEW_BEFORE // 2
        self.expiry["scheduled"] = self.now + 10 * guard.RENEW_BEFORE
        self.refresh.side_effect = None
        self.refresh.return_value = dict(self.rotated, access_token="scheduled")
        guard.maintain()
        self.assertEqual(self.refresh.call_count, 2)
        self.assertEqual(self.store["credential_pool"][guard.PROVIDER][1]["access_token"], "scheduled")


    def test_quota_signals_do_not_rotate_or_write(self):
        cases = (
            ({"rate_limit": {"allowed": False}, "credits": {"has_credits": True}}, "included_limit_credits_reported", False),
            ({"rate_limit": {"allowed": False}, "credits": {"unlimited": True}}, "included_limit_credits_reported", False),
            ({"rate_limit": {"allowed": False}, "credits": {"has_credits": False}}, "included_limit_reached", False),
            ({"rate_limit": {"primary_window": {"used_percent": 100}}}, "included_limit_reached", False),
            ({"rate_limit": {"secondary_window": {"used_percent": 100}}}, "included_limit_reached", False),
            ({"rate_limit": {"limit_reached": True, "allowed": True}}, "included_limit_reached", False),
            ({"rate_limit": {"allowed": False}}, "included_limit_reached", False),
            ({"rate_limit": {"primary_window": {"used_percent": 99.5}}}, "available", False),
            ({"plan_type": "test"}, "unknown", False),
            ({"rate_limit": {}}, "unknown", False),
            ({"rate_limit": {"primary_window": {"used_percent": False}}}, "unknown", False),
            ({"rate_limit": {"primary_window": {"used_percent": 20}, "secondary_window": {}}}, "unknown", False),
            ({"rate_limit": {"allowed": True}, "additional_rate_limits": [
                {"limit_name": "untrusted model name", "rate_limit": {"allowed": False}}]}, "available", True),
        )
        before = copy.deepcopy(self.store)
        for body, quota, scoped in cases:
            with self.subTest(body=body):
                self.probe.return_value = guard.classify_account(200, body)
                output = StringIO()
                with redirect_stdout(output):
                    guard.maintain()
                mode = "ok" if quota in ("available", "included_limit_credits_reported") and not scoped else "degraded"
                self.assertIn(f"state={mode} auth=valid quota={quota} ", output.getvalue())
                self.assertEqual("scoped_quota=limited" in output.getvalue(), scoped)
                self.assertNotIn("untrusted", output.getvalue())
        self.assertEqual(self.store, before)
        self.refresh.assert_not_called()
        self.save.assert_not_called()
        self.assertFalse(guard.REJECTED.exists())

    def test_transient_and_unrecognized_response_keep_quota_unknown(self):
        for status, body in [(None, None), (429, None), (503, None), (403, {}),
                             (200, None), (200, {"error": "untrusted remote text"})]:
            with self.subTest(status=status, body=body):
                self.probe.return_value = guard.classify_account(status, body)
                with self.assertRaisesRegex(guard.CheckError, "state=degraded auth=unknown quota=unknown"):
                    guard.maintain()
        self.refresh.assert_not_called()
        self.save.assert_not_called()

    def test_renewal_due_and_quota_limited_does_not_refresh_again(self):
        self.due()
        self.probe.return_value = guard.classify_account(200, {"rate_limit": {"allowed": False}})
        with redirect_stdout(StringIO()) as report:
            guard.maintain()
            guard.maintain()
        self.assertEqual(report.getvalue().count("state=degraded auth=valid quota=included_limit_reached"), 2)
        self.refresh.assert_called_once()
        self.save.assert_called_once()


class TestAccountEndpoint(unittest.TestCase):
    def test_only_usage_get_runs_and_network_errors_are_unknown(self):
        headers = ModuleType("agent.codex_headers")
        headers.codex_account_headers = lambda token: {}
        codex = ModuleType("hermes_cli.auth_codex")
        constants = ModuleType("hermes_cli.auth_constants")
        constants.httpx = SimpleNamespace(RequestError=ConnectionError)
        client = Mock()
        codex._codex_http_client = lambda **kwargs: nullcontext(client)
        with patch.dict(sys.modules, {"agent.codex_headers": headers, "hermes_cli.auth_codex": codex,
                                     "hermes_cli.auth_constants": constants}):
            client.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"rate_limit": {"allowed": False}})
            self.assertEqual(guard.check_provider("fixture"), guard.AccountCheck(200, "valid", "included_limit_reached"))
            client.get.side_effect = ConnectionError("untrusted detail")
            self.assertEqual(guard.check_provider("fixture"), guard.AccountCheck(None, "unknown", "unknown"))
            self.assertEqual([entry.args[0] for entry in client.get.call_args_list], [guard.USAGE, guard.USAGE])
            client.post.assert_not_called()
