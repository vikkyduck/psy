"""Apply or verify the reviewed PSY gateway release at the pinned Hermes commit.

TRACKED, ADDED and SHARED are the single release inventory. `release` regenerates manifest.json and
group-manifest.json (checked before every gateway start by ops/libexec/preflight.py) from it."""
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
MODULES = ("psy_group_audio", "psy_group_codex", "psy_group_client", "psy_group_pipeline",
           "psy_group_sources", "psy_group_lecture", "psy_group_continuation", "psy_group_queue",
           "psy_group_route", "psy_owner_recovery", "psy_source_evidence", "psy_group_store", "psy_instance",
           "psy_upload_handoff", "psy_group_admin", "psy_group_prompts")
ADDED = tuple(TELEGRAM + name + ".py" for name in MODULES) + (
    "hermes_cli/psy_codex_policy.py", "hermes_cli/psy_codex_incident.py",
)
HANDOFF = TELEGRAM + "psy_upload_handoff.py"
# Root-owned files outside the checkout that the gateway loads by explicit path (installed by
# ops/install-runtime and ops/update_live.py): installed path -> source in this repository.
SHARED = {"/usr/local/lib/psy-common/psy_common.py": "common/psy_common.py"}


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source(relative):
    return HERE.parent/"media/telegram_handoff.py" if relative == HANDOFF else HERE/relative


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def inventory(repo):
    """Hash the patched tracked files and the added sources without touching the checkout."""
    repo = repo.resolve(strict=True)
    patch = HERE/"hermes.patch"
    sums = {}
    with tempfile.TemporaryDirectory() as folder:
        tree = Path(folder)
        git(tree, "init", "-q")
        for relative in TRACKED:
            (tree/relative).parent.mkdir(parents=True, exist_ok=True)
            (tree/relative).write_bytes(git(repo, "show", BASE + ":" + relative))
        git(tree, "apply", str(patch))
        for relative in TRACKED:
            sums[relative] = digest((tree/relative).read_bytes())
    for relative in ADDED:
        sums[relative] = digest(source(relative).read_bytes())
    return {"base": BASE, "patch_sha256": digest(patch.read_bytes()), "sha256": sums, "shared": shared()}


def shared():
    return {installed: digest((HERE.parent/relative).read_bytes()) for installed, relative in SHARED.items()}


def release(repo):
    manifest = inventory(repo)
    write_json(HERE/"manifest.json", manifest)
    # The boundary check before service start covers the same files, by checkout-relative path,
    # and the shared files by their absolute installed path.
    write_json(HERE/"group-manifest.json", {**manifest["sha256"], **manifest["shared"]})


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
    if manifest.get("shared") != shared():
        raise ValueError("Release source integrity mismatch: " + ", ".join(SHARED.values()))
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
            content = source(relative).read_bytes()
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
    parser.add_argument("action", choices=["apply", "verify", "release"])
    parser.add_argument("checkout", type=Path)
    opts = parser.parse_args()
    try:
        if opts.action == "release":
            release(opts.checkout)
        else:
            run(opts.checkout, opts.action == "apply")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(f"PSY gateway {opts.action} OK")
