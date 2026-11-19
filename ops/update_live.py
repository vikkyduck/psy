"""Update a PSY host that is already serving from this checkout, with backup and automatic rollback.

Run as root from the repository root on the bot host:

    python3 ops/update_live.py plan            # what would change; writes nothing
    python3 ops/update_live.py apply [--force]
    python3 ops/update_live.py rollback /var/lib/psy-deploy/<backup>   # put a backup back by hand

install-runtime stages an idle host; this is its counterpart for a running one. It installs the same
files, derives the Hermes edits from gateway/hermes.patch at the pinned commit, and leaves the owner's
profile (SOUL.md, skills), instance.env and private files alone. Drop-ins that only repeat what the
shipped unit now says are retired. Changed files are saved under /var/lib/psy-deploy first; a failed
preflight, service start or Telegram reconnect puts them back.

Trust boundary: the Hermes checkout belongs to the hermes account, which also runs an agent with a
terminal. Root never opens, writes, backs up or restores a path inside that checkout; a small helper
does it as hermes, so a link planted there can only reach what hermes could already change. Files in
root-owned directories are opened without following links, in directories that only root can modify.
"""
import argparse
import base64
import errno
import hashlib
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKOUT = Path("/home/hermes/.hermes/hermes-agent")
SYSTEMD = Path("/etc/systemd/system")
GROUP_LIB = Path("/usr/local/lib/psy-group")
COMMON = Path("/usr/local/lib/psy-common")
LIBEXEC = Path("/usr/local/libexec")
UPLOAD = Path("/opt/psy-upload")
INSTANCE = Path("/etc/psy/instance.env")
BACKUPS = Path("/var/lib/psy-deploy")
RECORD = Path("/etc/psy/deployed")
GROUP_DB = "/home/hermes/.hermes/vault/group-psy/group.sqlite3"
UPLOAD_HEALTH = Path("/var/lib/psy-upload-health/state.json")
UPLOAD_PYTHON = "/opt/psy-upload-venv/bin/python"
# Copied before an apply as their own owners (never opened by root): a release may migrate them.
DATABASES = (GROUP_DB, "/home/hermes/.hermes/vault/uploads/state.sqlite3",
             "/var/lib/psy-recording-receipts/results.sqlite3")

# Kept in step with install-runtime, install-backup and install-platform-bridge;
# units come from ops/inventory.json.
MEDIA = ("access_broker", "asr_client", "asr_sandbox", "asr_service", "psy_upload_common", "queue_health",
         "recording_grant", "recording_receipts", "server", "storage_capacity", "transcribe", "worker")
HELPERS = {"psy_codex_auth.py": "psy-codex-auth.py", "psy_healthcheck.py": "psy-healthcheck.py",
           "psy_alerts.py": "psy-alerts.py", "psy_vault_archive.py": "psy-vault-archive.py",
           "upload_healthcheck.py": "psy-upload-healthcheck.py", "psy_restart_outdated.py": "psy-restart-outdated.py",
           "deploy-cert": "psy-upload-deploy-cert"}
INVENTORY = json.loads((REPO / "ops/inventory.json").read_text())
INSTALLED = {name: unit for name, unit in INVENTORY["units"].items()
             if unit["installer"] in ("runtime", "backup", "platform")}
# Long-running services in restart order: the gateway last, once the upload side it calls is back.
SERVICES = tuple(name.removesuffix(".service") for name in INVENTORY["restart_order"])
# Socket-activated services run checkout code too and restart through their socket: Requires= in the
# service takes a running bridge along, an idle one starts with the new code on the next connection.
ACTIVATED = {name.removesuffix(".service"): name.removesuffix(".service") + ".socket"
             for name, unit in INVENTORY["units"].items() if unit["kind"] == "socket-service"}
GATEWAY_IMPORTS = {"psy_upload_common.py", "storage_capacity.py", "recording_grant.py"}  # via psy_upload_handoff
NEEDRESTART = Path("/etc/needrestart/conf.d/50-psy.conf")
BACKUP_LIB = Path("/usr/local/lib/psy-backup")


