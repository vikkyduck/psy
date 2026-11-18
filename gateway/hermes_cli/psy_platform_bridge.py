"""Lend PSY's single Codex login and owner notices to the co-located platform over a Unix socket.

systemd starts it on the first connection to psy-platform-bridge.socket (Accept=no) as the hermes
account: `hermes --run-module hermes_cli.psy_platform_bridge`. `--socket PATH` binds a socket of its
own, for tests.

The bridge keeps no copy of the ChatGPT login and starts no renewal of its own. Every request borrows
the preferred credential through psy_codex_policy exactly like the group module, so a renewal (near
expiry, or after the model refused the bearer with HTTP 401) runs on that one guarded path: the
auth-store lock and the single-use grant journal. The platform never becomes a second owner of the
refresh token.

HTTP/1.1 with JSON, one request per connection. SO_PEERCRED admits root and the accounts named in
PSY_PLATFORM_PEERS (default kotu); everyone else, hermes included, gets 403.
  GET  /health[?deep=1]  {"ok": true}; deep=1 also admits the login: "codex": "ready", or 503 and its kind
  POST /responses        a Responses API payload; Codex's SSE stream is relayed byte for byte
  POST /images           {"prompt", "size": "WxH", "quality"} -> PNG {"b64", "mime", "width", "height", "upstream_size"}
  POST /notify           {"text"} -> a Telegram message to the bot owner's private chat
Errors are {"error": {"kind": K}}; a refusal by Codex adds its "status" and "resets_at". The log names
method, path, status, duration and error kind only: never tokens, prompts, bodies or Telegram text.
"""
import argparse
import base64
from collections import deque
from contextlib import contextmanager
import hashlib
import http.client
import http.server
import importlib
import io
import json
import logging
import math
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import stat
import struct
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import uuid

from hermes_cli import psy_codex_policy as codex_policy

ENDPOINT = "https://chatgpt.com/backend-api/codex"
TELEGRAM = "https://api.telegram.org"
MAX_BODY = 32 * 1024**2
MAX_ERROR_BODY = 16384
MAX_IMAGE_RESPONSE = 40 * 1024**2
MAX_IMAGE_B64 = 28_000_000
CONCURRENCY = 8
CONNECT_SECONDS = 10
READ_IDLE_SECONDS = 300
IMAGE_CONNECT_SECONDS = 30
IMAGE_READ_SECONDS = 300
CLIENT_SECONDS = 60
TELEGRAM_SECONDS = 15
NOTICE_CHARS = 3500
DUPLICATE_SECONDS = 600
HOURLY_CAP = 30
LOOPBACK = ("127.0.0.1", "::1", "localhost")
ROUTES = {"/health": "GET", "/responses": "POST", "/images": "POST", "/notify": "POST"}
MODEL = re.compile(r"[A-Za-z0-9._:-]{1,64}")
SIZE = re.compile(r"([1-9][0-9]{2,3})x([1-9][0-9]{2,3})")
REQUIRED = ("model", "instructions", "input")
EFFORTS = ("low", "medium", "high")
SUMMARIES = ("auto", "concise", "detailed")
# The accepted subset of the Responses API. stream and store may only repeat the forced values.
VALID = {
    "model": lambda v: isinstance(v, str) and MODEL.fullmatch(v) is not None,
    "instructions": lambda v: isinstance(v, str),
    "input": lambda v: isinstance(v, (str, list)),
    "tools": lambda v: isinstance(v, list) and all(item == {"type": "web_search"} for item in v),
    "tool_choice": lambda v: isinstance(v, str) and v in ("auto", "required", "none"),
    "include": lambda v: isinstance(v, list) and all(item == "web_search_call.action.sources" for item in v),
    "reasoning": lambda v: (isinstance(v, dict) and set(v) <= {"effort", "summary"}
                            and isinstance(v.get("effort"), str) and v["effort"] in EFFORTS
                            and ("summary" not in v or isinstance(v["summary"], str) and v["summary"] in SUMMARIES)),
    "text": lambda v: isinstance(v, dict),
    "prompt_cache_key": lambda v: isinstance(v, str) and len(v) <= 64,
    "parallel_tool_calls": lambda v: type(v) is bool,
    "stream": lambda v: v is True,
    "store": lambda v: v is False,
}
FORCED = {"stream": True, "store": False}
QUOTA = ("usage_limit_reached", "insufficient_quota", "usage_limit_exceeded")
CONTEXT = ("context_length_exceeded", "context_window_exceeded", "input_too_large")

