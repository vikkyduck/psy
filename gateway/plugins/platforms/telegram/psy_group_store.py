"""A member's group materials and conversations. No filename from a message is opened."""
import contextlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path("/home/hermes/.hermes/vault/group-psy")
MEMBER_LIMIT = 128 * 1024**2
TOTAL_LIMIT = 512 * 1024**2


@contextlib.contextmanager
def database(root=ROOT):
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    database_path = root / "group.sqlite3"
    fd = os.open(database_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    con = sqlite3.connect(database_path, timeout=5)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL,
              body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (
              request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
              key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(request_id,key));
            CREATE TABLE IF NOT EXISTS write_receipts (
              request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
              operation TEXT NOT NULL, kind TEXT NOT NULL, topic INTEGER NOT NULL, result_id INTEGER NOT NULL,
              PRIMARY KEY(request_id, operation));
            CREATE TABLE IF NOT EXISTS delivery_parts (
              request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
              part INTEGER NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
              message_id INTEGER, retry_at REAL, PRIMARY KEY(request_id,part));
            CREATE TABLE IF NOT EXISTS completed_updates (
              id INTEGER PRIMARY KEY, completed_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS completed_age ON completed_updates(completed_at);
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, topic INTEGER NOT NULL,
              name TEXT NOT NULL, kind TEXT NOT NULL, data BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS member_docs ON documents(uid,topic,id);
            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, topic INTEGER NOT NULL,
              prompt TEXT NOT NULL, answer TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS member_turns ON turns(uid,topic,id);
            CREATE TABLE IF NOT EXISTS turn_sources (
              turn_id INTEGER PRIMARY KEY, doc_id INTEGER);
            CREATE INDEX IF NOT EXISTS source_turns ON turn_sources(doc_id,turn_id);
            CREATE TABLE IF NOT EXISTS message_sources (
              uid INTEGER NOT NULL, topic INTEGER NOT NULL, message_id INTEGER NOT NULL,
              doc_id INTEGER, turn_id INTEGER, PRIMARY KEY(uid,topic,message_id));
            CREATE INDEX IF NOT EXISTS source_messages ON message_sources(uid,topic,doc_id);
            CREATE TABLE IF NOT EXISTS current_sources (
              uid INTEGER NOT NULL, topic INTEGER NOT NULL, doc_id INTEGER,
              PRIMARY KEY(uid,topic));
        """)
        yield con
        con.commit()
    finally:
        con.close()


def _bind(con, uid, topic, message_id, doc_id, turn_id):
    if type(message_id) is int and message_id > 0:
        con.execute("""INSERT INTO message_sources(uid,topic,message_id,doc_id,turn_id) VALUES (?,?,?,?,?)
                       ON CONFLICT(uid,topic,message_id) DO UPDATE SET
                         doc_id=excluded.doc_id,turn_id=coalesce(excluded.turn_id,message_sources.turn_id)""",
                    (uid, topic, message_id, doc_id, turn_id))


def bind_message(uid, topic, message_id, doc_id, turn_id=None, *, root=ROOT):
    with database(root) as con:
        _bind(con, uid, topic, message_id, doc_id, turn_id)


def message_source(uid, topic, message_id, *, root=ROOT):
    if type(message_id) is not int:
        return None
    with database(root) as con:
        return con.execute("""SELECT s.doc_id,t.answer,t.id FROM message_sources s
                              LEFT JOIN turns t ON t.id=s.turn_id AND t.uid=s.uid AND t.topic=s.topic
                              WHERE s.uid=? AND s.topic=? AND s.message_id=?""",
                           (uid, topic, message_id)).fetchone()


def _previous_write(con, request_key, uid, topic, kind):
    ident, operation = request_key
    owner = con.execute("SELECT uid FROM requests WHERE id=?", (ident,)).fetchone()
    if owner != (uid,):
        raise ValueError("Group job owner mismatch")
    receipt = con.execute("""SELECT kind,topic,result_id FROM write_receipts
                             WHERE request_id=? AND operation=?""", (ident, operation)).fetchone()
    if receipt is None:
        return None
    if receipt[:2] != (kind, topic):
        raise ValueError("Group job operation mismatch")
    return receipt[2]


def _record_write(con, request_key, topic, kind, result_id):
    if request_key is not None:
        con.execute("INSERT INTO write_receipts VALUES(?,?,?,?,?)",
                    (*request_key, kind, topic, result_id))


def archive(uid, topic, name, kind, data, *, message_id=None, root=ROOT, request_key=None):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        if request_key is not None:
            saved = _previous_write(con, request_key, uid, topic, "document")
            if saved is not None:
                return saved
        if type(message_id) is int:
            existing = con.execute("""SELECT d.id FROM message_sources s JOIN documents d
                                      ON d.id=s.doc_id AND d.uid=s.uid AND d.topic=s.topic
                                      WHERE s.uid=? AND s.topic=? AND s.message_id=?""",
                                   (uid, topic, message_id)).fetchone()
            if existing:
                _record_write(con, request_key, topic, "document", existing[0])
                return existing[0]
        member, total = con.execute("""SELECT coalesce(sum(CASE WHEN uid=? THEN length(data)
                      ELSE 0 END),0), coalesce(sum(length(data)),0) FROM documents""", (uid,)).fetchone()
        if member + len(data) > MEMBER_LIMIT or total + len(data) > TOTAL_LIMIT:
            raise ValueError("В хранилище недостаточно места для нового файла.")
        cursor = con.execute("INSERT INTO documents(uid,topic,name,kind,data) VALUES (?,?,?,?,?)",
                             (uid, topic, name[:140], kind, data))
        doc_id = cursor.lastrowid
        _bind(con, uid, topic, message_id, doc_id, None)
        _record_write(con, request_key, topic, "document", doc_id)
        return doc_id


def listing(uid, topic, *, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT id,name FROM documents WHERE uid=? AND topic=? ORDER BY id DESC LIMIT 40",
                           (uid, topic)).fetchall()


def document(uid, topic, ident, *, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT name,kind,data FROM documents WHERE uid=? AND topic=? AND id=?",
                           (uid, topic, ident)).fetchone()


def set_current(uid, topic, doc_id, message_id, *, root=ROOT):
    with database(root) as con:
        con.execute("""INSERT INTO current_sources(uid,topic,doc_id) VALUES (?,?,?)
                       ON CONFLICT(uid,topic) DO UPDATE SET doc_id=excluded.doc_id""", (uid, topic, doc_id))
        _bind(con, uid, topic, message_id, doc_id, None)


def choices(uid, topic, include_library=False, *, root=ROOT):
    """Return only the member's source labels and discussion cues, never document bodies."""
    with database(root) as con:
        current = con.execute("SELECT doc_id FROM current_sources WHERE uid=? AND topic=?", (uid, topic)).fetchone()
        active_id = current[0] if current else None
        if active_id is None and not include_library:
            return None, []
        docs = con.execute("""SELECT d.id,d.name FROM documents d
                              WHERE d.uid=? AND d.topic=? AND (
                                d.id=? OR EXISTS(SELECT 1 FROM message_sources s WHERE
                                  s.uid=d.uid AND s.topic=d.topic AND s.doc_id=d.id)
                                OR (? AND d.kind='text' AND d.name!='Ответ PSY.txt'))
                              ORDER BY (d.id IS ?) DESC,d.id DESC LIMIT 16""",
                           (uid, topic, active_id, include_library, active_id)).fetchall()
        options = []
        for doc_id, name in docs:
            related = """FROM turns t JOIN turn_sources s ON s.turn_id=t.id
                         WHERE t.uid=? AND t.topic=? AND s.doc_id=?"""
            args = (uid, topic, doc_id)
            first = con.execute("SELECT substr(t.prompt,1,320) " + related + " ORDER BY t.id LIMIT 1", args).fetchone()
            last = con.execute("SELECT substr(t.prompt,1,320), substr(t.answer,1,600) " + related
                               + " ORDER BY t.id DESC LIMIT 1", args).fetchone()
            options.append(dict(id=doc_id, name=name, first_question=first[0] if first else "",
                                last_question=last[0] if last else "", last_answer=last[1] if last else ""))
        return active_id, options


def history(uid, topic, *, source_id=Ellipsis, exclude_turn=None, root=ROOT):
    with database(root) as con:
        if source_id is Ellipsis:
            rows = con.execute("SELECT prompt,answer FROM turns WHERE uid=? AND topic=? ORDER BY id DESC LIMIT 6",
                               (uid, topic)).fetchall()
        else:
            rows = con.execute("""SELECT prompt,answer FROM turns JOIN turn_sources ON turn_id=id
                                  WHERE uid=? AND topic=? AND doc_id IS ? AND id IS NOT ?
                                  ORDER BY id DESC LIMIT 6""", (uid, topic, source_id, exclude_turn)).fetchall()
    turns = []
    for i, (prompt, answer) in enumerate(reversed(rows)):
        turns.append({"role": "user", "content": prompt[:3000]})
        latest_source_answer = source_id not in (None, Ellipsis) and exclude_turn is None and i == len(rows)-1
        turns.append({"role": "assistant", "content": answer if latest_source_answer else answer[:4000]})
    return turns


def remember(uid, topic, question, answer, *, source_id=None, message_id=None, root=ROOT, request_key=None):
    with database(root) as con:
        if request_key is not None:
            con.execute("BEGIN IMMEDIATE")
            saved = _previous_write(con, request_key, uid, topic, "turn")
            if saved is not None:
                return saved
        row = con.execute("INSERT INTO turns(uid,topic,prompt,answer) VALUES(?,?,?,?)",
                          (uid, topic, question[:5000], answer))
        turn_id = row.lastrowid
        con.execute("INSERT INTO turn_sources(turn_id,doc_id) VALUES (?,?)", (turn_id, source_id))
        _bind(con, uid, topic, message_id, source_id, turn_id)
        _record_write(con, request_key, topic, "turn", turn_id)
        return turn_id


class Checkpoints:
    """Private JSON stages for one queued update. Each thread opens its own connection."""

    def __init__(self, ident, *, root=ROOT):
        self.ident = ident
        self.root = root

    def get(self, key):
        with database(self.root) as con:
            row = con.execute("SELECT value FROM checkpoints WHERE request_id=? AND key=?",
                              (self.ident, key)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with database(self.root) as con:
            con.execute("""INSERT INTO checkpoints(request_id,key,value) VALUES(?,?,?)
                           ON CONFLICT(request_id,key) DO UPDATE SET value=excluded.value""",
                        (self.ident, key, encoded))


def pending(uid, ident, payload, *, root=ROOT):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        # Telegram can redeliver an update after our worker has already acknowledged it.
        # Keep only its ID beyond the transport replay window; no request body or user ID.
        con.execute("DELETE FROM completed_updates WHERE completed_at<?", (time.time() - 7*86400,))
        if con.execute("SELECT 1 FROM completed_updates WHERE id=?", (ident,)).fetchone():
            return "existing"
        if con.execute("SELECT 1 FROM requests WHERE id=?", (ident,)).fetchone():
            return "existing"
        count, own = con.execute("""SELECT count(*),coalesce(sum(uid=?),0)
                                    FROM requests WHERE state IN ('queued','running','sending')""",(uid,)).fetchone()
        if count>=12 or own>=4 or len(payload)>300000:
            return "full"
        con.execute("INSERT INTO requests(id,uid,body,state) VALUES(?,?,?,'queued')",(ident,uid,payload))
        return "queued"


def claim_next(*, root=ROOT, now=None):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        record = con.execute("""SELECT id,body FROM requests r WHERE state='queued'
                                AND NOT EXISTS(SELECT 1 FROM delivery_parts p WHERE p.request_id=r.id
                                               AND p.state='retry' AND p.retry_at>?)
                                ORDER BY r.rowid LIMIT 1""",
                             (time.time() if now is None else now,)).fetchone()
        if record:
            con.execute("UPDATE requests SET state='running' WHERE id=?", (record[0],))
        return record


def next_retry_delay(*, root=ROOT, now=None):
    with database(root) as con:
        due = con.execute("""SELECT min(p.retry_at) FROM delivery_parts p JOIN requests r ON r.id=p.request_id
                             WHERE r.state='queued' AND p.state='retry'""").fetchone()[0]
    return None if due is None else max(0, due - (time.time() if now is None else now))


def begin_delivery(ident, part, fingerprint, *, root=ROOT, now=None):
    """Decide whether this exact part may cross the non-idempotent Telegram send boundary."""
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        request = con.execute("SELECT state FROM requests WHERE id=?", (ident,)).fetchone()
        if request != ("running",):
            return "uncertain", None
        row = con.execute("""SELECT fingerprint,state,message_id,retry_at FROM delivery_parts
                             WHERE request_id=? AND part=?""", (ident, part)).fetchone()
        if row:
            saved, state, message_id, due = row
            if saved != fingerprint:
                return "uncertain", None
            if state == "confirmed":
                return "confirmed", message_id
            if state != "retry":
                return "uncertain", None
            if due > (time.time() if now is None else now):
                return "wait", due
            con.execute("""UPDATE delivery_parts SET state='sending',retry_at=NULL
                           WHERE request_id=? AND part=?""", (ident, part))
        else:
            con.execute("""INSERT INTO delivery_parts(request_id,part,fingerprint,state)
                           VALUES(?,?,?,'sending')""", (ident, part, fingerprint))
        con.execute("UPDATE requests SET state='sending' WHERE id=?", (ident,))
        return "send", None


def _finish_attempt(con, ident, part, state, message_id, due):
    con.execute("BEGIN IMMEDIATE")
    request = con.execute("SELECT state FROM requests WHERE id=?", (ident,)).fetchone()
    if request != ("sending",):
        raise ValueError("No active group delivery")
    count = con.execute("""UPDATE delivery_parts SET state=?,message_id=?,retry_at=?
                           WHERE request_id=? AND part=? AND state='sending'""",
                        (state, message_id, due, ident, part)).rowcount
    if count != 1:
        raise ValueError("No active group delivery part")
    con.execute("UPDATE requests SET state='running' WHERE id=?", (ident,))


def confirm_delivery(ident, part, message_id, *, root=ROOT):
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("Missing Telegram message ID")
    with database(root) as con:
        _finish_attempt(con, ident, part, "confirmed", message_id, None)


def retry_delivery(ident, part, retry_at, *, root=ROOT):
    if not math.isfinite(retry_at):
        raise ValueError("Invalid Telegram retry time")
    with database(root) as con:
        _finish_attempt(con, ident, part, "retry", None, retry_at)


def outbound(ident, *, root=ROOT):
    # Persist BEFORE non-idempotent Telegram delivery. An ambiguous send cannot be replayed safely.
    with database(root) as con:
        con.execute("UPDATE requests SET state='sending' WHERE id=? AND state='running'", (ident,))


def settle(ident, success, *, root=ROOT):
    with database(root) as con:
        if success:
            con.execute("""INSERT OR REPLACE INTO completed_updates(id,completed_at)
                           SELECT id,? FROM requests WHERE id=?""", (time.time(), ident))
            con.execute("DELETE FROM requests WHERE id=?", (ident,))
        else:
            con.execute("UPDATE requests SET state='uncertain',body='' WHERE id=?", (ident,))


def recover(*, root=ROOT):
    with database(root) as con:
        con.execute("UPDATE requests SET state='queued' WHERE state='running'")
        con.execute("UPDATE requests SET state='uncertain',body='' WHERE state='sending'")
        return con.execute("SELECT count(*) FROM requests WHERE state='queued'").fetchone()[0]


def lease(*, root=ROOT):
    import fcntl
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd=os.open(root/"worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def state(ident, *, root=ROOT):
    with database(root) as con:
        row = con.execute("SELECT state FROM requests WHERE id=?", (ident,)).fetchone()
        return row[0] if row else None