@dataclass
class Entry:
    target: Path
    data: bytes
    source: str
    owner: str = "root"
    mode: int = 0o644  # for new files; an existing file keeps its owner and mode


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RELEASE = load(REPO / "gateway/apply.py", "psy_release")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run(*cmd, user=None, check=True, text=True, env=None, input=None):
    if user:
        cmd = ("runuser", "-u", user, "--") + cmd
    return subprocess.run(cmd, check=check, capture_output=True, text=text, timeout=120, cwd="/", env=env,
                          input=input)


def hermes_git(*args, text=True):
    return run("env", "GIT_OPTIONAL_LOCKS=0", "git", "-C", str(CHECKOUT), *args, user="hermes", text=text).stdout


# ---------- what the release says the host should contain ----------

def system_entries(hermes_uid):
    # The shared helpers first: the files after it load it by its path as soon as they are replaced.
    entries = [Entry(COMMON / "psy_common.py", (REPO / "common/psy_common.py").read_bytes(), "common/psy_common.py")]
    entries += [Entry(UPLOAD / f"{name}.py", (REPO / f"media/{name}.py").read_bytes(), f"media/{name}.py")
                for name in MEDIA]
    entries += [Entry(UPLOAD / "static" / f.name, f.read_bytes(), f"media/static/{f.name}")
                for f in sorted((REPO / "media/static").iterdir()) if f.is_file()]
    entries += [Entry(GROUP_LIB / "upload_broker.py", (REPO / "gateway/upload_broker.py").read_bytes(), "gateway/upload_broker.py"),
                Entry(GROUP_LIB / "preflight.py", (REPO / "ops/libexec/preflight.py").read_bytes(), "ops/libexec/preflight.py"),
                Entry(GROUP_LIB / "manifest.json", (REPO / "gateway/group-manifest.json").read_bytes(), "gateway/group-manifest.json")]
    entries += [Entry(LIBEXEC / dst, (REPO / "ops/libexec" / src).read_bytes(), f"ops/libexec/{src}",
                      mode=0o755 if src == "deploy-cert" else 0o644) for src, dst in HELPERS.items()]
    entries.append(Entry(NEEDRESTART, (REPO / "ops/needrestart/50-psy.conf").read_bytes(), "ops/needrestart/50-psy.conf"))
    # Backup code, as ops/install-backup stages it; its keys, destination and timer state stay as they are.
    entries += [Entry(Path("/usr/local/sbin/psy-backup"), (REPO / "ops/backup/psy-backup").read_bytes(),
                      "ops/backup/psy-backup", mode=0o755),
                Entry(BACKUP_LIB / "sqlite_snapshot.py", (REPO / "ops/backup/sqlite_snapshot.py").read_bytes(),
                      "ops/backup/sqlite_snapshot.py"),
                Entry(BACKUP_LIB / "verify_backup.py", (REPO / "tools/verify_backup.py").read_bytes(), "tools/verify_backup.py")]
    names = list(INSTALLED) + [unit["timer"] for unit in INSTALLED.values() if unit.get("timer")]
    entries += [Entry(SYSTEMD / name, (REPO / "ops/systemd" / name).read_bytes(), f"ops/systemd/{name}") for name in names]
    for conf in sorted((REPO / "ops/systemd/hermes-gateway.service.d").glob("*.conf")):
        entries.append(Entry(SYSTEMD / "hermes-gateway.service.d" / conf.name, conf.read_bytes(),
                             f"ops/systemd/hermes-gateway.service.d/{conf.name}"))
    entries.append(Entry(SYSTEMD / "hermes-gateway.service.d/user-session.conf",
                         f"[Unit]\nAfter=user@{hermes_uid}.service\nWants=user@{hermes_uid}.service\n".encode(), "generated"))
    alert = f"[Unit]\nOnFailure={INVENTORY['on_failure']}\n".encode()
    entries += [Entry(SYSTEMD / f"{name}.d/owner-alert.conf", alert, "generated")
                for name, unit in INSTALLED.items() if unit.get("alert")]
    return entries


