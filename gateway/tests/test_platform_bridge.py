"""The platform bridge over a real Unix socket, against loopback stand-ins for Codex and Telegram."""
import base64
import http.client
import http.server
import importlib
import io
import json
import logging
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from hermes_cli import psy_platform_bridge as bridge
from psy_modules import psy_group_codex as group

try:  # Loaded before any test swaps sys.modules. The Hermes runtime ships Pillow, a bare system Python may not.
    from PIL import Image, ImageOps, PngImagePlugin  # noqa: F401
except ImportError:
    Image = None

IMAGES = importlib.import_module("plugins.image_gen.openai-codex")
CHECKOUT = Path(bridge.__file__).resolve().parents[1]
ME = pwd.getpwuid(os.getuid()).pw_name
LIVE = {"provider": "openai-codex", "base_url": bridge.ENDPOINT, "api_key": "live-bearer-fixture",
        "generation": "generation-live"}
RENEWED = dict(LIVE, api_key="renewed-bearer-fixture", generation="generation-renewed")
MINIMAL = {"model": "gpt-6.1-sol", "instructions": "Be brief.", "input": "6 * 7?"}
STREAM = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
          b": keep-alive\n\n"
          + 'data: {"type":"response.output_text.delta","delta":"Привет, мир"}\r\n\r\n'.encode()
          + b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
          + b"data: [DONE]\n\n")
RESET = 1893456000


def sse(*chunks):
    return 200, "text/event-stream", chunks


def answer(status, document):
    return status, "application/json", [json.dumps(document).encode()]


