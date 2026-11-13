"""Run the private Telegram owner flow against local fake endpoints and a private incident file."""
import asyncio
from contextlib import ExitStack, nullcontext
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_cli import psy_codex_incident as incident, psy_codex_policy as policy
from psy_modules import psy_group_route as route, psy_owner_recovery as recovery


class NetworkError(Exception):
    pass


class RetryAfter(Exception):
    def __init__(self, delay):
        self.retry_after = delay


class OwnerRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(policy, "MARKERS", self.home))
        self.row = dict(id="selected", access_token="synthetic-bearer", refresh_token="synthetic-grant")
        auth = ModuleType("hermes_cli.auth")
        auth._auth_store_lock = lambda **_: nullcontext()
        auth._load_auth_store = lambda *args: {}
        stack.enter_context(patch.object(policy, "preferred", side_effect=lambda store, **_: (self.row, 999)))
        stack.enter_context(patch.object(policy, "credential_snapshot", side_effect=lambda: (self.row, 999)))
        telegram = ModuleType("telegram")
        telegram.InlineKeyboardMarkup = lambda keyboard: keyboard
        telegram.InlineKeyboardButton = lambda text, callback_data: (text, callback_data)
        errors = ModuleType("telegram.error")
        errors.Forbidden = type("Forbidden", (Exception,), {})
        errors.NetworkError = NetworkError
        errors.RetryAfter = RetryAfter
        stack.enter_context(patch.dict(sys.modules, {"hermes_cli.auth": auth, "telegram": telegram,
                                                      "telegram.error": errors}))
        self.app = SimpleNamespace(adapter=SimpleNamespace(_psy_owner_recovery_wake=asyncio.Event()),
                                   bot=SimpleNamespace(id=route.BOT, username="TestBot", send_message=AsyncMock()))
        self.generation = policy.generation(self.row)
        self.incident = incident.report(self.row, "revoked")
        self.bot = self.app.bot
        self.device = dict(device_auth_id="synthetic-id", user_code="TEST-42", interval=3)
        self.approval = {"access_token": "fresh-synthetic", "refresh_token": "another-synthetic"}
        self.start_device = stack.enter_context(patch.object(recovery, "request_device", return_value=self.device))
        self.poll = stack.enter_context(patch.object(recovery, "poll_device", new=AsyncMock(return_value=self.approval)))

        def install(generation, pair):
            self.assertEqual(generation, self.generation)
            self.assertTrue(pair["last_refresh"])
            self.row.update({k: pair[k] for k in ("refresh_token", "access_token")})
            return policy.generation(self.row)
        self.install = stack.enter_context(patch.object(policy, "install_owner_login", side_effect=install))
        self.probe = stack.enter_context(patch.object(recovery, "probe_model", return_value=True))
        queue = ModuleType("psy_modules.psy_group_queue")
        self.resume = queue.resume_authorized = AsyncMock()
        stack.enter_context(patch.dict(sys.modules, {"psy_modules.psy_group_queue": queue}))

    def event(self, *, chat=None, user=None, data=None, text="/codex_login"):
        chat = route.OWNER if chat is None else chat
        user = route.OWNER if user is None else user
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat, type="private" if chat > 0 else "supergroup"),
                               effective_user=SimpleNamespace(id=user, is_bot=False),
                               message=SimpleNamespace(text=text) if data is None else None,
                               callback_query=SimpleNamespace(data=data, answer=AsyncMock()) if data is not None else None)

    async def test_single_alert_private_only_login_then_model_validation_wakes_queue(self):
        for _ in range(3):
            await recovery.tick(self.app)
            incident.report(self.row, "revoked")
        self.bot.send_message.assert_awaited_once()
        kwargs = self.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], route.OWNER)
        self.assertNotIn("TEST-42", kwargs["text"])
        callback = kwargs["reply_markup"][0][0][1]
        for update in (self.event(chat=7, user=7), self.event(chat=route.GROUP),
                       self.event(chat=route.GROUP, data=callback), self.event(chat=7, user=7, data=callback),
                       self.event(chat=route.OWNER, user=7, data=callback), self.event(data=callback+"bad")):
            self.assertTrue(await recovery.handle(self.app, update))
        self.start_device.assert_not_called()
        self.probe.assert_not_called()
        await recovery.handle(self.app, self.event(data=callback))
        await self.app.adapter._psy_owner_login_task
        self.start_device.assert_called_once()
        self.install.assert_called_once()
        self.assertIn("TEST-42", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], route.OWNER)
        self.probe.return_value = False
        await recovery.tick(self.app)
        self.resume.assert_not_awaited()
        self.assertEqual(incident.snapshot()["id"], self.incident)
        self.app.adapter._psy_owner_probe = None
        self.probe.return_value = True
        await recovery.tick(self.app)
        await recovery.tick(self.app)
        self.resume.assert_awaited_once_with(self.app, self.incident)
        self.assertIsNone(incident.snapshot())

    async def test_telegram_rate_limit_retries_and_unknown_receipt_does_not_duplicate(self):
        self.bot.send_message.side_effect = RetryAfter(47)
        now = time.time()
        with patch("time.time", return_value=now):
            await recovery.tick(self.app)
            await recovery.tick(self.app)
        self.assertEqual(self.bot.send_message.await_count, 1)
        with patch("time.time", return_value=now+50):
            self.bot.send_message.side_effect = NetworkError()
            # This branch only inspects transport cause. None = an ambiguous outcome.
            httpx = SimpleNamespace(ConnectError=ConnectionError, ConnectTimeout=TimeoutError, PoolTimeout=TimeoutError)
            with patch.dict(sys.modules, {"httpx": httpx}):
                await recovery.tick(self.app)
            await recovery.tick(self.app)
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(incident.snapshot()["notice"], "dispatched")

    async def test_indeterminate_device_exchange_or_expiry_never_rotates_unprompted(self):
        for phase, expires in (("exchanging", time.time()+30), ("polling", time.time()-1)):
            incident.update_device(self.incident, dict(self.device, phase=phase, expires=expires))
            await recovery.tick(self.app)
            await self.app.adapter._psy_owner_login_task
            self.assertNotIn("device", incident.snapshot())
        self.start_device.assert_not_called()
        self.poll.assert_not_awaited()
        self.install.assert_not_called()
        self.assertTrue(await recovery.handle(self.app, self.event()))
        await self.app.adapter._psy_owner_login_task
        self.install.assert_called_once()

    async def test_restart_resumes_one_private_device_session(self):
        current = dict(self.device, phase="polling", expires=time.time()+60)
        incident.update_device(self.incident, current)
        root = incident.directory()
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((root / "incident.json").stat().st_mode), 0o600)
        await asyncio.gather(recovery.tick(self.app), recovery.tick(self.app))
        await self.app.adapter._psy_owner_login_task
        self.start_device.assert_not_called()
        self.poll.assert_awaited_once_with(current)
        self.install.assert_called_once()

    async def test_old_report_and_late_model_proof_cannot_close_new_incident(self):
        stale = dict(self.row)
        self.row.update(self.approval)
        replacement = incident.report(self.row, "model_rejected")
        self.assertIsNone(incident.report(stale, "revoked"))
        self.assertEqual(incident.snapshot()["id"], replacement)
        self.row = dict(self.row, access_token="third")
        def change_while_probing():
            self.row = dict(self.row, access_token="fourth")
            return True
        self.probe.side_effect = change_while_probing
        await recovery.tick(self.app)
        self.resume.assert_not_awaited()
        self.assertEqual(incident.snapshot()["id"], replacement)


class DeviceProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_lost_approval_exchange_is_fenced_before_single_post(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        import time

        device = {"device_auth_id":"synthetic", "user_code":"TEST-42", "phase":"polling",
                  "interval":0, "expires":time.time()+30}
        current = {"id":"case", "generation":"before", "device":device}
        def advance(ident, new, *, expected):
            self.assertEqual(ident, "case")
            self.assertEqual(current["device"], expected)
            current["device"] = new
            return True
        native = ModuleType("hermes_cli.auth_codex")
        native.CODEX_OAUTH_CLIENT_ID = "client"
        native.CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
        def post(url, *, data, follow_redirects):
            self.assertEqual(current["device"]["phase"], "exchanging")
            self.assertEqual(url, native.CODEX_OAUTH_TOKEN_URL)
            self.assertEqual(data["redirect_uri"], "https://auth.openai.com/deviceauth/callback")
            self.assertEqual(data["code_verifier"], "verifier")
            self.assertFalse(follow_redirects)
            raise TimeoutError("fixture lost response")
        client = SimpleNamespace(post=Mock(side_effect=post))
        native._codex_http_client = lambda **_: nullcontext(client)
        httpx = SimpleNamespace(RequestError=ConnectionError)
        with patch.dict(sys.modules, {"hermes_cli.auth_codex":native,"httpx":httpx}), patch.object(
                incident, "snapshot", return_value=current), patch.object(incident, "update_device", side_effect=advance), patch.object(
                recovery, "selected_generation", return_value="before"), patch.object(
                recovery, "_poll_once", return_value={"authorization_code":"approved", "code_verifier":"verifier"}):
            with self.assertRaises(TimeoutError):
                await recovery.poll_device(device)
            self.assertIsNone(await recovery.poll_device(device))
        self.assertEqual(client.post.call_count, 1)
