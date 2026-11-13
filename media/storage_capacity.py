"""Capacity accounting for retained Mini App recordings. No cleanup or content reads."""
import os
from pathlib import Path
import re
import shutil
import stat

# Matches the upper bound enforced by the ASR service and socket client.
MAX_UPLOAD_BYTES = 1024 ** 3
SYSTEM_RESERVE_BYTES = 768 * 1024 ** 2
MAX_TRANSCRIPT_BYTES = 16 * 1024**2
DEFAULT_MEMBER_QUOTA = 4 * 1024**3
PENDING_RESULT = ('uploading', 'queued', 'processing')


def _regular_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Unexpected upload storage object')
    return info


def _member_usage(conn, files, uid):
    used = reserved = 0
    sources = set()
    for ident, ext, size, offset, state in conn.execute(
        'SELECT id,ext,size,offset,state FROM jobs WHERE uid=?', (uid,)
    ):
        if not re.fullmatch('[0-9a-f]{32}', ident) or not re.fullmatch(r'\.[a-z0-9]{1,8}', ext) or ext == '.txt':
            raise ValueError('Unexpected upload storage identifier')
        source = _regular_file(files / (ident + ext))
        if source is not None:
            inode = (source.st_dev, source.st_ino)
            if inode not in sources:
                used += source.st_size
                sources.add(inode)
        transcript = _regular_file(files / (ident + '.txt'))
        text_size = transcript.st_size if transcript else 0
        used += text_size
        if state == 'uploading':
            # Bytes written by an uncommitted PATCH may be rewound when it is retried.
            reserved += max(0, size - max(offset, source.st_size if source else 0))
        if state in PENDING_RESULT:
            reserved += max(0, MAX_TRANSCRIPT_BYTES - text_size)
    return used, reserved


def capacity_snapshot(conn, files, uid=None, *, max_upload_bytes, reserve_bytes, quota_bytes=None):
    """Call inside the write transaction for admission; uid=None is aggregate-only and never lists files."""
    files = Path(files)
    if quota_bytes is None:
        quota_bytes = int(os.environ.get('PSY_MEMBER_QUOTA_BYTES', DEFAULT_MEMBER_QUOTA))
    if min(max_upload_bytes, quota_bytes) <= 0 or reserve_bytes < 0:
        raise ValueError('Invalid upload storage limits')
    unreceived, results = conn.execute(
        """SELECT coalesce(sum(CASE WHEN state='uploading' THEN max(0,size-offset) ELSE 0 END),0), count(*)
           FROM jobs WHERE state IN ('uploading','queued','processing')"""
    ).fetchone()
    pending = unreceived + results * MAX_TRANSCRIPT_BYTES
    free = shutil.disk_usage(files).free
    disk_available = max(0, free - reserve_bytes - pending)
    required = max_upload_bytes + MAX_TRANSCRIPT_BYTES

    member_used = member_reserved = member_available = member_low = None
    if uid is not None:
        member_used, member_reserved = _member_usage(conn, files, uid)
        member_available = max(0, quota_bytes - member_used - member_reserved)
        member_low = member_available < required
    return dict(disk_free_bytes=free, pending_bytes=pending, disk_available_bytes=disk_available,
                disk_low=disk_available < required, member_limit_bytes=quota_bytes,
                member_used_bytes=member_used, member_reserved_bytes=member_reserved,
                member_available_bytes=member_available, member_low=member_low)


def admission_reason(snapshot, source_bytes, *, retry=False):
    """New recording, new transcript using existing audio (size=0), or retry an accepted job."""
    if type(source_bytes) is not int or source_bytes < 0 or (retry and source_bytes):
        raise ValueError('Invalid requested byte count')
    demand = source_bytes + MAX_TRANSCRIPT_BYTES
    if not retry:
        remaining = snapshot['member_available_bytes']
        if remaining is None:
            raise ValueError('A participant is required for upload admission')
        if demand > remaining:
            return 'member_quota'
    if demand > snapshot['disk_available_bytes']:
        return 'disk_capacity'
    return None