def new_timers(targets):
    """Timers this update installs for the first time; the release expects them running."""
    return [t.name for t in targets if t.parent == SYSTEMD and t.suffix == ".timer"
            and any(u.get("timer") == t.name and u["installer"] == "runtime" for u in INSTALLED.values())]


def release_sums():
    """Release digests: checkout files by relative path, shared root-owned files by installed path."""
    manifest = json.loads((REPO / "gateway/manifest.json").read_bytes())
    sums = manifest["sha256"]
    if (manifest["base"] != RELEASE.BASE or set(sums) != set(RELEASE.TRACKED + RELEASE.ADDED)
            or manifest.get("shared") != RELEASE.shared()
            or sha256((REPO / "gateway/hermes.patch").read_bytes()) != manifest["patch_sha256"]):
        raise ValueError("gateway/manifest.json does not describe this patch and file list")
    return {**sums, **manifest["shared"]}


def check_group_manifest(sums):
    """The preflight manifest must name the same bytes as the release manifest."""
    group = json.loads((REPO / "gateway/group-manifest.json").read_bytes())
    for name, digest in group.items():
        relative = name if "/" in name else RELEASE.TELEGRAM + name
        if sums.get(relative) != digest:
            raise ValueError(f"gateway/group-manifest.json disagrees with gateway/manifest.json on {name}")


def gateway_entries(sums):
    head = hermes_git("rev-parse", "HEAD").strip()
    if head != RELEASE.BASE:
        raise ValueError(f"Hermes on this host is at {head[:12]}, the release is for {RELEASE.BASE[:12]}. "
                         "Return the checkout to the pinned commit or port the patch first.")
    dirty = set(filter(None, hermes_git("diff", "--name-only", "-z", RELEASE.BASE).split("\0")))
    if dirty - set(RELEASE.TRACKED):
        raise ValueError("Hermes has edits the release does not know: " + ", ".join(sorted(dirty - set(RELEASE.TRACKED))))
    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        for relative in RELEASE.TRACKED:
            path = Path(tmp) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(hermes_git("show", f"{RELEASE.BASE}:{relative}", text=False))
        subprocess.run(["git", "apply", str(REPO / "gateway/hermes.patch")], cwd=tmp, check=True, capture_output=True)
        for relative in RELEASE.TRACKED:
            entries.append(Entry(CHECKOUT / relative, (Path(tmp) / relative).read_bytes(), "gateway/hermes.patch", owner="hermes"))
    for relative in RELEASE.ADDED:
        source = "media/telegram_handoff.py" if relative == RELEASE.HANDOFF else f"gateway/{relative}"
        entries.append(Entry(CHECKOUT / relative, (REPO / source).read_bytes(), source, owner="hermes"))
    for entry in entries:
        if sha256(entry.data) != sums[str(entry.target.relative_to(CHECKOUT))]:
            raise ValueError(f"{entry.source} does not produce the release bytes of {entry.target}")
    return entries


# ---------- drop-ins the shipped units made redundant ----------

def directives(text):
    section, found = None, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        key, sep, value = line.partition("=")
        if sep:
            found.append((section, key.strip(), value.strip()))
    return found


def redundant(dropin, unit):
    """True when every setting of the drop-in is already in the unit (list values may be a subset)."""
    have = {}
    for section, key, value in directives(unit):
        have.setdefault((section, key), set()).update(value.split())
    wanted = directives(dropin)
    return bool(wanted) and all(set(value.split()) <= have.get((section, key), set()) for section, key, value in wanted)


def retired_dropins(entries, systemd=SYSTEMD):
    shipped = {e.target for e in entries}
    retire, kept = [], []
    for entry in entries:
        if entry.target.parent != systemd or entry.target.suffix not in (".service", ".socket", ".timer"):
            continue
        folder = systemd / (entry.target.name + ".d")
        for conf in sorted(folder.glob("*.conf")) if folder.is_dir() else []:
            if conf in shipped:
                continue
            (retire if redundant(conf.read_text(), entry.data.decode()) else kept).append(conf)
    return retire, kept


# ---------- file access on both sides of the trust boundary ----------

