#!/usr/bin/python3
"""Owner-only alerts independent of Hermes, the model and the Telegram update loop.

One run collects every problem, then sends at most one digest message to the owner. A problem is
reported once and reminded with growing gaps; it counts as resolved only after CLEAR_AFTER clean
runs in a row, and events (a restart that already happened) are never "resolved". The owner gets
at most HOURLY_CAP messages per hour; anything held back goes into the next digest. Delivery
failures are logged with the HTTP status only. After FAILING_AFTER failed deliveries in a row the
run exits non-zero, so the broken alert path shows in `systemctl --failed` and in psy-healthcheck.
"""
import argparse
import calendar
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

STATE = Path("/var/lib/psy-alerts")
HOME = Path("/home/hermes/.hermes")
CORE = ("hermes-gateway", "psy-upload", "psy-upload-worker", "psy-asr")
CHECKS = ("psy-healthcheck", "psy-upload-health", "psy-codex-auth",
          "psy-upload-cert-renew", "psy-backup", "psy-vault-archive", "psy-restart-outdated")
# Every timer the bot relies on, with the longest normal gap between two of its runs.
TIMERS = {
    "psy-alerts.timer": 600, "psy-healthcheck.timer": 600, "psy-upload-health.timer": 600,
    "psy-vault-archive.timer": 3600, "psy-codex-auth.timer": 3 * 3600,
    "psy-upload-cert-renew.timer": 14 * 3600, "psy-backup.timer": 27 * 3600,
    "psy-restart-outdated.timer": 25 * 3600,
}
BACKUP_SUCCESS = Path("/var/lib/psy-backup/last-success")
BACKUP_MAX_AGE = 26 * 3600
CLEAR_AFTER = 3            # clean runs in a row before a problem counts as resolved
HOURLY_CAP = 4             # messages to the owner per rolling hour
REMIND = (3600, 3 * 3600, 6 * 3600)   # gaps between reminders of one unresolved problem
FAILING_AFTER = 3          # failed deliveries in a row before the run itself fails
EVENTS = ("restart:", "gateway_health")   # one-off events: reported once, never "resolved"
MAX_TEXT = 3500
TOKEN = re.compile(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,}")
UNIT_NAME = re.compile(r"[A-Za-z0-9@_.:\\-]{1,200}")


def properties(unit, *names):
    names = names or ("ActiveState", "Result", "NRestarts")
    command = ["systemctl", "show", unit]
    for name in names:
        command += ["-p", name]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True,
                            env={"PATH": "/usr/bin:/bin", "TZ": "UTC", "LC_ALL": "C"})
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def timestamp(value):
    """systemctl prints 'Tue 2026-11-17 01:39:57 UTC' with TZ=UTC; empty or 'n/a' when never."""
    try:
        return calendar.timegm(time.strptime(value.split(" ", 1)[1], "%Y-%m-%d %H:%M:%S UTC"))
    except (IndexError, ValueError):
        return None


def uptime():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def hours(seconds):
    return f"{seconds / 3600:g} ч" if seconds >= 3600 else f"{seconds // 60:g} мин"


def timer_problems(now, booted_for):
    problems, enabled = {}, set()
    for timer, gap in TIMERS.items():
        try:
            info = properties(timer, "UnitFileState", "ActiveState", "LastTriggerUSec")
        except Exception:
            problems["probe:" + timer] = f"Не удалось проверить таймер {timer}."
            continue
        if info.get("UnitFileState") not in ("enabled", "enabled-runtime") or info.get("ActiveState") != "active":
            problems["timer:" + timer] = f"Таймер {timer} выключен или не установлен, его проверка не выполняется."
            continue
        enabled.add(timer)
        last = timestamp(info.get("LastTriggerUSec", ""))
        idle = now - last if last else booted_for
        if idle > gap:
            problems["timer:" + timer] = f"Таймер {timer} не запускался больше {hours(gap)}."
    return problems, enabled


def backup_problem(now, timer_enabled):
    try:
        age = now - BACKUP_SUCCESS.stat().st_mtime
    except FileNotFoundError:
        return "Успешная резервная копия еще не записана, хотя ночное копирование включено." if timer_enabled else None
    except OSError:
        return "Не удалось проверить время последней резервной копии."
    if age > BACKUP_MAX_AGE:
        return f"Последней успешной резервной копии больше {int(age // 3600)} ч."
    return None


def open_readonly(path):
    """Read metadata without creating files. A WAL database between writes may have no -wal/-shm,
    which a read-only mount cannot create; then it is fully checkpointed and read as a file."""
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return db
    except sqlite3.OperationalError:
        db.close()
        return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=3)


