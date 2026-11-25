"""A member's group materials and conversations. No filename from a message is opened."""
import contextlib
import importlib
import json
import math
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path


def _hermes_home():
    configured = os.environ.get("HERMES_HOME", "").strip()
    return Path(os.path.expanduser(configured)) if configured else Path("/home/hermes/.hermes")


ROOT = _hermes_home() / "vault" / "group-psy"
# Members' files and answers have no quota. Only the physical disk is protected: keep this
# much free for the system, the -wal file and recordings (the uploader keeps the same reserve).
DISK_RESERVE = 768 * 1024**2
MAX_LONG_JOBS = 2
AUTH_WAITS = ("signin", "access_wait")
# Several worker threads and the operator CLI share one file. A writer waits for the
# lock instead of failing a job after a short contention spike.
BUSY_SECONDS = 30
SCHEMA_VERSION = 3

_SCHEMA = """
            CREATE TABLE IF NOT EXISTS request_audit (
              request_key TEXT PRIMARY KEY, uid INTEGER NOT NULL, topic INTEGER NOT NULL,
              message_id INTEGER, started REAL NOT NULL, updated REAL NOT NULL,
              state TEXT NOT NULL, provenance TEXT NOT NULL, turn_id INTEGER);
            CREATE TABLE IF NOT EXISTS request_audit_events (
              id INTEGER PRIMARY KEY, request_key TEXT NOT NULL REFERENCES request_audit(request_key),
              recorded REAL NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS audit_events_request ON request_audit_events(request_key,id);
            CREATE TABLE IF NOT EXISTS conversation_context (
              uid INTEGER NOT NULL, topic INTEGER NOT NULL, source_key INTEGER NOT NULL,
              before_message_id INTEGER NOT NULL, state TEXT NOT NULL,
              PRIMARY KEY(uid,topic,source_key));
            CREATE TABLE IF NOT EXISTS instance_binding (
              singleton INTEGER PRIMARY KEY CHECK(singleton=1), bot INTEGER NOT NULL, chat INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_retries (
              request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
              attempts INTEGER NOT NULL, retry_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_recoveries (
              revision INTEGER PRIMARY KEY, incident TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS model_attempts (
              request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
              auth_revision INTEGER NOT NULL, wait_kind TEXT);
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
            CREATE TABLE IF NOT EXISTS job_progress (
              request_id INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
              updated REAL NOT NULL, stage TEXT NOT NULL);
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
            CREATE TABLE IF NOT EXISTS turn_results (
              turn_id INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
              task_status TEXT NOT NULL, generation_complete INTEGER,
              missing_requirements TEXT NOT NULL, metadata TEXT NOT NULL,
              recorded_at REAL NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'pending',
              confirmed_at REAL);
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
"""

# Database files this process has already prepared, by path and file identity.
_prepared = {}
_prepare_lock = threading.Lock()


def _connect(path):
    con = sqlite3.connect(path, timeout=BUSY_SECONDS)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _prepare(root, path):
    """Create the private file, switch it to WAL once and apply the schema version."""
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    con = _connect(path)
    try:
        # WAL is persistent in the file. Readers no longer block the writer that
        # records a job's progress, and the -wal/-shm files inherit the 0600 mode.
        if con.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            con.execute("PRAGMA journal_mode=WAL")
        if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            con.executescript(_SCHEMA + f"PRAGMA user_version={SCHEMA_VERSION};")
    finally:
        con.close()
    return info.st_dev, info.st_ino


