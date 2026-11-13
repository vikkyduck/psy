"""A member's group materials and conversations. No filename from a message is opened."""
import contextlib
import os
import sqlite3
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
        con.executescript("""
            CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, uid INTEGER NOT NULL,
              body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, topic INTEGER NOT NULL,
              name TEXT NOT NULL, kind TEXT NOT NULL, data BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS member_docs ON documents(uid,topic,id);
            CREATE TABLE IF NOT EXISTS turns (
              id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, topic INTEGER NOT NULL,
              prompt TEXT NOT NULL, answer TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS member_turns ON turns(uid,topic,id);
        """)
        yield con
        con.commit()
    finally:
        con.close()


def archive(uid, topic, name, kind, data, *, root=ROOT):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        member, total = con.execute("""SELECT coalesce(sum(CASE WHEN uid=? THEN length(data)
                      ELSE 0 END),0), coalesce(sum(length(data)),0) FROM documents""", (uid,)).fetchone()
        if member + len(data) > MEMBER_LIMIT or total + len(data) > TOTAL_LIMIT:
            raise ValueError("В хранилище недостаточно места для нового файла.")
        cursor = con.execute("INSERT INTO documents(uid,topic,name,kind,data) VALUES (?,?,?,?,?)",
                             (uid, topic, name[:140], kind, data))
        return cursor.lastrowid


def listing(uid, topic, *, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT id,name FROM documents WHERE uid=? AND topic=? ORDER BY id DESC LIMIT 40",
                           (uid, topic)).fetchall()


def document(uid, topic, ident, *, root=ROOT):
    with database(root) as con:
        return con.execute("SELECT name,kind,data FROM documents WHERE uid=? AND topic=? AND id=?",
                           (uid, topic, ident)).fetchone()


def history(uid, topic, *, root=ROOT):
    with database(root) as con:
        rows = con.execute("SELECT prompt,answer FROM turns WHERE uid=? AND topic=? ORDER BY id DESC LIMIT 6",
                           (uid, topic)).fetchall()
    turns = []
    for prompt, answer in reversed(rows):
        turns.append({"role": "user", "content": prompt[:3000]})
        turns.append({"role": "assistant", "content": answer[:4000]})
    return turns


def remember(uid, topic, question, answer, *, root=ROOT):
    with database(root) as con:
        con.execute("INSERT INTO turns(uid,topic,prompt,answer) VALUES(?,?,?,?)",
                    (uid, topic, question[:5000], answer[:65000]))


def pending(uid, ident, payload, *, root=ROOT):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM requests WHERE id=?", (ident,)).fetchone():
            return "existing"
        count, own = con.execute("""SELECT count(*),coalesce(sum(uid=?),0)
                                    FROM requests WHERE state IN ('queued','running','sending')""",(uid,)).fetchone()
        if count>=12 or own>=4 or len(payload)>300000:
            return "full"
        con.execute("INSERT INTO requests(id,uid,body,state) VALUES(?,?,?,'queued')",(ident,uid,payload))
        return "queued"


def claim_next(*, root=ROOT):
    with database(root) as con:
        con.execute("BEGIN IMMEDIATE")
        record = con.execute("SELECT id,body FROM requests WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
        if record:
            con.execute("UPDATE requests SET state='running' WHERE id=?", (record[0],))
        return record


def outbound(ident, *, root=ROOT):
    # Persist BEFORE non-idempotent Telegram delivery. An ambiguous send cannot be replayed safely.
    with database(root) as con:
        con.execute("UPDATE requests SET state='sending' WHERE id=? AND state='running'", (ident,))


def settle(ident, success, *, root=ROOT):
    with database(root) as con:
        if success:
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