# Runs as the checkout owner. Reads, writes and removes files below the checkout root and refuses
# links or odd file types on the way; even if it were fooled, it only has the owner's rights.
OWNER_IO = r'''
import base64, json, os, stat, sys
action, root = sys.argv[1], os.path.realpath(sys.argv[2])
payload = json.load(sys.stdin)

def walk(path, create=False):
    rel = os.path.relpath(path, root)
    if rel == "." or rel.startswith(".." + os.sep) or rel == ".." or os.path.isabs(rel):
        raise ValueError("outside the checkout: " + path)
    current = root
    for part in rel.split(os.sep)[:-1]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(current, 0o755)
            info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("not a plain directory: " + current)

def read(path):
    try:
        walk(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file: " + path)
        return {"data": base64.b64encode(handle.read()).decode(), "mode": stat.S_IMODE(info.st_mode)}

def write(path, data, mode):
    walk(path, create=True)
    try:
        old = os.lstat(path)
    except FileNotFoundError:
        old = None
    if old is not None and not stat.S_ISREG(old.st_mode):
        raise ValueError("not a regular file: " + path)
    mode = stat.S_IMODE(old.st_mode) if old is not None else mode
    folder, name = os.path.split(path)
    tmp = os.path.join(folder, "." + name + ".psy-new-" + os.urandom(6).hex())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64decode(data))
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

def remove(path):
    try:
        walk(path)
        os.unlink(path)
    except FileNotFoundError:
        pass

try:
    if action == "read":
        result = {path: read(path) for path in payload["paths"]}
    elif action == "write":
        for item in payload["files"]:
            write(item["path"], item["data"], item["mode"])
        result = {"ok": True}
    elif action == "remove":
        for path in payload["paths"]:
            remove(path)
        result = {"ok": True}
    else:
        raise ValueError("unknown action")
except (OSError, ValueError) as exc:
    json.dump({"error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
    sys.exit(1)
json.dump(result, sys.stdout)
'''


def owner_io(action, payload, root=CHECKOUT, user="hermes"):
    """Run OWNER_IO as `user` (None: as the caller, for tests) and return its JSON answer."""
    result = run("/usr/bin/python3" if user else sys.executable, "-I", "-c", OWNER_IO, action, str(root),
                 user=user, check=False, input=json.dumps(payload))
    try:
        answer = json.loads(result.stdout or "{}")
    except ValueError:
        answer = {}
    if result.returncode or "error" in answer:
        raise ValueError(f"checkout {action} refused: {answer.get('error') or result.stderr.strip() or 'no output'}")
    return answer


def in_checkout(target, root=CHECKOUT):
    return str(target).startswith(str(root) + "/")


def check_root_dir(directory):
    """Every existing component is a real directory that no other account can rearrange."""
    me = os.geteuid()
    current = Path("/")
    for part in Path(directory).parts[1:]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            return  # created below by us, inside a checked parent
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, me)
                or (info.st_mode & 0o022 and not sticky_root)):
            raise ValueError(f"{current} could be changed by another account; refusing to write below it")


def read_plain(path):
    """Bytes and stat of a regular file, or None; never follows a link at the last component."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"{path} is a symbolic link (a masked unit?); refusing to touch it") from None
        raise
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path} is not a regular file")
        return handle.read(), info


def write_plain(target, data, uid, gid, mode):
    target = Path(target)
    check_root_dir(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    check_root_dir(target.parent)
    tmp = target.parent / f".{target.name}.psy-new-{os.urandom(6).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write(target, data, owner, mode):
    """Install one root-side file; an existing file keeps its owner and mode. A folder created for it
    is 0755 whatever root's umask, as install-runtime makes it: the services read code there."""
    import pwd
    folder = Path(target).parent
    if not folder.exists():
        check_root_dir(folder)
        folder.mkdir(parents=True)
        folder.chmod(0o755)
    current = read_plain(target)
    if current is not None:
        info = current[1]
        uid, gid, mode = info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)
    else:
        account = pwd.getpwnam(owner)
        uid, gid = account.pw_uid, account.pw_gid
    write_plain(target, data, uid, gid, mode)


