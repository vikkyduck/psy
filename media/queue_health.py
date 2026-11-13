"""Print aggregate queue diagnostics only. Run under the data owner's UID."""
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path

# Production invokes this script with -I from the root-owned deployment directory.
if __name__ == '__main__' and sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from storage_capacity import MAX_UPLOAD_BYTES, SYSTEM_RESERVE_BYTES, capacity_snapshot

PROGRESS_WARNING_SECONDS = 35 * 60


def processing_age(updated, ident, progress_dir, now=None):
    now = int(time.time()) if now is None else now
    since = min(updated, now)
    if (progress_dir is not None and isinstance(ident, str) and len(ident) == 32
            and all(c in '0123456789abcdef' for c in ident)):
        try:
            stamp = (Path(progress_dir) / (ident + '.json')).lstat()
            if stat.S_ISREG(stamp.st_mode) and since < stamp.st_mtime <= now + 1:
                since = min(now, int(stamp.st_mtime))
        except OSError:
            pass
    return int(now - since)


def snapshot(path, now=None, progress=None, files=None):
    now = int(time.time()) if now is None else now
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=4) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        rows = db.execute('SELECT state,delivery,count(*),min(updated) FROM jobs GROUP BY state,delivery')
        queue = delivery_wait = unconfirmed = processing = max_wait = 0
        for state, delivery, count, oldest in rows:
            if state=='queued':
                queue += count
                max_wait = max(max_wait, now-oldest)
            if state=='processing':
                processing += count
            if state in ('delivery_pending','sending'):
                delivery_wait = max(delivery_wait, now-oldest)
            if state=='done' and delivery in ('unknown','failed'):
                unconfirmed += count
        running = db.execute("SELECT id,updated FROM jobs WHERE state='processing'")
        stagnant = max((processing_age(since, ident, progress, now) for ident, since in running), default=0)
        report = dict(oldest_processing_without_progress=stagnant,queued=queue,processing=processing,
                      oldest_queue_seconds=max_wait,oldest_delivery_seconds=delivery_wait,
                      unconfirmed_deliveries=unconfirmed,progress_warning=stagnant >= PROGRESS_WARNING_SECONDS)
        if files is not None:
            capacity = capacity_snapshot(db, files, max_upload_bytes=MAX_UPLOAD_BYTES, reserve_bytes=SYSTEM_RESERVE_BYTES)
            for field in ('disk_free_bytes', 'disk_available_bytes', 'pending_bytes', 'disk_low'):
                report[field] = capacity[field]
    return report

if __name__ == '__main__':
    print(json.dumps(snapshot(Path('/srv/psy-queue/state.sqlite3'), progress=Path('/srv/psy-progress'),
                              files=Path('/srv/psy-media'))))
