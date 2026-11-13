"""Check PSY's preferred Codex login. Never print credentials or response bodies."""
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

PROVIDER = "openai-codex"
USAGE = "https://chatgpt.com/backend-api/wham/usage"


class CheckError(Exception):
    """Only fixed operational messages may be put in this exception."""


def renewal_detail(error):
    status = getattr(error, "oauth_refresh_status", None)
    category = getattr(error, "oauth_refresh_category", None)
    details = []
    if type(status) is int and 100 <= status <= 599:
        details.append(f"HTTP {status}")
    if category in ("timeout", "connect_error", "transport_error", "rate_limited",
                    "invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused",
                    "refresh_token_expired", "refresh_token_invalidated", "edge_challenge",
                    "http_error", "invalid_response"):
        details.append(category)
    return " (" + ", ".join(details) + ")" if details else ""


class AccountCheck(NamedTuple):
    http: Optional[int]
    auth: str
    quota: str
    scoped_limited: bool = False

    def summary(self):
        state = "ok" if self.auth == "valid" and self.quota in ("available", "included_limit_credits_reported") and not self.scoped_limited else "degraded"
        fields = f"state={state} auth={self.auth} quota={self.quota} account_http={self.http or 'unavailable'}"
        return fields + (" scoped_quota=limited" if self.scoped_limited else "")


def quota_status(limit):
    if not isinstance(limit, dict):
        return "unknown"
    if limit.get("limit_reached") is True or limit.get("allowed") is False:
        return "quota_limited"
    has_window = False
    incomplete = False
    for key in ("primary_window", "secondary_window"):
        window = limit.get(key)
        if window is None:
            continue
        used = window.get("used_percent") if isinstance(window, dict) else None
        if type(used) in (int, float) and 0 <= used <= 100:
            if used == 100:
                return "quota_limited"
            has_window = True
        else:
            incomplete = True
    if limit.get("allowed") is True or (has_window and not incomplete):
        return "available"
    return "unknown"


def classify_account(http, body=None):
    if http != 200:
        # HTTP 429 can throttle this monitoring endpoint independently of inference.
        return AccountCheck(http, "rejected" if http == 401 else "unknown", "unknown")
    if not isinstance(body, dict) or not (
            isinstance(body.get("rate_limit"), dict) or isinstance(body.get("plan_type"), str)):
        return AccountCheck(http, "unknown", "unknown")
    extra = body.get("additional_rate_limits")
    scoped_limited = isinstance(extra, list) and any(
        isinstance(item, dict) and quota_status(item.get("rate_limit")) == "quota_limited"
        for item in extra)
    quota = quota_status(body.get("rate_limit"))
    if quota == "quota_limited":
        credits = body.get("credits")
        if isinstance(credits, dict) and (credits.get("has_credits") is True or credits.get("unlimited") is True):
            quota = "included_limit_credits_reported"
        else:
            quota = "included_limit_reached"
    # This endpoint describes account usage. Model rejections are reported by the gateway.
    return AccountCheck(http, "valid", quota, scoped_limited)


def check_provider(access):
    from agent.codex_headers import codex_account_headers
    from hermes_cli.auth_codex import _codex_http_client
    from hermes_cli.auth_constants import httpx
    headers = {"Authorization": "Bearer " + access, "User-Agent": "codex-cli",
               "Accept": "application/json", **codex_account_headers(access)}
    try:
        with _codex_http_client(timeout=12.0) as client:
            response = client.get(USAGE, headers=headers, follow_redirects=False)
    except (httpx.RequestError, OSError):
        return classify_account(None)
    body = None
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            pass
    return classify_account(response.status_code, body)


def maintain(force=False):
    from hermes_cli import psy_codex_policy as policy
    try:
        expiry, renewed, account = policy.account_credentials(check_provider, force=force)
    except policy.CredentialIssue as exc:
        raise CheckError(str(exc) + renewal_detail(exc.__cause__)) from None
    if account.auth != "valid":
        raise CheckError(account.summary())
    hours = int((expiry - time.time()) // 3600)
    if hours < 1 or (renewed and expiry - time.time() <= policy.RENEW_BEFORE):
        raise CheckError("OpenAI returned a short-lived credential; check again")
    print(f"{account.summary()} renewed={renewed} valid_hours={hours}")


if __name__ == "__main__":
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    source = Path("/home/hermes/.hermes/hermes-agent")
    sys.path.insert(0, str(source))
    try:
        import hermes_bootstrap
        if sys.argv[1:] not in ([], ["--renew-now"]):
            raise CheckError("unknown arguments")
        maintain(force=sys.argv[1:] == ["--renew-now"])
    except CheckError as error:
        sys.exit(str(error))
    except Exception as error:
        sys.exit("Codex authorization check failed (" + type(error).__name__ + ")")
