#!/usr/bin/python3
"""Restart the bot's long-running services after library upgrades, at night and only while idle.

/etc/needrestart/conf.d/50-psy.conf stops needrestart (run by unattended-upgrades) from restarting
hermes-gateway and the psy-* services in the middle of the day. This helper, started every 20
minutes during the night by psy-restart-outdated.timer, asks needrestart which of them run outdated
binaries or libraries and restarts those, in dependency order, only when:
  - the group queue has no queued, running or sending request (read as hermes, like the updater),
  - the upload queue has nothing queued or processing (psy-upload-health's recent snapshot),
  - no Telegram message reached the gateway in the last QUIET seconds.
Otherwise it waits for the next run. A failed restart exits non-zero, which alerts the owner.
"""
import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import time

# ops/inventory.json restart_order: the upload side first, the gateway that calls it last.
RESTART_ORDER = ("psy-asr.service", "psy-upload-worker.service", "psy-upload.service", "hermes-gateway.service")
GROUP_DB = "/home/hermes/.hermes/vault/group-psy/group.sqlite3"
GATEWAY_LOG = Path("/home/hermes/.hermes/logs/gateway.log")
UPLOAD_HEALTH = Path("/var/lib/psy-upload-health/state.json")
QUIET = 20 * 60
FRESH = 10 * 60
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

GROUP_PROBE = """
import sqlite3, sys
path = "file:" + sys.argv[1]
try:
    db = sqlite3.connect(path + "?mode=ro", uri=True, timeout=5)
    db.execute("select count(*) from sqlite_master").fetchone()
except sqlite3.OperationalError:
    db = sqlite3.connect(path + "?mode=ro&immutable=1", uri=True, timeout=5)
tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
print(db.execute("select count(*) from requests where state in ('queued','running','sending')").fetchone()[0]
      if "requests" in tables else 0)
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
    state = json.loads(UPLOAD_HEALTH.read_text())
    if now - float(state.get("checked_at", 0)) > FRESH or "queue" not in state:
        raise RuntimeError("upload queue state is stale or unavailable")
    queue = state["queue"]
    return int(queue.get("queued", 0)) + int(queue.get("processing", 0))


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


def busy_reasons(now):
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
    if not units:
        print("restart-outdated: no PSY service runs outdated code")
        return 0
    reasons = busy_reasons(time.time())
    print(f"restart-outdated: outdated {', '.join(units)}; "
          + ("waiting: " + "; ".join(reasons) if reasons else "bot idle"))
    if reasons or args.check_only:
        return 0
    failed = restart(units)
    if failed:
        print(f"restart-outdated: not running after restart: {', '.join(failed)}")
        return 1
    print(f"restart-outdated: restarted {', '.join(units)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print("restart-outdated: a command timed out")
        raise SystemExit(1)
    except RuntimeError as error:
        print(f"restart-outdated: {error}")
        raise SystemExit(1)
