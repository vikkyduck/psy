"""Test-only CAS transaction used by the 2026-11-24 release regression examples.

The normal deployment tool is ops/update_live.py. This fixture has no CLI,
production paths, service control, SSH or credential access.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

def digest(data):
    return hashlib.sha256(data).hexdigest()

def regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Non-regular release target")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(fd)

def atomic(path, content, uid, gid, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.psy-release-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)

def install(package, selection, backup):
    targets = [item for item in package['targets'] if item['group'] in selection]
    prepared = []
    for item in targets:
        path = Path(item['path'])
        if path.is_symlink() or path.parent.resolve() != path.parent:
            raise RuntimeError('Symlinked release target')
        data = item['content'].encode()
        compile(data, str(path), 'exec')
        if digest(data) != item['after_sha256']:
            raise RuntimeError('Release integrity mismatch')
        before = regular(path) if path.exists() else None
        current = digest(before) if before is not None else None
        if current not in (item['before_sha256'], item['after_sha256']):
            raise RuntimeError('Concurrent source change: aborting without overwrite')
        if current == item['after_sha256']:
            continue
        metadata = path.stat() if before is not None else path.parent.stat()
        prepared.append((item, before, metadata))
    changed = []
    try:
        for index, (item, before, metadata) in enumerate(prepared):
            path = Path(item['path'])
            # Recheck immediately before each write, protecting independent maintenance.
            live = regular(path) if path.exists() else None
            if live != before:
                raise RuntimeError('Concurrent source change during release')
            if before is not None:
                snapshot = backup / ('original-' + str(index) + '-' + path.name)
                snapshot.write_bytes(before)
                snapshot.chmod(0o600)
            mode = stat.S_IMODE(metadata.st_mode) if before is not None else 0o644
            atomic(path, item['content'].encode(), metadata.st_uid, metadata.st_gid, mode)
            changed.append((item, before, metadata, mode))
        (backup / 'applied.json').write_text(json.dumps([
            {'path': item['path'], 'before_sha256': item['before_sha256'],
             'after_sha256': item['after_sha256'], 'uid': meta.st_uid,
             'gid': meta.st_gid, 'mode': mode}
            for item, before, meta, mode in changed], indent=2))
        return changed
    except BaseException:
        restore(changed)
        raise

def restore(changed):
    for item, before, metadata, mode in reversed(changed):
        path = Path(item['path'])
        if digest(regular(path)) != item['after_sha256']:
            raise RuntimeError('Refusing rollback over independent change')
        if before is None:
            path.unlink()
        else:
            atomic(path, before, metadata.st_uid, metadata.st_gid, mode)
