"""PSY's preferred ChatGPT login and single-use OAuth renewal policy."""
import hashlib
import json
import math
import os
import secrets
from pathlib import Path
import time

PROVIDER = "openai-codex"
BASE = "https://chatgpt.com/backend-api/codex"
MARKERS = Path("/var/lib/psy-codex-auth")
RENEW_BEFORE = 24 * 3600
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


def guarded_exchange(grant, exchange, *, on_received=None):
    """Journal intent before the POST. A crash or partial response must never replay a grant.

    All pinned Hermes Codex entry points invoke this below their usual auth-store lock.
    The journal stores state, a random attempt ID and timestamps. A distinct grant has its own key.
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
        pending = {"state": "pending"}
        if on_received is not None:
            pending["attempt"] = secrets.token_hex(16)
        _save_private_json(path, pending, fsync_dir=True)
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
        if on_received is not None:
            # Keep pending until the auth-store transaction commits. Its private receipt
            # allows a later process to finish locally, including when the grant is unchanged.
            on_received(result, pending["attempt"])
            return result
        # Keeping the old grant blocked also protects legacy rows read by other Hermes callers.
        if result["refresh_token"] == grant:
            path.unlink()
        else:
            _save_private_json(path, {"state": "spent"}, fsync_dir=True)
        return result


def _primary_rows(store):
    rows = (store.get("credential_pool") or {}).get(PROVIDER) or []
    return [row for row in rows if isinstance(row, dict) and type(row.get("priority")) is int
            and row["priority"] == 0]


def preferred(store, *, allow_dead=False):
    from hermes_cli.auth_codex import _codex_pool_route_base_url
    from hermes_cli.auth_constants import _decode_jwt_claims

    choices = _primary_rows(store)
    if len(choices) != 1 or ((store.get("providers") or {}).get(PROVIDER) or {}).get("tokens"):
        raise CredentialIssue("signin", "preferred Codex credential missing or ambiguous")
    row = choices[0]
    base = row.get("base_url") or BASE
    if (not row.get("id") or row.get("source") != "manual:device_code" or row.get("auth_type") != "oauth"
            or not isinstance(base, str) or base.rstrip("/") != BASE or _codex_pool_route_base_url(base).rstrip("/") != BASE
            or not all(isinstance(row.get(k), str) and row[k].strip()
                                                           for k in ("access_token", "refresh_token"))):
        raise CredentialIssue("signin", "preferred login unavailable, owner login required")
    if row.get("last_status") == "dead" and not allow_dead:
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
    matching = [r for r in rows if isinstance(r, dict) and r.get("refresh_token") == grant]
    for alias in matching:
        base = alias.get("base_url") or BASE
        if (alias.get("auth_type") != "oauth" or alias.get("source") not in ("manual:device_code", "device_code")
                or not isinstance(base, str) or base.rstrip("/") != BASE):
            raise CredentialIssue("access_wait", "inconsistent Codex grant aliases; review credential store")
    if not compatible_owners([alias.get("access_token") for alias in matching]):
        raise CredentialIssue("access_wait", "conflicting Codex grant owners; review credential store")
    return matching


def compatible_owners(tokens):
    owners = [grant_owner(token) for token in tokens]
    for known in zip(*owners):
        if any(value is not None and not isinstance(value, str) for value in known):
            return False
        if len({value for value in known if value}) > 1:
            return False
    return True


def subscription_cooldown(row):
    import re

    # Hermes treats any 429 as quota-shaped. Keep documented traffic throttles in force.
    if row.get("last_status") != "exhausted" or row.get("last_error_code") not in (None, 429):
        return False
    reason = str(row.get("last_error_reason") or "").lower()
    if reason in {"usage_limit_reached", "usage_limit_exceeded", "insufficient_quota"}:
        return True
    if reason in {"rate_limit_exceeded", "too_many_requests"}:
        return False
    message = str(row.get("last_error_message") or "").lower()
    if any(part in message for part in ("usage_limit_reached", "usage_limit_exceeded", "insufficient_quota",
                                        "hit your usage limit", "usage limit reached", "usage limit has been reached")):
        return True
    # With only an HTTP status and reset, the upstream classifier can persist a
    # long subscription window with no reason. Let a real job test it after
    # the same backoff Hermes uses for account monitoring. No probe is issued.
    if row.get("last_error_code") != 429 or reason or not re.fullmatch(
            r"(?:|(?:http(?: error)? |error code: ?)?429[.]?)", message.strip()):
        return False
    started = row.get("last_status_at")
    reset = row.get("last_error_reset_at")
    return (type(started) in (int, float) and type(reset) in (int, float)
            and reset - started >= 3600 and started + 300 <= time.time() < reset)


def generation(row):
    """An opaque, non-secret key for evidence about one exact preferred login."""
    return fingerprint(json.dumps([row["id"], row["access_token"], row["refresh_token"]]))


def account_denied(markers, row):
    try:
        marker = (markers / "account-denied").read_text().strip()
    except FileNotFoundError:
        return False
    # The original monitor wrote only a bearer fingerprint.
    if marker == fingerprint(row["access_token"]):
        return True
    try:
        data = json.loads(marker)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("generation") == generation(row)


def admit(store, row, expiry, *, markers, purpose, model_rejected=False, target=None):
    if legacy_marker(markers / "rejected", row["refresh_token"]):
        _incident(row, "revoked", target=target)
        raise CredentialIssue("signin", "OpenAI previously rejected this grant, owner login required")
    if purpose not in {"model", "native", "native_probe", "account_401", "scheduled"}:
        raise ValueError("unknown Codex renewal purpose")
    if purpose in {"model", "native", "account_401"} and account_denied(markers, row):
        _incident(row, "model_rejected", target=target)
        raise CredentialIssue("signin", "Codex account endpoint still returns HTTP 401 after renewal")
    recovery = group_recovery(row)
    if recovery == "access_wait" or (recovery == "signin" and purpose in {"model", "native"}):
        if recovery == "signin":
            _incident(row, "model_rejected", target=target)
        raise CredentialIssue(recovery, "model authorization recovery pending")
    if model_rejected and recovery == "renewed":
        raise CredentialIssue("signin", "model refused renewed bearer")
    if purpose in {"model", "native"}:
        from hermes_cli.auth_codex import _codex_pool_row_available
        rows = store["credential_pool"][PROVIDER]
        sole = sum(isinstance(r, dict) and r.get("last_status") != "dead" for r in rows) <= 1
        if not _codex_pool_row_available(row, sole) and not subscription_cooldown(row):
            raise CredentialIssue("access_wait", "preferred login in cooldown")
    elif purpose in {"scheduled", "account_401"}:
        from hermes_cli.auth_codex import _entry_is_rate_limit_exhausted
        if _entry_is_rate_limit_exhausted(row) and expiry <= time.time() + 120:
            raise CredentialIssue("access_wait", "preferred login expired in cooldown; inspect gateway")


def _incident(row, reason, *, target=None):
    from hermes_cli.psy_codex_incident import report
    report(row, reason, target=target, markers=MARKERS)


def _receipt_path(target):
    from hermes_cli.auth import _auth_file_path
    return (target or _auth_file_path()).parent / "psy-codex-received.json"


def _load_store(target):
    from hermes_cli.auth import _load_auth_store
    return _load_auth_store() if target is None else _load_auth_store(target)


def _save_store(store, target):
    from hermes_cli.auth import _save_auth_store
    if target is None:
        _save_auth_store(store)
    else:
        _save_auth_store(store, target_path=target)


def _tokens_for_owner(updated, aliases, *, device_login=False):
    from hermes_cli.auth_constants import _decode_jwt_claims
    keys = ("access_token", "refresh_token", "last_refresh")
    if not isinstance(updated, dict) or any(not isinstance(updated.get(k), str) or not updated[k].strip() for k in keys):
        raise CredentialIssue("signin", "Codex returned incomplete credentials")
    bearer = updated["access_token"]
    expiry = _decode_jwt_claims(bearer).get("exp")
    if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
        raise CredentialIssue("signin", "Codex returned a credential with no expiration")
    old = [alias["access_token"] for alias in aliases]
    if not compatible_owners([*old, bearer]):
        raise CredentialIssue("signin", "Codex returned another owner, owner login required")
    if device_login:
        # The browser can be signed into a different account. Require affirmative
        # continuity for every identity field known before the interactive login.
        def device_identity(token):
            claims = _decode_jwt_claims(token)
            auth = claims.get("https://api.openai.com/auth")
            auth = auth if isinstance(auth, dict) else {}
            return claims.get("sub"), auth.get("chatgpt_user_id"), auth.get("chatgpt_account_id")

        subjects, users, accounts = zip(*(device_identity(token) for token in old))
        new_sub, new_user, new_account = device_identity(bearer)
        # Members can share a workspace. Conversely a person can belong to several
        # workspaces. Both the person and the subscription must be identifiable.
        if not (any(subjects) or any(users)) or not any(accounts):
            raise CredentialIssue("signin", "Prior login cannot identify the ChatGPT user and workspace")
        for recorded, fresh in ((subjects, new_sub), (users, new_user), (accounts, new_account)):
            if any(value is not None and (not isinstance(value, str) or not value.strip() or value != fresh)
                   for value in recorded):
                raise CredentialIssue("signin", "Could not verify the same ChatGPT user and workspace")
        if expiry <= time.time():
            raise CredentialIssue("signin", "The new owner login has expired")
    return {key: updated[key] for key in keys}


def _apply_pair(row, aliases, tokens, model_rejected):
    refused_again = model_rejected and tokens["access_token"] == row["access_token"]
    for alias in aliases:
        alias.update(tokens)
    if model_rejected:
        mark_model(row, "signin" if refused_again else "renewed", 0 if refused_again else 300)
    return refused_again


def _durably_unlink(path):
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _finish_receipt(path, receipt):
    from hermes_cli.auth import _auth_store_lock, _save_private_json
    # Never use an arbitrary file name from an interrupted/corrupted receipt.
    old = receipt["grant_hash"]
    if not isinstance(old, str) or len(old) != 64 or any(c not in "0123456789abcdef" for c in old):
        raise CredentialIssue("access_wait", "damaged Codex renewal receipt")
    journal = path.parent / "psy-codex-renewals" / (old + ".json")
    with _auth_store_lock(timeout_seconds=35, target_path=journal):
        try:
            previous = json.loads(journal.read_bytes())
        except FileNotFoundError:
            previous = None
        # A newer attempt for an unchanged grant owns its own replay barrier.
        if previous == {"state": "pending", "attempt": receipt["attempt"]}:
            if old == fingerprint(receipt["tokens"]["refresh_token"]):
                _durably_unlink(journal)
            else:
                _save_private_json(journal, {"state": "spent"}, fsync_dir=True)
    _durably_unlink(path)


def _recover(store, target):
    """Under the store's lock finish one received response, or discard a superseded one."""
    path = _receipt_path(target)
    try:
        pending = json.loads(path.read_bytes())
    except FileNotFoundError:
        return False
    except ValueError:
        raise CredentialIssue("access_wait", "damaged Codex renewal receipt") from None
    if (not isinstance(pending, dict) or not isinstance(pending.get("before"), str)
            or not isinstance(pending.get("tokens"), dict) or not isinstance(pending["tokens"].get("refresh_token"), str)
            or not isinstance(pending.get("attempt"), str) or not isinstance(pending.get("grant_hash"), str)
            or type(pending.get("model_rejected")) is not bool):
        raise CredentialIssue("access_wait", "damaged Codex renewal receipt")
    row, _ = preferred(store, allow_dead=True)
    applied = generation(row) == pending["before"] and row.get("last_status") != "dead"
    if applied:
        if fingerprint(row["refresh_token"]) != pending["grant_hash"]:
            raise CredentialIssue("access_wait", "damaged Codex renewal receipt")
        aliases = grant_aliases(store["credential_pool"][PROVIDER], row)
        tokens = _tokens_for_owner(pending["tokens"], aliases)
        refused = _apply_pair(row, aliases, tokens, pending["model_rejected"])
        _save_store(store, target)
        if refused:
            _incident(row, "model_rejected", target=target)
    _finish_receipt(path, pending)
    return applied