def current_bytes(entries, root=CHECKOUT, user="hermes"):
    """What each target holds now (None if missing), reading the checkout as its owner."""
    inside = [str(e.target) for e in entries if in_checkout(e.target, root)]
    found = owner_io("read", {"paths": inside}, root, user) if inside else {}
    now = {}
    for entry in entries:
        if in_checkout(entry.target, root):
            item = found.get(str(entry.target))
            now[entry.target] = None if item is None else base64.b64decode(item["data"])
        else:
            plain = read_plain(entry.target)
            now[entry.target] = None if plain is None else plain[0]
    return now


# ---------- plan ----------

def plan(entries, now=None):
    now = current_bytes(entries) if now is None else now
    return [(("new" if now[e.target] is None else "update"), e) for e in entries if now[e.target] != e.data]


def units_for(target):
    text = str(target)
    if text.startswith(str(CHECKOUT) + "/"):
        return {"hermes-gateway", *ACTIVATED.values()}
    if text.startswith(str(UPLOAD) + "/"):
        return {"psy-asr", "psy-upload-worker", "psy-upload"} | ({"hermes-gateway"} if target.name in GATEWAY_IMPORTS else set())
    if text.startswith(str(COMMON) + "/"):
        # Loaded at start by the uploader and the worker, and in the gateway by the upload handoff,
        # whose preflight checks these bytes. Brokers and timer checks load it on their next run.
        return {"psy-upload-worker", "psy-upload", "hermes-gateway"}
    if text.startswith(str(SYSTEMD) + "/"):
        name = Path(text).relative_to(SYSTEMD).parts[0].removesuffix(".d")
        unit = name.removesuffix(".service")
        if unit in ACTIVATED:
            return {ACTIVATED[unit]}
        return {unit} if unit in SERVICES else ({name} if name.endswith((".socket", ".timer")) else set())
    return set()


def check_instance():
    """Import the release's settings readers with this host's instance.env before anything changes.
    They load the release's psy_common, which is not installed yet at this point."""
    parser = load(REPO / "ops/instance_env.py", "psy_instance_env")
    env = {"PATH": "/usr/bin:/bin", **parser.parse(INSTANCE.read_text(), complete=True),
           "PSY_COMMON_PATH": str(REPO / "common/psy_common.py")}
    probes = [("/usr/bin/python3", REPO / "gateway" / RELEASE.TELEGRAM, "import psy_instance"),
              ("/usr/bin/python3", REPO / "gateway", "import upload_broker"),
              (UPLOAD_PYTHON, REPO / "media", "import psy_upload_common")]
    for python, folder, statement in probes:
        code = f"import sys; sys.path.insert(0, {str(folder)!r}); {statement}"
        result = run(python, "-I", "-B", "-c", code, check=False, env=env)
        if result.returncode:
            raise ValueError(f"{INSTANCE} is not accepted by the release ({statement}): "
                             + (result.stderr.strip().splitlines() or ["no output"])[-1])


# Opened read-write as its owner: a read-only open of a WAL database fails while no other process
# holds it, and only the owner may create its -shm file.
def check_backup_config():
    """psy-backup reads its destination from a file that ops/install-backup creates; a host whose
    backup predates that file would fail its next nightly run after this update."""
    enabled = not run("systemctl", "is-enabled", "--quiet", "psy-backup.timer", check=False).returncode
    if enabled and read_plain("/etc/psy-backup/destination.env") is None:
        raise ValueError("psy-backup.timer is enabled but /etc/psy-backup/destination.env is missing; "
                         "set PSY_BACKUP_DEST there (see ops/backup/destination.env.example) first")


GROUP_PROBE = """
import sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=30)
count = lambda where: db.execute("select count(*) from requests where " + where).fetchone()[0]
print(count("state in ('queued', 'running', 'sending')"), count("state in ('uncertain', 'failed')"))
"""


