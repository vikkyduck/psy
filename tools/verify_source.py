"""Check a PSY source tree without reading private state or following symlinks."""
import ast
from pathlib import Path
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {"vault", "media-vault", "backups", "outputs", "transcripts", "recordings", "node_modules"}
PRIVATE_SUFFIX = {".log", ".db", ".sqlite", ".sqlite3", ".wav", ".mp3", ".m4a", ".ogg", ".mp4",
                  ".webm", ".aiff", ".aif", ".caf", ".flac", ".aac", ".opus", ".mov", ".jpg", ".jpeg", ".png", ".pdf", ".docx", ".zip", ".gz", ".pkl", ".safetensors",
                  ".pem", ".pfx", ".key", ".session"}
SECRETS = [
    rb"\b\d{7,15}:[A-Za-z0-9_-]{30,}\b",
    rb"\bgh[pousr]_[a-zA-Z0-9]{30,}\b",
    rb"\bgithub_pat_[a-zA-Z0-9_]{60,}\b",
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    rb"\beyJ[a-zA-Z0-9_-]{12,}\.eyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{12,}",
    rb"\bsk-(?:proj-|svcacct-)?[a-zA-Z0-9_-]{30,}\b",
]


def inspect(path):
    name = path.name
    relative = path.relative_to(ROOT)
    if path.is_symlink() or set(relative.parts) & PRIVATE_DIRS:
        raise ValueError(f"non-source path: {relative}")
    if (name in ("auth.json", "instance.env") or name == ".env" or name.startswith(".env.") and not name.endswith(".example")
            or path.suffix.lower() in PRIVATE_SUFFIX):
        raise ValueError(f"private file type: {relative}")
    data = path.read_bytes()
    if b"\x00" in data or any(re.search(pattern, data) for pattern in SECRETS):
        raise ValueError(f"binary or credential-like content: {relative}")
    data.decode("utf-8")
    if path.suffix == ".py":
        ast.parse(data, filename=str(relative))
    if path.suffix == ".sh":
        subprocess.run(["bash", "-n", str(path)], check=True)


def main():
    count = 0
    for directory, names, files in os.walk(ROOT):
        names[:] = [name for name in names if name not in (".git", "__pycache__", ".pytest_cache")]
        if any((Path(directory)/n).is_symlink() for n in names):
            raise ValueError("symlinked source directory")
        for name in files:
            inspect(Path(directory)/name)
            count += 1
    print(f"Verified {count} source files")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, SyntaxError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
