"""Worker-owned ASR results and Telegram send boundary. Web opens only committed SQLite pages read-only."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading

import recording_grant as grant
from storage_capacity import MAX_TRANSCRIPT_BYTES


def root():
    return Path(os.environ.get('PSY_RECEIPT_ROOT', '/var/lib/psy-recording-receipts'))


_readers = threading.local()


def _reader():
    """Reused read-only connection per thread. The web UID cannot write here, so this stays
    a rollback-journal database and every read ends its transaction promptly."""
    path = root() / 'results.sqlite3'
    cached = getattr(_readers, 'conn', None)
    if cached is None or cached[0] != path:
        if cached is not None:
            cached[1].close()
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', timeout=10, uri=True)
        conn.row_factory = sqlite3.Row
        cached = _readers.conn = (path, conn)
    return cached[1]


@contextlib.contextmanager
def db(write=False):
    if not write:
        conn = _reader()
        try:
            with conn:
                yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
        return
    conn = sqlite3.connect(str(root() / 'results.sqlite3'), timeout=10)
    conn.row_factory = sqlite3.Row
    # Deleted transcripts must not linger in free pages.
    conn.execute('PRAGMA secure_delete=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    with db(True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS results(
            id TEXT PRIMARY KEY, binding TEXT NOT NULL, body BLOB NOT NULL,
            duration REAL NOT NULL, speech INTEGER NOT NULL, delivery TEXT NOT NULL,
            message_id INTEGER, next_try INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)""")
        conn.execute('CREATE TABLE IF NOT EXISTS used_retry(nonce TEXT PRIMARY KEY)')
        # The delivery column follows the body, so scanning it would read every transcript.
        conn.execute('CREATE INDEX IF NOT EXISTS results_delivery ON results(delivery)')
    (root() / 'results.sqlite3').chmod(0o640)


def read_regular(path, limit=MAX_TRANSCRIPT_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as file:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit or info.st_nlink != 1:
            raise ValueError('Invalid result file')
        data = file.read(limit + 1)
        if len(data) != info.st_size:
            raise ValueError('Result file changed')
        data.decode('utf-8')
        return data


def save(job, result, body, *, delivery='ready', message_id=None):
    if not 0 < len(body) <= MAX_TRANSCRIPT_BYTES:
        raise ValueError('Invalid ASR output size')
    binding = json.dumps({**grant.job_fields(job), 'proof': job['proof']}, ensure_ascii=True)
    with db(True) as conn:
        conn.execute("""INSERT INTO results(id,binding,body,duration,speech,delivery,message_id)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
            (job['id'], binding, body, result['duration'], bool(result['speech_detected']),delivery,message_id))


def get(ident, *, body=False):
    fields = 'binding,duration,speech,delivery,message_id,next_try,attempts'
    with db() as conn:
        row = conn.execute('SELECT '+fields+(',body' if body else '')+' FROM results WHERE id=?',(ident,)).fetchone()
    return dict(row) if row else None


def body_size(ident):
    with db() as conn:
        row = conn.execute('SELECT length(body) FROM results WHERE id=?', (ident,)).fetchone()
    return None if row is None else row[0]


def read_body(ident, offset, size):
    """One bounded piece of a transcript, looked up by id inside the same read transaction."""
    with db() as conn:
        conn.execute('BEGIN')
        row = conn.execute('SELECT rowid FROM results WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise LookupError('Transcript removed')
        with conn.blobopen('results', 'body', row[0], readonly=True) as blob:
            blob.seek(offset)
            return blob.read(size)


def start(ident, now, retry=None):
    with db(True) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT delivery,next_try FROM results WHERE id=?',(ident,)).fetchone()
        if row is None:
            return False
        state = row['delivery']
        if state != 'ready':
            if state not in ('failed','access_revoked','unknown') or not isinstance(retry,dict):
                return False
            fields = {k: retry.get(k) for k in ('id','nonce','confirmed')}
            if (fields['id'] != ident or (state=='unknown' and fields['confirmed'] is not True)
                    or not grant.checked('redeliver',fields,retry.get('proof'))):
                return False
            fresh = conn.execute('INSERT OR IGNORE INTO used_retry VALUES(?)',(fields['nonce'],)).rowcount
            if not fresh:
                return False
        elif row['next_try'] > now:
            return False
        conn.execute("UPDATE results SET delivery='sending' WHERE id=?",(ident,))
        return True


def status(ident, state, msg=None, *, next_try=0, attempt=0):
    with db(True) as conn:
        conn.execute('UPDATE results SET delivery=?,message_id=?,next_try=?,attempts=? WHERE id=?',
                     (state,msg,next_try,attempt,ident))