log = logging.getLogger("psy.platform_bridge")
clock = time.monotonic


class Refusal(Exception):
    """The answer instead of the requested work. `kind` is safe to log."""

    def __init__(self, status, kind, *, upstream=None, resets_at=None):
        super().__init__(kind)
        self.status, self.kind, self.upstream, self.resets_at = status, kind, upstream, resets_at

    def document(self):
        error = {"kind": self.kind}
        if self.upstream is not None:
            error.update(status=self.upstream, resets_at=self.resets_at)
        return {"error": error}


def bad_request():
    return Refusal(400, "bad_request")


class ClientGone(Exception):
    """The platform stopped sending its request; nobody is left to answer."""


class Undelivered(Exception):
    """Telegram did not confirm the message to the owner."""


def classify(status, detail):
    """psy_group_client._failure for the platform: (kind, resets_at) of a refusal before the stream."""
    if status in (408, 504):
        return "timeout", None
    detail = detail if isinstance(detail, dict) else {}
    codes = (detail.get("code"), detail.get("type"))
    if any(code in QUOTA for code in codes):
        reset = detail.get("resets_at")
        if type(reset) in (int, float) and math.isfinite(reset):
            return "quota", reset
        # Some refusals give only the wait. The platform schedules by a moment in time.
        wait = detail.get("resets_in_seconds")
        if type(wait) in (int, float) and math.isfinite(wait) and 0 <= wait <= 30 * 86400:
            return "quota", int(time.time() + wait)
        return "quota", None
    if any(code in CONTEXT for code in codes):
        return "context", None
    if status == 429 or "rate_limit_exceeded" in codes:
        return "busy", None
    if status == 401 or any(code in ("invalid_token", "token_expired") for code in codes):
        return "signin", None
    return "unavailable", None


def override(name, default):
    """Tests replace an endpoint. Only a loopback http:// URL is accepted, so a token never leaves the host."""
    value = os.environ.get(name)
    if not value:
        return default
    parts = urlsplit(value)
    if (parts.scheme != "http" or parts.hostname not in LOOPBACK or parts.username or parts.password
            or parts.query or parts.fragment or parts.port == 0):
        raise ValueError(f"{name} accepts only a loopback http:// URL")
    return value.rstrip("/")


def upstream():
    return override("PSY_PLATFORM_UPSTREAM", ENDPOINT)


def telegram_api():
    return override("PSY_PLATFORM_TELEGRAM_API", TELEGRAM)


