"""PSY's preferred ChatGPT login and single-use OAuth renewal policy."""
import hashlib
import json
import math
from pathlib import Path
import time

PROVIDER = "openai-codex"
BASE = "https://chatgpt.com/backend-api/codex"
REJECT_CODES = {"invalid_grant", "invalid_token", "refresh_token_expired", "refresh_token_reused",
                "refresh_token_invalidated"}


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class CredentialIssue(Exception):
    def __init__(self, kind, detail, *, may_use_current=False):
        super().__init__(detail)
        self.kind = kind
        self.may_use_current = may_use_current


def terminal(error):
    return (getattr(error, "relogin_required", False) and getattr(error, "oauth_refresh_status", None) in (400, 401, 403)
            and getattr(error, "code", None) in REJECT_CODES)


def block_error(code):
    from hermes_cli.auth_constants import AuthError
    return AuthError("Codex renewal temporarily unavailable" if code == "psy_refresh_wait" else
                     "Codex grant requires a fresh owner login", provider=PROVIDER, code=code)


def refresh_outcome(error):
    """Only retry an exchange known not to have consumed its rotating grant."""
    from hermes_cli.auth_constants import httpx

    if terminal(error):
        return "rejected"
    if isinstance(error, (httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ConnectError)):
        return "wait"
    status = getattr(error, "oauth_refresh_status", None)
    if status in (400, 401, 403, 404, 405, 415, 429) or (status == 503 and getattr(error, "code", None) == "temporarily_unavailable"):
        return "wait"
    return "uncertain"


def guarded_exchange(grant, exchange):
    """Journal intent before the POST. A crash or partial response must never replay a grant.

    All pinned Hermes Codex entry points invoke this below their usual auth-store lock.
    The private journal stores only state and timestamps. A distinct login has a distinct key.
    """
    from hermes_cli.auth import _auth_file_path, _global_auth_file_path, _auth_store_lock, _save_private_json

    owner = _auth_file_path()
    root = _global_auth_file_path()
    if root is not None:
        from agent.credential_pool import _profile_owns_pool_provider
        if not _profile_owns_pool_provider(PROVIDER):
            owner = root
    directory = owner.parent / "psy-codex-renewals"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (fingerprint(grant) + ".json")
    with _auth_store_lock(timeout_seconds=35, target_path=path):
        absent = object()
        try:
            previous = json.loads(path.read_bytes())
        except FileNotFoundError:
            previous = absent
        except (ValueError, TypeError):
            raise block_error("psy_refresh_uncertain") from None
        state = previous.get("state") if isinstance(previous, dict) else None
        if state == "wait":
            retry_at = previous.get("retry_at")
            if type(retry_at) not in (int, float) or not math.isfinite(retry_at):
                raise block_error("psy_refresh_uncertain")
            if retry_at > time.time():
                raise block_error("psy_refresh_wait")
        elif previous is not absent:
            raise block_error("psy_refresh_rejected" if state == "rejected" else "psy_refresh_uncertain")
        # Never send on an unwritable store. Atomic writes include fsync of the directory.
        _save_private_json(path, {"state": "pending"}, fsync_dir=True)
        try:
            result = exchange()
        except Exception as error:
            state = refresh_outcome(error)
            record = {"state": state}
            if state == "wait":
                delay = getattr(error, "retry_after", None)
                record["retry_at"] = time.time() + max(60, delay if type(delay) in (int, float) and math.isfinite(delay) else 0)
            _save_private_json(path, record, fsync_dir=True)
            if state == "uncertain":
                raise block_error("psy_refresh_uncertain") from None
            raise
        # Keeping the old grant blocked also protects legacy rows read by other Hermes callers.
        if result["refresh_token"] == grant:
            path.unlink()
        else:
            _save_private_json(path, {"state": "spent"}, fsync_dir=True)
        return result


def preferred(store):
    from hermes_cli.auth_codex import _codex_pool_route_base_url
    from hermes_cli.auth_constants import _decode_jwt_claims

    rows = (store.get("credential_pool") or {}).get(PROVIDER) or []
    choices = [r for r in rows if isinstance(r, dict) and type(r.get("priority")) is int and r["priority"] == 0]
    if len(choices) != 1 or ((store.get("providers") or {}).get(PROVIDER) or {}).get("tokens"):
        raise CredentialIssue("signin", "preferred Codex credential missing or ambiguous")
    row = choices[0]
    base = row.get("base_url") or BASE
    if (not row.get("id") or row.get("source") != "manual:device_code" or row.get("auth_type") != "oauth"
            or not isinstance(base, str) or base.rstrip("/") != BASE or _codex_pool_route_base_url(base).rstrip("/") != BASE
            or row.get("last_status") == "dead" or not all(isinstance(row.get(k), str) and row[k].strip()
                                                           for k in ("access_token", "refresh_token"))):
        raise CredentialIssue("signin", "preferred login unavailable, owner login required")
    expiry = _decode_jwt_claims(row["access_token"]).get("exp")
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise CredentialIssue("signin", "preferred credential has no usable expiration")
    return row, expiry


