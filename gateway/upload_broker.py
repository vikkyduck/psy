"""One systemd-accepted, UID-checked request -> a 5-minute upload session for PSY only."""
import json
import os
import pwd
import secrets
import socket
import sqlite3
import struct
import sys
sys.path.insert(0,"/opt/psy-upload")
import recording_grant
import time

def group_id():
    value = os.environ.get("PSY_GROUP_ID", "")
    if not value.startswith("-") or not value[1:].isascii() or not value[1:].isdecimal() or len(value) > 17:
        raise ValueError("Invalid PSY_GROUP_ID")
    group = int(value)
    if str(group) != value or not -(2**52) < group < 0:
        raise ValueError("Invalid PSY_GROUP_ID")
    return group


GROUP = group_id()
DATABASE = "/srv/psy-queue/state.sqlite3"


def issue(request, database=DATABASE):
    if not isinstance(request, dict) or set(request) != {"uid","thread","reply"}:
        raise ValueError()
    uid, thread, reply = (request[k] for k in ("uid", "thread", "reply"))
    if not (type(uid) is int and 0 < uid < 2**52 and type(reply) is int and 0 < reply < 2**31):
        raise ValueError()
    if thread is not None and not (type(thread) is int and 0 < thread < 2**31):
        raise ValueError()
    now, token = int(time.time()), secrets.token_urlsafe(32)
    with sqlite3.connect("file:" + database + "?mode=rw", uri=True, timeout=5) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM sessions WHERE expires<?", (now,))
        n = c.execute("SELECT count(*) FROM sessions WHERE uid=? AND chat=? AND expires<?",
                      (uid,GROUP,now+301)).fetchone()[0]
        if n >= 24:
            raise ValueError()
        grant = recording_grant.session(token,dict(uid=uid,chat=GROUP,thread=thread,reply=reply),now+300)
        c.execute("INSERT INTO sessions(hash,uid,chat,thread,reply,expires,proof) VALUES(:hash,:uid,:chat,:thread,:reply,:expires,:proof)",grant)
    return token


def serve(peer, expected_uid, database=DATABASE):
    _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    if uid != expected_uid:
        return
    peer.settimeout(5)
    with peer.makefile("rb") as reader:
        request = reader.readline(513)
        if len(request) > 512 or not request.endswith(b"\n"):
            return
        try:
            result = issue(json.loads(request), database)
        except (ValueError,TypeError,sqlite3.Error):
            return
        peer.sendall(result.encode() + b"\n")


if __name__ == "__main__":
    with socket.socket(fileno=0) as client:
        serve(client,pwd.getpwnam("hermes").pw_uid)
