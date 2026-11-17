"""Owner-only recovery of PSY's Codex login. Never requires an LLM to start sign-in."""
import asyncio
import contextvars
import logging
import re
import time

from hermes_cli import psy_codex_incident as incident
from hermes_cli import psy_codex_policy as policy

from .psy_instance import BOT, OWNER

ISSUER = "https://auth.openai.com"
DEVICE_URL = ISSUER + "/codex/device"
CALLBACK = "psy_codex:"
COMMAND = re.compile(r"/codex_login(?:@[a-zA-Z0-9_]+)?\s*", re.I)
VERIFY_DELAY = 60
log = logging.getLogger("psy.owner.recovery")

# PTB's debug logger logs complete request arguments and responses, including the DM
# containing the human device code. Suppress only transport traces for these operations.
_private_io = contextvars.ContextVar("psy_owner_recovery_private", default=False)


class _PrivacyFilter(logging.Filter):
    def filter(self, record):
        return not _private_io.get()


_filter = _PrivacyFilter()


async def private_send(app, text, **kwargs):
    token = _private_io.set(True)
    try:
        return await app.bot.send_message(chat_id=OWNER, text=text, protect_content=True, **kwargs)
    finally:
        _private_io.reset(token)


def _private_logs():
    # PTB can log full method arguments and responses at DEBUG. Filter only this
    # task's transport records, including loggers created after gateway start.
    for name in ("httpx", "httpcore.http11", "httpcore.connection", "httpcore.http2",
                 "telegram.Bot", "telegram.ext.ExtBot", "telegram.request.HTTPXRequest"):
        logging.getLogger(name).addFilter(_filter)


def selected_generation():
    row, _ = policy.credential_snapshot()
    return policy.generation(row)


def probe_model():
    from hermes_cli.config import load_config_readonly
    from agent.process_bootstrap import enable_happy_eyeballs_on_client
    from . import psy_group_codex as group
    import httpx

    model = load_config_readonly()["model"]
    if model.get("provider") != policy.PROVIDER:
        return False
    prompt = "Ответь одним числом: 6 умножить на 7."
    with group._transport(), httpx.Client(trust_env=False) as client:
        enable_happy_eyeballs_on_client(client)
        answer = group._authenticated_request(lambda token, headers: group._stream(
            client, headers, [group._user(prompt)], False, model["default"],
            instructions="Ответь только числом.", read_idle=25, progress_idle=30))
    return answer.complete and answer.text.strip().rstrip(".") == "42"


async def _verify(app, record, observed):
    from .psy_group_queue import resume_authorized

    adapter = app.adapter
    now = time.monotonic()
    previous = getattr(adapter, "_psy_owner_probe", None)
    if previous and previous[:2] == (record["id"], observed) and now < previous[2]:
        return
    adapter._psy_owner_probe = (record["id"], observed, now + VERIFY_DELAY)
    if not await asyncio.to_thread(probe_model):
        return
    if observed != await asyncio.to_thread(selected_generation):
        return
    latest = await asyncio.to_thread(incident.snapshot)
    if not latest or latest["id"] != record["id"]:
        return
    await resume_authorized(app, record["id"])
    await asyncio.to_thread(incident.close, record["id"])


async def _notice(app, record):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import Forbidden, NetworkError, RetryAfter

    if not await asyncio.to_thread(incident.claim_notice, record["id"]):
        return
    text = ("ПСИ потерял подтверждение продления входа в Codex. Пока текущий доступ действует, "
            "я продолжу работу." if record["reason"] == "uncertain" else
            "Для работы ПСИ с моделью нужно подтвердить вход в ChatGPT.")
    text += " Нажмите кнопку, когда будете готовы. Принятые задания группы сохранены."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Подтвердить вход", callback_data=CALLBACK + record["id"])]])
    try:
        await private_send(app, text, reply_markup=keyboard)
    except RetryAfter as error:
        delay = error.retry_after
        await asyncio.to_thread(incident.retry_notice, record["id"],
                                delay.total_seconds() if hasattr(delay, "total_seconds") else float(delay))
    except Forbidden:
        # The owner may unblock the bot later. Telegram explicitly refused this send.
        await asyncio.to_thread(incident.retry_notice, record["id"], 3600)
    except NetworkError as error:
        import httpx
        cause = error.__cause__
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            await asyncio.to_thread(incident.retry_notice, record["id"], 60)
        # A read timeout can mean success. The owner command remains available.


