"""Exercise three production callers against a private store and a synthetic OAuth endpoint."""
import argparse
import base64
from contextlib import ExitStack
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx


def test_token(expiry, account):
    claims = {"exp": expiry, "sub": account, "https://api.openai.com/auth": {"chatgpt_account_id": account}}

    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return ".".join((encode({"alg": "none"}), encode(claims), "fixture"))


def deny_network(*args, **kwargs):
    raise AssertionError("Live network access in the offline OAuth regression")


class Renewal(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home), "HERMES_HOME": str(self.home)}))
        self.assertEqual(auth._auth_file_path().parent.resolve(), self.home)
        global_store = auth._global_auth_file_path()
        self.assertTrue(global_store is None or global_store.resolve().is_relative_to(self.home))
        stack.enter_context(patch.object(policy, "MARKERS", self.home))
        self.now = int(time.time())
        self.network_down = False
        self.new_access = test_token(self.now + 172800, "primary")
        self.new_grant = "fixture-renewed-grant"
        self.oauth_posts = 0
        self.model_posts = 0
        self.transport = httpx.MockTransport(self.endpoint)
        stack.enter_context(patch.object(provider, "_codex_http_client",
                                         side_effect=lambda **_: httpx.Client(transport=self.transport)))

    def seed(self, remaining_seconds=7200):
        self.old_access = test_token(self.now + remaining_seconds, "primary")
        row = dict(id="primary", label="primary", priority=0, auth_type="oauth", source="manual:device_code",
                   access_token=self.old_access, refresh_token="fixture-old-grant")
        alias = dict(row, id="alias", label="alias", priority=1)
        self.other = dict(row, id="other", label="other", priority=2,
                          access_token=test_token(self.now + 172800, "other"), refresh_token="fixture-other-grant")
        auth._save_auth_store({"credential_pool": {"openai-codex": [row, alias, self.other]}})

    def endpoint(self, request):
        if request.method == "POST" and str(request.url) == provider.CODEX_OAUTH_TOKEN_URL:
            self.oauth_posts += 1
            if self.network_down:
                raise httpx.ConnectError("Fixture connection failure before dispatch", request=request)
            return httpx.Response(200, json={"access_token": self.new_access, "refresh_token": self.new_grant})
        if request.method == "GET" and str(request.url) == monitor.USAGE:
            return httpx.Response(200, json={"rate_limit": {"allowed": True}})
        if request.method == "POST" and str(request.url) == group.ENDPOINT + "/responses":
            self.model_posts += 1
            if request.headers["Authorization"] == "Bearer " + self.old_access:
                return httpx.Response(401, json={"error": {"code": "token_expired"}})
            self.assertEqual(request.headers["Authorization"], "Bearer " + self.new_access)
            event = {"type": "response.completed", "response": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "42"}]}]}}
            return httpx.Response(200, text="data: " + json.dumps(event) + "\n")
        raise AssertionError(f"Unexpected fixture request: {request.method} {request.url.path}")

    def question(self):
        with httpx.Client(transport=self.transport) as client:
            question = "6 * 7?"
            return group._compose(client, [group._user(question)], False, "fixture", question).text

    def native_refresh(self):
        return pools.load_pool("openai-codex").try_refresh_matching(credential_id="primary")

    def assert_rotated(self):
        primary, alias, independent = auth._load_auth_store()["credential_pool"]["openai-codex"]
        for row in (primary, alias):
            self.assertEqual((row["access_token"], row["refresh_token"]), (self.new_access, self.new_grant))
        self.assertEqual(independent, self.other)

    def test_shared_backoff_and_recovery(self):
        self.seed()
        self.network_down = True
        with patch("time.time", return_value=self.now):
            for _ in range(3):
                with self.assertRaises(group.ModelIssue) as failure:
                    self.question()
                self.assertEqual(failure.exception.kind, "access_wait")
            with self.assertRaises(monitor.CheckError):
                monitor.maintain(force=True)
            self.assertIsNone(self.native_refresh())
            self.assertEqual((self.oauth_posts, self.model_posts), (1, 1))
        self.network_down = False
        with patch("time.time", return_value=self.now + 65):
            self.assertEqual(self.question(), "42")
        self.assertEqual(self.oauth_posts, 2)
        self.assert_rotated()

    def test_group_rotates_aliases(self):
        self.seed()
        self.assertEqual(self.question(), "42")
        self.assertEqual(self.oauth_posts, 1)
        self.assert_rotated()

    def test_monitor_rotates_aliases(self):
        self.seed()
        monitor.maintain(force=True)
        self.assertEqual(self.oauth_posts, 1)
        self.assert_rotated()

    def test_hermes_rotates_aliases(self):
        self.seed()
        self.assertIsNotNone(self.native_refresh())
        self.assertEqual(self.oauth_posts, 1)
        self.assert_rotated()

    def test_three_processes_rotate_once(self):
        self.seed(remaining_seconds=-60)
        ctx = multiprocessing.get_context("fork")
        start = ctx.Barrier(3)
        results = ctx.Queue()
        self.addCleanup(results.close)

        def compete(role):
            start.wait(timeout=10)
            if role == "group":
                self.assertEqual(self.question(), "42")
            elif role == "monitor":
                monitor.maintain()
            else:
                self.assertEqual(provider.resolve_codex_runtime_credentials()["api_key"], self.new_access)
            results.put(self.oauth_posts)

        children = [ctx.Process(target=compete, args=(role,)) for role in ("group", "monitor", "hermes")]
        try:
            for child in children:
                child.start()
            for child in children:
                child.join(15)
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(sum(results.get(timeout=2) for _ in children), 1)
            self.assert_rotated()
        finally:
            for child in children:
                if child.is_alive():
                    child.kill()
                    child.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="Hermes checkout restored with gateway/apply.py")
    checkout = parser.parse_args().checkout.resolve()
    sys.path.insert(0, str(checkout))
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory() as init_home, ExitStack() as offline:
        offline.enter_context(patch.dict(os.environ, {
            "HOME": init_home, "HERMES_HOME": init_home, "HERMES_DISABLE_LAZY_INSTALLS": "1"}))
        for operation in ("connect", "connect_ex"):
            offline.enter_context(patch.object(socket.socket, operation, deny_network))
        from agent import credential_pool as pools
        from hermes_cli import auth, auth_codex as provider, psy_codex_policy as policy

        # Import the Telegram plugin without starting the gateway or its polling loop.
        sys.path.insert(0, str(checkout / "plugins/platforms/telegram"))
        import psy_group_codex as group
        monitor_path = Path(__file__).resolve().parents[2] / "ops/libexec/psy_codex_auth.py"
        spec = importlib.util.spec_from_file_location("psy_codex_auth", monitor_path)
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        unittest.main(argv=[sys.argv[0]])
