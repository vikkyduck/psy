"""Physical free-space guard for Mini App recordings. No per-member quota, cleanup or content reads."""
import shutil
from pathlib import Path

# Matches the upper bound enforced by the ASR service and socket client.
MAX_UPLOAD_BYTES = 1024 ** 3
# Never handed out to recordings: larger than one maximum upload plus room for logs and databases.
SYSTEM_RESERVE_BYTES = MAX_UPLOAD_BYTES + 512 * 1024 ** 2
MAX_TRANSCRIPT_BYTES = 16 * 1024**2
PENDING_RESULT = ('uploading', 'queued', 'processing')


def demand(source_bytes, asr_bytes, asr_reserved):
    """New upload bytes, one transcript, and growth of the single ASR snapshot reservation.

    The ASR service copies the recording it decodes into its own directory on the same disk,
    one recording at a time, so the largest queued or running recording is reserved once."""
    return source_bytes + MAX_TRANSCRIPT_BYTES + max(0, asr_bytes - asr_reserved)


def capacity_snapshot(conn, files, *, max_upload_bytes, reserve_bytes):
    """Aggregate only: one indexed query and one statvfs, never a per-file listing."""
    if max_upload_bytes <= 0 or reserve_bytes < 0:
        raise ValueError('Invalid upload storage limits')
    unreceived, results, asr_reserved = conn.execute(
        """SELECT coalesce(sum(CASE WHEN state='uploading' THEN max(0,size-offset) ELSE 0 END),0), count(*),
                  coalesce(max(CASE WHEN state IN ('queued','processing') THEN size END),0)
           FROM jobs WHERE state IN ('uploading','queued','processing')"""
    ).fetchone()
    pending = unreceived + results * MAX_TRANSCRIPT_BYTES + asr_reserved
    free = shutil.disk_usage(Path(files)).free
    disk_available = max(0, free - reserve_bytes - pending)
    return dict(disk_free_bytes=free, pending_bytes=pending, asr_reserved_bytes=asr_reserved,
                disk_available_bytes=disk_available,
                disk_low=disk_available < demand(max_upload_bytes, max_upload_bytes, asr_reserved))


def admission_reason(snapshot, source_bytes, asr_bytes):
    """New recording (source_bytes=size), repeat or retry of stored audio (source_bytes=0)."""
    if any(type(value) is not int or value < 0 for value in (source_bytes, asr_bytes)):
        raise ValueError('Invalid requested byte count')
    if demand(source_bytes, asr_bytes, snapshot['asr_reserved_bytes']) > snapshot['disk_available_bytes']:
        return 'disk_capacity'
    return None
