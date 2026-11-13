"""Apply or verify the reviewed PSY gateway release at the pinned Hermes commit."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

BASE = "71d34bad1045610252eda02ee79549436896e784"
ORIGIN = "https://github.com/NousResearch/hermes-agent"
HERE = Path(__file__).resolve().parent
TELEGRAM = "plugins/platforms/telegram/"
TRACKED = (
    "agent/auxiliary_client.py", "agent/credential_pool.py", "hermes_cli/auth.py",
    "hermes_cli/auth_codex.py", TELEGRAM + "adapter.py", TELEGRAM + "update_admission.py",
    "tools/transcription_tools.py",
)
MODULES = ("psy_group_audio", "psy_group_codex", "psy_group_queue", "psy_group_route",
           "psy_owner_recovery", "psy_source_evidence", "psy_group_store", "psy_instance", "psy_local_media", "psy_upload_handoff")
ADDED = tuple(TELEGRAM + name + ".py" for name in MODULES) + (
    "hermes_cli/psy_codex_policy.py", "hermes_cli/psy_codex_incident.py",
)
HANDOFF = TELEGRAM + "psy_upload_handoff.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(repo, apply):
    repo = repo.resolve(strict=True)
    if (Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo
            or git(repo, "rev-parse", "HEAD").decode().strip() != BASE
            or git(repo, "remote", "get-url", "origin").decode().strip().removesuffix(".git") != ORIGIN):
        raise ValueError("Expected the release checkout at the pinned official Hermes commit")
    manifest = json.loads((HERE/"manifest.json").read_bytes())
    sums = manifest["sha256"]
    patch = HERE/"hermes.patch"
    if (manifest["base"] != BASE or set(sums) != set(TRACKED + ADDED)
            or digest(patch.read_bytes()) != manifest["patch_sha256"]):
        raise ValueError("Release inventory or patch integrity mismatch")
    dirty = set(filter(None, git(repo, "diff", "--name-only", "-z", BASE).decode().split("\0")))
    if dirty.difference(TRACKED):
        raise ValueError("Checkout contains independently modified tracked files")
    missing_tracked = []
    missing_added = {}
    for relative in TRACKED + ADDED:
        path = repo/relative
        if path.is_symlink() or path.parent.resolve() != path.parent or path.exists() and not path.is_file():
            raise ValueError(f"Unexpected file type: {relative}")
        expected = sums[relative]
        current = digest(path.read_bytes()) if path.exists() else None
        if relative in ADDED:
            source = HERE.parent/"media/telegram_handoff.py" if relative == HANDOFF else HERE/relative
            content = source.read_bytes()
            if digest(content) != expected:
                raise ValueError(f"Release source integrity mismatch: {relative}")
            if current is None:
                missing_added[path] = content
            elif current != expected:
                raise ValueError(f"Checkout contains a different extension: {relative}")
        elif current != expected:
            base = git(repo, "show", BASE + ":" + relative)
            if current != digest(base):
                raise ValueError(f"Checkout contains an independent edit: {relative}")
            missing_tracked.append(relative)
    if not apply and (missing_tracked or missing_added):
        raise ValueError("The full PSY release is not installed")
    if missing_tracked:
        options = ["--include=" + path for path in missing_tracked]
        git(repo, "apply", "--check", *options, str(patch))
        git(repo, "apply", *options, str(patch))
    for path, data in missing_added.items():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
        try:
            temporary.write_bytes(data)
            temporary.chmod(0o644)
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    for relative, expected in sums.items():
        if digest((repo/relative).read_bytes()) != expected:
            raise ValueError(f"Final integrity check failed: {relative}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "verify"])
    parser.add_argument("checkout", type=Path)
    opts = parser.parse_args()
    try:
        run(opts.checkout, opts.action == "apply")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"PSY gateway {opts.action} OK")
