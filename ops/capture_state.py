"""Capture a private, verified baseline before changing a running PSY installation.

Run as root. Profile files are read and snapshotted by hermes, never opened by root.
Only checksums, paths and nonsecret runtime metadata are printed.
No credentials or conversation are exported.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import time


UNIT = "hermes-gateway.service"
USER = "hermes"
PROFILE = Path("/home/hermes/.hermes")
CHECKOUT = PROFILE / "hermes-agent"
PROC = Path("/proc")
SNAPSHOTS = Path("/var/lib/psy-deploy")
ROOT_FILES = (Path("/etc/psy/instance.env"), Path("/etc/psy/deployed"))
PROPERTIES = ("MainPID", "User", "ActiveState", "SubState", "FragmentPath", "InvocationID",
              "ExecMainStartTimestampMonotonic")


def run(command, **options):
    """Never propagate a helper's stdout/stderr into an exception or terminal log."""
    try:
        return subprocess.run(command, capture_output=True, check=True, timeout=options.pop("timeout", 30), **options)
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError("Baseline command failed") from None


def service_state(uid):
    shown = run(["systemctl", "show", UNIT, "--no-pager", *["--property=" + key for key in PROPERTIES]],
                text=True).stdout
    fields = dict(line.partition("=")[::2] for line in shown.splitlines() if "=" in line)
    try:
        pid = int(fields["MainPID"])
        started = int(fields["ExecMainStartTimestampMonotonic"])
        if (pid <= 0 or started <= 0 or fields["User"] not in (USER, str(uid))
                or fields["ActiveState"] != "active" or fields["SubState"] != "running"
                or not re.fullmatch(r"[0-9a-f]{32}", fields["InvocationID"])):
            raise ValueError
        process = PROC / str(pid)
        if process.stat().st_uid != uid:
            raise ValueError
        status = dict(line.partition(":")[::2] for line in (process / "status").read_text().splitlines() if ":" in line)
        if tuple(map(int, status["Uid"].split())) != (uid,) * 4:
            raise ValueError
        raw = (process / "stat").read_text()
        ticks = int(raw[raw.rfind(")") + 2:].split()[19])
        executable = os.readlink(process / "exe")
        if not executable.startswith("/") or executable.endswith(" (deleted)") or any(c in executable for c in "\n\r\0"):
            raise ValueError
        live = (process / "exe").stat()
        target = Path(executable).stat()
        if ((live.st_dev, live.st_ino) != (target.st_dev, target.st_ino)
                or not stat.S_ISREG(live.st_mode) or live.st_uid not in (0, uid)
                or live.st_mode & 0o022):
            raise ValueError
    except (KeyError, ValueError, IndexError, OSError):
        raise RuntimeError("Gateway process identity could not be verified") from None
    return {"unit": UNIT, "main_pid": pid, "uid": uid, "start_ticks": ticks,
            "invocation_id": fields["InvocationID"], "started_monotonic_us": started,
            "unit_fragment": fields["FragmentPath"], "active_state": fields["ActiveState"],
            "sub_state": fields["SubState"], "executable": executable,
            "executable_identity": {"device": live.st_dev, "inode": live.st_ino,
                                    "size": live.st_size, "mtime_ns": live.st_mtime_ns}}


def profile_code(implementation, uid):
    bootstrap = f'''
import os
import sys
from pathlib import Path
if os.geteuid() != {uid!r} or os.getuid() != {uid!r}:
    raise RuntimeError("Wrong baseline account")
profile_path = Path({str(PROFILE)!r})
os.environ["HERMES_HOME"] = str(profile_path)
checkout = Path({str(CHECKOUT)!r})
sys.path.insert(0, str(checkout))
from pm.environments import committed_venv, site_packages, venv_python_version
environment = committed_venv(checkout)
if environment is None or venv_python_version(environment) != tuple(sys.version_info[:2]):
    raise RuntimeError("Committed dependency environment does not match the running interpreter")
packages = site_packages(environment)
sys.path.insert(1, str(packages))
from hermes_cli.config import load_config_readonly
selected_model = load_config_readonly()["model"]
model_name = selected_model.get("default")
provider = selected_model.get("provider")
if not isinstance(model_name, str) or not model_name or not isinstance(provider, str) or not provider:
    raise RuntimeError("Missing configured model")
'''
    return bootstrap + implementation + '''
snapshot = capture_runtime(model_name, include_manifest=True, profile=profile_path,
    code_dir=checkout / "plugins/platforms/telegram",
    policy={"provider": provider, "reasoning_policy":"baseline_unmodified",
            "capture_kind":"predeployment", "model_source":"profile_config"})
if committed_venv(checkout) != environment:
    raise RuntimeError("Dependency environment changed during baseline capture")
snapshot["capture_runtime"] = {"python": list(sys.version_info[:3]),
    "executable": str(Path(sys.executable).resolve()), "uid": os.getuid(), "effective_uid": os.geteuid(),
    "dependency_environment": str(environment), "dependency_source": "committed_venv",
    "site_packages": str(packages)}
print(json.dumps(snapshot))
'''


def capture():
    if os.geteuid() != 0:
        raise RuntimeError("Run baseline capture as root")
    repo = Path(__file__).resolve().parents[1]
    implementation = (repo / "gateway/plugins/platforms/telegram/psy_group_observability.py").read_text()
    uid = pwd.getpwnam(USER).pw_uid
    if type(uid) is not int or uid <= 0:
        raise RuntimeError("Baseline requires a non-root hermes account")
    before = service_state(uid)
    code = profile_code(implementation, uid)
    # The implementation is the reviewed release file, not code supplied by the service account.
    # Even an interpreter writable by hermes is executed only after dropping root.
    result = run(["runuser", "-u", USER, "--", before["executable"], "-I", "-"],
                 input=code, text=True, timeout=120, cwd="/")
    try:
        profile = json.loads(result.stdout)
        runtime = profile["capture_runtime"]
        if (runtime["executable"] != before["executable"] or runtime["uid"] != uid
                or runtime["effective_uid"] != uid or not profile["snapshot_verified"]):
            raise ValueError
    except (TypeError, KeyError, ValueError):
        raise RuntimeError("Invalid baseline helper result") from None
    if service_state(uid) != before:
        raise RuntimeError("Gateway changed during baseline capture")
    folder = SNAPSHOTS / ("baseline-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    folder.mkdir(mode=0o700)
    sums = {}
    for path in ROOT_FILES:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode):
                raise RuntimeError("Unexpected root configuration ownership")
            data = stream.read()
        destination = folder / path.name
        destination.write_bytes(data)
        destination.chmod(0o600)
        sums[path.name] = hashlib.sha256(data).hexdigest()
    unit = run(["systemctl", "cat", UNIT]).stdout
    (folder / "hermes-gateway.unit").write_bytes(unit)
    (folder / "hermes-gateway.unit").chmod(0o600)
    sums["hermes-gateway.unit"] = hashlib.sha256(unit).hexdigest()
    for name, expected in sums.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Baseline verification failed")
    if service_state(uid) != before:
        raise RuntimeError("Gateway changed while saving baseline")
    report = {"profile": profile, "gateway_runtime": before, "root_snapshot": str(folder), "root_sha256": sums,
              "verified": True, "created_at": time.time()}
    (folder / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    (folder / "manifest.json").chmod(0o600)
    return report


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2))