def request_device():
    from hermes_cli.auth_codex import _codex_request_device_code, CODEX_OAUTH_CLIENT_ID
    return _codex_request_device_code(ISSUER, CODEX_OAUTH_CLIENT_ID)


def _poll_once(device):
    from hermes_cli.auth_codex import _codex_http_client
    with _codex_http_client(timeout=12.0, trust_env=False) as client:
        response = client.post(ISSUER + "/api/accounts/deviceauth/token", json={
            "device_auth_id": device["device_auth_id"], "user_code": device["user_code"]})
    if response.status_code in (403, 404, 429, 500, 502, 503, 504):
        return None
    response.raise_for_status()
    return response.json()


def _exchange_device(approval):
    # The generic native login helper retries transport errors, including a lost
    # successful response. This authorization code is single-use.
    from hermes_cli.auth_codex import _codex_http_client, CODEX_OAUTH_CLIENT_ID, CODEX_OAUTH_TOKEN_URL
    if not all(isinstance(approval.get(k), str) and approval[k] for k in ("authorization_code", "code_verifier")):
        raise ValueError("incomplete owner device approval")
    data = {"grant_type": "authorization_code", "code": approval["authorization_code"],
            "redirect_uri": ISSUER + "/deviceauth/callback", "client_id": CODEX_OAUTH_CLIENT_ID,
            "code_verifier": approval["code_verifier"]}
    with _codex_http_client(timeout=15.0, trust_env=False) as client:
        response = client.post(CODEX_OAUTH_TOKEN_URL, data=data, follow_redirects=False)
    response.raise_for_status()
    return response.json()


async def poll_device(device):
    """Poll only while this session owns the device flow. Exchange each authorization code once."""
    import httpx
    while time.time() < device["expires"]:
        await asyncio.sleep(device["interval"])
        record = await asyncio.to_thread(incident.snapshot)
        if not record or record.get("device") != device or record["generation"] != await asyncio.to_thread(selected_generation):
            return None
        try:
            approval = await asyncio.to_thread(_poll_once, device)
        except httpx.RequestError:
            continue
        if approval is None:
            continue
        # A process killed during the single-use authorization-code exchange cannot
        # retry that POST. Only a fresh press by the owner can start another attempt.
        exchanging = dict(device, phase="exchanging")
        if not await asyncio.to_thread(incident.update_device, record["id"], exchanging, expected=device):
            return None
        return await asyncio.to_thread(_exchange_device, approval)
    return None


async def _show_code(app, device):
    return await private_send(app, "Откройте страницу OpenAI для входа в Codex и введите код " +
                              device["user_code"] + ".\n" + DEVICE_URL +
                              "\nЗавершите подтверждение в браузере.", disable_web_page_preview=True)


async def _login(app, incident_id, *, initiate):
    # Serialize even across gateway restarts which briefly overlap. No lock on the
    # credential store is held while the owner uses their browser.
    with incident.lock(incident.directory(), "device.lock", blocking=False) as claimed:
        if not claimed:
            return
        record = await asyncio.to_thread(incident.snapshot)
        if not record or record["id"] != incident_id:
            return
        if record["generation"] != await asyncio.to_thread(selected_generation):
            return
        device = record.get("device")
        if device and (device["expires"] <= time.time() or device["phase"] == "exchanging"):
            cleared = await asyncio.to_thread(incident.update_device, incident_id, None, expected=device)
            if cleared and not initiate:
                await private_send(app, "Подтверждение входа прервалось. Когда будете готовы, отправьте /codex_login.")
            device = None
        if device is None:
            if not initiate:
                return
            raw = await asyncio.to_thread(request_device)
            if not re.fullmatch(r"[A-Za-z0-9-]{4,32}", raw["user_code"]):
                raise ValueError("unsupported device challenge")
            device = {"user_code": raw["user_code"], "device_auth_id": raw["device_auth_id"],
                      "interval": max(3, int(raw.get("interval", 5))),
                      "expires": time.time() + min(900, max(1, int(raw.get("expires_in", 900)))),
                      "phase": "prepared"}
            if not await asyncio.to_thread(incident.update_device, incident_id, device):
                return
        if device["phase"] == "prepared" or initiate:
            delivered = dict(device, phase="polling")
            if not await asyncio.to_thread(incident.update_device, incident_id, delivered, expected=device):
                return
            device = delivered
            await _show_code(app, device)
        tokens = await poll_device(device)
        if tokens:
            from datetime import datetime, timezone
            pair = dict(tokens, last_refresh=datetime.now(timezone.utc).isoformat())
            await asyncio.to_thread(policy.install_owner_login, record["generation"], pair)
        current = await asyncio.to_thread(incident.snapshot)
        if current and current["id"] == incident_id:
            await asyncio.to_thread(incident.update_device, incident_id, None, expected=current.get("device"))
        if not tokens and current and current["id"] == incident_id and record["generation"] == await asyncio.to_thread(selected_generation):
            await private_send(app, "Подтверждение входа истекло. Когда будете готовы, отправьте /codex_login.")


