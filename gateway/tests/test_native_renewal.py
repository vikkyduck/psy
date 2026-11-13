"""Native Hermes boundaries for the PSY-owned grant. All data and endpoints are local fixtures."""
import copy
from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from hermes_cli import psy_codex_policy as policy


class NativeRenewal(unittest.TestCase):
    def setUp(self):
        self.auth = ModuleType("hermes_cli.auth")
        self.auth._nonempty_str = lambda value: isinstance(value, str) and bool(value.strip())
        self.auth._utc_now_z = lambda: "fixture"
        self.auth._POOL_STATUS_FIELDS = ("last_status", "last_status_at", "last_error_code", "last_error_reset_at")
        utils = ModuleType("utils")
        utils.env_float = lambda name, default: default
        utils.normalize_proxy_env_vars = lambda: None
        self.pool = ModuleType("agent.credential_pool")
        mocks = patch.dict(sys.modules, {
            "utils": utils, "hermes_cli.auth": self.auth, "agent.credential_pool": self.pool})
        mocks.start()
        self.addCleanup(mocks.stop)
        source = Path(policy.__file__).with_name("auth_codex.py")
        spec = importlib.util.spec_from_file_location("_psy_native_provider_fixture", source)
        self.provider = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.provider)
        self.primary = dict(id="main", source="manual:device_code", priority=0, auth_type="oauth",
                            access_token="shared-access", refresh_token="managed-grant")
        self.alias = dict(self.primary, id="alias", priority=1, source="device_code")
        self.independent = dict(self.primary, id="other", priority=2, refresh_token="other-grant")
        self.store = {"credential_pool": {policy.PROVIDER: [self.primary, self.alias, self.independent]}}
        self.fresh = dict(access_token="next-access", refresh_token="next-grant", last_refresh="later")

    def test_independent_singleton_with_same_access_cannot_overwrite_managed_rows(self):
        before = copy.deepcopy([self.primary, self.alias])
        self.provider._sync_codex_pool_entries(
            self.store, self.fresh, "later", previous_singleton_tokens=dict(self.independent))
        self.assertEqual([self.primary, self.alias], before)
        self.assertEqual(self.independent["refresh_token"], self.fresh["refresh_token"])

    def test_singleton_refusal_never_reaches_legacy_exchange_or_cli_import(self):
        # An independent singleton was installed between observation and the refresh lock.
        self.auth._provider_state_transaction = lambda *args: nullcontext(
            (self.store, {"tokens": self.independent}, None))
        self.auth._save_codex_tokens = Mock()
        self.auth.refresh_codex_oauth_pure = Mock()
        with patch.object(policy, "renew_native", side_effect=policy.CredentialIssue("access_wait", "denied")), patch.object(
                self.provider, "_recover_codex_tokens_from_cli") as recover:
            with self.assertRaises(self.provider.AuthError) as error:
                self.provider._refresh_codex_auth_tokens(self.primary, 7)
        self.assertEqual(error.exception.code, "psy_access_wait")
        self.auth._save_codex_tokens.assert_not_called()
        self.auth.refresh_codex_oauth_pure.assert_not_called()
        recover.assert_not_called()

    def test_independent_singleton_still_adopts_its_peer_rotation(self):
        self.auth._provider_state_transaction = lambda *args: nullcontext(
            (self.store, {"tokens": self.fresh}, None))
        self.auth._save_codex_tokens = Mock()
        self.auth.refresh_codex_oauth_pure = Mock()
        with patch.object(policy, "renew_native", return_value=None):
            updated = self.provider._refresh_codex_auth_tokens(self.independent, 7)
        self.assertEqual(updated["refresh_token"], self.fresh["refresh_token"])
        self.auth._save_codex_tokens.assert_not_called()
        self.auth.refresh_codex_oauth_pure.assert_not_called()

    def test_failed_authority_commit_cannot_fall_through_to_second_exchanger(self):
        self.pool.load_pool = Mock()
        self.auth.read_credential_pool = Mock()
        with patch.object(self.provider, "_codex_pool_owner_lock", side_effect=lambda: nullcontext()), patch.object(
                policy, "renew_native", side_effect=OSError("write failed")):
            with self.assertRaises(self.provider.AuthError) as error:
                self.provider._refresh_pool_codex_row(self.primary, force_refresh=True, skew=300)
        self.assertTrue(error.exception.retryable)
        self.pool.load_pool.assert_not_called()

    def test_runtime_resolver_adopts_managed_pair_without_second_pool_refresh(self):
        self.pool.load_pool = Mock()
        self.auth.read_credential_pool = Mock()
        current = dict(self.primary, **self.fresh)
        with patch.object(self.provider, "_codex_pool_owner_lock", side_effect=lambda: nullcontext()), patch.object(
                policy, "renew_native", return_value=(current, 999, False)) as authority:
            pair = self.provider._refresh_pool_codex_row(self.primary, force_refresh=True, skew=300)
        self.assertEqual(pair[0], "next-access")
        self.assertEqual(authority.call_count, 1)
        self.pool.load_pool.assert_not_called()

    def test_quota_probe_commits_only_quota_evidence_after_renewal(self):
        self.primary.update(last_status="exhausted", last_error_code=429, last_status_at=1,
                            last_error_reset_at=100)
        def rotate(observed, *, quota_probe, timeout_seconds):
            self.assertTrue(quota_probe)
            rows = self.store["credential_pool"][policy.PROVIDER]
            for row in rows[:2]:
                row.update(self.fresh)
            self.store["concurrent_change"] = "retain"
            return copy.deepcopy(rows[0]), 999, True
        def save(store, *, target_path):
            self.store = copy.deepcopy(store)
        self.auth._load_auth_store = lambda target: copy.deepcopy(self.store)
        self.auth._save_auth_store = Mock(side_effect=save)
        with patch.object(self.provider, "_codex_pool_owner_lock", side_effect=lambda: nullcontext()), patch.object(
                self.provider, "_codex_access_token_is_expiring", return_value=True), patch.object(
                self.provider, "_codex_pool_route_base_url", return_value=policy.BASE), patch.object(
                self.provider, "_probe_codex_quota_restored", return_value=True) as usage, patch.object(
                policy, "renew_native", side_effect=rotate), patch.object(
                self.provider, "_refresh_expired_codex_probe_token") as legacy:
            self.assertTrue(self.provider._probe_codex_pool_entry_quota_restored(self.primary))
        legacy.assert_not_called()
        usage.assert_called_once_with("next-access", base_url=policy.BASE)
        self.auth._save_auth_store.assert_called_once()
        self.assertEqual(self.store["concurrent_change"], "retain")
        rows = self.store["credential_pool"][policy.PROVIDER]
        self.assertEqual([r["refresh_token"] for r in rows], ["next-grant", "next-grant", "other-grant"])
        self.assertIsNone(rows[0]["last_status"])