def peer_uid(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII"))
    return struct.unpack("iII", credentials)[1]  # struct ucred: pid, uid, gid


def allowed_uids():
    """root and the accounts in PSY_PLATFORM_PEERS, resolved per connection so a later useradd counts."""
    uids = {0}
    for name in os.environ.get("PSY_PLATFORM_PEERS", "kotu").split(","):
        try:
            uids.add(pwd.getpwnam(name.strip()).pw_uid)
        except (KeyError, ValueError):
            pass
    return uids


# ---------- Codex ----------

def credentials(rejected=None):
    """(bearer, generation) admitted by the shared policy for one real inference."""
    try:
        cred = codex_policy.model_credentials(rejected)
    except codex_policy.CredentialIssue as exc:
        raise Refusal(503, exc.kind) from None
    if cred["base_url"].rstrip("/") != ENDPOINT or cred["provider"] != "openai-codex":
        raise Refusal(503, "unavailable")
    return cred["api_key"], cred["generation"]


def identity(token):
    from agent.codex_headers import codex_cloudflare_headers
    return {**codex_cloudflare_headers(token), "Authorization": f"Bearer {token}"}


def authenticated(attempt):
    """psy_group_client._authenticated_request: one retry with a fresh credential after a refused login."""
    token, generation = credentials()
    try:
        return attempt(token)
    except Refusal as exc:
        # Only a refusal before the stream starts guarantees no accepted work.
        if exc.kind != "signin" or exc.upstream is None:
            raise
        refused = exc
    replacement, next_generation = credentials(rejected=generation)
    if replacement != token:
        try:
            return attempt(replacement)
        except Refusal as exc:
            if exc.kind != "signin" or exc.upstream is None:
                raise
            refused = exc
    try:
        # With an identical bearer the evidence belongs to the first generation; a concurrent owner login survives.
        codex_policy.record_model_refusal(generation if replacement == token else next_generation)
    except codex_policy.CredentialIssue as exc:
        raise Refusal(503, exc.kind) from None
    raise refused


@contextmanager
def transport():
    try:
        yield
    except TimeoutError:
        raise Refusal(504, "timeout") from None
    except (OSError, http.client.HTTPException):
        raise Refusal(502, "connection") from None


def refusal(response):
    raw = response.read(MAX_ERROR_BODY + 1)  # bounded, never logged or relayed
    try:
        document = json.loads(raw) if len(raw) <= MAX_ERROR_BODY else {}
    except (ValueError, RecursionError):
        document = {}
    kind, reset = classify(response.status, document.get("error") if isinstance(document, dict) else None)
    return Refusal(response.status if 400 <= response.status <= 599 else 502, kind,
                   upstream=response.status, resets_at=reset)


def connection(url, timeout):
    parts = urlsplit(url)
    factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    return factory(parts.hostname, parts.port, timeout=timeout), parts.path


def exchange(url, data, headers, *, connect, read, limit=None):
    """POST data. A 200 returns (connection, open response), or its body when limit is set; else a Refusal."""
    conn, path = connection(url, connect)
    try:
        conn.connect()
        conn.sock.settimeout(read)  # from here on the timeout is the longest silence between bytes
        conn.request("POST", path, body=data, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            raise refusal(response)
        if limit is None:
            return conn, response
        body = response.read(limit + 1)
        if len(body) > limit:
            raise Refusal(502, "unavailable")
        conn.close()
        return body
    except BaseException:
        conn.close()
        raise


def refuse_constant(name):
    raise ValueError("NaN and Infinity are not JSON")


def encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    except ValueError:  # a lone surrogate cannot travel as UTF-8
        raise bad_request() from None


def responses_payload(body):
    if not all(key in body for key in REQUIRED) or not all(key in VALID and VALID[key](value)
                                                            for key, value in body.items()):
        raise bad_request()
    return {**body, **FORCED}


def image_request(body):
    prompt, size, quality = body.get("prompt"), body.get("size", "1536x864"), body.get("quality", "medium")
    match = SIZE.fullmatch(size) if isinstance(size, str) else None
    if (not set(body) <= {"prompt", "size", "quality"} or not isinstance(prompt, str)
            or not 1 <= len(prompt) <= 32000 or not prompt.strip() or match is None
            or not isinstance(quality, str) or quality not in ("low", "medium", "high")):
        raise bad_request()
    width, height = map(int, match.groups())
    if any(side % 16 or not 256 <= side <= 3840 for side in (width, height)):
        raise bad_request()
    return prompt, width, height, quality


def upstream_size(width, height):
    """The sizes the image endpoint knows. It does not guarantee them either; fit_png cuts the target."""
    return "1536x1024" if width > height else "1024x1536" if width < height else "1024x1024"


def image_bytes(raw):
    try:
        encoded = json.loads(raw)["data"][0]["b64_json"]
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise Refusal(502, "unavailable") from None
    if not isinstance(encoded, str) or not encoded or len(encoded) > MAX_IMAGE_B64:
        raise Refusal(502, "unavailable")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError:
        raise Refusal(502, "unavailable") from None


def fit_png(data, width, height):
    """Center-crop to the target aspect ratio, scale to exactly width x height, encode PNG.

    Returns the PNG and the size Codex actually delivered."""
    from PIL import Image, ImageOps
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            received = f"{source.width}x{source.height}"
            image = source
            if source.mode not in ("RGB", "RGBA"):
                alpha = source.mode in ("LA", "PA") or "transparency" in source.info
                image = source.convert("RGBA" if alpha else "RGB")
            fitted = ImageOps.fit(image, (width, height), method=lanczos, centering=(0.5, 0.5))
        output = io.BytesIO()
        fitted.save(output, format="PNG")
    except (OSError, ValueError, SyntaxError, struct.error, Image.DecompressionBombError):
        raise Refusal(502, "unavailable") from None
    return output.getvalue(), received


# ---------- owner notices ----------

class Notices:
    """In-memory budget: one copy of a text per DUPLICATE_SECONDS, HOURLY_CAP Telegram attempts per hour."""

    def __init__(self):
        self.lock = threading.Lock()
        self.recent = {}
        self.attempts = deque()

    def reserve(self, text):
        """(key, None) when the message may go now, else (None, "duplicate" or "rate_limited")."""
        key = hashlib.sha256(text.encode()).digest()
        with self.lock:
            now = clock()
            while self.attempts and now - self.attempts[0] >= 3600:
                self.attempts.popleft()
            for old in [k for k, at in self.recent.items() if now - at >= DUPLICATE_SECONDS]:
                del self.recent[old]
            if key in self.recent:
                return None, "duplicate"
            # Failed attempts count too: the gateway shares this bot token and its flood limits.
            if len(self.attempts) >= HOURLY_CAP:
                return None, "rate_limited"
            self.recent[key] = now
            self.attempts.append(now)
            return key, None

    def release(self, key):
        """An undelivered text may be sent again at once."""
        with self.lock:
            self.recent.pop(key, None)


def owner_chat():
    owner = os.environ.get("PSY_OWNER_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,15}", owner):
        raise LookupError("owner")
    return owner


def read_regular(path, limit=1 << 20):
    """A small regular file, never through a link or a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("not a regular file")
        return stream.read(limit).decode("utf-8", errors="replace")


def bot_token():
    """psy_alerts.token() for the hermes account: the environment, then the profile .env under HERMES_HOME."""
    supplied = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if supplied:
        return supplied
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    try:
        text = read_regular(home / ".env")
    except OSError:
        raise LookupError("token") from None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            value = value.strip().strip("\"'")
            if value:
                return value
    raise LookupError("token")


def deliver(owner, token, text):
    conn, path = connection(telegram_api() + "/bot" + quote(token, safe=":") + "/sendMessage", TELEGRAM_SECONDS)
    body = urlencode({"chat_id": owner, "text": text, "protect_content": "true"}).encode()
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        status, raw = response.status, response.read(65537)
    except (OSError, http.client.HTTPException):
        raise Undelivered() from None
    finally:
        conn.close()
    try:
        answer = json.loads(raw)
        confirmed = answer["ok"] is True and str(answer["result"]["chat"]["id"]) == owner
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        confirmed = False
    if status != 200 or not confirmed:
        raise Undelivered()


# ---------- HTTP ----------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "psy-platform-bridge"
    timeout = CLIENT_SECONDS

    def handle(self):
        self.began = time.monotonic()
        self.status, self.replied = None, False
        self.close_connection = True
        try:
            allowed = peer_uid(self.connection) in allowed_uids()
        except OSError:
            allowed = False
        if allowed:
            self.handle_one_request()
            return
        body = b'{"error":{"kind":"forbidden"}}'
        try:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                             b"Connection: close\r\n\r\n%s" % (len(body), body))
        except OSError:
            pass
        self.record(403, "forbidden")

    def do_GET(self):
        route = self.path.partition("?")[0]
        kind = None
        try:
            kind = self.serve(route)
        except Refusal as exc:
            kind = exc.kind
            if not self.replied:
                self.reply(exc.status, exc.document())
        except ClientGone:
            kind = "client_gone"
        except Exception as exc:
            kind = "internal"
            log.error("internal_error %s", type(exc).__name__)
            if not self.replied:
                self.reply(500, {"error": {"kind": "internal"}})
        self.record(self.status, kind, route)

    do_POST = do_GET

    def serve(self, route):
        if route not in ROUTES:
            raise Refusal(404, "not_found")
        if self.command != ROUTES[route]:
            raise Refusal(405, "method_not_allowed")
        if route == "/health" and parse_qs(urlsplit(self.path).query).get("deep") != ["1"]:
            self.reply(200, {"ok": True})
            return None
        with self.server.slot():
            if route == "/health":
                return self.serve_health()
            # serve_responses, serve_images, serve_notify (BaseHTTPRequestHandler.responses is its status table)
            return getattr(self, "serve_" + route[1:])(self.read_json())

    def read_json(self):
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,12}", length):
            raise Refusal(411, "length_required")
        if int(length) > MAX_BODY:
            raise Refusal(413, "too_large")
        try:
            data = self.rfile.read(int(length))
        except OSError:
            raise ClientGone() from None
        if len(data) != int(length):
            raise ClientGone()
        try:
            body = json.loads(data, parse_constant=refuse_constant)
        except (ValueError, RecursionError):
            raise bad_request() from None
        if not isinstance(body, dict):
            raise bad_request()
        return body

    def serve_health(self):
        try:
            credentials()
        except Refusal as exc:
            self.reply(503, {"ok": False, "codex": exc.kind})
            return exc.kind
        self.reply(200, {"ok": True, "codex": "ready"})
        return None

    def serve_responses(self, body):
        data = encode(responses_payload(body))
        url = upstream() + "/responses"

        def attempt(token):
            headers = {**identity(token), "Accept": "text/event-stream", "Content-Type": "application/json"}
            return exchange(url, data, headers, connect=CONNECT_SECONDS, read=READ_IDLE_SECONDS)

        with transport():
            conn, response = authenticated(attempt)
        try:
            self.status, self.replied = 200, True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                # Asked for identity; should Codex compress anyway, the unchanged bytes need their label.
                encoding = response.getheader("Content-Encoding", "identity")
                if encoding.lower() != "identity":
                    self.send_header("Content-Encoding", encoding)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
            except OSError:
                return "client_gone"
            return self.relay(response)
        finally:
            conn.close()

    def relay(self, response):
        """Codex's bytes unchanged until its EOF; the kind that cut the stream short, if any."""
        while True:
            try:
                chunk = response.read1(65536)
            except TimeoutError:
                return "timeout"
            except (OSError, http.client.HTTPException):
                return "connection"
            if not chunk:
                return None
            try:
                self.wfile.write(chunk)
            except OSError:
                return "client_gone"

    def serve_images(self, body):
        prompt, width, height, quality = image_request(body)
        # The Hermes openai-codex image plugin owns the request body of the native endpoint.
        plugin = importlib.import_module("plugins.image_gen.openai-codex")
        path, request = plugin._build_image_request(prompt=prompt, size=upstream_size(width, height), quality=quality)
        data = encode(request)
        url = f"{upstream()}/{path}"

        def attempt(token):
            # The plugin's own headers: its httpx client also sends Accept: */*.
            headers = {**identity(token), "Accept": "*/*", "Content-Type": "application/json",
                       "x-codex-image-turn-id": str(uuid.uuid4())}
            return exchange(url, data, headers, connect=IMAGE_CONNECT_SECONDS, read=IMAGE_READ_SECONDS,
                            limit=MAX_IMAGE_RESPONSE)

        with transport():
            raw = authenticated(attempt)
        png, received = fit_png(image_bytes(raw), width, height)
        self.reply(200, {"b64": base64.b64encode(png).decode(), "mime": "image/png", "width": width,
                         "height": height, "upstream_size": received})
        return None

    def serve_notify(self, body):
        text = body.get("text")
        if set(body) != {"text"} or not isinstance(text, str) or not 1 <= len(text) <= NOTICE_CHARS or not text.strip():
            raise bad_request()
        try:
            text.encode()
        except UnicodeEncodeError:
            raise bad_request() from None
        try:
            owner, token = owner_chat(), bot_token()
        except LookupError:
            self.reply(503, {"sent": False, "reason": "unconfigured"})
            return "unconfigured"
        key, held = self.server.notices.reserve(text)
        if held:
            self.reply(200 if held == "duplicate" else 429, {"sent": False, "reason": held})
            return held
        try:
            deliver(owner, token, text)
        except Undelivered:
            self.server.notices.release(key)
            self.reply(502, {"sent": False, "reason": "telegram"})
            return "telegram"
        self.reply(200, {"sent": True})
        return None

    def reply(self, status, document):
        data = json.dumps(document, separators=(",", ":")).encode()
        self.status, self.replied = status, True
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def send_error(self, code, message=None, explain=None):
        # A malformed request or an unknown method, before any route: the same JSON errors.
        status = 405 if code == 501 else code
        kind = {405: "method_not_allowed", 414: "too_large", 431: "too_large"}.get(status, "bad_request")
        self.close_connection = True
        self.reply(status, {"error": {"kind": kind}})
        self.record(status, kind)

    def record(self, status, kind, route=None):
        method = self.command if re.fullmatch(r"[A-Z]{1,16}", getattr(self, "command", None) or "") else "-"
        log.info("request method=%s path=%s status=%s ms=%d kind=%s", method, route if route in ROUTES else "-",
                 status or "-", (time.monotonic() - self.began) * 1000, kind or "-")

    def version_string(self):
        return self.server_version

    def log_request(self, code="-", size="-"):
        pass  # record() writes the one line per request

    def log_message(self, format, *args):
        pass  # http.server would quote the request line


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, address, *, bind=True):
        super().__init__(address, Handler, bind_and_activate=bind)
        self.slots = threading.BoundedSemaphore(CONCURRENCY)
        self.notices = Notices()

    @contextmanager
    def slot(self):
        if not self.slots.acquire(blocking=False):
            raise Refusal(503, "busy")
        try:
            yield
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # The default prints a traceback, which can quote request data.
        log.error("connection_error")


def listening_socket():
    """The one socket systemd passed (sd_listen_fds): fd 3, taken out of the environment of any child."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("expected exactly one socket from systemd; use --socket PATH outside systemd")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    sock = socket.socket(fileno=3)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        raise RuntimeError("the systemd socket must be a Unix stream socket")
    sock.set_inheritable(False)
    return sock


def make_server(path=None):
    """The server on systemd's socket, or on a Unix socket bound at path (tests)."""
    if path is None:
        server = Server(None, bind=False)
        server.socket.close()
        server.socket = listening_socket()
        return server
    path = os.fspath(path)
    try:
        if stat.S_ISSOCK(os.lstat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass
    return Server(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lend PSY's Codex login and owner notices to the co-located platform.")
    parser.add_argument("--socket", metavar="PATH", help="bind this Unix socket instead of systemd's (tests)")
    opts = parser.parse_args(argv)
    output = logging.StreamHandler()
    output.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(output)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        upstream(), telegram_api()
        server = make_server(opts.socket)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"psy-platform-bridge: {exc}\n")
    log.info("listening %s", "on the systemd socket" if opts.socket is None else "on " + opts.socket)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
