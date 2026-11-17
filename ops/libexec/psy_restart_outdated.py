#!/usr/bin/python3
"""Restart the bot's long-running services after library upgrades, at night and only while idle.

/etc/needrestart/conf.d/50-psy.conf stops needrestart (run by unattended-upgrades) from restarting
hermes-gateway and the psy-* services in the middle of the day. This helper, started every 20
minutes during the night by psy-restart-outdated.timer, asks needrestart which of them run outdated
binaries or libraries and restarts those, in dependency order, only when:
  - the group queue has nothing running or sending and no queued request that could start now
    (requests parked until a retry time — OAuth, quota, Telegram flood control — do not count),
  - the upload queue has nothing processing and no queued recording that is due,
  - the owner's own agent has no active run (gateway control socket), when the gateway is due,
  - no Telegram message reached the gateway in the last QUIET seconds.
Otherwise it waits for the next run. A failed restart exits non-zero, which alerts the owner, and so
does a service left on outdated code for more than OUTDATED_ALERT seconds.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time

# ops/inventory.json restart_order: the upload side first, the gateway that calls it last.
RESTART_ORDER = ("psy-asr.service", "psy-upload-worker.service", "psy-upload.service", "hermes-gateway.service")
HOME = Path("/home/hermes/.hermes")
GROUP_DB = str(HOME / "vault/group-psy/group.sqlite3")
UPLOAD_DB = str(HOME / "vault/uploads/state.sqlite3")
GATEWAY_LOG = HOME / "logs/gateway.log"
GATEWAY_SOCKET = HOME / "gateway.sock"
UPLOAD_HEALTH = Path("/var/lib/psy-upload-health/state.json")
STATE = Path("/var/lib/psy-restart-outdated/state.json")
QUIET = 20 * 60
FRESH = 10 * 60
OUTDATED_ALERT = 48 * 3600
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

# Both probes run as hermes, the owner of both databases, and open them read-write: a read-only
# open of a WAL database fails while no other process holds it, and only the owner may create -shm.
GROUP_PROBE = """
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], timeout=30)
tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
waits = []
if "model_retries" in tables:
    waits.append("exists (select 1 from model_retries m where m.request_id=r.id and m.retry_at>:now)")
if "delivery_parts" in tables:
    waits.append("exists (select 1 from delivery_parts p where p.request_id=r.id and p.state='retry' and p.retry_at>:now)")
parked = " or ".join(waits) or "0"
print(db.execute("select count(*) from requests r where r.state in ('running','sending') "
                 "or (r.state='queued' and not (" + parked + "))", {"now": time.time()}).fetchone()[0]
      if "requests" in tables else 0)
"""
UPLOAD_PROBE = """
import sqlite3, sys, time
db = sqlite3.connect(sys.argv[1], timeout=30)
columns = {r[1] for r in db.execute("pragma table_info(jobs)")}
due = "next_try<=:now" if "next_try" in columns else "1"
print(db.execute("select count(*) from jobs where state in ('processing','sending') "
                 "or (state='queued' and " + due + ")", {"now": time.time()}).fetchone()[0])
"""


def run(*command, timeout=60):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd="/", env=ENV)


def outdated():
    """Services needrestart lists as running outdated code (batch mode never restarts anything)."""
    result = run("needrestart", "-b", "-r", "l", timeout=600)
    if result.returncode:
        raise RuntimeError(f"needrestart exited with {result.returncode}")
    listed = {line.split(":", 1)[1].strip() for line in result.stdout.splitlines()
              if line.startswith("NEEDRESTART-SVC:")}
    return [unit for unit in RESTART_ORDER if unit in listed]


def group_busy():
    if not os.path.exists(GROUP_DB):
        return 0
    result = run("runuser", "-u", "hermes", "--", "/usr/bin/python3", "-I", "-c", GROUP_PROBE, GROUP_DB)
    if result.returncode:
        raise RuntimeError("group queue unreadable")
    return int(result.stdout.strip())


def upload_busy(now):
    """Recordings being processed or due now; the health snapshot only when the database can't be read."""
    if os.path.exists(UPLOAD_DB):
        result = run("runuser", "-u", "hermes", "--", "/usr/bin/python3", "-I", "-c", UPLOAD_PROBE, UPLOAD_DB)
        if not result.returncode:
            return int(result.stdout.strip())
    state = json.loads(UPLOAD_HEALTH.read_text())
    if now - float(state.get("checked_at", 0)) > FRESH or "queue" not in state:
        raise RuntimeError("upload queue state is stale or unavailable")
    queue = state["queue"]
    return int(queue.get("queued", 0)) + int(queue.get("processing", 0))