def _start_login(app, record, initiate):
    existing = getattr(app.adapter, "_psy_owner_login_task", None)
    if existing and not existing.done():
        return False
    async def run():
        privacy = _private_io.set(True)
        try:
            await _login(app, record["id"], initiate=initiate)
        except Exception as error:
            log.warning("PSY owner login failed (%s)", type(error).__name__)
            try:
                current = await asyncio.to_thread(incident.snapshot)
                if current and current["id"] == record["id"]:
                    await asyncio.to_thread(incident.update_device, record["id"], None, expected=current.get("device"))
                    await private_send(app, "Подтвердить вход пока не удалось. Повторите /codex_login.")
            except Exception:
                pass
        finally:
            _private_io.reset(privacy)
            app.adapter._psy_owner_recovery_wake.set()
    app.adapter._psy_owner_login_task = asyncio.create_task(run(), name="psy-owner-device-login")
    return True


async def tick(app):
    record = await asyncio.to_thread(incident.snapshot)
    if not record:
        return
    observed = await asyncio.to_thread(selected_generation)
    if record["generation"] != observed:
        await _verify(app, record, observed)
        return
    if record.get("device"):
        _start_login(app, record, initiate=False)
    if incident.notice_due(record):
        if record["generation"] == await asyncio.to_thread(selected_generation):
            await _notice(app, record)


def start(app):
    adapter = app.adapter
    previous = getattr(adapter, "_psy_owner_watcher", None)
    if previous and not previous.done():
        return
    _private_logs()
    wake = adapter._psy_owner_recovery_wake = asyncio.Event()
    async def watch():
        log.info("PSY private Codex recovery watcher active")
        try:
            while app.running:
                wake.clear()
                try:
                    await tick(app)
                except Exception as error:
                    # Device payloads, URLs that might contain tokens, and chat content
                    # must never enter a log through an exception message or traceback.
                    log.warning("PSY owner recovery check failed (%s)", type(error).__name__)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        finally:
            task = getattr(adapter, "_psy_owner_login_task", None)
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    adapter._psy_owner_watcher = asyncio.create_task(watch(), name="psy-owner-recovery")


async def handle(app, update):
    from .psy_group_route import scope
    query = getattr(update, "callback_query", None)
    data = getattr(query, "data", None)
    msg = getattr(update, "message", None)
    command = COMMAND.fullmatch(getattr(msg, "text", "") or "")
    callback = isinstance(data, str) and data.startswith(CALLBACK)
    if not command and not callback:
        return False
    if (app is None or app.bot.id != BOT or scope(update) != "owner"
            or getattr(update.effective_chat, "id", None) != OWNER):
        return True
    if command and "@" in command.group() and command.group().split("@",1)[1].strip().lower() != app.bot.username.lower():
        return True
    record = await asyncio.to_thread(incident.snapshot)
    if callback:
        if not record or data != CALLBACK + record["id"]:
            await query.answer("Эта кнопка уже не действует.")
            return True
        await query.answer()
    if not record:
        await private_send(app, "У ПСИ нет запроса на повторный вход.")
        return True
    if record["generation"] != await asyncio.to_thread(selected_generation):
        app.adapter._psy_owner_recovery_wake.set()
        await private_send(app, "Новый вход уже сохранен. ПСИ проверит доступ к модели.")
    elif not _start_login(app, record, initiate=True):
        device = record.get("device")
        if device and device["phase"] == "polling" and device["expires"] > time.time():
            await _show_code(app, device)
        else:
            await private_send(app, "Подтверждение уже запущено.")
    return True