def busy():
    """Work a restart would cut off. The group database is read as hermes, never as root."""
    reasons = []
    active, stuck = map(int, run("/usr/bin/python3", "-I", "-c", GROUP_PROBE, GROUP_DB, user="hermes").stdout.split())
    if active:
        reasons.append(f"group queue has {active} request(s) in progress")
    if stuck:
        print(f"note: {stuck} group request(s) wait for the operator in 'uncertain' or 'failed' (not blocking)")
    queue = (json.loads(UPLOAD_HEALTH.read_text()).get("queue") or {}) if UPLOAD_HEALTH.exists() else {}
    if queue.get("queued") or queue.get("processing"):
        reasons.append(f"recordings: {queue.get('queued')} queued, {queue.get('processing')} processing")
    return reasons


# ---------- apply ----------

class Backup:
    """Previous bytes, owner and mode of every target, plus an index a later `rollback` can replay.

    Checkout files are read and put back through owner_io; root-side files never through a link.
    """

    INDEX = "backup.json"

    def __init__(self, label=None, root=None, checkout=CHECKOUT, owner="hermes"):
        self.checkout, self.owner = checkout, owner
        if root is None:
            check_root_dir(BACKUPS)
            BACKUPS.mkdir(parents=True, exist_ok=True)
            root = BACKUPS / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{label}"
            root.mkdir(mode=0o711)  # traversable so owners can drop database copies in their own folder
            self.index = {"saved": [], "created": [], "units": [], "reload": False}
        else:
            self.index = json.loads((Path(root) / self.INDEX).read_text())
        self.root = Path(root)

    def _save_index(self):
        write_plain(self.root / self.INDEX, json.dumps(self.index, indent=1).encode(), os.geteuid(), os.getegid(), 0o600)

    def note(self, units, reload_units):
        self.index.update(units=sorted(units), reload=bool(reload_units))
        self._save_index()

    def keep(self, targets):
        inside = [str(t) for t in targets if in_checkout(t, self.checkout)]
        owned = owner_io("read", {"paths": inside}, self.checkout, self.owner) if inside else {}
        for number, target in enumerate(targets):
            if in_checkout(target, self.checkout):
                item = owned.get(str(target))
                data = None if item is None else base64.b64decode(item["data"])
                meta = None if item is None else {"mode": item["mode"]}
            else:
                plain = read_plain(target)
                data = None if plain is None else plain[0]
                meta = None if plain is None else {"mode": stat.S_IMODE(plain[1].st_mode),
                                                    "uid": plain[1].st_uid, "gid": plain[1].st_gid}
            if data is None:
                self.index["created"].append(str(target))
                continue
            copy = f"files/{number:04d}"
            write_plain(self.root / copy, data, os.geteuid(), os.getegid(), 0o600)
            self.index["saved"].append({"target": str(target), "copy": copy, **meta})
        self._save_index()

    def restore(self):
        inside = [item for item in self.index["saved"] if in_checkout(item["target"], self.checkout)]
        if inside:
            owner_io("write", {"files": [{"path": item["target"], "mode": item["mode"],
                                          "data": base64.b64encode((self.root / item["copy"]).read_bytes()).decode()}
                                         for item in inside]}, self.checkout, self.owner)
        for item in self.index["saved"]:
            if not in_checkout(item["target"], self.checkout):
                write_plain(item["target"], (self.root / item["copy"]).read_bytes(), item["uid"], item["gid"], item["mode"])
        created_inside = [t for t in self.index["created"] if in_checkout(t, self.checkout)]
        if created_inside:
            owner_io("remove", {"paths": created_inside}, self.checkout, self.owner)
        for target in self.index["created"]:
            if not in_checkout(target, self.checkout):
                check_root_dir(Path(target).parent)
                Path(target).unlink(missing_ok=True)  # unlink never follows a link