def group_status(path, now):
    """Read metadata only. Neither checkpoint text nor participant data leave the host."""
    if not path.exists():
        return ["Хранилище заданий группы недоступно."]
    db = open_readonly(path)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "job_progress" not in tables:
            return []
        stalled = db.execute("""SELECT count(*) FROM requests r JOIN job_progress p ON p.request_id=r.id
                              WHERE r.state='running' AND p.updated<?""", (now - 420,)).fetchone()[0]
        # Queued jobs may intentionally wait for OAuth or Telegram flood control.
        uncertain = db.execute("SELECT count(*) FROM requests WHERE state='uncertain'").fetchone()[0]
        retrying = 0
        if "model_retries" in tables:
            retrying = db.execute("""SELECT count(*) FROM requests r JOIN model_retries m ON m.request_id=r.id
                                   WHERE r.state IN ('queued','running') AND m.attempts>0""").fetchone()[0]
    finally:
        db.close()
    messages = []
    if stalled:
        messages.append(f"Задания группы без признаков прогресса более 7 минут: {stalled}.")
    if uncertain:
        messages.append(f"Задания с неподтвержденным результатом отправки: {uncertain}. Нужна проверка доставки.")
    if retrying:
        messages.append(f"Сохраненные задания после сбоя модели: {retrying}. ПСИ ожидает повторного обращения к модели или уже выполняет его.")
    return messages


def memory_status(state, available, pressure):
    struggling = available < 96 * 1024**2 or pressure > 20
    state["memory_fails"] = state.get("memory_fails", 0) + 1 if struggling else 0
    return state["memory_fails"] >= 3


def collect(previous, now):
    problems = {}
    counters = {}
    for unit in CORE + CHECKS:
        try:
            info = properties(unit)
        except Exception:
            problems["probe:" + unit] = f"Не удалось проверить службу {unit}."
            continue
        counter = int(info.get("NRestarts", 0) or 0)
        counters[unit] = counter
        if unit in CORE and info.get("ActiveState") != "active":
            problems["service:" + unit] = f"Служба {unit} остановлена или не смогла запуститься."
        if info.get("Result", "success") not in ("", "success"):
            problems["failure:" + unit] = f"Сбой службы {unit}. Подробности сохранены в закрытом журнале сервера."
        old = previous.get("counters", {}).get(unit)
        if old is not None and counter > old:
            problems["restart:" + unit] = f"Служба {unit} автоматически перезапускалась."
    timers, enabled = timer_problems(now, uptime())
    problems.update(timers)
    backup = backup_problem(now, "psy-backup.timer" in enabled)
    if backup:
        problems["backup_age"] = backup
    for key, path in (("gateway_health", Path("/var/lib/psy-healthcheck/state.json")),
                      ("upload_health", Path("/var/lib/psy-upload-health/state.json"))):
        if not path.exists():
            continue
        state = json.loads(path.read_bytes())
        if key == "upload_health" and state.get("healthy") is False:
            problems[key] = "Проверка загрузки или распознавания сообщает о сбое."
        if key == "upload_health" and state.get("last_restart", 0) > previous.get("upload_restart", 0):
            if previous.get("upload_restart") is not None:
                units = ", ".join(u for u in state.get("restart_units", []) if UNIT_NAME.fullmatch(str(u)))
                problems["restart:upload-health"] = f"Проверка загрузки перезапустила службы: {units or 'неизвестно'}."
            previous["upload_restart"] = state["last_restart"]
        if key == "gateway_health" and state.get("last_restart", 0) > previous.get("last_restart", 0):
            if previous.get("last_restart") is not None:
                problems[key] = "Проверка ПСИ выполнила восстановительный перезапуск."
            previous["last_restart"] = state["last_restart"]
    try:
        messages = group_status(HOME / "vault/group-psy/group.sqlite3", now)
        previous["group_read_fails"] = 0
    except (OSError, sqlite3.Error):
        # A busy or mid-checkpoint read fails now and then; report only a lasting failure.
        previous["group_read_fails"] = previous.get("group_read_fails", 0) + 1
        messages = ["Не удалось прочитать состояние очереди группы."] if previous["group_read_fails"] >= 3 else []
    if messages:
        problems["group"] = " ".join(messages)
    try:
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        available = int(memory["MemAvailable"].split()[0]) * 1024
        pressure_line = Path("/proc/pressure/memory").read_text().splitlines()[0].split()[1:]
        pressure = float(dict(item.split("=", 1) for item in pressure_line)["avg10"])
        if memory_status(previous, available, pressure):
            problems["memory"] = "На сервере ПСИ сохраняется нехватка оперативной памяти или ожидание памяти при 3 проверках подряд. Требуется увеличить ресурсы."
    except (OSError, ValueError, KeyError, IndexError):
        pass
    origin = urllib.parse.urlsplit(os.environ["PSY_PUBLIC_URL"])
    context = ssl.create_default_context()
    try:
        with socket.create_connection((origin.hostname, origin.port or 443), timeout=8) as conn:
            with context.wrap_socket(conn, server_hostname=origin.hostname) as secured:
                expiry = ssl.cert_time_to_seconds(secured.getpeercert()["notAfter"])
        if expiry - now < 86400:
            problems["tls"] = "Сертификат мини-апки истекает менее чем через сутки. Требуется проверить продление."
    except (OSError, ValueError):
        problems["tls"] = "Не удалось проверить защищенное соединение мини-апки."
    return problems, counters