def legacy_marker(path, value):
    try:
        return Path(path).read_text().strip() == fingerprint(value)
    except FileNotFoundError:
        return False


def group_recovery(row):
    recovery = row.get("psy_model_auth_recovery")
    if not isinstance(recovery, dict) or recovery.get("bearer") != fingerprint(row["access_token"]):
        return None
    if "grant" in recovery and recovery["grant"] != fingerprint(row["refresh_token"]):
        return None
    until = recovery.get("retry_at")
    if recovery.get("kind") == "signin" or type(until) in (int, float) and until > time.time():
        return recovery.get("kind")


def mark_model(row, kind, seconds):
    row["psy_model_auth_recovery"] = {
        "kind": kind, "bearer": fingerprint(row["access_token"]), "grant": fingerprint(row["refresh_token"]),
        "retry_at": time.time() + seconds,
    }


def grant_owner(token):
    from hermes_cli.auth_constants import _decode_jwt_claims
    claims = _decode_jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    account = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return claims.get("sub"), account


def grant_aliases(rows, row):
    # A grant match identifies a legacy alias in pinned Hermes. Refuse to copy
    # a fresh bearer into a row with conflicting claims, type, source or route.
    grant = row["refresh_token"]
    identity = grant_owner(row["access_token"])
    matching = [r for r in rows if isinstance(r, dict) and r.get("refresh_token") == grant]
    for alias in matching:
        base = alias.get("base_url") or BASE
        if (alias.get("auth_type") != "oauth" or alias.get("source") not in ("manual:device_code", "device_code")
                or not isinstance(base, str) or base.rstrip("/") != BASE):
            raise CredentialIssue("access_wait", "inconsistent Codex grant aliases; review credential store")
        other = grant_owner(alias.get("access_token"))
        if any(first and second and first != second for first, second in zip(identity, other)):
            raise CredentialIssue("access_wait", "conflicting Codex grant owners; review credential store")
    return matching


def renew(observed, *, check, rejected_file, model_rejected=False):
    """Re-read the preferred generation, check caller admission, rotate aliases in one Hermes write."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    from hermes_cli.auth_codex import refresh_codex_oauth_pure
    from hermes_cli.auth_constants import AuthError, httpx

    with _auth_store_lock(timeout_seconds=35):
        store = _load_auth_store()
        row, expiry = preferred(store)
        if row["id"] != observed["id"]:
            raise CredentialIssue("access_wait", "preferred login changed during renewal")
        if any(row[k] != observed[k] for k in ("access_token", "refresh_token")):
            return row, expiry, False
        if legacy_marker(rejected_file, row["refresh_token"]):
            raise CredentialIssue("signin", "OpenAI previously rejected this grant, owner login required")
        check(store, row, expiry)
        grant = row["refresh_token"]
        matching = grant_aliases(store["credential_pool"][PROVIDER], row)
        try:
            updated = refresh_codex_oauth_pure(row["access_token"], grant)
        except (AuthError, httpx.RequestError, OSError) as error:
            code = getattr(error, "code", None)
            if terminal(error) or code == "psy_refresh_rejected":
                for alias in matching:
                    alias.update(last_status="dead", last_status_at=time.time(), last_error_reason=code,
                                 last_error_code=None, last_error_message="Codex OAuth grant rejected")
                _save_auth_store(store)
                raise CredentialIssue("signin", "OpenAI rejected renewal, owner login required") from None
            if code == "psy_refresh_uncertain":
                if model_rejected:
                    mark_model(row, "signin", 0)
                    _save_auth_store(store)
                raise CredentialIssue("signin", "renewal outcome unknown, owner login required", may_use_current=True) from None
            if model_rejected:
                mark_model(row, "access_wait", 60)
                _save_auth_store(store)
            raise CredentialIssue("access_wait", "OpenAI did not complete renewal, retry next check", may_use_current=True) from error

        tokens = {key: updated[key] for key in ("access_token", "refresh_token", "last_refresh")}
        for alias in matching:
            alias.update(tokens)
        if model_rejected:
            mark_model(row, "renewed", 300)
        _save_auth_store(store)
        row, expiry = preferred(store)
        return row, expiry, True
