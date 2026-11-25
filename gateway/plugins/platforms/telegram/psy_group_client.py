"""Codex model client for group jobs: connection and sign-in retry, streaming, error classification,
and the checkpointed model call shared by the parallel stages of one job.

Closed interface: no filesystem tools, MCP, callbacks, local execution or previous response IDs."""
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial

from hermes_cli import psy_codex_policy as codex_policy

if __package__:
    from .psy_group_prompts import SYSTEM
else:
    from psy_group_prompts import SYSTEM

ENDPOINT = "https://chatgpt.com/backend-api/codex"
log = logging.getLogger("psy.group")


@dataclass
class Result:
    text: str
    images: list
    searches: int
    complete: bool = True
    source_urls: tuple = ()
    # Set on a fragment kept from a cut-off stream: seconds it ran without visible progress.
    silent: float = field(default=None, compare=False, repr=False)
    # Transport completion is not proof that the participant's task was fulfilled.
    task_status: str = "unknown"
    missing_requirements: list = field(default_factory=list)
    quality_metadata: dict = field(default_factory=dict)
    # Stable provider/transport outcome, independent of semantic task completion.
    stop_reason: str = "unknown"
    stop_stage: str = None
    model_actual: str = None

    @property
    def searched(self):
        return self.searches > 0


# HTTPX measures read timeouts between received bytes, including before the
# first response. A lecture or research job has no deadline across reads/calls.
# At high effort the model may think for minutes before the first byte.
READ_IDLE_SECONDS = {False: 300, True: 300}
# Every group model stage uses the highest supported wire effort of the selected model.
# "ultra" is a Codex application harness with delegation, not a Responses wire level.
QUICK_EFFORT = DEEP_EFFORT = "max"  # compatibility names for the selected gpt-6.1-sol
DEEP_IDLE_SECONDS = 900
# Retained solely for reading old release metadata/tests. Silent cuts no longer lower effort.
SILENT_CUT_SECONDS = 600
FALLBACK_EFFORT = "high"
EFFORT_MARK = "model:effort:v1"
ATTEMPT_AUDIT = "model:audit:v2"


def _maximum_effort(model):
    from agent.reasoning_effort import EFFORT_LADDER, codex_supported_efforts
    supported = codex_supported_efforts(model)
    return max(supported, key=EFFORT_LADDER.index)


# job_progress stage of a deep model call: psy-alerts allows it DEEP_IDLE_SECONDS of silence.
DEEP_PROGRESS_STAGE = "model_deep"


class ModelIssue(Exception):
    MESSAGES = {
        "context": "Материал превысил доступное окно модели. Полный разбор не сформирован.",
        "quota": "Codex отклонил запрос из-за лимита использования. Когда доступ восстановится, повторите запрос.",
        "busy": "Codex временно ограничил частоту запросов. Попробуйте через минуту.",
        "signin": "Codex отклонил вход PSY. Владельцу бота нужно проверить подключение к подписке.",
        "access_wait": "PSY не успел получить доступ к Codex. Попробуйте через минуту.",
        "timeout": "От Codex перестали поступать данные. Повторите запрос.",
        "connection": "Соединение PSY с Codex прервалось до завершения ответа. Повторите запрос.",
        "unavailable": "Codex не завершил ответ. Попробуйте еще раз чуть позже.",
    }
    def __init__(self, kind, stage, reset=None, *, stop_reason=None):
        super().__init__(kind)
        self.kind, self.stage, self.reset = kind, stage, reset
        self.stop_reason = stop_reason or kind
        self.quality_metadata = {}
        self.model_actual = None
        self.http_status = None
        self.provider_error_code = None
        # Seconds an interrupted stream ran without any visible text or reasoning; None otherwise.
        self.silent = None

    @property
    def retryable(self):
        return self.kind in ("signin", "access_wait", "timeout", "connection", "busy", "quota") or (
            self.kind == "unavailable" and self.stage == "upstream")

    def public(self):
        reset = self.reset
        if self.kind == "quota" and type(reset) in (int, float) and time.time() < reset <= time.time() + 32*86400:
            when = datetime.fromtimestamp(reset, timezone(timedelta(hours=3))).strftime("%d.%m в %H:%M")
            return f"Codex отклонил запрос из-за лимита использования. По данным Codex он обновится {when} по Москве. После этого повторите запрос."
        return self.MESSAGES[self.kind]


