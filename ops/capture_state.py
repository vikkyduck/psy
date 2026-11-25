"""Capture a private, verified baseline before changing a running PSY installation.

Run as root. Profile files are read and snapshotted by hermes, never opened by root.
Only checksums and paths are printed. No credentials or conversation are exported.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time


def capture():
    if os.geteuid() != 0:
        raise RuntimeError("Run baseline capture as root")
    repo = Path(__file__).resolve().parents[1]
    implementation = (repo / "gateway/plugins/platforms/telegram/psy_group_observability.py").read_text()
    bootstrap = '''
import sys
from pathlib import Path
checkout = Path("/home/hermes/.hermes/hermes-agent")
sys.path.insert(0, str(checkout))
from pm.environments import committed_venv, site_packages
environment = committed_venv(checkout)
if environment is None:
    raise RuntimeError("Missing committed runtime environment")
sys.path.insert(1, str(site_packages(environment)))
'''
    code = bootstrap + implementation + '''
print(json.dumps(capture_runtime("see-profile-config.yaml",
    include_manifest=True,
    profile="/home/hermes/.hermes",
    code_dir="/home/hermes/.hermes/hermes-agent/plugins/platforms/telegram",
    policy={"reasoning_policy":"baseline_unmodified","capture_kind":"predeployment"})))
'''
    # The implementation is the reviewed release file, not code supplied by the service account.
    result = subprocess.run(["runuser", "-u", "hermes", "--", "python3", "-I", "-"],
                            input=code, text=True, capture_output=True, check=True, timeout=120, cwd="/")
    profile = json.loads(result.stdout)
    folder = Path("/var/lib/psy-deploy") / ("baseline-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    folder.mkdir(mode=0o700)
    sums = {}
    for path in (Path("/etc/psy/instance.env"), Path("/etc/psy/deployed")):
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
    unit = subprocess.run(["systemctl", "cat", "hermes-gateway.service"], capture_output=True, check=True).stdout
    (folder / "hermes-gateway.unit").write_bytes(unit)
    (folder / "hermes-gateway.unit").chmod(0o600)
    sums["hermes-gateway.unit"] = hashlib.sha256(unit).hexdigest()
    for name, expected in sums.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Baseline verification failed")
    report = {"profile": profile, "root_snapshot": str(folder), "root_sha256": sums,
              "verified": True, "created_at": time.time()}
    (folder / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    (folder / "manifest.json").chmod(0o600)
    return report


if __name__ == "__main__":
    print(json.dumps(capture(), indent=2))