def snapshot_databases(backup):
    """Copy each database as the account that owns it into a folder only that account can write.

    The release's own ops/backup/sqlite_snapshot.py does the copy (paced, WAL-aware), passed as code
    because the database owners cannot read root's copy of the repository.
    """
    import pwd
    code = (REPO / "ops/backup/sqlite_snapshot.py").read_text()
    copies = []
    for number, path in enumerate(DATABASES):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid == 0:
            print(f"note: not copying {path} (not a regular file owned by a service account)")
            continue
        user = pwd.getpwuid(info.st_uid).pw_name
        folder = backup.root / f"db-{number}"
        folder.mkdir(mode=0o700)
        os.chown(folder, info.st_uid, info.st_gid)
        source = Path(path)
        result = run("/usr/bin/python3", "-I", "-c", code, str(source.parent), str(folder), str(source),
                     user=user, check=False)
        if result.returncode:
            raise ValueError(f"could not copy {path} before the update: "
                             + (result.stderr.strip().splitlines() or ["no output"])[-1])
        copies.append(str(folder / source.name))
    return copies


class Log:
    """stdout plus a file in the backup folder; a dropped SSH session must not stop the update."""

    def __init__(self, stream):
        self.stream, self.file = stream, None

    def write(self, text):
        if self.file:
            self.file.write(text)
            self.file.flush()
        try:
            self.stream.write(text)
            self.stream.flush()
        except (BrokenPipeError, OSError):
            pass
        return len(text)

    def flush(self):
        pass


def telegram_connected(since, deadline):
    while time.time() < deadline:
        log = run("journalctl", "-u", "hermes-gateway", "--since", since, "--no-pager", "-o", "cat", check=False).stdout
        if "does not match PSY_BOT_ID" in log:
            return False
        if "Connected to Telegram" in log:
            return True
        time.sleep(3)
    return False


def restart(units):
    """Sockets first, then SERVICES in order; the names of those not running afterwards."""
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    failed = []
    for unit in sorted(u for u in units if u not in SERVICES) + [u for u in SERVICES if u in units]:
        run("systemctl", "restart", unit, check=False)
        deadline = time.time() + 60
        while run("systemctl", "is-active", "--quiet", unit, check=False).returncode and time.time() < deadline:
            time.sleep(2)
        if run("systemctl", "is-active", "--quiet", unit, check=False).returncode:
            failed.append(unit)
    if "hermes-gateway" in units and "hermes-gateway" not in failed:
        if not telegram_connected(since, time.time() + 120) or \
                run("systemctl", "is-active", "--quiet", "hermes-gateway", check=False).returncode:
            failed.append("hermes-gateway (no Telegram connection)")
    return failed