def _current_store(target=None):
    from hermes_cli.auth import _auth_store_lock
    if not _receipt_path(target).exists():
        return _load_store(target)
    with _auth_store_lock(timeout_seconds=35, target_path=target):
        store = _load_store(target)
        _recover(store, target)
        return store


def credential_snapshot():
    """Return the sole configured managed identity, including a disabled generation."""
    return preferred(_current_store(), allow_dead=True)


def _active(store, *, target=None):
    row, expiry = preferred(store, allow_dead=True)
    if row.get("last_status") == "dead":
        _incident(row, "revoked", target=target)
        raise CredentialIssue("signin", "preferred login unavailable, owner login required")
    return row, expiry


def install_owner_login(observed_generation, tokens):
    """Install a browser-authorized login for the same subscription under generation CAS."""
    from hermes_cli.auth import _auth_store_lock
    with _auth_store_lock(timeout_seconds=35):
        store = _load_store(None)
        _recover(store, None)
        row, _ = preferred(store, allow_dead=True)
        if generation(row) != observed_generation:
            return generation(row)
        aliases = grant_aliases(store["credential_pool"][PROVIDER], row)
        pair = _tokens_for_owner(tokens, aliases, device_login=True)
        if pair["refresh_token"] == row["refresh_token"]:
            raise CredentialIssue("signin", "Owner login did not issue a fresh grant")
        for alias in aliases:
            if alias.get("last_status") == "dead" or alias.get("last_error_code") == 401:
                for field in ("last_status", "last_status_at", "last_error_reason", "last_error_code",
                              "last_error_message", "last_error_reset_at"):
                    alias.pop(field, None)
            alias.pop("psy_model_auth_recovery", None)
            alias.update(pair)
        _save_store(store, None)
        return generation(row)


