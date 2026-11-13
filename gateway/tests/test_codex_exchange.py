"""Exercise the restored Hermes wire entry point across a lost rotation response."""
import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from hermes_cli import psy_codex_policy as policy


class HermesWire(unittest.TestCase):
    def test_read_timeout_blocks_the_original_grant_and_new_login_still_rotates(self):
        # Load the actual pinned provider. Only the HTTP client and supporting services
        # are fixtures; this test never imports a Telegram bot, contacts an OAuth service,
        # or reads an existing credential file.
        path = Path(policy.__file__).with_name("auth_codex.py")
        spec = importlib.util.spec_from_file_location("_pinned_psy_auth_codex_test", path)
        provider = importlib.util.module_from_spec(spec)
        utils = ModuleType("utils")
        utils.env_float = lambda name, default: default
        utils.normalize_proxy_env_vars = lambda: None
        auth = ModuleType("hermes_cli.auth")
        auth._nonempty_str = lambda value: isinstance(value, str) and bool(value.strip())
        auth._utc_now_z = lambda: "test-now"
        auth._global_auth_file_path = lambda: None
        auth._auth_store_lock = lambda **kwargs: nullcontext()
        auth._save_private_json = lambda path, data, **kwargs: path.write_text(json.dumps(data))
        pool = ModuleType("agent.credential_pool")
        pool._profile_owns_pool_provider = lambda _: True
        class RequestError(Exception):
            pass
        class Timeout(RequestError):
            pass
        class ReadTimeout(Timeout):
            pass
        fake_http = SimpleNamespace(RequestError=RequestError, TimeoutException=Timeout,
                                    ConnectError=type("ConnectError", (RequestError,), {}),
                                    PoolTimeout=type("PoolTimeout", (Timeout,), {}),
                                    ConnectTimeout=type("ConnectTimeout", (Timeout,), {}), Timeout=lambda duration: duration)
        client = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                "utils": utils, "hermes_cli.auth": auth, "agent.credential_pool": pool}):
            auth._auth_file_path = lambda: Path(tmp) / "auth.json"
            spec.loader.exec_module(provider)
            from hermes_cli import auth_constants
            with patch.object(provider, "_codex_http_client", return_value=nullcontext(client)), patch.object(
                    provider, "httpx", fake_http), patch.object(auth_constants, "httpx", fake_http):
                client.post.side_effect = ReadTimeout()
                for _ in range(3):
                    with self.assertRaises(auth_constants.AuthError) as issue:
                        provider.refresh_codex_oauth_pure("old-bearer", "old-grant")
                    self.assertEqual(issue.exception.code, "psy_refresh_uncertain")
                client.post.assert_called_once()
                client.post.side_effect = None
                client.post.return_value = SimpleNamespace(
                    status_code=200, json=lambda: {"access_token": "new", "refresh_token": "newer"})
                fresh = provider.refresh_codex_oauth_pure("other-bearer", "new-login")
                self.assertEqual(fresh["refresh_token"], "newer")
                with self.assertRaises(auth_constants.AuthError):
                    provider.refresh_codex_oauth_pure("other-bearer", "new-login")
                self.assertEqual(client.post.call_count, 2)
                for entry in (Path(tmp) / "psy-codex-renewals").glob("*.json"):
                    self.assertNotIn("grant", entry.read_text())
