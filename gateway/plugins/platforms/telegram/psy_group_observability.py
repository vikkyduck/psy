"""Private, content-addressed runtime snapshots and text-free execution diagnostics.

Snapshots stay in the owning service account's vault. Their contents are never sent to a
model or printed in logs. Public metadata contains only hashes and effective policy.
"""
import hashlib
import errno
from functools import lru_cache
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
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Runtime snapshot needs regular files")
    return _read_version(str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@lru_cache(maxsize=8192)
def _read_version(path, device, inode, size, modified, changed):
    # Reuse bytes only while identity, length, mtime and ctime all match.
    # This includes snapshot files, so changed/corrupted snapshots are re-read.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Runtime snapshot needs regular files")
        return stream.read()


@lru_cache(maxsize=1)
def _environment():
    # Dependency environments are committed and fixed for a gateway process.
    return json.dumps({"python": list(sys.version_info[:3]), "packages": sorted(
        (d.metadata.get("Name", "unknown"), d.version) for d in importlib.metadata.distributions())},
        sort_keys=True).encode()


def _private_directory(path, *, parent=None, create=False):
    if create:
        try:
            os.mkdir(path, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise ValueError("Runtime snapshot directory has a foreign owner")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_private(folder, name, data):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or any(part in (".", "..") for part in parts):
        raise ValueError("Invalid runtime snapshot member")
    parent = _private_directory(folder)
    try:
        for part in parts[:-1]:
            child = _private_directory(part, parent=parent, create=True)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    finally:
        os.close(parent)


def _private_tree(folder):
    """Repair only owned inodes inside this opened tree, never links or foreign files."""
    def visit(parent):
        for name in os.listdir(parent):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _private_directory(name, parent=parent)
                try:
                    visit(child)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    current = os.fstat(fd)
                    if not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid() or current.st_nlink != 1:
                        raise ValueError("Runtime snapshot file has unsafe ownership or links")
                    if stat.S_IMODE(current.st_mode) != 0o600:
                        os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)
            else:
                raise ValueError("Runtime snapshot contains a link or special file")
    root_fd = _private_directory(folder)
    try:
        visit(root_fd)
    finally:
        os.close(root_fd)


def capture_runtime(model, *, profile=None, code_dir=None, root=None, policy=None, include_manifest=False):
    profile = Path(profile or os.environ.get("HERMES_HOME", "/home/hermes/.hermes"))
    code_dir = Path(code_dir or Path(__file__).parent)
    root = Path(root or profile / "vault/group-psy/runtime-snapshots")
    files = {"code/" + p.name: _read(p) for p in sorted(code_dir.glob("psy_*.py"))}
    checkout = code_dir.parents[2] if len(code_dir.parents) > 2 else code_dir
    if (checkout / "agent/reasoning_effort.py").is_file():
        # These are the actual execution dependencies, not just the group wrappers.
        # In particular reasoning capabilities, context catalog and OAuth policy live here.
        for area in ("agent", "hermes_cli", "gateway", "pm", "plugins/platforms/telegram", "plugins/image_gen"):
            for path in sorted((checkout / area).rglob("*.py")):
                files["runtime/" + str(path.relative_to(checkout))] = _read(path)
        # Image tier resolution and native request parameters live outside the group
        # wrapper. Keep both provider code and plugin declarations in the same baseline.
        for path in sorted((checkout / "plugins/image_gen").rglob("plugin.yaml")):
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
    files["runtime/environment.json"] = _environment()
    for name in ("manifest.json", "requirements.lock.txt"):
        path = Path("/opt/psy-context-tokenizer/current") / name
        if path.is_file():
            files["runtime/tokenizer/" + name] = _read(path)
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
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_fd = _private_directory(root, create=True)
    os.close(root_fd)
    target = root / ident
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=root, prefix=".capture-") as temporary:
            folder = Path(temporary)
            for name, data in {**files, "manifest.json": manifest}.items():
                _write_private(folder, name, data)
            _private_tree(folder)
            try:
                os.rename(folder, target)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    _private_tree(target)
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
    if type(metadata.get("current_generation_complete")) is bool:
        safe["current_generation_complete"] = metadata["current_generation_complete"]
    return {"task_status": details.get("task_status", "unknown"),
            "generation_complete": details.get("generation_complete"),
            "missing_requirements_count": len(details.get("missing_requirements", [])),
            "metadata": safe}