def model_credentials(rejected=None):
    """Admit this generation for one real inference. Only a matching HTTP 401 demands renewal."""
    from hermes_cli.auth_constants import httpx

    try:
        store = _current_store()
        row, expiry = _active(store)
        model_rejected = rejected == generation(row)
        if model_rejected and group_recovery(row) == "renewed":
            record_model_refusal(rejected)
            raise CredentialIssue("signin", "model refused renewed bearer")
        admit(store, row, expiry, markers=MARKERS, purpose="model", model_rejected=model_rejected)
        if model_rejected or expiry <= time.time() + 300:
            try:
                row, expiry, _ = renew(row, markers=MARKERS, purpose="model", model_rejected=model_rejected)
            except CredentialIssue as exc:
                # A failed proactive renewal does not invalidate an unexpired, unrefused bearer.
                if model_rejected or expiry <= time.time() or not exc.may_use_current:
                    raise
            if expiry <= time.time():
                raise CredentialIssue("access_wait", "Codex login expired")
        return {"provider": PROVIDER, "base_url": BASE, "api_key": row["access_token"], "generation": generation(row)}
    except (OSError, httpx.RequestError):
        raise CredentialIssue("access_wait", "credential store unavailable") from None


def record_model_refusal(observed_generation):
    """A second explicit rejection is evidence only for the exact retried pair."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    try:
        with _auth_store_lock(timeout_seconds=35):
            store = _load_auth_store()
            row, _ = preferred(store)
            if observed_generation == generation(row):
                mark_model(row, "signin", 0)
                _save_auth_store(store)
                _incident(row, "model_rejected")
    except OSError:
        raise CredentialIssue("access_wait", "credential store unavailable") from None


def record_account_probe(observed, account, *, after_renewal, markers):
    """Commit monitor evidence only while the probed generation is still current."""
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_private_json
    with _auth_store_lock(timeout_seconds=35):
        row, _ = preferred(_load_auth_store())
        if generation(row) != generation(observed):
            raise CredentialIssue("access_wait", "preferred login changed during account check")
        path = markers / "account-denied"
        if account.http == 401 and after_renewal:
            _save_private_json(path, {"generation": generation(row)}, fsync_dir=True)
            _incident(row, "model_rejected")
        elif account.auth == "valid" and account_denied(markers, row):
            path.unlink(missing_ok=True)


def account_credentials(probe, *, force=False):
    """Perform the scheduled check; only the account probe itself is supplied by the monitor."""
    row, expiry = _active(_current_store())
    due = force or expiry - time.time() <= RENEW_BEFORE
    account = None if due else probe(row["access_token"])
    renewed = False
    recovering = due or account.http == 401
    if recovering:
        row, expiry, renewed = renew(row, markers=MARKERS, purpose="scheduled" if due else "account_401")
        account = probe(row["access_token"])
    # A peer's new pair is also a freshly probed generation after recovery.
    record_account_probe(row, account, after_renewal=recovering, markers=MARKERS)
    return expiry, renewed, account


def renew(observed, *, markers, purpose, model_rejected=False, timeout_seconds=20.0, target=None):
    """Re-read the preferred generation, check caller admission, rotate aliases in one Hermes write."""
    from hermes_cli.auth import _auth_store_lock
    from hermes_cli.auth_codex import refresh_codex_oauth_pure
    from hermes_cli.auth_constants import AuthError, httpx

    lock_path = {} if target is None else {"target_path": target}

    with _auth_store_lock(timeout_seconds=max(35, timeout_seconds + 5), **lock_path):
        store = _load_store(target)
        recovered = _recover(store, target)
        row, expiry = _active(store, target=target)
        if row["id"] != observed["id"]:
            raise CredentialIssue("access_wait", "preferred login changed during renewal")
        changed = generation(row) != generation(observed)
        admit(store, row, expiry, markers=markers, purpose=purpose, model_rejected=model_rejected and not changed, target=target)
        if changed or recovered:
            return row, expiry, False
        grant = row["refresh_token"]
        matching = grant_aliases(store["credential_pool"][PROVIDER], row)
        receipt = None
        def received(updated, attempt):
            from hermes_cli.auth import _save_private_json
            nonlocal receipt
            tokens = _tokens_for_owner(updated, matching)
            receipt = {"before": generation(row), "grant_hash": fingerprint(grant), "attempt": attempt,
                       "model_rejected": model_rejected, "tokens": tokens}
            _save_private_json(_receipt_path(target), receipt, fsync_dir=True)
        try:
            updated = refresh_codex_oauth_pure(row["access_token"], grant, timeout_seconds=timeout_seconds,
                                               on_received=received)
            tokens = _tokens_for_owner(updated, matching)
        except CredentialIssue:
            # A syntactically successful exchange returned unusable credentials.
            for alias in matching:
                alias.update(last_status="dead", last_status_at=time.time(), last_error_reason="codex_owner_conflict",
                             last_error_code=None, last_error_message="Unusable Codex renewal response")
            _save_store(store, target)
            _incident(row, "revoked", target=target)
            raise
        except (AuthError, httpx.RequestError, OSError) as error:
            code = getattr(error, "code", None)
            if terminal(error) or code == "psy_refresh_rejected":
                for alias in matching:
                    alias.update(last_status="dead", last_status_at=time.time(), last_error_reason=code,
                                 last_error_code=None, last_error_message="Codex OAuth grant rejected")
                _save_store(store, target)
                _incident(row, "revoked", target=target)
                raise CredentialIssue("signin", "OpenAI rejected renewal, owner login required") from None
            if code == "psy_refresh_uncertain":
                _incident(row, "uncertain", target=target)
                if model_rejected:
                    mark_model(row, "signin", 0)
                    _save_store(store, target)
                raise CredentialIssue("signin", "renewal outcome unknown, owner login required", may_use_current=True) from None
            if model_rejected:
                mark_model(row, "access_wait", 60)
                _save_store(store, target)
            raise CredentialIssue("access_wait", "OpenAI did not complete renewal, retry next check", may_use_current=True) from error

        same_rejected_bearer = _apply_pair(row, matching, tokens, model_rejected)
        _save_store(store, target)
        if receipt is not None:
            _finish_receipt(_receipt_path(target), receipt)
        if same_rejected_bearer:
            _incident(row, "model_rejected", target=target)
            raise CredentialIssue("signin", "renewal returned the rejected bearer")
        row, expiry = preferred(store)
        return row, expiry, True


def is_managed(store, observed):
    """Identify native observations of the designated login, including stale pool objects."""
    managed = [row for row in _primary_rows(store) if row.get("source") == "manual:device_code"]
    grants = [row["refresh_token"] for row in managed if isinstance(row.get("refresh_token"), str)
              and row["refresh_token"]]
    if observed.get("refresh_token") in grants:
        return True
    identifier = observed.get("id")
    if not identifier:
        return False
    rows = (store.get("credential_pool") or {}).get(PROVIDER) or []
    return any(isinstance(row, dict) and row.get("id") == identifier
               and (row in managed or row.get("refresh_token") in grants) for row in rows)


def renew_native(observed, *, timeout_seconds=20.0, quota_probe=False):
    """Renew an observed PSY login from a native Hermes entry point.

    None delegates an independent credential to Hermes. A matching but ambiguous
    or inadmissible login raises, so a caller cannot fall back to the spent grant.
    Entry points decide when a refresh is needed. This function always checks
    admission, even for forced requests, and adopts a peer's rotation if present.
    """
    from hermes_cli.auth_codex import _codex_pool_owner_lock

    with _codex_pool_owner_lock() as target:
        store = _current_store(target)
        if not is_managed(store, observed):
            return None
        rows = store["credential_pool"][PROVIDER]
        identifier = observed.get("id")
        grant = observed.get("refresh_token")
        by_id = [row for row in rows if isinstance(row, dict) and identifier and row.get("id") == identifier]
        primary, expiry = _active(store, target=target)
        aliases = grant_aliases(rows, primary)
        if identifier and (len(by_id) != 1 or not any(by_id[0] is alias for alias in aliases)):
            raise CredentialIssue("access_wait", "ambiguous native Codex credential")
        if identifier:
            current = by_id[0]
        else:
            # Legacy aliases can have a different bearer even before the shared grant rotates.
            current = next((alias for alias in aliases if alias.get("access_token") == observed.get("access_token")), primary)
        # A stale object from a pool must never adopt a bearer for a different account or endpoint.
        snapshot = dict(current)
        snapshot.update({key: observed[key] for key in ("source", "auth_type", "base_url", "access_token") if key in observed})
        grant_aliases([*aliases, snapshot], primary)
        purpose = "native_probe" if quota_probe else "native"
        if grant != primary["refresh_token"] or observed.get("access_token") != current["access_token"]:
            admit(store, primary, expiry, markers=MARKERS, purpose=purpose, target=target)
            return current, expiry, False
        updated, expiry, changed = renew(primary, markers=MARKERS, purpose=purpose,
                                         timeout_seconds=timeout_seconds, target=target)
        if identifier and identifier != primary["id"]:
            store = _load_store(target)
            updated = next(row for row in store["credential_pool"][PROVIDER] if isinstance(row, dict) and row.get("id") == identifier)
        return updated, expiry, changed
