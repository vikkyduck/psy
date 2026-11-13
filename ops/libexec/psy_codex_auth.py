"""Check PSY's preferred Codex login. Never print credentials or response bodies."""
import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

PROVIDER = "openai-codex"
BASE = "https://chatgpt.com/backend-api/codex"
USAGE = "https://chatgpt.com/backend-api/wham/usage"
RENEW_BEFORE = 24 * 3600
REJECTED = Path("/var/lib/psy-codex-auth/rejected")


class CheckError(Exception):
    """Only fixed operational messages may be put in this exception."""


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


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


def primary(store):
    from hermes_cli.auth_constants import _decode_jwt_claims
    rows = store.get("credential_pool", {}).get(PROVIDER, [])
    preferred = [r for r in rows if r.get("priority") == 0]
    if len(preferred) != 1:
        raise CheckError("preferred Codex credential missing or ambiguous")
    row = preferred[0]
    if (row.get("source") != "manual:device_code" or row.get("auth_type") != "oauth"
            or (row.get("base_url") or BASE).rstrip("/") != BASE
            or (store.get("providers", {}).get(PROVIDER) or {}).get("tokens")):
        raise CheckError("credential layout changed; review before renewing")
    if row.get("last_status") == "dead" or not row.get("refresh_token"):
        raise CheckError("preferred login needs owner authorization")
    expiry = _decode_jwt_claims(row.get("access_token")).get("exp")
    if not isinstance(expiry, (int, float)) or expiry <= 0:
        raise CheckError("preferred credential has no usable expiration")
    return row, expiry


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
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    from hermes_cli.auth_codex import _entry_is_rate_limit_exhausted, refresh_codex_oauth_pure
    from hermes_cli.auth_constants import AuthError

    row, expiry = primary(_load_auth_store())
    observed = row["access_token"]
    checked = observed
    denied_bearer = REJECTED.with_name("account-denied")
    # When renewal is due it can proceed even if the account-status endpoint is down.
    due = force or expiry - time.time() <= RENEW_BEFORE
    account = None if due else check_provider(observed)
    renewed = False
    if due or account.http == 401:
        with _auth_store_lock(timeout_seconds=35):
            store = _load_auth_store()
            current, expiry = primary(store)
            if (current["id"] == row["id"] and current["access_token"] == observed
                    and current["refresh_token"] == row["refresh_token"]):
                # Hermes has a separate recovery path for an expired quota-limited grant.
                # Do not race it or remove its cooldown.
                if _entry_is_rate_limit_exhausted(current) and expiry <= time.time() + 120:
                    raise CheckError("preferred login expired in cooldown; inspect gateway")
                if not due and denied_bearer.exists() and denied_bearer.read_text() == fingerprint(observed):
                    raise CheckError("Codex account endpoint still returns HTTP 401 after renewal")
                refresh = current["refresh_token"]
                signature = fingerprint(refresh)
                if REJECTED.exists() and REJECTED.read_text() == signature:
                    raise CheckError("OpenAI previously rejected this grant; owner login required")
                try:
                    updated = refresh_codex_oauth_pure(observed, refresh)
                except AuthError as exc:
                    status_code = getattr(exc, "oauth_refresh_status", None)
                    if (exc.relogin_required and status_code in (None, 400, 401, 403)
                            and exc.code in {"invalid_grant", "refresh_token_reused", "refresh_token_expired",
                                             "refresh_token_invalidated", "invalid_token"}):
                        REJECTED.write_text(signature)
                        raise CheckError("OpenAI rejected renewal; owner login required" + renewal_detail(exc)) from None
                    raise CheckError("OpenAI did not complete renewal; retry next check" + renewal_detail(exc)) from None
                # Save the replacement immediately, under the same Hermes lock, keeping quota,
                # identity, priority and the other login intact.
                current.update({k: updated[k] for k in ("access_token", "refresh_token", "last_refresh")})
                _save_auth_store(store)
                renewed = True
        disk, expiry = primary(_load_auth_store())
        if renewed and (disk["id"] != current["id"] or disk["refresh_token"] != current["refresh_token"]):
            raise CheckError("persisted credential changed during renewal; check again")
        checked = disk["access_token"]
        account = check_provider(checked)
        if renewed and account.http == 401:
            # Continue probing hourly, but a fresh grant did not repair this endpoint.
            denied_bearer.write_text(fingerprint(checked))
    if account.auth != "valid":
        raise CheckError(account.summary())
    if denied_bearer.exists() and denied_bearer.read_text() == fingerprint(checked):
        denied_bearer.unlink(missing_ok=True)
    hours = int((expiry - time.time()) // 3600)
    if hours < 1 or (renewed and expiry - time.time() <= RENEW_BEFORE):
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
