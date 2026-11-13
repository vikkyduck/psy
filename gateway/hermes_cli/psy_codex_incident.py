"""Private, cross-process record of owner intervention for the selected PSY login."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from . import psy_codex_policy as policy


def directory(target=None, markers=None):
    if target is not None:
        home = Path(target).parent
    elif Path(markers or policy.MARKERS) != Path("/var/lib/psy-codex-auth"):
        # Alternate isolated marker directories used by local profiles / offline tests.
        home = Path(markers or policy.MARKERS)
    else:
        from hermes_cli.auth import _auth_file_path
        home = _auth_file_path().parent
    return home / "psy-owner-recovery"


def read(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return None


def write(path, content):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(json.dumps(content, separators=(",", ":")).encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_directory(parent):
    fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(root, name, *, blocking=True):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / name, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def snapshot():
    return read(directory() / "incident.json")


def report(row, reason, *, target=None, markers=None):
    """Requires no network. A single generation has a single owner notification."""
    if reason not in ("uncertain", "revoked", "model_rejected"):
        raise ValueError("invalid authorization incident")
    root = directory(target, markers)
    path = root / "incident.json"
    generation = policy.generation(row)
    from hermes_cli.auth import _auth_store_lock, _load_auth_store
    args = {} if target is None else {"target_path": target}
    with _auth_store_lock(timeout_seconds=35, **args):
        store = _load_auth_store() if target is None else _load_auth_store(target)
        current, _ = policy.preferred(store, allow_dead=True)
        if generation != policy.generation(current):
            return None
        with lock(root, "incident.lock"):
            old = read(path)
            if old and old["generation"] == generation:
                if old["reason"] == "uncertain" and reason != "uncertain":
                    old["reason"] = reason
                    write(path, old)
                return old["id"]
            record = {"id": secrets.token_hex(12), "generation": generation, "reason": reason,
                      "notice": "ready", "retry_at": 0}
            write(path, record)
            return record["id"]


def notice_due(record):
    return record["notice"] == "ready" and record["retry_at"] <= time.time()


def claim_notice(incident_id):
    root = directory()
    path = root / "incident.json"
    with lock(root, "incident.lock"):
        record = read(path)
        if not record or record["id"] != incident_id or not notice_due(record):
            return False
        # If Telegram got the DM but its acknowledgement was lost, do not send it twice.
        record["notice"] = "dispatched"
        write(path, record)
        return True


def retry_notice(incident_id, delay):
    root = directory()
    path = root / "incident.json"
    with lock(root, "incident.lock"):
        record = read(path)
        if record and record["id"] == incident_id:
            record.update(notice="ready", retry_at=time.time() + max(5, delay))
            write(path, record)


def close(incident_id):
    root = directory()
    path = root / "incident.json"
    with lock(root, "incident.lock"):
        record = read(path)
        if record and record["id"] == incident_id:
            path.unlink()
            sync_directory(root)


def update_device(incident_id, device, *, expected=None):
    """Replace only the exact owner's device attempt. Never resurrect a stale incident."""
    root = directory()
    path = root / "incident.json"
    with lock(root, "incident.lock"):
        record = read(path)
        if not record or record["id"] != incident_id or record.get("device") != expected:
            return False
        if device is None:
            record.pop("device", None)
        else:
            record["device"] = device
        write(path, record)
        return True