def commit_label():
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def roll_back_to(backup):
    """Put a backup's files back and restart what it touched; the names still not running."""
    for timer in backup.index.get("enabled") or ():
        run("systemctl", "disable", "--now", timer, check=False)
    backup.restore()
    if backup.index.get("reload"):
        run("systemctl", "daemon-reload", check=False)
    units = set(backup.index.get("units") or ())
    return restart(units) if units else []


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "apply", "rollback"))
    parser.add_argument("backup", nargs="?", help="for rollback: the /var/lib/psy-deploy folder to put back")
    parser.add_argument("--force", action="store_true", help="apply even while group or recording work is in progress; rollback a backup that is not the latest")
    parser.add_argument("--commit", default=None, help="label for the record when the checkout has no .git")
    opts = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run as root on the PSY host.")
    if opts.action == "rollback":
        if not opts.backup or Path(opts.backup).resolve().parent != BACKUPS:
            sys.exit(f"rollback needs a folder directly under {BACKUPS}")
        # An older backup removes files that later releases added and still need (psy_common, new
        # modules). Only the backup of the update the host runs now can be put back safely.
        record = read_plain(RECORD)
        latest = dict(line.split("=", 1) for line in (record[0].decode() if record else "").splitlines() if "=" in line)
        if latest.get("backup") != str(Path(opts.backup).resolve()) and not opts.force:
            sys.exit(f"{opts.backup} is not the backup of the current update ({latest.get('backup', 'none recorded')}); "
                     "roll back the newer updates first, or pass --force")
        still = roll_back_to(Backup(root=Path(opts.backup).resolve()))
        sys.exit(f"still not running: {', '.join(still)}" if still else f"Restored {opts.backup}.")
    import pwd
    try:
        sums = release_sums()
        check_group_manifest(sums)
        entries = system_entries(pwd.getpwnam("hermes").pw_uid) + gateway_entries(sums)
        check_instance()
        check_backup_config()
        todo = plan(entries)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(f"refused: {exc}")
    retire, kept = retired_dropins(entries)
    for kind, entry in todo:
        print(f"  {kind:<7} {entry.target}  <- {entry.source}")
    for conf in retire:
        print(f"  retire  {conf}  (the shipped unit already says this)")
    for conf in kept:
        print(f"  keep    {conf}  (local override, not in the release)")
    targets = [e.target for _, e in todo] + retire
    units = set().union(*(units_for(t) for t in targets)) if targets else set()
    units = {u for u in units if not run("systemctl", "is-enabled", "--quiet", u, check=False).returncode}
    reload_units = any(str(t).startswith(str(SYSTEMD) + "/") for t in targets)
    if not targets:
        print("The host already runs this release.")
        return
    print(f"{len(targets)} change(s); restart: {', '.join(u for u in SERVICES if u in units) or 'nothing'}"
          f"{'; systemd reload' if reload_units else ''}")
    if opts.action == "plan":
        return
    reasons = busy() if units else []
    if reasons and not opts.force:
        sys.exit("refused, work in progress: " + "; ".join(reasons) + ". Retry later or pass --force.")

    # From here on a dropped SSH session must not leave the host half-updated.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    sys.stdout = Log(sys.stdout)
    commit = opts.commit or commit_label()
    try:
        backup = Backup(commit[:12])
        sys.stdout.file = os.fdopen(os.open(backup.root / "apply.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                            | os.O_NOFOLLOW, 0o600), "a", encoding="utf-8")
        backup.keep(targets)
        backup.note(units, reload_units)
        for copy in snapshot_databases(backup):
            print(f"  database copy {copy}")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"refused before changing anything: {exc}")
    print(f"Previous files: {backup.root} (put back by hand with: python3 ops/update_live.py rollback {backup.root})")

    def roll_back(reason):
        print(f"FAILED: {reason}\nRestoring the previous files from {backup.root}")
        still = roll_back_to(backup)
        sys.exit("rolled back" + (f"; still not running: {', '.join(still)}" if still else ""))

    try:
        inside = [e for _, e in todo if in_checkout(e.target)]
        if inside:
            owner_io("write", {"files": [{"path": str(e.target), "mode": e.mode,
                                          "data": base64.b64encode(e.data).decode()} for e in inside]})
        for _, entry in todo:
            if not in_checkout(entry.target):
                write(entry.target, entry.data, entry.owner, entry.mode)
        for conf in retire:
            check_root_dir(conf.parent)
            conf.unlink()
        if reload_units:
            run("systemctl", "daemon-reload")
        # As the service runs it: the gateway's ExecStartPre has no root rights either.
        check = run("/usr/bin/python3", "-I", str(GROUP_LIB / "preflight.py"), user="hermes", check=False)
        if check.returncode:
            roll_back("preflight: " + (check.stderr or check.stdout).strip())
        # Enable new timers before the restarts: the per-minute alert check already runs the new
        # code and would report a timer the release expects as disabled.
        fresh = [t for t in new_timers([e.target for kind, e in todo if kind == "new"])
                 if run("systemctl", "is-enabled", "--quiet", t, check=False).returncode]
        backup.index["enabled"] = fresh
        backup.note(units, reload_units)
        for timer in fresh:
            run("systemctl", "enable", "--now", timer)
            print(f"  enabled {timer}")
        failed = restart(units)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        roll_back(str(exc))
    if failed:
        roll_back("not running after restart: " + ", ".join(failed))
    record = read_plain(RECORD)
    write_plain(RECORD, f"commit={commit}\nupdated_at={backup.root.name[:16]}\nchanges={len(targets)}\nbackup={backup.root}\n".encode(),
                0, 0, stat.S_IMODE(record[1].st_mode) if record else 0o644)
    print(f"Updated to {commit[:12]}: {len(targets)} change(s), restarted {', '.join(u for u in SERVICES if u in units) or 'nothing'}. "
          f"Previous files: {backup.root}")


if __name__ == "__main__":
    main()