def token():
    """The root-only systemd credential first, then the environment, then the profile .env."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        try:
            staged = (Path(directory) / "telegram").read_text().strip()
        except OSError:
            staged = ""
        if TOKEN.fullmatch(staged):   # an unstaged credential arrives as a placeholder
            return staged
    supplied = os.environ.get("TELEGRAM_BOT_TOKEN")
    if supplied:
        return supplied
    credential = Path(os.environ.get("PSY_TELEGRAM_CREDENTIAL_FILE", str(HOME / ".env")))
    for line in credential.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "TELEGRAM_BOT_TOKEN":
            value = value.strip().strip("\"'")
            if value:
                return value
    raise ValueError("credential missing")


class Later(Exception):
    """Delivery did not happen; try again after `seconds`. `reason` is safe to log."""
    def __init__(self, seconds, reason="network"):
        super().__init__(reason)
        self.seconds = max(1, min(float(seconds), 86400))
        self.reason = reason


def send_owner(text):
    owner = os.environ.get("PSY_OWNER_ID", "")
    if not owner.isdecimal() or int(owner) <= 0:
        raise ValueError("Invalid owner")
    body = urllib.parse.urlencode(dict(chat_id=owner, text=text, protect_content="true")).encode()
    request = urllib.request.Request("https://api.telegram.org/bot" + token() + "/sendMessage", data=body)
    # Never print exception strings, URLs, request bodies or credential paths: status codes only.
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        retry = 300
        if error.code == 429:
            try:
                retry = json.loads(error.read(8192)).get("parameters", {}).get("retry_after", 60)
            except ValueError:
                retry = 60
        raise Later(retry, f"http={error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise Later(60, "network=" + type(error).__name__) from None
    if not answer.get("ok"):
        raise Later(answer.get("parameters", {}).get("retry_after", 300), f"api={answer.get('error_code')}")
    if str(answer["result"]["chat"]["id"]) != owner:
        raise ValueError("Owner delivery mismatch")


def is_event(key):
    return key.startswith(EVENTS)


def digest(new, ongoing, resolved, note):
    parts = ["ПСИ. Личное уведомление владельцу."]
    for title, items in (("Требуется внимание:", new), ("Все еще не устранено:", ongoing),
                         (f"Больше не наблюдается ({CLEAR_AFTER} проверки подряд без сбоя):", resolved)):
        if items:
            parts.append(title + "\n" + "\n".join("• " + item for item in items))
    if note:
        parts.append(note)
    text = "\n\n".join(parts)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rsplit("\n", 1)[0] + "\n…остальное в закрытом журнале сервера."
    return text


def update_alerts(state, problems, now, sender=send_owner):
    """Fold this run's problems into the state and send at most one digest."""
    alerts = state.setdefault("alerts", {})
    for record in alerts.values():   # state written by the previous version of this script
        record.setdefault("notified", bool(record.pop("sent", False)))
        record.setdefault("remind_at", record.pop("due", 0))
        record.setdefault("reminders", 0)
        record.setdefault("clean", 0)
        record.pop("recovery_due", None)
    for key, summary in problems.items():
        record = alerts.setdefault(key, {"notified": False, "remind_at": 0, "reminders": 0, "clean": 0})
        record.update(summary=summary, clean=0)
        record.pop("resolved", None)
    for key in list(alerts):
        if key in problems:
            continue
        record = alerts[key]
        record["clean"] += 1
        if is_event(key):
            if record["notified"]:
                del alerts[key]         # reported once; an event has nothing to resolve
        elif not record["notified"]:
            del alerts[key]             # gone before anyone was told
        elif record["clean"] >= CLEAR_AFTER:
            record["resolved"] = True

    new = [k for k, r in alerts.items() if not r["notified"]]
    ongoing = [k for k, r in alerts.items() if r["notified"] and not r.get("resolved") and now >= r["remind_at"]]
    resolved = [k for k, r in alerts.items() if r.get("resolved")]
    if not (new or ongoing or resolved):
        return
    delivery = state.setdefault("delivery", {"failures": 0})
    sent = [t for t in state.get("sent", []) if now - t < 3600]
    state["sent"] = sent
    if now < delivery.get("retry_at", 0):
        return
    if len(sent) >= HOURLY_CAP:
        if not delivery.get("capped_at"):
            print(f"alerts_held hourly_cap={HOURLY_CAP} pending={len(new) + len(ongoing) + len(resolved)}")
            delivery["capped_at"] = now
        return
    note = None
    if delivery.get("failures"):
        note = (f"Предыдущие уведомления не доставлены: попыток {delivery['failures']}, "
                f"причина {delivery.get('reason', 'unknown')}.")
    try:
        sender(digest([alerts[k]["summary"] for k in new], [alerts[k]["summary"] for k in ongoing],
                      [alerts[k]["summary"] for k in resolved], note))
    except Exception as error:
        later = error if isinstance(error, Later) else Later(60, "error=" + type(error).__name__)
        delivery.update(failures=delivery.get("failures", 0) + 1, reason=later.reason,
                        retry_at=now + later.seconds)
        print(f"alerts_delivery_failed {later.reason} failures={delivery['failures']} retry_in={int(later.seconds)}s")
        return
    state["delivery"] = {"failures": 0}
    sent.append(now)
    for key in ongoing:
        alerts[key]["reminders"] += 1
    for key in new + ongoing:
        record = alerts[key]
        record["remind_at"] = now + REMIND[min(record["reminders"], len(REMIND) - 1)]
        record["notified"] = True
    for key in resolved:
        del alerts[key]
    print(f"alerts_sent new={len(new)} ongoing={len(ongoing)} resolved={len(resolved)}")