def png(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode()


class Recorder(http.server.ThreadingHTTPServer):
    """A loopback stand-in that records every request and plays queued replies, chunked."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Playback)
        self.seen, self.replies, self.default = [], [], None
        self.release = threading.Event()
        threading.Thread(target=self.serve_forever, args=(0.02,), daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.release.set()
        self.shutdown()
        self.server_close()

    def handle_error(self, request, client_address):
        pass  # a stalled reply finds its client gone after the test


class Playback(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.seen.append((self.path, self.headers, body))
        reply = self.server.replies.pop(0) if self.server.replies else self.server.default
        if callable(reply):
            reply(self)
            return
        status, kind, chunks = reply
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.write_chunk(chunk)
        self.wfile.write(b"0\r\n\r\n")

    def write_chunk(self, chunk):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
        self.wfile.flush()

    def log_message(self, *args):
        pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("bridge.invalid", timeout=10)
        self.unix_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PlatformBridge(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        (self.home / ".env").write_text('TELEGRAM_BOT_TOKEN="123456:fixture"\n')
        self.codex, self.telegram = Recorder(), Recorder()
        self.addCleanup(self.codex.stop)
        self.addCleanup(self.telegram.stop)
        self.telegram.default = answer(200, {"ok": True, "result": {"message_id": 7, "chat": {"id": 101}}})
        self.enterContext(patch.dict(os.environ, {
            "PSY_PLATFORM_UPSTREAM": self.codex.base + "/backend-api/codex",
            "PSY_PLATFORM_TELEGRAM_API": self.telegram.base, "PSY_PLATFORM_PEERS": ME,
            "PSY_OWNER_ID": "101", "HERMES_HOME": str(self.home)}))
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        headers = ModuleType("agent.codex_headers")
        headers.codex_cloudflare_headers = lambda token: {"originator": "psy-fixture", "ChatGPT-Account-ID": "account-fixture"}
        self.enterContext(patch.dict(sys.modules, {"agent.codex_headers": headers}))
        self.credentials = self.enterContext(patch.object(bridge.codex_policy, "model_credentials", side_effect=self.admit))
        self.refused = self.enterContext(patch.object(bridge.codex_policy, "record_model_refusal"))
        self.capture = Capture()
        bridge.log.addHandler(self.capture)
        self.addCleanup(bridge.log.removeHandler, self.capture)
        self.addCleanup(bridge.log.setLevel, bridge.log.level)
        bridge.log.setLevel(logging.INFO)
        self.path = self.home / "bridge.sock"
        self.server = bridge.make_server(self.path)
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def admit(self, rejected=None):
        if rejected is None:
            return dict(LIVE)
        self.assertEqual(rejected, LIVE["generation"])
        return dict(RENEWED)

    def call(self, method, path, body=None, *, data=None):
        conn = UnixConnection(self.path)
        try:
            if data is None and body is not None:
                data = json.dumps(body).encode()
            conn.request(method, path, body=data)
            response = conn.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            conn.close()

    def json_call(self, method, path, body=None, **options):
        status, _, data = self.call(method, path, body, **options)
        return status, json.loads(data)

    def logged(self, fragment):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(fragment in line for line in self.capture.lines):
                return True
            time.sleep(0.01)
        return False

    def bearers(self):
        return [headers["Authorization"] for _, headers, _ in self.codex.seen]

    # ---------- /responses ----------

    def test_sse_is_relayed_byte_for_byte(self):
        # Cut mid-line and mid-character: only the bytes matter, never the event boundaries.
        self.codex.replies.append(sse(*(STREAM[i:i + 7] for i in range(0, len(STREAM), 7))))
        request = {"model": "gpt-6.1-sol", "instructions": "Отвечай кратко.",
                   "input": [{"role": "user", "content": "Привет"}], "tools": [{"type": "web_search"}],
                   "tool_choice": "required", "include": ["web_search_call.action.sources"],
                   "reasoning": {"effort": "high", "summary": "auto"}, "text": {"verbosity": "low"},
                   "prompt_cache_key": "kotu-fixture", "parallel_tool_calls": False, "stream": True}
        self.assertEqual(self.call("POST", "/responses", request), (200, "text/event-stream", STREAM))
        path, headers, body = self.codex.seen[0]
        self.assertEqual(path, "/backend-api/codex/responses")
        self.assertEqual(json.loads(body), dict(request, stream=True, store=False))
        self.assertEqual((headers["Authorization"], headers["Accept"], headers["Accept-Encoding"], headers["originator"]),
                         ("Bearer live-bearer-fixture", "text/event-stream", "identity", "psy-fixture"))
        self.credentials.assert_called_once_with(None)
        self.assertTrue(self.logged("request method=POST path=/responses status=200"))
        self.assertFalse([line for line in self.capture.lines if "bearer" in line or "Привет" in line or "data:" in line])

    def test_payload_outside_the_accepted_subset_is_refused(self):
        for wrong in ({"temperature": 0.2}, {"previous_response_id": "resp_1"}, {"tools": [{"type": "web_search", "filters": {}}]},
                      {"tools": [{"type": "code_interpreter"}]}, {"tools": {"type": "web_search"}}, {"model": "gpt 6"},
                      {"model": "x" * 65}, {"model": 6}, {"instructions": None}, {"input": {"role": "user"}},
                      {"tool_choice": "any"}, {"include": ["file_search_call.results"]}, {"reasoning": {"effort": "max"}},
                      {"reasoning": {"summary": "auto"}}, {"reasoning": {"effort": "low", "budget": 1}},
                      {"text": "plain"}, {"prompt_cache_key": "k" * 65}, {"parallel_tool_calls": 1},
                      {"stream": False}, {"store": True}):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.json_call("POST", "/responses", {**MINIMAL, **wrong}),
                                 (400, {"error": {"kind": "bad_request"}}))
        for missing in MINIMAL:
            body = {key: value for key, value in MINIMAL.items() if key != missing}
            self.assertEqual(self.json_call("POST", "/responses", body)[0], 400)
        for data in (b"{", b"[]", b"", b'{"model": NaN, "instructions": "", "input": ""}',
                     b'{"model": "m", "instructions": "", "input": "\\ud800"}'):
            with self.subTest(data=data):
                self.assertEqual(self.json_call("POST", "/responses", data=data), (400, {"error": {"kind": "bad_request"}}))
        self.credentials.assert_not_called()
        self.assertEqual(self.codex.seen, [])

    def test_refusals_before_the_stream_are_classified(self):
        cases = ((answer(429, {"error": {"type": "usage_limit_reached", "message": "fixture", "resets_at": RESET}}),
                  429, {"kind": "quota", "status": 429, "resets_at": RESET}),
                 (answer(400, {"error": {"code": "context_length_exceeded"}}), 400, {"kind": "context", "status": 400, "resets_at": None}),
                 (answer(429, {"error": {"code": "rate_limit_exceeded"}}), 429, {"kind": "busy", "status": 429, "resets_at": None}),
                 ((503, "text/html", [b"<html>upstream connect error</html>"]), 503,
                  {"kind": "unavailable", "status": 503, "resets_at": None}),
                 (answer(504, {}), 504, {"kind": "timeout", "status": 504, "resets_at": None}))
        for reply, status, error in cases:
            with self.subTest(error=error):
                self.codex.replies.append(reply)
                self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (status, {"error": error}))
        self.assertEqual(self.bearers(), ["Bearer live-bearer-fixture"] * len(cases))
        self.refused.assert_not_called()
        self.assertTrue(self.logged("status=429 ms="))
        self.assertTrue(self.logged("kind=quota"))

    def test_classification_matches_the_group_module(self):
        details = (None, "text", {}, {"code": ["x"]}, {"type": "usage_limit_reached", "resets_at": RESET},
                   {"code": "insufficient_quota"}, {"code": "usage_limit_exceeded", "resets_at": "soon"},
                   {"code": "context_length_exceeded"}, {"type": "context_window_exceeded"}, {"code": "input_too_large"},
                   {"code": "rate_limit_exceeded"}, {"code": "invalid_token"}, {"type": "token_expired"})
        for status in (400, 401, 403, 404, 408, 429, 500, 502, 503, 504):
            for detail in details:
                with self.subTest(status=status, detail=detail):
                    kind, reset = bridge.classify(status, detail)
                    issue = group._failure(status, detail, "http")
                    self.assertEqual(kind, issue.kind)
                    self.assertEqual(reset, issue.reset if type(issue.reset) is int else None)

    def test_401_is_retried_once_with_a_fresh_login(self):
        self.codex.replies += [answer(401, {"error": {"code": "token_expired"}}), sse(STREAM)]
        self.assertEqual(self.call("POST", "/responses", MINIMAL), (200, "text/event-stream", STREAM))
        self.assertEqual(self.bearers(), ["Bearer live-bearer-fixture", "Bearer renewed-bearer-fixture"])
        self.assertEqual([call.args for call in self.credentials.call_args_list], [(None,), ("generation-live",)])
        self.refused.assert_not_called()

    def test_second_401_answers_signin_and_records_the_refusal(self):
        self.codex.replies += [answer(401, {}), answer(401, {"error": {"code": "invalid_token"}})]
        self.assertEqual(self.json_call("POST", "/responses", MINIMAL),
                         (401, {"error": {"kind": "signin", "status": 401, "resets_at": None}}))
        self.assertEqual(self.bearers(), ["Bearer live-bearer-fixture", "Bearer renewed-bearer-fixture"])
        self.refused.assert_called_once_with("generation-renewed")
        # The policy kept the bearer and only the grant changed: no replay, the evidence is the first generation's.
        self.refused.reset_mock()
        self.credentials.side_effect = lambda rejected=None: dict(LIVE, generation=rejected and "generation-grant" or "generation-live")
        self.codex.replies.append(answer(401, {}))
        self.assertEqual(self.json_call("POST", "/responses", MINIMAL)[1]["error"]["kind"], "signin")
        self.assertEqual(len(self.codex.seen), 3)
        self.refused.assert_called_once_with("generation-live")
        # A renewal the policy refuses answers with its kind; nothing more reaches Codex.
        self.credentials.side_effect = [dict(LIVE), bridge.codex_policy.CredentialIssue("access_wait", "fixture")]
        self.codex.replies.append(answer(401, {}))
        self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (503, {"error": {"kind": "access_wait"}}))
        self.assertEqual(len(self.codex.seen), 4)

    def test_credential_and_transport_failures(self):
        self.credentials.side_effect = bridge.codex_policy.CredentialIssue("signin", "owner login required")
        self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (503, {"error": {"kind": "signin"}}))
        self.assertEqual(self.codex.seen, [])
        self.credentials.side_effect = self.admit
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with patch.dict(os.environ, {"PSY_PLATFORM_UPSTREAM": f"http://127.0.0.1:{closed}/backend-api/codex"}):
            self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (502, {"error": {"kind": "connection"}}))
        first = STREAM[:40]

        def partial(handler):
            handler.send_response(200)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            handler.write_chunk(first)
            handler.server.release.wait(10)

        self.codex.replies += [lambda handler: handler.server.release.wait(10), partial]
        with patch.object(bridge, "READ_IDLE_SECONDS", 0.3):
            self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (504, {"error": {"kind": "timeout"}}))
            # Once the stream has started only its bytes are left to give: the cut is the end of the body.
            self.assertEqual(self.call("POST", "/responses", MINIMAL), (200, "text/event-stream", first))
        self.assertTrue(self.logged("path=/responses status=200"))
        self.assertTrue(any("status=200" in line and "kind=timeout" in line for line in self.capture.lines))

    def test_endpoint_overrides_stay_on_loopback(self):
        for value in ("https://127.0.0.1:1/x", "http://chatgpt.com/backend-api/codex", "http://127.0.0.1.example/x",
                      "http://user:pw@127.0.0.1/x", "http://127.0.0.1:0/x"):
            with self.subTest(value=value), patch.dict(os.environ, {"PSY_PLATFORM_UPSTREAM": value}):
                with self.assertRaises(ValueError):
                    bridge.upstream()
        with patch.dict(os.environ, {"PSY_PLATFORM_UPSTREAM": "", "PSY_PLATFORM_TELEGRAM_API": ""}):
            self.assertEqual((bridge.upstream(), bridge.telegram_api()), (bridge.ENDPOINT, "https://api.telegram.org"))

    # ---------- access, health and limits ----------

    def test_peer_outside_the_allowed_accounts_is_refused(self):
        with patch.object(bridge, "allowed_uids", return_value={os.getuid() + 1}):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(10)
                client.connect(str(self.path))
                try:
                    client.sendall(b"GET /health HTTP/1.1\r\nHost: bridge\r\n\r\n")
                except OSError:
                    pass  # the refusal may already be on its way
                data = b""
                try:
                    while chunk := client.recv(4096):
                        data += chunk
                except ConnectionResetError:
                    pass
        self.assertTrue(data.startswith(b"HTTP/1.1 403 Forbidden\r\n"), data)
        self.assertTrue(data.endswith(b'\r\n\r\n{"error":{"kind":"forbidden"}}'), data)
        self.assertTrue(self.logged("request method=- path=- status=403 ms="))
        self.credentials.assert_not_called()
        with patch.dict(os.environ, {"PSY_PLATFORM_PEERS": f" {ME} ,no-such-psy-account"}):
            self.assertEqual(bridge.allowed_uids(), {0, os.getuid()})
        with patch.dict(os.environ, {"PSY_PLATFORM_PEERS": ""}):
            self.assertEqual(bridge.allowed_uids(), {0})
        left, right = socket.socketpair()
        with left, right:
            self.assertEqual(bridge.peer_uid(left), os.getuid())

    def test_health_and_routes(self):
        self.assertEqual(self.json_call("GET", "/health"), (200, {"ok": True}))
        self.credentials.assert_not_called()
        self.assertEqual(self.json_call("GET", "/health?deep=1"), (200, {"ok": True, "codex": "ready"}))
        self.credentials.assert_called_once_with(None)
        self.credentials.side_effect = bridge.codex_policy.CredentialIssue("access_wait", "store busy")
        self.assertEqual(self.json_call("GET", "/health?deep=1"), (503, {"ok": False, "codex": "access_wait"}))
        self.assertEqual(self.codex.seen, [])
        for method, path, status, kind in (("POST", "/health", 405, "method_not_allowed"),
                                           ("GET", "/responses", 405, "method_not_allowed"),
                                           ("PUT", "/notify", 405, "method_not_allowed"),
                                           ("GET", "/secret?token=1", 404, "not_found")):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.json_call(method, path, {}), (status, {"error": {"kind": kind}}))
        self.assertTrue(self.logged("request method=GET path=- status=404"))
        self.assertFalse([line for line in self.capture.lines if "secret" in line or "deep" in line])

    def test_concurrency_and_size_limits(self):
        for _ in range(bridge.CONCURRENCY):
            self.server.slots.acquire()
        try:
            self.assertEqual(self.json_call("POST", "/responses", MINIMAL), (503, {"error": {"kind": "busy"}}))
            self.assertEqual(self.json_call("GET", "/health?deep=1"), (503, {"error": {"kind": "busy"}}))
            self.assertEqual(self.json_call("GET", "/health"), (200, {"ok": True}))
        finally:
            for _ in range(bridge.CONCURRENCY):
                self.server.slots.release()
        conn = UnixConnection(self.path)
        conn.putrequest("POST", "/responses")
        conn.putheader("Content-Length", str(bridge.MAX_BODY + 1))
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual((response.status, json.loads(response.read())), (413, {"error": {"kind": "too_large"}}))
        conn.close()
        self.assertEqual(self.json_call("POST", "/notify", data=iter([b'{"text": "chunked"}'])),
                         (411, {"error": {"kind": "length_required"}}))
        self.credentials.assert_not_called()
        self.assertEqual(self.telegram.seen, [])

    def test_main_serves_the_socket_systemd_passes(self):
        path = self.home / "activated.sock"
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.bind(str(path))
        listener.listen()
        code = "import sys; sys.path.insert(0, sys.argv[1]); from hermes_cli import psy_platform_bridge as b; b.main([])"
        # As systemd does it: the socket as fd 3, LISTEN_PID naming the process that runs the bridge.
        script = 'eval "exec 3<&$0"; export LISTEN_PID=$$ LISTEN_FDS=1; exec "$@"'
        child = subprocess.Popen(["/bin/sh", "-c", script, str(listener.fileno()), sys.executable, "-I", "-c", code,
                                  str(CHECKOUT)], pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                              "PSY_PLATFORM_PEERS": ME})
        try:
            conn = UnixConnection(path)
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())), (200, {"ok": True}))
            conn.close()
        finally:
            child.terminate()
            _, errors = child.communicate(timeout=10)
        self.assertIn(b"INFO listening on the systemd socket", errors)
        self.assertIn(b"INFO request method=GET path=/health status=200", errors)

    # ---------- /images ----------

    @unittest.skipIf(Image is None, "Pillow is part of the Hermes runtime, not of this Python")
    def test_image_is_center_cropped_and_scaled_to_the_exact_size(self):
        source = Image.new("RGB", (1536, 1024), (0, 160, 0))
        source.paste((200, 0, 0), (0, 0, 1536, 80))
        source.paste((0, 0, 200), (0, 944, 1536, 1024))
        self.codex.replies.append(answer(200, {"data": [{"b64_json": png(source)}], "size": "1536x1024"}))
        status, document = self.json_call("POST", "/images", {"prompt": "Круг на зеленом фоне"})
        self.assertEqual(status, 200)
        self.assertEqual({key: document[key] for key in ("mime", "width", "height", "upstream_size")},
                         {"mime": "image/png", "width": 1536, "height": 864, "upstream_size": "1536x1024"})
        with Image.open(io.BytesIO(base64.b64decode(document["b64"]))) as result:
            self.assertEqual((result.format, result.size), ("PNG", (1536, 864)))
            corners = {result.getpixel(point) for point in ((0, 0), (1535, 0), (768, 432), (0, 863), (1535, 863))}
        self.assertEqual(corners, {(0, 160, 0)})  # both bands fell outside the centered crop
        path, headers, body = self.codex.seen[0]
        self.assertEqual(path, "/backend-api/codex/images/generations")
        expected = IMAGES._build_image_request(prompt="Круг на зеленом фоне", size="1536x1024", quality="medium")
        self.assertEqual(("/backend-api/codex/" + expected[0], json.loads(body)), (path, expected[1]))
        self.assertEqual(json.loads(body)["model"], "gpt-image-2")
        self.assertEqual((headers["Authorization"], headers["originator"]), ("Bearer live-bearer-fixture", "psy-fixture"))
        self.assertRegex(headers["x-codex-image-turn-id"], r"^[0-9a-f-]{36}$")
        for target, requested, delivered in (("864x1536", "1024x1536", (1024, 1536)), ("512x512", "1024x1024", (1024, 1024)),
                                             ("1536x864", "1536x1024", (1024, 1024))):
            with self.subTest(target=target):
                self.codex.replies.append(answer(200, {"data": [{"b64_json": png(Image.new("P", delivered))}]}))
                status, document = self.json_call("POST", "/images", {"prompt": "p", "size": target, "quality": "low"})
                width, height = map(int, target.split("x"))
                self.assertEqual((status, document["width"], document["height"], document["upstream_size"]),
                                 (200, width, height, "%dx%d" % delivered))
                with Image.open(io.BytesIO(base64.b64decode(document["b64"]))) as result:
                    self.assertEqual(result.size, (width, height))
                sent = json.loads(self.codex.seen[-1][2])
                self.assertEqual((sent["size"], sent["quality"]), (requested, "low"))

    def test_image_refusals_retry_and_validation(self):
        self.codex.replies.append(answer(429, {"error": {"code": "usage_limit_reached", "resets_at": RESET}}))
        self.assertEqual(self.json_call("POST", "/images", {"prompt": "p"}),
                         (429, {"error": {"kind": "quota", "status": 429, "resets_at": RESET}}))
        fixture = base64.b64encode(b"fixture image").decode()
        self.codex.replies += [answer(401, {"error": {"code": "token_expired"}}), answer(200, {"data": [{"b64_json": fixture}]})]
        with patch.object(bridge, "fit_png", return_value=(b"fitted png", "1024x1024")) as fit:
            status, document = self.json_call("POST", "/images", {"prompt": "p", "size": "1024x1024"})
        self.assertEqual((status, base64.b64decode(document["b64"]), document["upstream_size"]), (200, b"fitted png", "1024x1024"))
        fit.assert_called_once_with(b"fixture image", 1024, 1024)
        self.assertEqual(self.bearers()[1:], ["Bearer live-bearer-fixture", "Bearer renewed-bearer-fixture"])
        self.refused.assert_not_called()
        self.codex.replies.append(answer(200, {"data": []}))
        self.assertEqual(self.json_call("POST", "/images", {"prompt": "p"}), (502, {"error": {"kind": "unavailable"}}))
        seen = len(self.codex.seen)
        for wrong in ({}, {"prompt": ""}, {"prompt": "   "}, {"prompt": "p" * 32001}, {"prompt": 5},
                      {"prompt": "p", "size": "1000x504"}, {"prompt": "p", "size": "240x240"}, {"prompt": "p", "size": "4000x2000"},
                      {"prompt": "p", "size": "01536x864"}, {"prompt": "p", "size": 1536}, {"prompt": "p", "quality": "hd"},
                      {"prompt": "p", "n": 2}):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.json_call("POST", "/images", wrong), (400, {"error": {"kind": "bad_request"}}))
        self.assertEqual(len(self.codex.seen), seen)

    # ---------- /notify ----------

    def test_owner_notice_is_sent_and_a_repeat_is_held_for_ten_minutes(self):
        now = [5000.0]
        self.enterContext(patch.object(bridge, "clock", lambda: now[0]))
        notice = {"text": "Платформа: новая заявка"}
        self.assertEqual(self.json_call("POST", "/notify", notice), (200, {"sent": True}))
        path, _, body = self.telegram.seen[0]
        self.assertEqual(path, "/bot123456:fixture/sendMessage")
        self.assertEqual(parse_qs(body.decode()), {"chat_id": ["101"], "text": [notice["text"]], "protect_content": ["true"]})
        now[0] += 599
        self.assertEqual(self.json_call("POST", "/notify", notice), (200, {"sent": False, "reason": "duplicate"}))
        self.assertEqual(self.json_call("POST", "/notify", {"text": "Другая заявка"}), (200, {"sent": True}))
        now[0] += 1
        self.assertEqual(self.json_call("POST", "/notify", notice), (200, {"sent": True}))
        self.assertEqual(len(self.telegram.seen), 3)
        self.assertTrue(self.logged("path=/notify status=200 ms="))
        self.assertTrue(self.logged("kind=duplicate"))
        self.assertFalse([line for line in self.capture.lines if "заявка" in line or "fixture" in line])

    def test_hourly_cap_counts_every_attempt(self):
        now = [5000.0]
        self.enterContext(patch.object(bridge, "clock", lambda: now[0]))
        self.telegram.replies.append(answer(500, {"ok": False, "error_code": 500}))
        self.assertEqual(self.json_call("POST", "/notify", {"text": "notice 0"}), (502, {"sent": False, "reason": "telegram"}))
        for number in range(bridge.HOURLY_CAP - 1):
            self.assertEqual(self.json_call("POST", "/notify", {"text": f"notice {number}"}), (200, {"sent": True}))
        self.assertEqual(self.json_call("POST", "/notify", {"text": "one more"}), (429, {"sent": False, "reason": "rate_limited"}))
        self.assertEqual(len(self.telegram.seen), bridge.HOURLY_CAP)
        now[0] += 3600
        self.assertEqual(self.json_call("POST", "/notify", {"text": "one more"}), (200, {"sent": True}))

    def test_undelivered_or_unconfigured_notice(self):
        self.telegram.replies += [answer(400, {"ok": False, "description": "chat not found"}),
                                  answer(200, {"ok": True, "result": {"chat": {"id": 999}}}),
                                  (200, "application/json", [b"not json"])]
        for _ in range(3):
            self.assertEqual(self.json_call("POST", "/notify", {"text": "x"}), (502, {"sent": False, "reason": "telegram"}))
        # An undelivered text is not a duplicate.
        self.assertEqual(self.json_call("POST", "/notify", {"text": "x"}), (200, {"sent": True}))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with patch.dict(os.environ, {"PSY_PLATFORM_TELEGRAM_API": f"http://127.0.0.1:{closed}"}):
            self.assertEqual(self.json_call("POST", "/notify", {"text": "y"}), (502, {"sent": False, "reason": "telegram"}))
        (self.home / ".env").write_text("OTHER=1\n")
        self.assertEqual(self.json_call("POST", "/notify", {"text": "z"}), (503, {"sent": False, "reason": "unconfigured"}))
        (self.home / ".env").unlink()
        (self.home / ".env").symlink_to(self.home / "elsewhere")
        (self.home / "elsewhere").write_text("TELEGRAM_BOT_TOKEN=123456:linked\n")
        self.assertEqual(self.json_call("POST", "/notify", {"text": "z"})[0], 503)
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:environment", "PSY_OWNER_ID": "0"}):
            self.assertEqual(self.json_call("POST", "/notify", {"text": "z"})[0], 503)
        self.assertEqual(len(self.telegram.seen), 4)
        for wrong in ({"text": ""}, {"text": " \n"}, {"text": "x" * 3501}, {"text": 5}, {"text": "x", "chat_id": 1}, {}):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.json_call("POST", "/notify", wrong), (400, {"error": {"kind": "bad_request"}}))
        self.assertEqual(self.json_call("POST", "/notify", data=b'{"text": "\\udc80"}'), (400, {"error": {"kind": "bad_request"}}))
        self.assertEqual(len(self.telegram.seen), 4)


if __name__ == "__main__":
    unittest.main()
