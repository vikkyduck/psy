#!/usr/bin/python3
"""Owner-only alerts independent of Hermes, the model and the Telegram update loop."""
import argparse
import fcntl
import json
import os
from pathlib import Path
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
          "psy-upload-cert-renew", "psy-backup")
REMIND = 3600


def properties(unit):
    result = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result",
                             "-p", "NRestarts"], capture_output=True, text=True, timeout=5, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def group_status(path, now):
    """Read metadata only. Neither checkpoint text nor participant data leave the host."""
    if not path.exists():
        return ["Хранилище заданий группы недоступно."]
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as db:
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
        counter = int(info.get("NRestarts", 0))
        counters[unit] = counter
        if unit in CORE and info.get("ActiveState") != "active":
            problems["service:" + unit] = f"Служба {unit} остановлена или не смогла запуститься."
        if info.get("Result", "success") not in ("", "success"):
            problems["failure:" + unit] = f"Сбой службы {unit}. Подробности сохранены в закрытом журнале сервера."
        old = previous.get("counters", {}).get(unit)
        if old is not None and counter > old:
            problems["restart:" + unit] = f"Служба {unit} автоматически перезапускалась."
    for key, path in (("gateway_health", Path("/var/lib/psy-healthcheck/state.json")),
                      ("upload_health", Path("/var/lib/psy-upload-health/state.json"))):
        if not path.exists():
            continue
        state = json.loads(path.read_bytes())
        if key == "upload_health" and state.get("healthy") is False:
            problems[key] = "Проверка загрузки или распознавания сообщает о сбое."
        if key == "gateway_health" and state.get("last_restart", 0) > previous.get("last_restart", 0):
            if previous.get("last_restart") is not None:
                problems[key] = "Проверка ПСИ выполнила восстановительный перезапуск."
            previous["last_restart"] = state["last_restart"]
    try:
        messages = group_status(HOME / "vault/group-psy/group.sqlite3", now)
    except (OSError, sqlite3.Error):
        messages = ["Не удалось прочитать состояние очереди группы."]
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
    def __init__(self, seconds):
        self.seconds = max(1, min(float(seconds), 86400))


def send_owner(text):
    owner = os.environ.get("PSY_OWNER_ID", "")
    if not owner.isdecimal() or int(owner) <= 0:
        raise ValueError("Invalid owner")
    body = urllib.parse.urlencode(dict(chat_id=owner, text=text, protect_content="true")).encode()
    request = urllib.request.Request("https://api.telegram.org/bot" + token() + "/sendMessage", data=body)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            answer = json.load(response)
    except urllib.error.HTTPError as error:
        # Never print exception strings, URLs, request bodies or credential paths.
        if error.code == 429:
            answer = json.loads(error.read(8192))
            raise Later(answer.get("parameters", {}).get("retry_after", 60)) from None
        raise Later(300) from None
    if not answer.get("ok"):
        raise Later(answer.get("parameters", {}).get("retry_after", 300))
    if str(answer["result"]["chat"]["id"]) != owner:
        raise ValueError("Owner delivery mismatch")


def update_alerts(state, problems, now, sender=send_owner):
    alerts = state.setdefault("alerts", {})
    for key in list(alerts):
        if key not in problems and alerts[key].get("sent"):
            if now < alerts[key].get("recovery_due", 0):
                continue
            try:
                sender("ПСИ. Ранее обнаруженная проблема устранена. Проверка " + key + " прошла успешно.")
            except Later as error:
                alerts[key]["recovery_due"] = now + error.seconds
            except Exception:
                alerts[key]["recovery_due"] = now + 60
            else:
                del alerts[key]
        elif key not in problems:
            del alerts[key]
    for key, summary in problems.items():
        record = alerts.setdefault(key, {"due": 0, "summary": summary})
        record["summary"] = summary
        if now < record["due"]:
            continue
        try:
            sender("ПСИ. Требуется внимание. " + summary + " Это личное уведомление владельцу.")
        except Later as error:
            record["due"] = now + error.seconds
        except Exception:
            record["due"] = now + 60
        else:
            record.update(sent=True, due=now + REMIND)


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--test-notice", action="store_true")
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        print("alerts_check issues=" + str(len(problems)))
        if args.check_only:
            return
        update_alerts(state, problems, now)
        state["counters"] = counters
        state["checked_at"] = now
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.chmod(0o600)
        temporary.replace(path)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("alerts_check_failed " + type(error).__name__)
        raise SystemExit(1)