def remote_status(state):
    """Check the primary HTTPS endpoint from a second host without loading its files."""
    request = urllib.request.Request(os.environ["PSY_PUBLIC_URL"].rstrip("/") + "/healthz")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            ok = response.status == 200 and json.loads(response.read(4096)).get("ok") is True
    except Exception:
        ok = False
    state["remote_fails"] = 0 if ok else state.get("remote_fails", 0) + 1
    if state["remote_fails"] >= 3:
        return {"primary_https": "Основной сервер ПСИ или его мини-апка недоступны при 3 внешних проверках подряд. Уведомление отправлено с резервного сервера."}
    return {}


def acquire(lock, seconds):
    """A run started by a failing unit waits for the timer's run instead of failing next to it."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--test-notice", action="store_true")
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--triggered-by", metavar="UNIT", help="set by psy-alert@<unit>.service (OnFailure)")
    args = parser.parse_args()
    os.umask(0o077)
    if args.triggered_by:
        unit = args.triggered_by if UNIT_NAME.fullmatch(args.triggered_by) else "invalid"
        result = os.environ.get("MONITOR_SERVICE_RESULT", "")
        print(f"alerts_triggered_by {unit} result={result if UNIT_NAME.fullmatch(result) else 'unknown'}")
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "lock").open("a") as lock:
        if not acquire(lock, 90):
            print("alerts_busy another check is still running")
            return
        if args.test_notice:
            send_owner("ПСИ. Проверка независимых уведомлений владельцу. Сообщение отправлено отдельной службой контроля.")
            print("owner_notice_confirmed")
            return
        path = STATE / "state.json"
        state = json.loads(path.read_bytes()) if path.exists() else {}
        now = time.time()
        if args.remote:
            problems, counters = remote_status(state), {}
        else:
            problems, counters = collect(state, now)
        # Keys name units and checks only, never summaries with counts or paths.
        print("alerts_check issues=" + str(len(problems)) + " keys=" + ",".join(sorted(problems)))
        if args.check_only:
            return
        update_alerts(state, problems, now)
        state["counters"] = counters
        state["checked_at"] = now
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)
        failures = state.get("delivery", {}).get("failures", 0)
        if failures >= FAILING_AFTER:
            print(f"alerts_delivery_failing failures={failures} {state['delivery'].get('reason', 'unknown')}")
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        print("alerts_check_failed " + type(error).__name__)
        raise SystemExit(1)