def _failure(status, detail, stage):
    detail = detail if isinstance(detail, dict) else {}
    codes = (detail.get("code"), detail.get("type"))
    safe_codes = {"usage_limit_reached", "insufficient_quota", "usage_limit_exceeded",
                  "context_length_exceeded", "context_window_exceeded", "input_too_large",
                  "rate_limit_exceeded", "invalid_token", "token_expired", "server_error",
                  "invalid_request_error", "invalid_value", "unsupported_value", "model_not_found",
                  "permission_denied", "account_deactivated", "content_filter"}

    def issue(kind, at=stage, reset=None):
        error = ModelIssue(kind, at, reset)
        error.http_status = status if type(status) is int else None
        error.provider_error_code = next((code for code in codes if isinstance(code, str) and code in safe_codes), None)
        return error

    if status in (408, 504):
        return issue("timeout")
    if any(c in ("usage_limit_reached", "insufficient_quota", "usage_limit_exceeded") for c in codes):
        return issue("quota", reset=detail.get("resets_at"))
    if any(c in ("context_length_exceeded", "context_window_exceeded", "input_too_large") for c in codes):
        return issue("context")
    if status == 429 or "rate_limit_exceeded" in codes:
        return issue("busy")
    if status == 401 or any(c in ("invalid_token", "token_expired") for c in codes):
        return issue("signin")
    return issue("unavailable", "upstream" if type(status) is int and 500 <= status < 600 else stage)


CredentialIssue = codex_policy.CredentialIssue


def get_credentials(rejected=None):
    return codex_policy.model_credentials(rejected)


def _credentials(rejected=None):
    try:
        return get_credentials(rejected)
    except CredentialIssue as exc:
        raise ModelIssue(exc.kind, "credentials") from None


@contextmanager
def _transport():
    import httpx
    try:
        yield
    except httpx.TimeoutException:
        raise ModelIssue("timeout", "transport") from None
    except httpx.RequestError:
        raise ModelIssue("connection", "transport") from None


