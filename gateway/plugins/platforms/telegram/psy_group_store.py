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
MAX_LONG_JOBS = 2


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
            CREATE TABLE IF NOT EXISTS instance_binding (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), bot INTEGER NOT NULL, chat INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_retries (
              request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
              attempts INTEGER NOT NULL, retry_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL,
              body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedule (
              request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
              topic INTEGER NOT NULL, message_id INTEGER, reply_id INTEGER,
              long_job INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS source_events (
              uid INTEGER NOT NULL, topic INTEGER NOT NULL, message_id INTEGER NOT NULL, doc_id INTEGER,
              PRIMARY KEY(uid,topic,message_id));
            CREATE TABLE IF NOT EXISTS turn_order (
              turn_id INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE, message_id INTEGER NOT NULL);
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


def bind_instance(bot, chat, *, root=ROOT, adopt_legacy=False):
    """An operator must identify the owner of a populated vault created before binding."""
    if type(bot) is not int or bot <= 0 or type(chat) is not int or chat >= 0:
        raise ValueError("Invalid group vault identity")
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        bound = con.execute("SELECT bot,chat FROM instance_binding WHERE singleton=1").fetchone()
        if bound == (bot, chat):
            return
        if bound is not None:
            raise RuntimeError("Group vault identity mismatch")
        if not adopt_legacy:
            tables = ("documents","turns","requests","completed_updates","source_events","turn_sources",
                      "message_sources","current_sources")
            if any(con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in tables):
                raise RuntimeError("Existing group vault requires explicit identity binding")
        con.execute("INSERT INTO instance_binding VALUES(1,?,?)", (bot, chat))


class ArchiveFull(ValueError):
    pass


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
            raise ArchiveFull("В хранилище недостаточно места для нового файла.")
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
        con.execute("BEGIN IMMEDIATE")
        if type(message_id) is int and message_id > 0:
            # Preserve the source selected before this migration as the timeline baseline.
            con.execute("""INSERT OR IGNORE INTO source_events
                           SELECT uid,topic,0,doc_id FROM current_sources WHERE uid=? AND topic=?
                           AND NOT EXISTS (SELECT 1 FROM source_events WHERE uid=? AND topic=?)""",
                        (uid, topic, uid, topic))
            con.execute("INSERT OR REPLACE INTO source_events VALUES (?,?,?,?)", (uid, topic, message_id, doc_id))
        con.execute("""INSERT INTO current_sources(uid,topic,doc_id) VALUES (?,?,?)
                       ON CONFLICT(uid,topic) DO UPDATE SET doc_id=excluded.doc_id
                       WHERE NOT EXISTS (SELECT 1 FROM source_events WHERE uid=? AND topic=? AND message_id>?)""",
                    (uid, topic, doc_id, uid, topic, message_id))
        _bind(con, uid, topic, message_id, doc_id, None)


# Older turns predate turn_order but many retain both their incoming and outgoing bindings.
# Telegram numbers every message in a chat in sequence, so the first binding is the incoming one.
_TURN_ORIGIN = """coalesce(o.message_id, (SELECT min(ms.message_id) FROM message_sources ms
                 WHERE ms.uid=t.uid AND ms.topic=t.topic AND ms.turn_id=t.id), 0)"""


def choices(uid, topic, include_library=False, *, root=ROOT, before_message_id=None):
    """Return source labels and discussion cues visible before this incoming message."""
    with database(root) as con:
        con.execute("BEGIN")
        current = con.execute("SELECT doc_id FROM current_sources WHERE uid=? AND topic=?", (uid, topic)).fetchone()
        if before_message_id is not None:
            event = con.execute("""SELECT doc_id FROM source_events WHERE uid=? AND topic=? AND message_id<?
                                   ORDER BY message_id DESC LIMIT 1""", (uid, topic, before_message_id)).fetchone()
            if event is not None or con.execute("SELECT 1 FROM source_events WHERE uid=? AND topic=? LIMIT 1",
                                                (uid, topic)).fetchone():
                current = event
        active_id = current[0] if current else None
        if active_id is None and not include_library:
            return None, []
        docs = con.execute("""SELECT d.id,d.name FROM documents d
                              WHERE d.uid=? AND d.topic=? AND (
                                d.id=? OR EXISTS(SELECT 1 FROM message_sources s WHERE
                                  s.uid=d.uid AND s.topic=d.topic AND s.doc_id=d.id)
                                OR (? AND d.kind='text' AND d.name!='Ответ PSY.txt'))
                              AND (? IS NULL OR coalesce((SELECT min(s.message_id) FROM message_sources s
                                   WHERE s.uid=d.uid AND s.topic=d.topic AND s.doc_id=d.id),0) < ?)
                              ORDER BY (d.id IS ?) DESC,d.id DESC LIMIT 16""",
                           (uid, topic, active_id, include_library, before_message_id, before_message_id, active_id)).fetchall()
        options = []
        for doc_id, name in docs:
            related = f"""FROM turns t JOIN turn_sources s ON s.turn_id=t.id
                          LEFT JOIN turn_order o ON o.turn_id=t.id WHERE t.uid=? AND t.topic=? AND s.doc_id=?
                          AND (? IS NULL OR {_TURN_ORIGIN} < ?)"""
            args = (uid, topic, doc_id, before_message_id, before_message_id)
            first = con.execute(f"SELECT substr(t.prompt,1,320) {related} ORDER BY {_TURN_ORIGIN},t.id LIMIT 1",
                                args).fetchone()
            last = con.execute(f"""SELECT substr(t.prompt,1,320),substr(t.answer,1,600) {related}
                                   ORDER BY {_TURN_ORIGIN} DESC,t.id DESC LIMIT 1""", args).fetchone()
            options.append(dict(id=doc_id, name=name, first_question=first[0] if first else "",
                                last_question=last[0] if last else "", last_answer=last[1] if last else ""))
        return active_id, options


def history(uid, topic, *, source_id=Ellipsis, exclude_turn=None, root=ROOT, before_message_id=None):
    with database(root) as con:
        query = f"""SELECT prompt,answer FROM turns t LEFT JOIN turn_order o ON o.turn_id=t.id
                    WHERE t.uid=? AND t.topic=? AND (? IS NULL OR {_TURN_ORIGIN} < ?)"""
        args = (uid, topic, before_message_id, before_message_id)
        if source_id is not Ellipsis:
            query += """ AND t.id IS NOT ? AND EXISTS (SELECT 1 FROM turn_sources s
                         WHERE s.turn_id=t.id AND s.doc_id IS ?)"""
            args += (exclude_turn, source_id)
        rows = con.execute(query + f" ORDER BY {_TURN_ORIGIN} DESC,t.id DESC LIMIT 6", args).fetchall()
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
        if type(message_id) is int and message_id > 0:
            con.execute("INSERT INTO turn_order VALUES (?,?)", (turn_id, message_id))
        con.execute("INSERT INTO turn_sources(turn_id,doc_id) VALUES (?,?)", (turn_id, source_id))
        _bind(con, uid, topic, message_id, source_id, turn_id)
        _record_write(con, request_key, topic, "turn", turn_id)
        return turn_id


class GroupJobParked(Exception):
    """Durably queued to resume when a long-work slot becomes available."""


class Checkpoints:
    """Private JSON stages for one queued update. Each thread opens its own connection."""

    def __init__(self, ident, *, root=ROOT):
        self.ident = ident
        self.root = root

    def admit_long(self):
        if not admit_long(self.ident, root=self.root):
            raise GroupJobParked()

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


def _schedule_request(con, ident, payload):
    # Legacy rows can be non-JSON. Their existing FIFO semantics are preserved.
    try:
        obj = json.loads(payload)
        msg = obj.get("message", {}) if isinstance(obj, dict) else {}
        mid = msg.get("message_id")
        topic = msg.get("message_thread_id", 0) if msg.get("is_topic_message") else 0
        reply = msg.get("reply_to_message") or {}
        reply_id = None
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is not None and reply.get("chat", {}).get("id") == chat_id:
            reply_id = reply.get("message_id")
    except (ValueError, TypeError, AttributeError):
        mid, topic, reply_id = None, 0, None
    con.execute("INSERT INTO schedule(request_id,topic,message_id,reply_id) VALUES (?,?,?,?)",
                (ident, topic if type(topic) is int else 0, mid if type(mid) is int else None,
                 reply_id if type(reply_id) is int else None))


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
        _schedule_request(con, ident, payload)
        return "queued"


# Exclude jobs waiting on a full long-work lane, their own explicit Telegram reply
# target, or an earlier active source-selection step in this member and topic.
# An independent question may overtake a queued follow-up that is itself waiting.
_CLAIMABLE = """(s.long_job=0 OR (SELECT count(*) FROM requests a JOIN schedule b ON b.request_id=a.id
                     WHERE a.state IN ('running','sending') AND b.long_job=1) < ?)
                AND NOT EXISTS (SELECT 1 FROM requests a JOIN schedule b ON b.request_id=a.id
                    WHERE a.uid=r.uid AND b.topic=s.topic AND b.message_id<s.message_id
                      AND a.state IN ('running','sending','queued') AND (
                        s.reply_id=b.message_id OR EXISTS (SELECT 1 FROM delivery_parts p WHERE
                          p.request_id=a.id AND p.message_id=s.reply_id)
                        OR (s.reply_id IS NOT NULL AND a.state='sending')
                        OR (a.state IN ('running','sending') AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.request_id=a.id
                                AND c.key IN ('route:input:v1','outbound.single_text'))
                            AND NOT EXISTS (SELECT 1 FROM delivery_parts p WHERE p.request_id=a.id))))"""


def claim_next(*, root=ROOT, now=None):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        # A pre-upgrade live queue has no schedule rows yet. At most 12 jobs can be admitted.
        for ident, body in con.execute("""SELECT r.id,r.body FROM requests r LEFT JOIN schedule s
                                          ON s.request_id=r.id WHERE s.request_id IS NULL AND
                                          r.state IN ('queued','running','sending')""").fetchall():
            _schedule_request(con, ident, body)
        now = time.time() if now is None else now
        record = con.execute(f"""SELECT r.id,r.body FROM requests r JOIN schedule s ON s.request_id=r.id
              WHERE r.state='queued' AND {_CLAIMABLE}
                AND NOT EXISTS (SELECT 1 FROM delivery_parts p WHERE p.request_id=r.id
                                AND p.state='retry' AND p.retry_at>?)
                AND NOT EXISTS (SELECT 1 FROM model_retries m WHERE m.request_id=r.id AND m.retry_at>?)
              ORDER BY (SELECT count(*) FROM requests a WHERE a.uid=r.uid AND a.state IN ('running','sending')),
                       r.rowid LIMIT 1""", (MAX_LONG_JOBS, now, now)).fetchone()
        if record:
            con.execute("UPDATE requests SET state='running' WHERE id=?", (record[0],))
        return record


def admit_long(ident, *, root=ROOT):
    """Reserve one of the long-job slots or requeue a newly classified job atomically."""
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("""SELECT s.long_job FROM schedule s JOIN requests r ON r.id=s.request_id
                             WHERE r.id=? AND r.state='running'""", (ident,)).fetchone()
        if row is None:
            raise RuntimeError("Group request is not running")
        if row[0]:
            return True
        busy = con.execute("""SELECT count(*) FROM schedule s JOIN requests r ON r.id=s.request_id
                              WHERE s.long_job=1 AND r.state IN ('running','sending')""").fetchone()[0]
        con.execute("UPDATE schedule SET long_job=1 WHERE request_id=?", (ident,))
        if busy >= MAX_LONG_JOBS:
            con.execute("UPDATE requests SET state='queued' WHERE id=?", (ident,))
            return False
        return True


def next_retry_delay(*, root=ROOT, now=None):
    now = time.time() if now is None else now
    with database(root) as con:
        due = con.execute(f"""WITH timers AS (
                   SELECT request_id,retry_at FROM delivery_parts WHERE state='retry'
                   UNION ALL SELECT request_id,retry_at FROM model_retries),
                 ready AS (SELECT request_id,max(retry_at) due FROM timers GROUP BY request_id)
                 SELECT min(t.due) FROM ready t JOIN requests r ON r.id=t.request_id
                 JOIN schedule s ON s.request_id=r.id WHERE r.state='queued'
                 AND {_CLAIMABLE}""", (MAX_LONG_JOBS,)).fetchone()[0]
    return None if due is None else max(0, due - now)


def owns_running_job(ident, uid, *, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT 1 FROM requests WHERE id=? AND uid=? AND state='running'",
                           (ident, uid)).fetchone() is not None


def wait_for_model(ident, kind, reset=None, *, root=ROOT, now=None):
    """Commit retry before any best-effort notice. A restart cannot lose chapters or repeat the notice."""
    now = time.time() if now is None else now
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM requests WHERE id=? AND state='running'", (ident,)).fetchone():
            raise RuntimeError("Group request is not running")
        row = con.execute("SELECT attempts FROM model_retries WHERE request_id=?", (ident,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        delay = min(1800, (300 if kind in ("signin","quota") else 60) * 2 ** min(attempts-1, 5))
        due = now + delay
        if kind == "quota" and type(reset) in (int,float) and now < reset <= now + 32*86400:
            due = max(due, reset)
        con.execute("INSERT OR REPLACE INTO model_retries VALUES(?,?,?)", (ident, attempts, due))
        con.execute("UPDATE requests SET state='queued' WHERE id=?", (ident,))
        return row is None


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


def _finish_attempt(con, ident, part, state, message_id, due, *, park=False):
    con.execute("BEGIN IMMEDIATE")
    request = con.execute("SELECT state FROM requests WHERE id=?", (ident,)).fetchone()
    if request != ("sending",):
        raise ValueError("No active group delivery")
    count = con.execute("""UPDATE delivery_parts SET state=?,message_id=?,retry_at=?
                           WHERE request_id=? AND part=? AND state='sending'""",
                        (state, message_id, due, ident, part)).rowcount
    if count != 1:
        raise ValueError("No active group delivery part")
    con.execute("UPDATE requests SET state=? WHERE id=?", ("queued" if park else "running", ident))


def confirm_delivery(ident, part, message_id, *, root=ROOT):
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("Missing Telegram message ID")
    with database(root) as con:
        _finish_attempt(con, ident, part, "confirmed", message_id, None)


def retry_delivery(ident, part, retry_at, *, root=ROOT, park=False):
    if not math.isfinite(retry_at):
        raise ValueError("Invalid Telegram retry time")
    with database(root) as con:
        _finish_attempt(con, ident, part, "retry", None, retry_at, park=park)


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
