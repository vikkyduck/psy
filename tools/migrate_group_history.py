"""Append legacy PSY turns verbatim to their existing Hermes sessions, transactionally.

Run against a WAL-aware snapshot first. This never creates a profile, session or
parallel store. Originals remain in group.sqlite3. Import timestamps describe
the migration because the legacy turns table contains no historical timestamps.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    default=lambda v: v.hex()).encode()).hexdigest()


def migrate(source, target, group, users):
    source, target = Path(source).resolve(strict=True), Path(target).resolve(strict=True)
    if source == target:
        raise ValueError("source and target must differ")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        turns = src.execute("SELECT id,uid,topic,prompt,answer FROM turns ORDER BY id").fetchall()
    if not turns or any(uid not in users for _, uid, *_ in turns):
        raise ValueError("empty history or an unapproved sender")
    fingerprint = digest(turns)
    ledger_key = "psy:standard-history:v1:" + str(group)
    db = sqlite3.connect(target, timeout=30)
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT value FROM state_meta WHERE key=?", (ledger_key,)).fetchone()
        if prior:
            report = json.loads(prior[0])
            if report["source_sha256"] != fingerprint:
                raise ValueError("source changed after an earlier import")
            for mid, expected in report["inserted_rows"]:
                row = db.execute("SELECT session_id,role,content FROM messages WHERE id=?", (mid,)).fetchone()
                if digest(row) != expected:
                    raise ValueError("previously imported message changed")
            db.rollback()
            return {**report, "inserted_now": 0}
        old_messages = db.execute("SELECT * FROM messages ORDER BY id").fetchall()
        old_max = max((row[0] for row in old_messages), default=0)
        old_hash = digest(old_messages)
        now = time.time()
        sessions, inserted = {}, []
        for turn_id, uid, topic, prompt, answer in turns:
            key = f"agent:main:telegram:group:{group}:{uid}" if topic == 0 else f"agent:main:telegram:group:{group}:{topic}"
            if key not in sessions:
                rows = db.execute("SELECT id FROM sessions WHERE session_key=? AND ended_at IS NULL", (key,)).fetchall()
                if len(rows) != 1:
                    raise ValueError("expected exactly one existing active session for " + key)
                sid = rows[0][0]
                routing = db.execute("SELECT scope,entry_json FROM gateway_routing WHERE session_key=?", (key,)).fetchall()
                if not routing:
                    raise ValueError("existing session has no routing entry")
                for scope, raw in routing:
                    entry = json.loads(raw)
                    if entry.get("session_id") != sid or entry.get("suspended"):
                        raise ValueError("routing mismatch or suspended session")
                    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                    db.execute("UPDATE gateway_routing SET entry_json=?,updated_at=? WHERE scope=? AND session_key=?",
                               (json.dumps(entry, ensure_ascii=False), now, scope, key))
                sessions[key] = sid
            sid = sessions[key]
            for role, content in [("user", prompt), ("assistant", answer)]:
                uid_key = f"psy-legacy:{group}:{uid}:{topic}:{turn_id}:{role}"
                cur = db.execute("INSERT INTO messages(session_id,role,content,timestamp,message_uid) VALUES(?,?,?,?,?)",
                                 (sid, role, content, now + len(inserted) / 1000000, uid_key))
                row = db.execute("SELECT session_id,role,content FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
                if row != (sid, role, content):
                    raise ValueError("message changed during insert")
                inserted.append((cur.lastrowid, digest(row)))
            db.execute("UPDATE sessions SET message_count=message_count+2,last_activity_at=?,last_activity_provenance=? WHERE id=?",
                       (now, "psy_legacy_history_import", sid))
        if digest(db.execute("SELECT * FROM messages WHERE id<=? ORDER BY id", (old_max,)).fetchall()) != old_hash:
            raise ValueError("preexisting messages changed")
        integrity = [r[0] for r in db.execute("PRAGMA integrity_check")]
        if integrity != ["ok"] or db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("target integrity check failed")
        report = {"source_sha256": fingerprint, "turns": len(turns), "messages": len(inserted),
                  "characters": sum(len(p)+len(a) for _, _, _, p, a in turns),
                  "sessions": sessions, "inserted_rows": inserted, "old_messages_sha256": old_hash,
                  "timestamp_semantics": "import time, legacy timestamps unavailable", "integrity": "ok"}
        db.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (ledger_key, json.dumps(report)))
        db.commit()
        return {**report, "inserted_now": len(inserted)}
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--users", required=True)
    parser.add_argument("--allow-live", action="store_true")
    args = parser.parse_args()
    if args.target.resolve() == Path("/home/hermes/.hermes/state.db") and not args.allow_live:
        parser.error("live target requires --allow-live after successful copy validation")
    result = migrate(args.source, args.target, args.group, {int(x) for x in args.users.split(",")})
    print(json.dumps({k: v for k, v in result.items() if k != "inserted_rows"}, ensure_ascii=False))