def owner_agent_busy():
    """True while the gateway runs an agent turn (the owner's own conversations included)."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(12)
        client.connect(str(GATEWAY_SOCKET))
        client.sendall(b'{"verb":"status"}\n')
        data = b""
        while b"\n" not in data and len(data) < 524288:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    answer = json.loads(data)
    if not answer.get("ok"):
        raise ValueError("control status rejected")
    result = answer["result"]
    return bool(result.get("active_agents") or result.get("active_work"))


def last_inbound():
    """Time of the newest inbound Telegram message in the gateway log, or None."""
    fd = os.open(GATEWAY_LOG, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as log:
        if not stat.S_ISREG(os.fstat(log.fileno()).st_mode):
            raise OSError("gateway log is not a regular file")
        log.seek(0, 2)
        log.seek(max(0, log.tell() - 262144))
        lines = log.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if "inbound message: platform=telegram" in line:
            try:
                return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                return None
    return None


def busy_reasons(now, units=()):
    reasons = []
    try:
        if count := group_busy():
            reasons.append(f"group queue has {count} request(s) in progress")
    except Exception as error:
        reasons.append(f"group queue not readable ({type(error).__name__})")
    try:
        if count := upload_busy(now):
            reasons.append(f"upload queue has {count} recording(s) queued or processing")
    except Exception as error:
        reasons.append(f"upload queue unknown ({type(error).__name__})")
    if "hermes-gateway.service" in units:
        try:
            if owner_agent_busy():
                reasons.append("the gateway is running an agent turn")
        except Exception as error:
            reasons.append(f"gateway status unknown ({type(error).__name__})")
    try:
        inbound = last_inbound()
        if inbound is not None and now - inbound < QUIET:
            reasons.append(f"a Telegram message arrived {int(now - inbound)} s ago")
    except OSError:
        pass   # no readable log: the queues above decide
    return reasons


def restart(units):
    failed = []
    for unit in units:
        result = run("systemctl", "restart", unit, timeout=180)
        for _ in range(45):   # up to 90 s to become active
            if not run("systemctl", "is-active", "--quiet", unit).returncode:
                break
            time.sleep(2)
        else:
            failed.append(unit)
            continue
        if result.returncode:
            failed.append(unit)
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-only", action="store_true", help="report, never restart")
    args = parser.parse_args()
    try:
        units = outdated()
    except FileNotFoundError:
        print("needrestart is not installed; nothing to do")
        return 0
    now = time.time()
    if not units:
        remember_outdated(None)
        print("restart-outdated: no PSY service runs outdated code")
        return 0
    since = remember_outdated(now)
    reasons = busy_reasons(now, units)
    print(f"restart-outdated: outdated {', '.join(units)}; "
          + ("waiting: " + "; ".join(reasons) if reasons else "bot idle"))
    if args.check_only:
        return 0
    if reasons:
        if now - since > OUTDATED_ALERT:
            print(f"restart-outdated: outdated for {int((now - since) // 3600)} h; the bot was never idle at night")
            return 1
        return 0
    failed = restart(units)
    if failed:
        print(f"restart-outdated: not running after restart: {', '.join(failed)}")
        return 1
    remember_outdated(None)
    print(f"restart-outdated: restarted {', '.join(units)}")
    return 0


def remember_outdated(now):
    """When the services were first seen outdated (None clears it); the stored time is returned."""
    try:
        since = float(json.loads(STATE.read_text()).get("since"))
    except (OSError, ValueError, TypeError):
        since = None
    if now is None or since is None:
        since = now
        try:
            STATE.parent.mkdir(mode=0o700, exist_ok=True)
            tmp = STATE.with_name(STATE.name + ".tmp")
            tmp.write_text(json.dumps({"since": since}))
            os.replace(tmp, STATE)
        except OSError:
            pass   # a missing state folder only delays the alert
    return since if since is not None else now


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print("restart-outdated: a command timed out")
        raise SystemExit(1)
    except RuntimeError as error:
        print(f"restart-outdated: {error}")
        raise SystemExit(1)