def _connection(rejected=None):
    from agent.codex_headers import codex_cloudflare_headers
    cred = _credentials(rejected)
    if cred["base_url"].rstrip("/") != ENDPOINT or cred["provider"] != "openai-codex":
        raise RuntimeError("group model route unavailable")
    token = cred["api_key"]
    headers = {**codex_cloudflare_headers(token), "Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    return token, headers, cred["generation"]


def _authenticated_request(request):
    token, headers, generation = _connection()
    try:
        return request(token, headers)
    except ModelIssue as exc:
        # Only a refusal before the stream starts guarantees no accepted work.
        if (exc.kind, exc.stage) != ("signin", "http"):
            raise
    replacement, headers, next_generation = _connection(rejected=generation)
    if replacement != token:
        try:
            return request(replacement, headers)
        except ModelIssue as exc:
            if (exc.kind, exc.stage) != ("signin", "http"):
                raise
    try:
        # If only the grant changed and the bearer is identical, the HTTP evidence
        # belongs to the first generation. A concurrent owner login must survive.
        codex_policy.record_model_refusal(generation if replacement == token else next_generation)
    except CredentialIssue as exc:
        raise ModelIssue(exc.kind, "credentials") from None
    raise ModelIssue("signin", "credentials")


class _ResumeModelJob(Exception):
    """Carry a deferred stage through manuscript editors that normally return partial output."""
    def __init__(self, issue):
        self.issue = issue


class _Halted(Exception):
    """A parallel sibling already ended this job. Stop before the next model call."""


class _Halt:
    """Shared by the stages of one job. After one stage defers or fails the job, no new
    model call starts. Calls in flight finish and keep their checkpoints for the resume."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reason = None

    def trip(self, reason):
        with self._lock:
            if self.reason is None:
                self.reason = reason

    def check(self):
        reason = self.reason
        if reason is None:
            return
        if isinstance(reason, _ResumeModelJob):
            raise _ResumeModelJob(reason.issue)
        raise _Halted()


def _parallel(call, func, items, *, workers=3, tolerate=()):
    """Run independent stages concurrently and return (result, tolerated issue) in input order.

    The first other failure, including a deferred 429, cancels the stages not yet started
    and halts the rest at their next model call; it is then raised for the whole job."""
    halt = getattr(call, "halt", None)
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(func, item) for item in items]
    failure = None
    try:
        for future in as_completed(futures):
            if future.cancelled() or failure is not None:
                continue
            error = future.exception()
            if error is None or isinstance(error, tolerate):
                continue
            failure = error
            if halt is not None:
                halt.trip(error)
            for other in futures:
                other.cancel()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if failure is not None:
        raise failure
    return [(None, f.exception()) if f.exception() is not None else (f.result(), None) for f in futures]


def _request_parameters(model, effort, deep, verbose, research):
    search = {"type": "web_search", "search_context_size": "high"} if deep else {"type": "web_search"}
    parameters = dict(model=model, tools=[search] if research else [], store=False, stream=True,
                      tool_choice="required" if research else "auto")
    if effort:
        parameters["reasoning"] = {"effort": effort}
    if verbose:
        parameters["text"] = {"verbosity": "high"}
    if research:
        parameters["include"] = ["web_search_call.action.sources"]
    return parameters


def _stream(client, headers, turns, research, model, *, instructions=SYSTEM, read_idle=None, save_prefix=None, progress_idle=300, on_progress=None,
            effort=None, deep=True, verbose=False):
    if effort is None:
        effort = _maximum_effort(model)
    if read_idle is None:
        read_idle = DEEP_IDLE_SECONDS if deep else READ_IDLE_SECONDS[bool(research)]
    if deep and progress_idle:
        progress_idle = max(progress_idle, DEEP_IDLE_SECONDS)
    payload = dict(_request_parameters(model, effort, deep, verbose, research),
                   instructions=instructions, input=turns)
    with client.stream("POST", ENDPOINT + "/responses", headers=headers, json=payload,
                       timeout=(10, read_idle, 60, 10)) as response:
        if response.status_code != 200:
            try:
                # Bound the error body and never log it or show it to a chat.
                raw = bytearray()
                for chunk in response.iter_bytes(4096):
                    raw += chunk
                    if len(raw) > 16384:
                        break
                obj = json.loads(raw) if len(raw) <= 16384 else {}
            except (ValueError, UnicodeError):
                obj = {}
            raise _failure(response.status_code, obj.get("error") if isinstance(obj, dict) else None, "http")
        commentary = set()
        searches = 0
        source_urls = set()
        images = []
        written = {}
        final_items = set()
        refused = False
        model_actual = None
        interruption = ModelIssue("connection", "stream_end")
        unsaved_chars = 0
        last_save = time.monotonic() if save_prefix else 0
        last_progress = time.monotonic() if progress_idle else None
        last_report = started = time.monotonic()
        visible = False
        if on_progress:
            on_progress()
        for line in _lines(response):
            if progress_idle and time.monotonic() - last_progress >= progress_idle:
                interruption = ModelIssue("timeout", "model_progress_idle")
                break
            if isinstance(line, ModelIssue):
                interruption = line
                break
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                interruption = ModelIssue("connection", "stream_format")
                break
            if not isinstance(event, dict):
                interruption = ModelIssue("connection", "stream_format")
                break
            typ = event.get("type")
            item = event.get("item") or {}
            item_id = event.get("item_id") or item.get("id") or event.get("output_index", 0)
            content_index = event.get("content_index", 0)
            if typ in ("response.output_item.added", "response.output_item.done") and item.get("phase") == "commentary":
                commentary.add(item_id)
                commentary.add(event.get("output_index", item_id))
            if item_id not in commentary and event.get("delta") and typ in (
                    "response.output_text.delta", "response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                visible = True
                if progress_idle:
                    last_progress = time.monotonic()
                    if on_progress and last_progress - last_report >= 15:
                        on_progress()
                        last_report = last_progress
            if typ == "response.output_item.added" and item.get("type") == "message" and item.get("phase") == "final_answer":
                final_items.add(item_id)
            part = event.get("part") or {}
            if typ in ("response.refusal.delta", "response.refusal.done") or part.get("type") == "refusal":
                refused = True
            if typ == "response.output_text.delta" and item_id not in commentary:
                delta = event.get("delta") or ""
                written.setdefault(item_id, {}).setdefault(content_index, []).append(delta)
                if save_prefix and item_id in final_items and delta:
                    unsaved_chars += len(delta)
                    if unsaved_chars >= 8192 or time.monotonic() - last_save >= 30:
                        # Positive phase identification keeps commentary/reasoning out
                        # of durable drafts. Unlabelled streams checkpoint at completion.
                        prefix = _written({k: v for k, v in written.items() if k in final_items}, commentary)
                        if prefix and not _dispatch_marker(prefix, complete=False):
                            save_prefix(prefix)
                        last_save = time.monotonic()
                        unsaved_chars = 0
            if typ == "response.output_text.done" and event.get("text"):
                written.setdefault(item_id, {})[content_index] = [event["text"]]
            if typ == "response.content_part.done" and part.get("type") == "output_text":
                text = "\n".join(_parts({"content": [part]}))
                if text:
                    written.setdefault(item_id, {})[content_index] = [text]
            if typ == "response.output_item.done":
                if research:
                    source_urls.update(_trace_urls(item))
                if item.get("type") == "web_search_call" and item.get("status") == "completed":
                    searches += 1
                if item.get("type") == "message" and item_id not in commentary:
                    # The completed item can carry text and citations even when
                    # no deltas arrived. Replace this item's deltas exactly once.
                    if any(p.get("type") == "refusal" for p in item.get("content") or ()):
                        refused = True
                    text = "\n".join(_parts(item))
                    if text:
                        written[item_id] = {0: [text]}
            body = event.get("response") or {}
            # Only the provider's model identifier, never arbitrary response metadata.
            actual = body.get("model")
            if isinstance(actual, str) and 0 < len(actual) <= 160 and all(
                    c.isalnum() or c in "-._/:" for c in actual):
                model_actual = actual
            if typ == "response.completed":
                if body.get("status") not in (None, "completed"):
                    raise _failure(None, body.get("error"), "stream_status")
                output = body.get("output") or []
                if research:
                    for item in output:
                        source_urls.update(_trace_urls(item))
                results = [part for item in output if item.get("type") == "message" for part in _parts(item)]
                if research and not searches:
                    searches = sum(i.get("type") == "web_search_call" and i.get("status") == "completed" for i in output)
                    if not searches:
                        raise ModelIssue("unavailable", "search_not_completed")
                text = "\n".join(results)
                if not text.strip():
                    text = _written(written, commentary)
                if not text.strip():
                    raise ModelIssue("unavailable", "stream_empty")
                provider_refusal = refused or any(
                    p.get("type") == "refusal" for item in output for p in item.get("content") or ())
                return Result(text, images, searches, source_urls=tuple(sorted(source_urls)),
                              stop_reason="provider_refusal" if provider_refusal else "completed",
                              stop_stage="stream", model_actual=model_actual)
            if typ == "response.incomplete":
                # Continue only for a documented output ceiling, never after a
                # refusal, a safety block, quota, or a failed provider event.
                reason = (body.get("incomplete_details") or {}).get("reason")
                for item in body.get("output") or ():
                    if any(p.get("type") == "refusal" for p in item.get("content") or ()):
                        refused = True
                if reason == "max_output_tokens" and not refused and (not research or searches):
                    partial = _written(written, commentary)
                    if not partial.strip():
                        partial = "\n".join(part for item in (body.get("output") or []) if item.get("type") == "message"
                                            for part in _parts(item))
                    if partial.strip():
                        return Result(partial, images, searches, False, tuple(sorted(source_urls)),
                                      stop_reason="max_output_tokens", stop_stage="stream",
                                      model_actual=model_actual)
                issue = _failure(None, body.get("error") or event.get("error"), "stream")
                issue.stop_reason = reason if reason in ("max_output_tokens", "content_filter") else "incomplete_unknown"
                if refused:
                    issue.stop_reason = "provider_refusal"
                issue.model_actual = model_actual
                raise issue
            if typ in ("error", "response.failed", "response.incomplete"):
                issue = _failure(None, body.get("error") or event.get("error") or event, "stream")
                issue.model_actual = model_actual
                raise issue
        partial = _written(written, commentary)
        silent = None if visible else time.monotonic() - started
        if partial.strip() and not refused and (not research or searches):
            return Result(partial, images, searches, False, tuple(sorted(source_urls)), silent=silent,
                          stop_reason=interruption.stop_reason, stop_stage=interruption.stage,
                          model_actual=model_actual)
        interruption.silent = silent
        interruption.model_actual = model_actual
        raise interruption


def _lines(response):
    # A transport failure after final-answer deltas may happen before a terminal
    # SSE event. Resume available text. Without output, preserve its error kind.
    try:
        yield from response.iter_lines()
    except Exception as exc:
        import httpx
        if isinstance(exc, httpx.TimeoutException):
            yield ModelIssue("timeout", "stream_idle")
        elif isinstance(exc, httpx.RequestError):
            yield ModelIssue("connection", "stream_read")
        else:
            raise


def _written(written, commentary):
    return "\n".join("".join(deltas) for ident, parts in written.items() if ident not in commentary
                     for _, deltas in sorted(parts.items()))


def _dispatch_marker(text, *, complete):
    marker = "PSY_TASK"
    text = text.strip()
    return text.startswith(marker) or (not complete and bool(text) and marker.startswith(text))


def _trace_urls(item):
    """Provider tool evidence only. Model-generated URLs in prose are not evidence."""
    if item.get("type") == "web_search_call" and item.get("status") == "completed":
        action = item.get("action") or {}
        if action.get("type") in ("open_page", "find_in_page") and action.get("url"):
            yield action["url"]
        for source in action.get("sources") or ():
            if isinstance(source, dict) and source.get("url"):
                yield source["url"]
    elif item.get("type") == "message" and item.get("phase") != "commentary":
        for part in item.get("content") or []:
            for citation in part.get("annotations") or []:
                if citation.get("type") == "url_citation" and citation.get("url"):
                    yield citation["url"]


def _parts(item):
    if item.get("phase") == "commentary":
        return
    for part in item.get("content") or []:
        if part.get("type") in ("output_text", "refusal"):
            text = part.get("text") or part.get("refusal") or ""
            yield text
            seen = set()
            for citation in part.get("annotations") or []:
                link = citation.get("url") or ""
                if citation.get("type") == "url_citation" and link.startswith("https://") and link not in text and link not in seen:
                    yield link
                    seen.add(link)


def _user(text):
    return {"role": "user", "content": text}


def _json_value(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.partition("\n")[2][:-3].strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _stage_key(model, messages, instructions, web=False, prose=True):
    request = json.dumps([model, web, prose, instructions, messages], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode()
    return "model:v1:" + hashlib.sha256(request).hexdigest()


def _job_calls(client, model, checkpoints):
    """Return (call, continuation_call) for the stages of one job.

    With checkpoints, `call` replays completed stages, saves final prose while it streams and,
    after a retryable refusal, halts the sibling stages (`call.halt`) and defers the job.
    `continuation_call` finds one continuation block by its chain digest or legacy prompt."""
    stages = {}
    audit_lock = threading.Lock()
    saved_audit = checkpoints.get(ATTEMPT_AUDIT) if checkpoints is not None else None
    failed_attempts = list(saved_audit.get("failed_attempts", [])) if isinstance(saved_audit, dict) else []

    def record_stage(key, metadata, *, replay=False):
        with audit_lock:
            ident = key if key is not None else "direct:" + str(len(stages))
            stages[ident] = {**metadata, "replayed": replay}

    def audit():
        # No prompts, answers, URLs, member identifiers or credentials. These
        # observations survive only with the private answer, not public logs.
        with audit_lock:
            values = [dict(item) for item in stages.values()]
            failures = [dict(item) for item in failed_attempts]
        efforts = {}
        for item in values:
            effort = item.get("effort", "unknown")
            efforts[effort] = efforts.get(effort, 0) + 1
        completions = [x.get("generation_complete") for x in values]
        complete = (False if any(x is False for x in completions) else
                    True if completions and all(x is True for x in completions) else None)
        generated = [x["generated_at"] for x in values if type(x.get("generated_at")) in (int, float)]
        return {"model_requested": model, "generation_audit": {
            "stages": len(values), "replayed_stages": sum(x.get("replayed", False) for x in values),
            "effort_counts": efforts,
            "fallback_stages": sum(x.get("fallback_used", False) for x in values),
            "prompt_hashes": sorted({x["prompt_sha256"] for x in values if x.get("prompt_sha256")}),
            "unknown_stages": sum(not x.get("generated_at") for x in values),
            "generation_complete": complete,
            "first_generated_at": min(generated) if generated else None,
            "last_generated_at": max(generated) if generated else None,
            "failed_attempts": failures,
            "stage_records": sorted(values, key=lambda item: item.get("started_at") or item.get("generated_at") or 0),
            "effort_policy": "maximum_supported_no_downgrade",
        }}

    def cached(key):
        saved = checkpoints.get(key)
        if saved is not None:
            metadata = saved.get("generation_metadata", {})
            record_stage(key, metadata, replay=True)
            return Result(saved["text"], [], saved["searches"], saved["complete"], tuple(saved["source_urls"]),
                          quality_metadata={"generation": metadata},
                          stop_reason=saved.get("stop_reason", metadata.get("stop_reason", "unknown")),
                          stop_stage=saved.get("stop_stage", metadata.get("stop_stage")),
                          model_actual=saved.get("model_actual", metadata.get("model_actual")))

    halt = _Halt()

    def call(messages, instructions, *, web=False, prose=False, key=None):
        # Replaying completed stages reconstructs the plan, source cards and edits locally.
        if checkpoints is not None and key is None:
            key = _stage_key(model, messages, instructions, web, prose)
            saved = cached(key)
            if saved is not None:
                return saved
        halt.check()

        def save(result):
            checkpoints.put(key, {"text": result.text, "searches": result.searches,
                                  "complete": result.complete, "source_urls": list(result.source_urls),
                                  "generation_metadata": result.quality_metadata.get("generation", {}),
                                  "stop_reason": result.stop_reason, "stop_stage": result.stop_stage,
                                  "model_actual": result.model_actual})

        effort = _maximum_effort(model)
        deep, verbose = True, bool(prose)
        parameters = _request_parameters(model, effort, deep, verbose, web)
        request_bytes = json.dumps(dict(parameters, instructions=instructions, input=messages),
                                   ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        metadata = {"model_requested": model, "effort": effort, "deep": deep,
                    "web": web, "prose": prose, "fallback_used": False,
                    "generation_complete": False, "started_at": time.time(),
                    "prompt_sha256": hashlib.sha256(instructions.encode()).hexdigest(),
                    "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                    "effective_parameters": parameters,
                    "transport": {"connect_seconds": 10, "read_idle_seconds": DEEP_IDLE_SECONDS,
                                  "write_seconds": 60, "pool_seconds": 10,
                                  "progress_idle_seconds": DEEP_IDLE_SECONDS},
                    "stop_reason": "in_progress", "stop_stage": "stream"}
        saved_prefix = False

        def save_prefix(text):
            nonlocal saved_prefix
            saved_metadata = dict(metadata, stop_reason="process_interrupted")
            save(Result(text, [], 0, False, quality_metadata={"generation": saved_metadata},
                        stop_reason="process_interrupted", stop_stage="stream"))
            saved_prefix = True

        options = {"save_prefix": save_prefix} if prose and checkpoints is not None else {}
        if checkpoints is not None:
            report = getattr(checkpoints, "progress", None)
            if report is not None:
                options["on_progress"] = partial(report, DEEP_PROGRESS_STAGE)
        started = time.monotonic()
        try:
            with _transport():
                result = _authenticated_request(lambda token, headers: _stream(
                    client, headers, messages, web, model, instructions=instructions,
                    effort=effort, deep=deep, verbose=verbose, **options))
        except ModelIssue as exc:
            failure = dict(metadata, generated_at=time.time(),
                           duration_seconds=max(0, time.monotonic() - started),
                           stop_reason=exc.stop_reason, stop_stage=exc.stage,
                           error_kind=exc.kind, retryable=exc.retryable, model_actual=exc.model_actual,
                           http_status=exc.http_status, provider_error_code=exc.provider_error_code,
                           silent_seconds=(exc.silent if exc.silent is not None else
                                           max(0, time.monotonic() - started) if exc.stage == "transport" else None))
            with audit_lock:
                failed_attempts.append(failure)
                if checkpoints is not None:
                    checkpoints.put(ATTEMPT_AUDIT, {"failed_attempts": failed_attempts})
            exc.quality_metadata.update(audit())
            if checkpoints is not None and exc.retryable:
                # Park in the durable queue with backoff. Never retry inline, switch
                # models or weaken effort after a silent cut.
                deferred = _ResumeModelJob(exc)
                halt.trip(deferred)
                raise deferred from None
            # A terminal error invalidates speculative prose. Process interruption
            # and deferred transport errors still leave a prefix to resume from.
            if saved_prefix:
                checkpoints.put(key, None)
            raise
        metadata.update(model_actual=result.model_actual, generation_complete=result.complete,
                        generated_at=time.time(), duration_seconds=max(0, time.monotonic() - started),
                        stop_reason=result.stop_reason, stop_stage=result.stop_stage, silent_seconds=result.silent)
        result.quality_metadata["generation"] = metadata
        record_stage(key, metadata)
        if checkpoints is not None and (result.complete or (prose and result.text and not _dispatch_marker(result.text, complete=False))):
            save(result)
        return result

    def continuation_call(messages, instructions, digest, legacy_prompt):
        key = "prose:v2:" + digest
        saved = cached(key)
        if saved is not None:
            # Legacy results are left under their v1 keys, so a v2 hit is always new.
            return saved, False
        if legacy_prompt is not None:
            saved = cached(_stage_key(model, legacy_prompt(), instructions))
            if saved is not None:
                return saved, True
        return call(messages, instructions, prose=True, key=key), False

    call.halt = halt
    call.audit = audit
    # Kept for callers that mark long jobs. Maximum effort applies from the first stage.
    call.deep = True
    return call, continuation_call