def _ready(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and _prepared.get(str(path)) == (info.st_dev, info.st_ino)


@contextlib.contextmanager
def database(root=ROOT):
    database_path = root / "group.sqlite3"
    if not _ready(database_path):
        # A replaced or deleted file is prepared again, never followed through a symlink.
        with _prepare_lock:
            if not _ready(database_path):
                _prepared[str(database_path)] = _prepare(root, database_path)
    con = _connect(database_path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def bind_instance(bot, chat, *, root=ROOT, adopt_legacy=False):
    """An operator must identify the owner of a populated vault created before binding."""
    if type(bot) is not int or bot <= 0 or type(chat) is not int or chat >= 0:
        raise ValueError("Invalid group vault identity")
    with database(root) as con:
        # Every accepted update confirms the binding. It is written once and never changes,
        # so only the first binding needs the write lock.
        bound = con.execute("SELECT bot,chat FROM instance_binding WHERE singleton=1").fetchone()
        if bound == (bot, chat):
            return
        if bound is not None:
            raise RuntimeError("Group vault identity mismatch")
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


def _free_bytes(con):
    # The filesystem that holds this database file, wherever its root is configured.
    path = con.execute("PRAGMA database_list").fetchone()[2]
    info = os.statvfs(os.path.dirname(path) or ".")
    return info.f_bavail * info.f_frsize


def _ensure_space(con, size):
    # The new row is written to the -wal file first and later checkpointed into the database.
    if _free_bytes(con) < DISK_RESERVE + 2 * size:
        raise ArchiveFull("В хранилище недостаточно места для нового файла.")


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
        _ensure_space(con, len(data))
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


def forget(uid, topic, ident, *, root=ROOT, request_key=None):
    """Delete a member's own stored document in this topic. Return whether it existed."""
    with database(root) as con:
        # Overwrite the freed pages instead of leaving the file's bytes in the free list.
        con.execute("PRAGMA secure_delete=ON")
        con.execute("BEGIN IMMEDIATE")
        if request_key is not None and _previous_write(con, request_key, uid, topic, "deletion") is not None:
            return True
        if con.execute("DELETE FROM documents WHERE uid=? AND topic=? AND id=?", (uid, topic, ident)).rowcount != 1:
            return False
        # A later file can reuse the row ID. No reply, selection or discussion may then point to
        # it. The member's turns about the deleted file stay together under a negative marker.
        for table in ("message_sources", "current_sources", "source_events"):
            con.execute(f"UPDATE {table} SET doc_id=NULL WHERE uid=? AND topic=? AND doc_id=?", (uid, topic, ident))
        con.execute("""UPDATE turn_sources SET doc_id=? WHERE doc_id=? AND turn_id IN
                       (SELECT id FROM turns WHERE uid=? AND topic=?)""", (-ident, ident, uid, topic))
        con.execute("DELETE FROM conversation_context WHERE uid=? AND topic=? AND source_key=?",
                    (uid, topic, ident))
        _record_write(con, request_key, topic, "deletion", ident)
    try:
        # Drop the deleted bytes from the -wal file too. A busy reader only postpones this.
        with database(root) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error:
        pass
    return True


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
        rows = con.execute(query + f" ORDER BY {_TURN_ORIGIN},t.id", args).fetchall()
    turns = []
    for prompt, answer in rows:
        turns.append({"role": "user", "content": prompt})
        turns.append({"role": "assistant", "content": answer})
    return turns


RESULT_STATUSES = frozenset(("unknown", "complete", "partial", "needs_source", "needs_clarification", "refused", "failed"))


def get_context(uid, topic, source_id, *, before_message_id=None, root=ROOT):
    with database(root) as con:
        row = con.execute("""SELECT state,before_message_id FROM conversation_context
                              WHERE uid=? AND topic=? AND source_key=?""",
                          (uid, topic, source_id or 0)).fetchone()
    if row is None or (before_message_id is not None and row[1] >= before_message_id):
        return None
    return json.loads(row[0])


def save_context(uid, topic, source_id, state, *, message_id, root=ROOT):
    if not state or type(message_id) is not int:
        return
    with database(root) as con:
        con.execute("""INSERT INTO conversation_context VALUES(?,?,?,?,?)
                       ON CONFLICT(uid,topic,source_key) DO UPDATE SET
                       before_message_id=excluded.before_message_id,state=excluded.state
                       WHERE excluded.before_message_id>=conversation_context.before_message_id""",
                    (uid, topic, source_id or 0, message_id,
                     json.dumps(state, ensure_ascii=False, allow_nan=False)))


def audit_start(key, uid, topic, message_id, provenance=None, *, root=ROOT):
    """No foreign key to the queue: terminal errors outlive request/checkpoint cleanup."""
    now = time.time()
    with database(root) as con:
        con.execute("""INSERT OR IGNORE INTO request_audit
                       VALUES(?,?,?,?,?,?,'started',?,NULL)""",
                    (key, uid, topic, message_id, now, now,
                     json.dumps(provenance or {}, ensure_ascii=False, allow_nan=False)))
        row = con.execute("SELECT uid,topic,message_id FROM request_audit WHERE request_key=?", (key,)).fetchone()
        if row != (uid, topic, message_id):
            raise ValueError("Audit owner mismatch")


def audit_event(key, event, details=None, *, state=None, turn_id=None, root=ROOT):
    now = time.time()
    encoded = json.dumps(details or {}, ensure_ascii=False, allow_nan=False)
    with database(root) as con:
        con.execute("INSERT INTO request_audit_events(request_key,recorded,event,details) VALUES(?,?,?,?)",
                    (key, now, event, encoded))
        con.execute("""UPDATE request_audit SET updated=?,state=coalesce(?,state),
                       turn_id=coalesce(?,turn_id) WHERE request_key=?""", (now, state, turn_id, key))


def _queue_audit(con, ident, event, state, details=None):
    key = "update:" + str(ident)
    if con.execute("SELECT 1 FROM request_audit WHERE request_key=?", (key,)).fetchone():
        now = time.time()
        con.execute("INSERT INTO request_audit_events(request_key,recorded,event,details) VALUES(?,?,?,?)",
                    (key, now, event, json.dumps(details or {}, allow_nan=False)))
        con.execute("UPDATE request_audit SET state=?,updated=? WHERE request_key=?", (state, now, key))


def explain_request(key, *, uid=None, topic=None, root=ROOT):
    """Operator diagnostics, optionally limited to the authenticated participant and topic."""
    with database(root) as con:
        row = con.execute("""SELECT state,started,updated,provenance,turn_id FROM request_audit
                              WHERE request_key=? AND (? IS NULL OR uid=?) AND (? IS NULL OR topic=?)""",
                          (key, uid, uid, topic, topic)).fetchone()
        if row is None:
            return None
        events = con.execute("SELECT recorded,event,details FROM request_audit_events WHERE request_key=? ORDER BY id",
                             (key,)).fetchall()
    return {"request_key": key, "state": row[0], "started": row[1], "updated": row[2],
            "provenance": json.loads(row[3]), "turn_id": row[4],
            "events": [{"at": stamp, "event": event, "details": json.loads(data)}
                       for stamp, event, data in events]}


def _remember_result(con, turn_id, details):
    if details is None:
        return
    status = details.get("task_status", "unknown")
    generated = details.get("generation_complete")
    missing = details.get("missing_requirements", [])
    metadata = details.get("quality_metadata", {})
    if (status not in RESULT_STATUSES or generated is not None and type(generated) is not bool
            or not isinstance(missing, list) or not all(isinstance(x, str) for x in missing)
            or not isinstance(metadata, dict)):
        raise ValueError("Invalid group result status")
    encoded = json.dumps(metadata, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # A replay must never overwrite the original provenance or erase confirmation.
    con.execute("""INSERT OR IGNORE INTO turn_results
                   (turn_id,task_status,generation_complete,missing_requirements,metadata,recorded_at)
                   VALUES(?,?,?,?,?,?)""",
                (turn_id, status, generated, json.dumps(missing, ensure_ascii=False), encoded, time.time()))


def result_details(uid, topic, turn_id, *, root=ROOT):
    """Read quality and delivery independently, only in the owning member/topic scope."""
    with database(root) as con:
        row = con.execute("""SELECT r.task_status,r.generation_complete,r.missing_requirements,
                              r.metadata,r.recorded_at,r.delivery_status,r.confirmed_at
                              FROM turns t LEFT JOIN turn_results r ON r.turn_id=t.id
                              WHERE t.id=? AND t.uid=? AND t.topic=?""", (turn_id, uid, topic)).fetchone()
    if row is None:
        return None
    if row[0] is None:
        return {"task_status": "unknown", "generation_complete": None, "missing_requirements": [],
                "quality_metadata": {}, "recorded_at": None, "delivery_status": "unknown", "confirmed_at": None}
    return {"task_status": row[0], "generation_complete": None if row[1] is None else bool(row[1]),
            "missing_requirements": json.loads(row[2]), "quality_metadata": json.loads(row[3]),
            "recorded_at": row[4], "delivery_status": row[5], "confirmed_at": row[6]}


def confirm_result_delivery(uid, topic, turn_id, *, root=ROOT):
    """Telegram receipts confirm transport acceptance, not reading or task completeness."""
    with database(root) as con:
        con.execute("""UPDATE turn_results SET delivery_status='confirmed',confirmed_at=coalesce(confirmed_at,?)
                       WHERE turn_id=? AND EXISTS(SELECT 1 FROM turns WHERE id=? AND uid=? AND topic=?)""",
                    (time.time(), turn_id, turn_id, uid, topic))


def remember(uid, topic, question, answer, *, source_id=None, message_id=None, root=ROOT, request_key=None,
             result_details=None):
    with database(root) as con:
        if request_key is not None:
            con.execute("BEGIN IMMEDIATE")
            saved = _previous_write(con, request_key, uid, topic, "turn")
            if saved is not None:
                _remember_result(con, saved, result_details)
                return saved
        row = con.execute("INSERT INTO turns(uid,topic,prompt,answer) VALUES(?,?,?,?)",
                          (uid, topic, question, answer))
        turn_id = row.lastrowid
        if type(message_id) is int and message_id > 0:
            con.execute("INSERT INTO turn_order VALUES (?,?)", (turn_id, message_id))
        con.execute("INSERT INTO turn_sources(turn_id,doc_id) VALUES (?,?)", (turn_id, source_id))
        _bind(con, uid, topic, message_id, source_id, turn_id)
        _record_write(con, request_key, topic, "turn", turn_id)
        _remember_result(con, turn_id, result_details)
        return turn_id


def recorded_turn(uid, topic, request_key, *, root=ROOT):
    """Read an idempotent turn only through its owning queued request and scope."""
    with database(root) as con:
        turn_id = _previous_write(con, request_key, uid, topic, "turn")
        if turn_id is None:
            return None
        row = con.execute("""SELECT t.id,t.prompt,t.answer,s.doc_id FROM turns t
                              LEFT JOIN turn_sources s ON s.turn_id=t.id
                              WHERE t.id=? AND t.uid=? AND t.topic=?""", (turn_id, uid, topic)).fetchone()
        if row is None:
            raise ValueError("Recorded group turn mismatch")
        return row


# Job outcomes shared by the route, the queue and the model pipelines. They live here because
# every group module already imports the store, and the store imports none of them.
class DeliveryUncertain(Exception):
    """A Telegram send may have reached the group without a receipt. It is never repeated."""

    def __init__(self, reason="unknown", stage="unknown", error_class=None):
        super().__init__("Delivery outcome is uncertain")
        self.audit = {"reason": reason, "stage": stage}
        if error_class in {"TimedOut", "NetworkError", "Forbidden", "BadRequest", "InvalidToken",
                           "ChatMigrated", "Conflict", "OperationalError", "OSError", "TimeoutError"}:
            self.audit["error_class"] = error_class
        elif error_class is not None:
            self.audit["error_class"] = "other"


class GroupJobParked(Exception):
    """Durably queued to resume when a long-work slot becomes available."""


class GroupJobFailed(Exception):
    """An unexpected error was reported to the member. Keep the job for the operator."""


def transient(exc):
    """Storage, disk and network failures. The job's checkpoints remain valid for a retry."""
    if isinstance(exc, (sqlite3.Error, OSError)):
        return True
    try:
        errors = importlib.import_module("telegram.error")
    except ImportError:
        return False
    retry, network, rejected = (getattr(errors, name, ()) for name in ("RetryAfter", "NetworkError", "BadRequest"))
    # PTB derives BadRequest from NetworkError, but a rejected request does not heal by waiting.
    return isinstance(exc, retry) or (isinstance(exc, network) and not isinstance(exc, rejected))


class Checkpoints:
    """Private JSON stages for one queued update. Each thread opens its own connection."""

    def __init__(self, ident, *, root=ROOT):
        self.ident = ident
        self.root = root

    def admit_long(self):
        if not admit_long(self.ident, root=self.root):
            raise GroupJobParked()

    def progress(self, stage="model", *, now=None):
        with database(self.root) as con:
            con.execute("""INSERT INTO job_progress VALUES(?,?,?)
                           ON CONFLICT(request_id) DO UPDATE SET updated=excluded.updated, stage=excluded.stage""",
                        (self.ident, time.time() if now is None else now, stage))

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
            if key == ANSWER and value is not None:
                # The final answer supersedes every model stage that produced it.
                _drop_stages(con, self.ident)
            con.execute("INSERT OR REPLACE INTO job_progress VALUES(?,?,?)", (self.ident, time.time(), "checkpoint"))


ANSWER = "route:answer:v1"
_STAGES = "(key GLOB 'model:*' OR key GLOB 'prose:*')"


def _drop_stages(con, ident):
    con.execute(f"DELETE FROM checkpoints WHERE request_id=? AND {_STAGES}", (ident,))


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
        # Every request is queued, however many a member or the group already has waiting.
        # Concurrency is bounded only by the workers that claim them.
        con.execute("INSERT INTO requests(id,uid,body,state) VALUES(?,?,?,'queued')",(ident,uid,payload))
        _schedule_request(con, ident, payload)
        topic, mid = con.execute("SELECT topic,message_id FROM schedule WHERE request_id=?", (ident,)).fetchone()
        # Bind full Telegram updates now. Opaque legacy payloads are bound only
        # when their actual restored message is available, never from guessed scope.
        try:
            sender = json.loads(payload).get("message", {}).get("from", {})
            trustworthy = type(mid) is int and mid > 0 and sender.get("id") == uid
        except (ValueError, TypeError, AttributeError):
            trustworthy = False
        if trustworthy:
            now = time.time()
            con.execute("INSERT OR IGNORE INTO request_audit VALUES(?,?,?,?,?,?,'queued','{}',NULL)",
                        ("update:" + str(ident), uid, topic, mid, now, now))
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
        # A pre-upgrade live queue has no schedule rows yet.
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
            con.execute("INSERT OR REPLACE INTO job_progress VALUES(?,?,?)", (record[0], now, "started"))
            # A verified login can arrive while a previously claimed model call is unwinding.
            con.execute("""INSERT OR REPLACE INTO model_attempts(request_id,auth_revision)
                           SELECT ?,coalesce(max(revision),0) FROM auth_recoveries""", (record[0],))
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


def runnable(*, root=ROOT, now=None):
    """Whether claim_next would start a queued request now. Read-only, for the periodic check."""
    now = time.time() if now is None else now
    with database(root) as con:
        return con.execute(f"""SELECT 1 FROM requests r LEFT JOIN schedule s ON s.request_id=r.id
              WHERE r.state='queued' AND (s.request_id IS NULL OR ({_CLAIMABLE}
                AND NOT EXISTS (SELECT 1 FROM delivery_parts p WHERE p.request_id=r.id
                                AND p.state='retry' AND p.retry_at>?)
                AND NOT EXISTS (SELECT 1 FROM model_retries m WHERE m.request_id=r.id AND m.retry_at>?)))
              LIMIT 1""", (MAX_LONG_JOBS, now, now)).fetchone() is not None


def has_running(*, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT 1 FROM requests WHERE state IN ('running','sending') LIMIT 1").fetchone() is not None


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
    """Commit retry before any best-effort notice. A restart cannot lose chapters or repeat the notice.

    kind is a model issue, or "infrastructure" after a storage or network failure."""
    now = time.time() if now is None else now
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM requests WHERE id=? AND state='running'", (ident,)).fetchone():
            raise RuntimeError("Group request is not running")
        return _retry_later(con, ident, kind, reset, now)


def interrupt(ident, *, root=ROOT, now=None):
    """Recover one job whose worker task ended unexpectedly, as a restart would, after a delay."""
    now = time.time() if now is None else now
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        current = con.execute("SELECT state FROM requests WHERE id=?", (ident,)).fetchone()
        if current == ("sending",):
            con.execute("UPDATE requests SET state='uncertain' WHERE id=?", (ident,))
        elif current == ("running",):
            _retry_later(con, ident, "infrastructure", None, now)


def _retry_later(con, ident, kind, reset, now):
    row = con.execute("SELECT attempts FROM model_retries WHERE request_id=?", (ident,)).fetchone()
    attempts = (row[0] if row else 0) + 1
    delay = min(1800, (300 if kind in ("signin","quota") else 60) * 2 ** min(attempts-1, 5))
    due = now + delay
    if kind == "quota" and type(reset) in (int,float) and now < reset <= now + 32*86400:
        due = max(due, reset)
    con.execute("UPDATE model_attempts SET wait_kind=? WHERE request_id=?", (kind, ident))
    if kind in AUTH_WAITS:
        recovered = con.execute("""SELECT 1 FROM model_attempts
                                   WHERE request_id=? AND auth_revision <
                                   (SELECT max(revision) FROM auth_recoveries)""", (ident,)).fetchone()
        if recovered:
            due = now
    con.execute("INSERT OR REPLACE INTO model_retries VALUES(?,?,?)", (ident, attempts, due))
    con.execute("UPDATE requests SET state='queued' WHERE id=?", (ident,))
    return row is None


def release_auth_waits(incident, *, root=ROOT):
    """Commit one verified recovery; leave Telegram, quota and pre-migration timers intact."""
    if not isinstance(incident, str) or not incident or len(incident) > 128:
        raise ValueError("Invalid Codex recovery identifier")
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        saved = con.execute("INSERT OR IGNORE INTO auth_recoveries(incident) VALUES(?)", (incident,))
        if not saved.rowcount:
            return 0
        # Keep model_retries so the next attempt cannot repeat the participant notice.
        return con.execute("""UPDATE model_retries SET retry_at=0 WHERE request_id IN (
                              SELECT a.request_id FROM model_attempts a JOIN requests r ON r.id=a.request_id
                              WHERE a.wait_kind IN (?,?) AND r.state='queued')""", AUTH_WAITS).rowcount


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


def settle(ident, success, *, root=ROOT, diagnostic=None):
    with database(root) as con:
        if success:
            key = "update:" + str(ident)
            audit_state = con.execute("SELECT state FROM request_audit WHERE request_key=?", (key,)).fetchone()
            if audit_state:
                terminal = ("failed", "finished", "refused", "operator_confirmed", "dropped", "needs_clarification")
                final_state = audit_state[0] if audit_state[0] in terminal else "finished"
                if audit_state[0] not in terminal:
                    _queue_audit(con, ident, "action_completed", final_state)
                parts = con.execute("SELECT state,message_id FROM delivery_parts WHERE request_id=?",
                                    (ident,)).fetchall()
                confirmed = sum(state == "confirmed" and type(mid) is int and mid > 0 for state, mid in parts)
                # A replayed single error/command bypasses process_group. Retain its
                # receipts before the request deletion cascades to delivery_parts.
                _queue_audit(con, ident, "queue_completed", final_state,
                             {"confirmed_parts": confirmed, "pending_parts": len(parts) - confirmed,
                              "delivery_confirmed": bool(parts) and confirmed == len(parts)})
            con.execute("""INSERT OR REPLACE INTO completed_updates(id,completed_at)
                           SELECT id,? FROM requests WHERE id=?""", (time.time(), ident))
            con.execute("DELETE FROM requests WHERE id=?", (ident,))
        else:
            # Keep the request text for an operator requeue, and the answer if there is one.
            con.execute("UPDATE requests SET state='uncertain' WHERE id=?", (ident,))
            _queue_audit(con, ident, "delivery_uncertain", "uncertain", diagnostic or {})
            if con.execute("SELECT 1 FROM checkpoints WHERE request_id=? AND key=?", (ident, ANSWER)).fetchone():
                _drop_stages(con, ident)


def fail(ident, *, root=ROOT):
    """After an unexpected error was reported, keep the request and its checkpoints for the operator."""
    with database(root) as con:
        con.execute("UPDATE requests SET state='failed' WHERE id=? AND state IN ('running','sending')", (ident,))
        _queue_audit(con, ident, "operator_required", "failed")


def recover(*, root=ROOT):
    with database(root) as con:
        for ident, state in con.execute("SELECT id,state FROM requests WHERE state IN ('running','sending')").fetchall():
            _queue_audit(con, ident, "process_recovery", "queued" if state == "running" else "uncertain")
        con.execute("UPDATE requests SET state='queued' WHERE state='running'")
        con.execute("UPDATE requests SET state='uncertain' WHERE state='sending'")
        return con.execute("SELECT count(*) FROM requests WHERE state='queued'").fetchone()[0]


# Operator view of jobs that need a decision. Metadata only: never message text or answers.
def jobs(*, root=ROOT, include_all=False):
    with database(root) as con:
        return con.execute(f"""SELECT r.id,r.state,r.uid,coalesce(s.topic,0),length(r.body)>0,
                 (SELECT count(*) FROM checkpoints c WHERE c.request_id=r.id),
                 (SELECT coalesce(sum(length(c.value)),0) FROM checkpoints c WHERE c.request_id=r.id),
                 EXISTS(SELECT 1 FROM checkpoints c WHERE c.request_id=r.id AND c.key=?),
                 (SELECT count(*) FROM delivery_parts p WHERE p.request_id=r.id AND p.state='confirmed'),
                 (SELECT count(*) FROM delivery_parts p WHERE p.request_id=r.id AND p.state!='confirmed'),
                 m.attempts,m.retry_at,a.wait_kind,g.updated,g.stage
               FROM requests r LEFT JOIN schedule s ON s.request_id=r.id
               LEFT JOIN model_retries m ON m.request_id=r.id LEFT JOIN model_attempts a ON a.request_id=r.id
               LEFT JOIN job_progress g ON g.request_id=r.id
               WHERE ? OR r.state IN ('uncertain','failed') OR m.request_id IS NOT NULL
               ORDER BY r.id""", (ANSWER, include_all)).fetchall()


def resolve(ident, action, *, root=ROOT, now=None):
    """Settle a job the gateway will not finish by itself. Running jobs are never touched.

    delivered: the member has the answer; finish it like a completed job.
    requeue: run it again from its checkpoints; an ambiguous part is sent again.
    drop: abandon it without an answer, also when it waits for a retry."""
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT state,body FROM requests WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise LookupError("No such group request")
        current, body = row
        allowed = {"delivered": ("uncertain", "failed"), "requeue": ("uncertain", "failed"),
                   "drop": ("uncertain", "failed", "queued")}[action]
        if current not in allowed:
            raise ValueError(f"A {current} request cannot be marked {action}")
        _queue_audit(con, ident, "operator_resolution", "queued" if action == "requeue" else
                     "operator_confirmed" if action == "delivered" else "dropped", {"action": action})
        if action == "delivered":
            con.execute("""UPDATE turn_results SET delivery_status='operator_confirmed',confirmed_at=?
                           WHERE turn_id=(SELECT turn_id FROM request_audit WHERE request_key=?)""",
                        (time.time(), "update:" + str(ident)))
        if action == "requeue":
            if not body:
                raise ValueError("The request text was not kept, so it cannot run again")
            if current == "failed":
                # Only the error notice was sent. Replace it with the real answer.
                con.execute("DELETE FROM delivery_parts WHERE request_id=?", (ident,))
                con.execute("DELETE FROM checkpoints WHERE request_id=? AND key='outbound.single_text'", (ident,))
            else:
                con.execute("DELETE FROM delivery_parts WHERE request_id=? AND state='sending'", (ident,))
            con.execute("UPDATE model_retries SET retry_at=0 WHERE request_id=?", (ident,))
            con.execute("UPDATE delivery_parts SET retry_at=0 WHERE request_id=? AND state='retry'", (ident,))
            con.execute("UPDATE requests SET state='queued' WHERE id=?", (ident,))
            return
        con.execute("INSERT OR REPLACE INTO completed_updates(id,completed_at) VALUES(?,?)",
                    (ident, time.time() if now is None else now))
        con.execute("DELETE FROM requests WHERE id=?", (ident,))


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
