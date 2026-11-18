#!/usr/bin/python3
"""Local-only durable archive of generated media and conversation text."""
import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile


def _common(default="/usr/local/lib/psy-common/psy_common.py"):
    """The shared PSY helpers, loaded by explicit path and never through sys.path: `default`, the
    root-owned installed copy unless a release tool passes the one shipped with it, or in tests
    the copy PSY_COMMON_PATH names."""
    module = sys.modules.get("psy_common")
    if module is None:
        path = str(os.environ.get("PSY_COMMON_PATH") or default)
        spec = importlib.util.spec_from_file_location("psy_common", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module = sys.modules.setdefault("psy_common", module)
    return module


common = _common()

HOME = pathlib.Path('/home/hermes/.hermes')
VAULT = HOME / 'vault'
# Size and modification time of every source already archived, so an unchanged file is not
# read and hashed again on every run.
MEDIA_CACHE = '.archive-media-cache.json'


def atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One file per message: the archive keeps its earlier pace and syncs nothing per file.
    common.atomic_write(path, data, durable=False)


def load_cache():
    try:
        cache = json.loads((VAULT / MEDIA_CACHE).read_text())
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def archive_media():
    count = 0
    source = HOME / 'cache/generated'
    if not source.exists():
        return count
    cache, seen = load_cache(), {}
    for path in source.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        before = path.stat()
        relative = path.relative_to(source)
        key = str(relative)
        known = cache.get(key)
        if (isinstance(known, list) and len(known) == 3 and known[:2] == [before.st_size, before.st_mtime_ns]
                and target_for(relative, known[2]).exists()):
            seen[key] = known
            continue
        digest_obj = hashlib.sha256()
        # Large generated audio/video must never be loaded into RAM as one blob.
        with path.open('rb') as source_stream:
            while block := source_stream.read(1024 * 1024):
                digest_obj.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            continue  # Do not archive an image/video still being written.
        digest = digest_obj.hexdigest()
        target = target_for(relative, digest)
        if not target.exists():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as out, path.open('rb') as source_stream:
                    copied = hashlib.sha256()
                    while block := source_stream.read(1024 * 1024):
                        out.write(block)
                        copied.update(block)
                final = path.stat()
                if copied.hexdigest() != digest or (final.st_size, final.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                    continue
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                pathlib.Path(temporary).unlink(missing_ok=True)
            count += 1
        seen[key] = [before.st_size, before.st_mtime_ns, digest]
    if seen != cache:   # files that disappeared from the cache folder drop out here
        atomic_write(VAULT / MEDIA_CACHE, json.dumps(seen).encode())
    return count


def target_for(relative, digest):
    return VAULT / 'generated' / relative.parent / (relative.stem + '--' + digest[:16] + relative.suffix)


def archive_messages():
    checkpoint = VAULT / '.archive-checkpoint.json'
    try:
        previous = json.loads(checkpoint.read_text())['last_message_id']
    except (OSError, ValueError, KeyError):
        previous = 0
    database = HOME / 'state.db'
    if not database.exists():
        return 0
    count = 0
    # A WAL database between writes has no -wal/-shm, which this read-only mount cannot create.
    connection = common.open_readonly(database, timeout=10)
    try:
        rows = connection.execute('SELECT id, session_id, role, content, timestamp, tool_calls FROM messages WHERE id > ? ORDER BY id', (previous,))
        for message_id, session_id, role, content, timestamp, calls in rows:
            if role in ('user', 'assistant') and content and not calls:
                session = hashlib.sha256(session_id.encode()).hexdigest()[:24]
                record = {'message_id': message_id, 'session_id': session_id, 'role': role, 'timestamp': timestamp, 'content': content}
                atomic_write(VAULT / 'conversations' / session / (str(message_id) + '.json'), json.dumps(record, ensure_ascii=False, indent=2).encode())
                count += 1
            previous = message_id
        atomic_write(checkpoint, json.dumps({'last_message_id': previous}).encode())
    finally:
        connection.close()
    return count


if __name__ == '__main__':
    os.umask(0o077)
    print('Local vault archive: new_media=' + str(archive_media()) + ' new_messages=' + str(archive_messages()))
