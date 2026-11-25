"""Offline acceptance: lose Codex access after chapter three and resume the original group job."""
import argparse
import asyncio
import base64
from collections import Counter
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx


OWNER = 101
GROUP = -202
BOT = 303
MEMBER = 7
UPDATE = 31
MESSAGE = 100


def synthetic_token(expires):
    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims = {"exp": expires, "sub": "test-member", "https://api.openai.com/auth": {
        "chatgpt_account_id": "test-subscription"}}
    return ".".join((encode({"alg": "none"}), encode(claims), "fixture"))


def forbid_network(*args, **kwargs):
    raise AssertionError("A live connection was attempted during offline acceptance")


def telegram_types():
    """The restore runner only needs the constructors used by this text-only route."""
    telegram = ModuleType("telegram")
    telegram.Update = Mock()
    telegram.InlineKeyboardMarkup = Mock()
    telegram.InlineKeyboardButton = Mock()
    errors = ModuleType("telegram.error")
    for name in ("RetryAfter", "NetworkError", "BadRequest", "TimedOut", "Forbidden", "TelegramError"):
        setattr(errors, name, type(name, (Exception,), {}))
    return {"telegram": telegram, "telegram.error": errors}


class TestEndpoints:
    """Only raw model responses, browser approval and Telegram recipients are simulated."""

    def __init__(self, group, prompts, quality, token_url):
        self.group = group
        self.prompts = prompts
        self.quality = quality
        self.token_url = token_url
        now = int(time.time())
        self.old_access = synthetic_token(now + 86400)
        self.new_access = synthetic_token(now + 172800)
        self.chapters = Counter()
        self.lock = threading.Lock()
        self.documents = []
        self.private_messages = []
        self.refreshes = 0
        self.proofs = 0
        self.task_reviews = []

    def stream(self, client, headers, messages, web, model, *, instructions, **kwargs):
        if instructions == "Ответь только числом.":
            assert headers["Authorization"] == "Bearer " + self.new_access
            self.proofs += 1
            text = "42"
        elif self.prompts.DISPATCH in instructions:
            text = 'PSY_TASK {"minutes":60}'
        elif instructions.startswith(self.quality._EXTRACT):
            text = json.dumps(dict(task="write lecture", audience=None, topics=[], examples=[], format=None,
                                   deliverables=[], constraints=[], obligations=["Complete lecture"], continuation=False,
                                   superseded_requirements=[], kind="lecture", minutes=60,
                                   outline_only=False, limited_scope=False, local_only=False))
        elif "Сейчас нужен внутренний редакционный" in instructions:
            plan = [{"title": f"Chapter {i}", "brief": "argument", "minutes": 15, "activity_minutes": 0}
                    for i in range(1, 5)]
            text = json.dumps({"title": "Test lecture", "sections": plan})
        elif match := re.search(r"Напиши целиком только раздел (\d+)", instructions):
            chapter = int(match.group(1))
            if chapter == 4 and headers["Authorization"] == "Bearer " + self.old_access:
                raise self.group.ModelIssue("signin", "http")
            with self.lock:
                self.chapters[chapter] += 1
            text = f"Chapter {chapter} saved intact. " * 90
        elif "Проверь содержание раздела" in instructions:
            text = '{"missing":""}'
        elif instructions == self.quality._REVIEW:
            assert headers["Authorization"] == "Bearer " + self.new_access
            self.task_reviews.append(json.dumps(messages, ensure_ascii=False))
            text = '{"complete":true,"missing":[]}'
        else:
            raise AssertionError("Unexpected writing stage")
        return self.group.Result(text, [], 0)

    def refuse_renewal(self, request):
        assert str(request.url) == self.token_url and request.method == "POST"
        self.refreshes += 1
        return httpx.Response(400, json={"error": {"code": "invalid_grant"}})

    async def receive_private(self, *, chat_id, text, **kwargs):
        assert chat_id == OWNER
        self.private_messages.append(text)
        return SimpleNamespace(message_id=90 + len(self.private_messages))

    async def receive_document(self, *, document, **kwargs):
        self.documents.append(document.read().decode())
        return SimpleNamespace(message_id=86)


