"""Update a PSY host that is already serving from this checkout, with backup and automatic rollback.

Run as root from the repository root on the bot host:

    python3 ops/update_live.py plan            # what would change; writes nothing
    python3 ops/update_live.py apply [--force]

install-runtime stages an idle host; this is its counterpart for a running one. It installs the same
files, derives the Hermes edits from gateway/hermes.patch at the pinned commit, and leaves the owner's
profile (SOUL.md, skills), instance.env and private files alone. Drop-ins that only repeat what the
shipped unit now says are retired. Changed files are saved under /var/lib/psy-deploy first; a failed
preflight, service start or Telegram reconnect puts them back.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
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
LIBEXEC = Path("/usr/local/libexec")
UPLOAD = Path("/opt/psy-upload")
INSTANCE = Path("/etc/psy/instance.env")
BACKUPS = Path("/var/lib/psy-deploy")
RECORD = Path("/etc/psy/deployed")
GROUP_DB = "/home/hermes/.hermes/vault/group-psy/group.sqlite3"
UPLOAD_HEALTH = Path("/var/lib/psy-upload-health/state.json")
UPLOAD_PYTHON = "/opt/psy-upload-venv/bin/python"

# Kept in step with install-runtime.
MEDIA = ("access_broker", "asr_client", "asr_sandbox", "asr_service", "psy_upload_common", "queue_health",
         "recording_grant", "recording_receipts", "server", "storage_capacity", "transcribe", "worker")
HELPERS = {"psy_codex_auth.py": "psy-codex-auth.py", "psy_healthcheck.py": "psy-healthcheck.py",
           "psy_alerts.py": "psy-alerts.py", "psy_vault_archive.py": "psy-vault-archive.py",
           "upload_healthcheck.py": "psy-upload-healthcheck.py", "deploy-cert": "psy-upload-deploy-cert"}
ALERTED = ("hermes-gateway", "psy-healthcheck", "psy-upload-health", "psy-codex-auth", "psy-upload-cert-renew",
           "psy-upload", "psy-upload-worker", "psy-asr")
# Long-running services in restart order: the gateway last, once the upload side it calls is back.
SERVICES = ("psy-asr", "psy-upload-worker", "psy-upload", "hermes-gateway")
GATEWAY_IMPORTS = {"psy_upload_common.py", "storage_capacity.py", "recording_grant.py"}  # via psy_upload_handoff


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


def run(*cmd, user=None, check=True, text=True, env=None):
    if user:
        cmd = ("runuser", "-u", user, "--") + cmd
    return subprocess.run(cmd, check=check, capture_output=True, text=text, timeout=120, cwd="/", env=env)


def hermes_git(*args, text=True):
    return run("env", "GIT_OPTIONAL_LOCKS=0", "git", "-C", str(CHECKOUT), *args, user="hermes", text=text).stdout


# ---------- what the release says the host should contain ----------

def system_entries(hermes_uid):
    entries = [Entry(UPLOAD / f"{name}.py", (REPO / f"media/{name}.py").read_bytes(), f"media/{name}.py")
               for name in MEDIA]
    entries += [Entry(UPLOAD / "static" / f.name, f.read_bytes(), f"media/static/{f.name}")
                for f in sorted((REPO / "media/static").iterdir()) if f.is_file()]
    entries += [Entry(GROUP_LIB / "upload_broker.py", (REPO / "gateway/upload_broker.py").read_bytes(), "gateway/upload_broker.py"),
                Entry(GROUP_LIB / "preflight.py", (REPO / "ops/libexec/preflight.py").read_bytes(), "ops/libexec/preflight.py"),
                Entry(GROUP_LIB / "manifest.json", (REPO / "gateway/group-manifest.json").read_bytes(), "gateway/group-manifest.json")]
    entries += [Entry(LIBEXEC / dst, (REPO / "ops/libexec" / src).read_bytes(), f"ops/libexec/{src}",
                      mode=0o755 if src == "deploy-cert" else 0o644) for src, dst in HELPERS.items()]
    for unit in sorted((REPO / "ops/systemd").iterdir()):
        if unit.is_file() and not unit.name.startswith("psy-audio-priority."):
            entries.append(Entry(SYSTEMD / unit.name, unit.read_bytes(), f"ops/systemd/{unit.name}"))
    for conf in sorted((REPO / "ops/systemd/hermes-gateway.service.d").glob("*.conf")):
        entries.append(Entry(SYSTEMD / "hermes-gateway.service.d" / conf.name, conf.read_bytes(),
                             f"ops/systemd/hermes-gateway.service.d/{conf.name}"))
    entries.append(Entry(SYSTEMD / "hermes-gateway.service.d/user-session.conf",
                         f"[Unit]\nAfter=user@{hermes_uid}.service\nWants=user@{hermes_uid}.service\n".encode(), "generated"))
    entries += [Entry(SYSTEMD / f"{unit}.service.d/owner-alert.conf", b"[Unit]\nOnFailure=psy-alerts.service\n", "generated")
                for unit in ALERTED]
    return entries


def release_sums():
    manifest = json.loads((REPO / "gateway/manifest.json").read_bytes())
    sums = manifest["sha256"]
    if (manifest["base"] != RELEASE.BASE or set(sums) != set(RELEASE.TRACKED + RELEASE.ADDED)
            or sha256((REPO / "gateway/hermes.patch").read_bytes()) != manifest["patch_sha256"]):
        raise ValueError("gateway/manifest.json does not describe this patch and file list")
    return sums


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


# ---------- plan ----------

def plan(entries):
    return [(("new" if not e.target.exists() else "update"), e) for e in entries
            if not e.target.exists() or e.target.read_bytes() != e.data]


def units_for(target):
    text = str(target)
    if text.startswith(str(CHECKOUT) + "/"):
        return {"hermes-gateway"}
    if text.startswith(str(UPLOAD) + "/"):
        return {"psy-asr", "psy-upload-worker", "psy-upload"} | ({"hermes-gateway"} if target.name in GATEWAY_IMPORTS else set())
    if text.startswith(str(SYSTEMD) + "/"):
        name = Path(text).relative_to(SYSTEMD).parts[0].removesuffix(".d")
        unit = name.removesuffix(".service")
        return {unit} if unit in SERVICES else ({name} if name.endswith(".socket") else set())
    return set()


def check_instance():
    """Import the release's settings readers with this host's instance.env before anything changes."""
    env = {"PATH": "/usr/bin:/bin"}
    for line in INSTANCE.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            env[key.strip()] = value.strip()
    probes = [("/usr/bin/python3", REPO / "gateway" / RELEASE.TELEGRAM, "import psy_instance"),
              ("/usr/bin/python3", REPO / "gateway", "import upload_broker"),
              (UPLOAD_PYTHON, REPO / "media", "import psy_upload_common")]
    for python, folder, statement in probes:
        code = f"import sys; sys.path.insert(0, {str(folder)!r}); {statement}"
        result = run(python, "-I", "-B", "-c", code, check=False, env=env)
        if result.returncode:
            raise ValueError(f"{INSTANCE} is not accepted by the release ({statement}): "
                             + (result.stderr.strip().splitlines() or ["no output"])[-1])


GROUP_PROBE = """
import sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=5)
count = lambda where: db.execute("select count(*) from requests where " + where).fetchone()[0]
print(count("state in ('queued', 'running')"), count("state = 'uncertain'"))
"""


def busy():
    """Work a restart would cut off. The group database is read as hermes, never as root."""
    reasons = []
    active, stuck = map(int, run("/usr/bin/python3", "-I", "-c", GROUP_PROBE, GROUP_DB, user="hermes").stdout.split())
    if active:
        reasons.append(f"group queue has {active} request(s) in progress")
    if stuck:
        print(f"note: {stuck} group request(s) are stuck in 'uncertain' (not blocking)")
    queue = (json.loads(UPLOAD_HEALTH.read_text()).get("queue") or {}) if UPLOAD_HEALTH.exists() else {}
    if queue.get("queued") or queue.get("processing"):
        reasons.append(f"recordings: {queue.get('queued')} queued, {queue.get('processing')} processing")
    return reasons


# ---------- apply ----------

def write(target, data, owner, mode):
    import pwd
    account = pwd.getpwnam(owner)
    if target.exists():
        st = target.stat()
        uid, gid, mode = st.st_uid, st.st_gid, st.st_mode & 0o7777
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        uid, gid = account.pw_uid, account.pw_gid
    tmp = target.with_name(target.name + ".psy-new")
    tmp.write_bytes(data)
    os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, target)


class Backup:
    def __init__(self, label):
        self.root = BACKUPS / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{label}"
        self.root.mkdir(parents=True, mode=0o700)
        self.saved, self.created = [], []

    def keep(self, target):
        if target.exists():
            copy = self.root / str(target).lstrip("/")
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, copy)
            st = target.stat()
            os.chown(copy, st.st_uid, st.st_gid)
            self.saved.append((target, copy))
        else:
            self.created.append(target)

    def restore(self):
        for target, copy in self.saved:
            target.parent.mkdir(parents=True, exist_ok=True)
            st = copy.stat()
            tmp = target.with_name(target.name + ".psy-new")
            shutil.copy2(copy, tmp)
            os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, target)
        for target in self.created:
            target.unlink(missing_ok=True)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--force", action="store_true", help="apply even while group or recording work is in progress")
    parser.add_argument("--commit", default=None, help="label for the record when the checkout has no .git")
    opts = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run as root on the PSY host.")
    import pwd
    try:
        sums = release_sums()
        check_group_manifest(sums)
        entries = system_entries(pwd.getpwnam("hermes").pw_uid) + gateway_entries(sums)
        check_instance()
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        sys.exit(f"refused: {exc}")
    todo = plan(entries)
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

    commit = opts.commit or commit_label()
    backup = Backup(commit[:12])
    for target in targets:
        backup.keep(target)

    def roll_back(reason):
        print(f"FAILED: {reason}\nRestoring the previous files from {backup.root}")
        backup.restore()
        if reload_units:
            run("systemctl", "daemon-reload", check=False)
        still = restart(units) if units else []
        sys.exit("rolled back" + (f"; still not running: {', '.join(still)}" if still else ""))

    try:
        for _, entry in todo:
            write(entry.target, entry.data, entry.owner, entry.mode)
        for conf in retire:
            conf.unlink()
        if reload_units:
            run("systemctl", "daemon-reload")
        check = run("/usr/bin/python3", "-I", str(GROUP_LIB / "preflight.py"), check=False)
        if check.returncode:
            roll_back("preflight: " + (check.stderr or check.stdout).strip())
        failed = restart(units)
    except (OSError, subprocess.CalledProcessError) as exc:
        roll_back(str(exc))
    if failed:
        roll_back("not running after restart: " + ", ".join(failed))
    RECORD.write_text(f"commit={commit}\nupdated_at={backup.root.name[:16]}\nchanges={len(targets)}\nbackup={backup.root}\n")
    print(f"Updated to {commit[:12]}: {len(targets)} change(s), restarted {', '.join(u for u in SERVICES if u in units) or 'nothing'}. "
          f"Previous files: {backup.root}")


if __name__ == "__main__":
    main()
