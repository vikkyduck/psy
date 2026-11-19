"""Add --hash lines to pip lock files from PyPI's JSON API (all files of each pinned release).

Usage: python3 tools/hash_locks.py LOCKFILE...   (rewrites each file in place; needs pypi.org)
Run it after changing any pinned version; tools/test_lock_hashes.py checks the result.
"""
import json
import re
import sys
import urllib.request

PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!_-]+)$")


def digests(name, version):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        release = json.load(response)
    found = sorted({f["digests"]["sha256"] for f in release["urls"] if not f.get("yanked")})
    if not found:
        raise SystemExit(f"{name}=={version}: no files on PyPI")
    return found


def rewrite(path):
    out = []
    header = ("# Every pinned release with the sha256 of each of its files on PyPI.\n"
              "# pip installs from this file in --require-hashes mode: a changed or substituted\n"
              "# file is refused. Regenerate the hashes whenever a version changes.\n")
    for raw in open(path, encoding="utf-8").read().splitlines():
        line = raw.split("\\")[0].strip()
        if not line or line.startswith("#") or line.startswith("--hash"):
            continue
        match = PIN.match(line)
        if not match:
            raise SystemExit(f"{path}: not an exact pin: {line!r}")
        hashes = digests(*match.groups())
        out.append(" \\\n".join([line] + [f"    --hash=sha256:{h}" for h in hashes]))
        print(f"{path}: {line} ({len(hashes)} files)", file=sys.stderr)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(header + "\n".join(out) + "\n")


for lock in sys.argv[1:]:
    rewrite(lock)