async def exercise(home, patches):
    from hermes_cli import auth, auth_codex, config, psy_codex_policy as policy, psy_codex_incident as incident
    from psy_modules import psy_group_codex as group, psy_group_queue as queue, psy_group_store as store
    from psy_modules import psy_group_client as codex, psy_group_pipeline as pipeline, psy_group_prompts as prompts
    from psy_modules import psy_group_quality as quality
    from psy_modules import psy_owner_recovery as owner, psy_group_route as route

    assert auth._auth_file_path().parent.resolve() == home
    global_store = auth._global_auth_file_path()
    assert global_store is None or global_store.resolve().is_relative_to(home)
    assert (route.OWNER, route.GROUP, route.BOT) == (OWNER, GROUP, BOT)
    patches.enter_context(patch.object(policy, "MARKERS", home))
    endpoints = TestEndpoints(group, prompts, quality, auth_codex.CODEX_OAUTH_TOKEN_URL)
    primary = dict(id="main", priority=0, source="manual:device_code", auth_type="oauth",
                   access_token=endpoints.old_access, refresh_token="fixture-grant")
    store_path = home / "group"
    auth._save_auth_store({"credential_pool": {policy.PROVIDER: [primary, dict(primary, id="alias", priority=1)]}})

    bot = SimpleNamespace(id=BOT, username="DemoTestBot", send_message=endpoints.receive_private)
    app = SimpleNamespace(bot=bot, adapter=SimpleNamespace(), running=True, create_task=asyncio.create_task)
    notice = AsyncMock(return_value=SimpleNamespace(message_id=85))
    message = SimpleNamespace(message_id=MESSAGE, chat=SimpleNamespace(id=GROUP, type="supergroup"),
                              from_user=SimpleNamespace(id=MEMBER, is_bot=False), sender_chat=None,
                              business_connection_id=None, is_topic_message=False, message_thread_id=None,
                              reply_document=endpoints.receive_document, reply_text=notice)

    def compose(turns, *, research, model, question, checkpoints, **kwargs):
        return pipeline._compose(None, turns, research, model, question, checkpoints=checkpoints,
                                 context_scope=kwargs.get("context_scope"), context_state=kwargs.get("context_state"),
                                 input_budget_tokens=100000)

    def oauth_client(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(endpoints.refuse_renewal))

    # All production SQLite, worker-lock and credential writes are redirected to this private home.
    open_db = store.database
    lease = store.lease
    patches.enter_context(patch.object(store, "database", lambda *args: open_db(store_path)))
    patches.enter_context(patch.object(store, "lease", lambda: lease(root=store_path)))
    patches.enter_context(patch.object(queue.Update, "de_json", return_value=SimpleNamespace(
        message=message, update_id=UPDATE)))
    patches.enter_context(patch.object(queue, "typing", AsyncMock()))
    patches.enter_context(patch.object(auth_codex, "_codex_http_client", oauth_client))
    patches.enter_context(patch.object(group, "_respond", compose))
    # The client is the one seam: chapters and the owner's sign-in probe both stream through it.
    patches.enter_context(patch.object(codex, "_stream", endpoints.stream))
    patches.enter_context(patch.object(config, "load_config_readonly", return_value={
        "model": {"provider": policy.PROVIDER, "default": "fixture-model"}}))

    await queue.ensure_vault(bot)
    body = json.dumps({"update_id": UPDATE, "message": {"message_id": MESSAGE}})
    assert store.pending(MEMBER, UPDATE, body) == "queued"
    store.Checkpoints(UPDATE).put("route:input:v1", {
        "destination": [GROUP, MEMBER, 0, MESSAGE], "doc_id": None, "question": "write lecture",
        "quote": "", "history": [], "research": False, "draw": False, "model": "fixture-model"})

    await queue.run_job(app, asyncio.Event(), *store.claim_next())
    assert endpoints.chapters == Counter({1: 1, 2: 1, 3: 1})
    assert not endpoints.documents
    assert not endpoints.task_reviews
    assert store.state(UPDATE) == "queued" and store.claim_next() is None
    assert incident.snapshot()["reason"] == "revoked"
    assert endpoints.refreshes == 1

    # Recreate the gateway's in-memory state. It must use the existing on-disk chapters.
    store.recover()
    app.adapter = SimpleNamespace(_psy_owner_recovery_wake=asyncio.Event())
    await owner.tick(app)
    await owner.tick(app)
    assert len(endpoints.private_messages) == 1
    assert store.next_retry_delay() > 0

    patches.enter_context(patch.object(owner, "request_device", return_value={
        "user_code": "TEST-FIXTURE", "device_auth_id": "fixture-device", "interval": 3}))
    patches.enter_context(patch.object(owner, "poll_device", AsyncMock(return_value={
        "access_token": endpoints.new_access, "refresh_token": "fixture-renewed-grant"})))
    private = SimpleNamespace(id=OWNER, type="private")
    sender = SimpleNamespace(id=OWNER, is_bot=False)
    command = SimpleNamespace(chat=private, from_user=sender, text="/codex_login",
                              business_connection_id=None, reply_text=AsyncMock())
    update = SimpleNamespace(effective_chat=private, effective_user=sender, message=command, callback_query=None)
    assert await route.intercept(app, update)
    await asyncio.wait_for(app.adapter._psy_owner_login_task, 10)
    await owner.tick(app)
    assert endpoints.proofs == 1 and incident.snapshot() is None
    await asyncio.wait_for(app.adapter._psy_group_task, 10)

    assert endpoints.chapters == Counter({1: 1, 2: 1, 3: 1, 4: 1})
    assert len(endpoints.task_reviews) == 1
    assert all(f"Chapter {i} saved intact." in endpoints.task_reviews[0] for i in range(1, 5))
    assert len(endpoints.documents) == 1
    assert all(f"Chapter {i} saved intact." in endpoints.documents[0] for i in range(1, 5))
    with store.database() as con:
        turns = con.execute("SELECT id FROM turns WHERE uid=? AND topic=?", (MEMBER, 0)).fetchall()
    assert len(turns) == 1
    details = store.result_details(MEMBER, 0, turns[0][0])
    assert details["task_status"] == "complete" and details["missing_requirements"] == []
    assert details["quality_metadata"]["completion_review"] == "passed"
    assert details["delivery_status"] == "confirmed"
    assert store.state(UPDATE) is None and store.claim_next() is None
    assert endpoints.refreshes == 1
    notice.assert_awaited_once()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="Hermes checkout restored with gateway/apply.py")
    checkout = parser.parse_args().checkout.resolve()
    sys.path.insert(0, str(checkout))
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory() as folder, ExitStack() as patches:
        home = Path(folder).resolve()
        patches.enter_context(patch.dict(os.environ, {
            "HOME": str(home), "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "PSY_OWNER_ID": str(OWNER), "PSY_GROUP_ID": str(GROUP), "PSY_BOT_ID": str(BOT),
            "PSY_BOT_USERNAME": "DemoTestBot", "PSY_PUBLIC_URL": "https://example.invalid"}, clear=True))
        for name in ("connect", "connect_ex"):
            patches.enter_context(patch.object(socket.socket, name, forbid_network))
        pkg = ModuleType("psy_modules")
        pkg.__path__ = [str(checkout / "plugins/platforms/telegram")]
        patches.enter_context(patch.dict(sys.modules, {"psy_modules": pkg, **telegram_types()}))
        asyncio.run(exercise(home, patches))
    print("Passed: three chapters saved, one owner alert, model proof, chapter four resumed, "
          "all chapters reviewed, one document")


if __name__ == "__main__":
    main()
