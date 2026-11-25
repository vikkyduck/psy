"""Private, content-addressed runtime snapshots and text-free execution diagnostics.

Snapshots stay in the owning service account's vault. Their contents are never sent to a
model or printed in logs. Public metadata contains only hashes and effective policy.
"""
import hashlib
import errno
import json
import importlib.metadata
import os
from pathlib import Path
import stat
import tempfile
import sys


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Runtime snapshot needs regular files")
        return stream.read()


def capture_runtime(model, *, profile=None, code_dir=None, root=None, policy=None, include_manifest=False):
    profile = Path(profile or os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
    code_dir = Path(code_dir or Path(__file__).parent)
    root = Path(root or profile / "vault/group-psy/runtime-snapshots")
    files = {"code/" + p.name: _read(p) for p in sorted(code_dir.glob("psy_*.py"))}
    checkout = code_dir.parents[2] if len(code_dir.parents) > 2 else code_dir
    if (checkout / "agent/reasoning_effort.py").is_file():
        # These are the actual execution dependencies, not just the group wrappers.
        # In particular reasoning capabilities, context catalog and OAuth policy live here.
        for area in ("agent", "hermes_cli", "gateway", "pm", "plugins/platforms/telegram"):
            for path in sorted((checkout / area).rglob("*.py")):
                files["runtime/" + str(path.relative_to(checkout))] = _read(path)
        for relative in ("pyproject.toml", "uv.lock", ".git/HEAD", ".git/packed-refs"):
            path = checkout / relative
            if path.is_file():
                files["runtime/" + relative] = _read(path)
        head = files.get("runtime/.git/HEAD", b"").decode(errors="replace").strip()
        if head.startswith("ref: refs/") and ".." not in head:
            path = checkout / ".git" / head.removeprefix("ref: ")
            if path.is_file():
                files["runtime/git-reference"] = _read(path)
    environment = {"python": list(sys.version_info[:3]), "packages": sorted(
        (d.metadata.get("Name", "unknown"), d.version) for d in importlib.metadata.distributions())}
    files["runtime/environment.json"] = json.dumps(environment, sort_keys=True).encode()
    for name in ("config.yaml", "SOUL.md"):
        path = profile / name
        if path.exists():
            files["profile/" + name] = _read(path)
    marker = Path("/etc/psy/deployed")
    try:
        files["deployment.txt"] = _read(marker)
    except (FileNotFoundError, PermissionError):
        # Root's deployment record is deliberately not made readable to the agent.
        # Actual executable bytes below identify the release independently of that label.
        pass
    effective = {"model": model, "provider": "openai-codex", "route": "group",
                 "reasoning_policy": "highest_supported_no_downgrade", **(policy or {})}
    files["effective.json"] = json.dumps(effective, sort_keys=True, ensure_ascii=False).encode()
    sums = {name: digest(value) for name, value in files.items()}
    manifest = json.dumps(sums, sort_keys=True, separators=(",", ":")).encode()
    ident = digest(manifest)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / ident
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=root, prefix=".capture-") as temporary:
            folder = Path(temporary)
            for name, data in {**files, "manifest.json": manifest}.items():
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
            try:
                os.rename(folder, target)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    # Verify even an existing snapshot. An ID is not evidence that its files still exist.
    if _read(target / "manifest.json") != manifest or any(
            digest(_read(target / name)) != expected for name, expected in sums.items()):
        raise ValueError("Runtime snapshot verification failed")
    code_version = digest(json.dumps({k: v for k, v in sums.items() if k.startswith(("code/", "runtime/"))},
                                    sort_keys=True).encode())
    result = {"snapshot_id": ident, "code_version": code_version,
              "snapshot_verified": True, "file_count": len(sums), "effective": effective}
    if include_manifest:
        result["file_sha256"] = sums
    return result


def result_audit(details):
    """Export only operational data, never contract, excerpts, source gaps or conversation."""
    metadata = details.get("quality_metadata", {})
    safe = {key: metadata[key] for key in ("generation_audit", "generation", "context_audit",
            "runtime", "model_requested", "source_reason_codes", "source_current_reason_codes",
            "source_check_attempts", "coverage_status",
            "completion_version", "completion_review", "completion_repairs",
            "post_source_completion_review", "stop_reason", "stop_stage") if key in metadata}
    return {"task_status": details.get("task_status", "unknown"),
            "generation_complete": details.get("generation_complete"),
            "missing_requirements_count": len(details.get("missing_requirements", [])),
            "metadata": safe}
