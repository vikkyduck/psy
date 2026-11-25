#!/usr/bin/env python3
"""Real Codex composition on fixed synthetic cases, with blinded local output.

Run each variant in a separate process using the installed Hermes interpreter as
hermes. --checkout selects the runtime and its existing shared OAuth controller.
--module-dir optionally selects candidate group modules from a separate checkout.
No Telegram transport is created. Queue/delivery acceptance is a separate test.

The source reader retains its public-IP checks and production parser. By default
only explicit --source-host domains can be fetched. --all-public-sources lets
only the source reader authorize additional public hosts. --allow-pdf permits only
the exact production PDF decoder command with bounded input, private output and
the decoder's existing seccomp/Landlock sandbox. No general subprocess is allowed.

Use an external process deadline for a one-shot evaluation. It must not change
production model settings. All generated files stay outside the source checkout.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib
import inspect
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import sys
import tempfile
import time
from types import ModuleType
from unittest.mock import patch
from urllib.parse import urlsplit

import group_example


SYNTHETIC_MATERIAL = """Учебный материал, полностью вымышленный.
В двух группах по 12 слушателей провели одно занятие. В первой группе шесть
участников задали вопросы, во второй три. Запись потеряла последние 10 минут
второй группы. Анкеты заполнили семь и девять человек соответственно. Ведущий
предположил, что первая группа была мотивированнее. Других данных нет.
Задача учебного разбора - отделять наблюдение от интерпретации, замечать пропуски
данных, предлагать альтернативы и выбирать следующий способ проверки.
Не приписывай участникам диагнозы, биографии и несуществующие реплики."""


def synthetic_exchange(question, answer):
    return [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]


def memory_history(*, expanded=False):
    """Identical, fixed participant input for both variants, never real history."""
    history = synthetic_exchange(
        "Учебный вымышленный проект: подготовить внутреннюю памятку. Бюджет 120000 рублей, "
        "срок шесть недель. В команде редактор, аналитик и дизайнер. Итог оформляй таблицей "
        "с колонками Этап, Ответственный, Срок.",
        "Зафиксирован учебный проект памятки с указанными ресурсами и форматом.")
    history += synthetic_exchange(
        "Исправление к этому проекту: бюджет теперь 90000 рублей, срок четыре недели. "
        "Дизайнера исключаем. В итоговой таблице замени колонку Срок на Критерий приемки. "
        "Остальные условия сохраняются.",
        "Актуальные ограничения обновлены. Редактор и аналитик остаются в команде.")
    filler = ("This is a synthetic discussion note, not a new requirement. We distinguished a planned "
              "activity from a verified result, kept assumptions visible, and treated examples as examples. "
              "No dates, budgets, roles, table columns or deadlines were changed by this note. "
              "A test can check an observable condition without proving every future outcome. "
              "Previous explicit corrections remain the participant's current instructions. ")
    filler += "Synthetic row labels without project requirements: " + " ".join(f"{index:03d}" for index in range(256))
    for number in range(1, 9):
        history += synthetic_exchange(
            f"Уточнение {number}: объясни на одном учебном примере разницу между действием и проверяемым результатом. "
            "Условия проекта не меняй.",
            (f"Пример {number}: написать черновик - действие, согласованный текст - проверяемый результат. "
             + (filler if expanded else "Условия проекта сохранены.")))
    return history

CASES = {
    "ordinary_multipart": {
        "question": "Реши полностью учебную задачу без внешних источников. Проданы 80 наборов по 120 рублей. "
                    "Переменные затраты 45 рублей на набор, постоянные затраты 3000 рублей. "
                    "Посчитай выручку, переменные затраты, общие затраты и прибыль. Затем найди минимальное "
                    "целое число наборов для безубыточности. Покажи формулы, объясни допущение о постоянной "
                    "цене и затратах, напиши функцию Python profit(units), а после нее два проверочных assert "
                    "для 40 и 80 наборов. Последнее предложение: что произойдет с прибылью при продаже 39 наборов.",
        "review_checks": ["Выручка 9600, переменные затраты 3600, общие затраты 6600, прибыль 3000 рублей.",
                          "Безубыточность 40 наборов. profit(40)=0, profit(80)=3000, profit(39)=-75.",
                          "Есть формулы, допущение, исполнимая функция и два требуемых assert, выполнено последнее предложение."],
    },
    "project_plan": {
        "question": "Подготовь законченный рабочий план учебного вымышленного проекта переноса внутренней "
                    "базы знаний за четыре недели. Работают редактор и инженер, у каждого не более 12 часов "
                    "в неделю. В базе 60 одинаковых по трудоемкости статей. На инвентаризацию одной статьи "
                    "редактору нужно 10 минут, на правку 20 минут. Инженеру нужно 8 часов на настройку, "
                    "2 часа на перенос каждой партии из 20 статей и 6 часов на финальную проверку. "
                    "Настройка предшествует переносу, статьи исправляются до переноса, итоговая проверка "
                    "идет после всех трех партий. Без веба и выдуманных условий. Дай план по неделям, "
                    "нагрузку каждого по неделям и всего, зависимости, четыре риска с действиями, критерии "
                    "приемки и готовый текст короткого сообщения команде о запуске. Проверь реализуемость "
                    "плана в заданной доступности и явно покажи расчеты.",
        "review_checks": ["Редактор: 10 часов инвентаризации и 20 часов правки, всего 30 часов.",
                          "Инженер: 8+6+6=20 часов, ни у кого не больше 12 часов в отдельную неделю.",
                          "Порядок настройки, правки партий, переноса и проверки соблюден. Все 60 статей охвачены.",
                          "Есть четыре предметных риска с действиями, критерии приемки и готовое сообщение команде."],
    },
    "memory_update8": {
        "history": memory_history(),
        "question": "Собери итоговую компактную таблицу плана памятки по действующим условиям этой переписки. "
                    "Перед таблицей укажи актуальные бюджет, срок и состав команды. Условия заново не спрашивай, "
                    "внешние источники не нужны.",
        "review_checks": ["После восьми обменов сохранены бюджет 90000 рублей, срок четыре недели, редактор и аналитик.",
                          "В итоговой таблице колонки Этап, Ответственный, Критерий приемки.",
                          "Дизайнер, бюджет 120000, срок шесть недель и колонка Срок не возвращаются как действующие."],
    },
    "memory_cancel_topic": {
        "history": memory_history() + synthetic_exchange(
            "Задачу памятки закрываем. Все ее ограничения и требование отвечать таблицей отменяю для следующих тем.",
            "Задача закрыта, ее условия не относятся к новым темам."),
        "question": "Новая тема. Объясни школьнику, почему в десятичной записи 0,5 равно 1/2. "
                    "Нужен один связный абзац из трех предложений с бытовым примером, без таблицы и без источников.",
        "review_checks": ["Один абзац из трех предложений, корректное объяснение 5/10=1/2 и бытовой пример.",
                          "Нет таблицы и переноса бюджета, команды, сроков или памятки из закрытого проекта."],
    },
    "memory_compacted": {
        "history": memory_history(expanded=True),
        "question": "Назови только действующие бюджет, срок, роли и названия колонок итоговой таблицы проекта памятки. "
                    "Ничего не меняй и не спрашивай заново. Без внешних источников.",
        "context_input_budget_tokens": 4096,
        "review_checks": ["Точные действующие условия: 90000 рублей, четыре недели, редактор и аналитик.",
                          "Колонки Этап, Ответственный, Критерий приемки. Прежние значения не восстановлены.",
                          "Технический допуск отдельно: у кандидата context_audit.compacted_messages > 0. "
                          "Это стресс сжатия при тестовом бюджете 4096, а не проверка реального размера окна."],
    },
    "short": {
        "question": "Объясни разницу между наблюдением и интерпретацией на одном вымышленном примере. "
                    "Ответ из трех предложений, без внешних источников.",
    },
    "concepts": {
        "question": "Объясни студентам различие ideal ego и ego ideal во французском психоанализе. "
                    "Обозначь варианты перевода и проверь авторскую атрибуцию по доступным первоисточникам. "
                    "Если подтверждена лишь часть, точно назови границы ответа.",
    },
    "comparison": {
        "question": "Сравни Винникотта и Биона по роли другого человека в переработке переживания. "
                    "Отдели собственное сопоставление от утверждений авторов, проверь первоисточники "
                    "обеих сторон. Не изображай сравнение полным, если одна сторона не проверена.",
    },
    "closed_link": {
        "question": "Разбери содержание закрытого сообщения https://t.me/c/1009000000001/99. "
                    "Это синтетическая ссылка, ее содержимое здесь не передано.",
    },
    "lecture30_local": {
        "question": "Составь полный текст 30-минутной лекции для начинающих аналитиков "
                    "о различии наблюдения и интерпретации. Нужны готовая речь, подробный разбор "
                    "учебного случая, две альтернативные интерпретации и итоговое упражнение. "
                    "Используй только приложенный вымышленный материал, без поиска и ссылок. "
                    "Не останавливайся на плане или первой части, не обещай продолжить позже.",
        "material": SYNTHETIC_MATERIAL,
    },
    "lecture180_local": {
        "question": "Составь полный текст лекции-семинара на 3 часа для начинающих аналитиков "
                    "о наблюдении, интерпретации и проверке альтернативных объяснений. Нужны готовая "
                    "речь преподавателя, последовательный разбор учебного случая, упражнения с "
                    "материалом и разборами ответов, обсуждение ошибок и итоговое задание. "
                    "Используй только приложенный вымышленный материал, без внешнего поиска и ссылок. "
                    "Обозначь время речи, упражнений и перерыва. Не подменяй полный текст планом или "
                    "первой частью, не обещай продолжить позже.",
        "material": SYNTHETIC_MATERIAL,
    },
    "lecture180_sources": {
        "question": "Составь полный текст трехчасовой лекции для студентов второго курса по статье "
                    "Фрейда Вытеснение, с французскими прочтениями и вымышленными учебными виньетками. "
                    "Проверь авторскую атрибуцию. Отдели текст Фрейда от последующих интерпретаций. "
                    "Нужны все разделы, а не план и обещание следующих частей.",
    },
}


def source_host(value):
    host = value.strip().lower().rstrip(".")
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            or "." not in host or ".." in host
            or host.endswith((".local", ".localhost", ".internal", ".invalid"))
            or host in ("localhost", "telegram.org", "t.me")
            or host.endswith((".telegram.org", ".t.me"))):
        raise ValueError("source host must be an explicit public non-Telegram hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("literal source IP addresses are not allowed")


class QualityNetworkGuard(group_example.NetworkGuard):
    """No Telegram or loopback. Additional hosts require the source-reader path."""
    def __init__(self, hosts, *, all_public_sources=False):
        self.source_hosts = frozenset(source_host(host) for host in hosts)
        self.all_public_sources = all_public_sources
        self.dynamic_source_hosts = set()
        super().__init__(-1, allow_model=True)
        self.HOSTS = set(super().HOSTS) | self.source_hosts
        original_dns = self.real_getaddrinfo

        def public_dns(host, *args, **kwargs):
            records = original_dns(host, *args, **kwargs)
            name = host.decode() if isinstance(host, bytes) else str(host)
            if name in self.HOSTS and any(not ipaddress.ip_address(row[4][0]).is_global
                                         or ipaddress.ip_address(row[4][0]).is_multicast
                                         for row in records):
                self.denied.append("non_public_peer")
                raise group_example.IsolationViolation("external hostname resolved to a non-public peer")
            return records

        self.real_getaddrinfo = public_dns

    def authorize_source(self, value):
        """Called only by the patched production PublicConnection constructor."""
        host = source_host(value)
        if host not in self.source_hosts:
            if not self.all_public_sources:
                raise ValueError("source hostname outside evaluation allowlist")
            self.dynamic_source_hosts.add(host)
            self.HOSTS.add(host)
        return host

    def allowed_url(self, value):
        parsed = urlsplit(str(value))
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.hostname not in self.HOSTS):
            self.denied.append("http")
            raise group_example.IsolationViolation("HTTP outside declared evaluation hosts")
        return super().allowed_url(value)


def restrict_source_reader(module, hosts, stack, *, allow_pdf=False, authorize_source=None):
    """Keep production retrieval/SSRF/parser checks and narrow allowed origins."""
    allowed = frozenset(hosts)
    excluded = {"host": 0, "pdf": 0}
    original_connection = module.PublicConnection
    original_retrieve = module.retrieve

    class DeclaredConnection(original_connection):
        def __init__(self, host, *args, **kwargs):
            try:
                if authorize_source is not None:
                    host = authorize_source(host)
                elif host.lower().rstrip(".") not in allowed:
                    raise ValueError("source hostname outside evaluation allowlist")
            except ValueError:
                excluded["host"] += 1
                # Expected source coverage limit, not an actual network request.
                raise
            super().__init__(host, *args, **kwargs)

    def html_retrieve(url):
        kind, charset, data = original_retrieve(url)
        if not allow_pdf and (kind == "application/pdf" or data.startswith(b"%PDF-")):
            excluded["pdf"] += 1
            raise ValueError("PDF extraction excluded from this evaluation")
        return kind, charset, data

    stack.enter_context(patch.object(module, "PublicConnection", DeclaredConnection))
    stack.enter_context(patch.object(module, "retrieve", html_retrieve))
    return excluded


def initialize_transport_without_startup(dependencies):
    """Import the real transport helper, suppress only entry-point maintenance.

    Dependencies were selected read-only by the caller. Importing the real
    Happy Eyeballs implementation must not repair a source tree, execute .pth
    files or acquire a dependency lease in this composition-only evaluation.
    No model, OAuth or network function is replaced.
    """
    if "hermes_bootstrap" in sys.modules or "agent.process_bootstrap" in sys.modules:
        raise group_example.IsolationViolation("bootstrap loaded before isolated initialization")
    recovery = importlib.import_module("hermes_cli._early_recovery")
    launch = importlib.import_module("hermes_cli.venv_sync")
    environments = importlib.import_module("pm.environments")
    with ExitStack() as stack:
        suppressed = {}
        for module, name, result in ((recovery, "restore_interrupted_pull", False),
                                     (launch, "prepare_launch", None),
                                     (recovery, "recover_if_needed", False),
                                     (environments, "activate_dependencies", None)):
            suppressed[name] = stack.enter_context(patch.object(module, name, return_value=result))
        stack.enter_context(patch.dict(os.environ, {"HERMES_DISABLE_LAZY_INSTALLS": "1"}))
        transport = importlib.import_module("agent.process_bootstrap")
        if not callable(getattr(transport, "enable_happy_eyeballs_on_client", None)):
            raise group_example.IsolationViolation("real transport helper unavailable")
        if str(dependencies) not in sys.path:
            raise group_example.IsolationViolation("selected dependency path was displaced")
        counts = {name: function.call_count for name, function in suppressed.items()}
    return {"mode": "real transport with entry-point maintenance suppressed", "suppressed_calls": counts}


def private_output(path, excluded):
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("output must be an absolute private directory")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved.stat().st_mode & 0o077:
        raise ValueError("output directory must exist with mode 0700")
    if resolved.stat().st_uid != os.geteuid():
        raise ValueError("output directory must belong to the evaluation user")
    if any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) for root in excluded):
        raise ValueError("evaluation output must be outside production and source directories")
    return resolved


def write_private(path, value):
    """Do not overwrite or follow a pre-existing output file."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(value)


def module_hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.glob("psy_*.py")) if not path.is_symlink()}


def recovery_manifest(case, variant, module_dir, *, model=None, historical_attempts=None, resumed_from=None,
                      source_policy=None):
    """Public helper for packaging an operator's atomic synthetic SQLite backup."""
    encoded = json.dumps(case_turns(CASES[case]), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return {"format": 1, "case": case, "variant": variant,
            "input_sha256": hashlib.sha256(encoded).hexdigest(),
            "source_sha256": module_hashes(Path(module_dir)), "model_requested": model,
            "historical_attempts": historical_attempts or [], "resumed_from": resumed_from,
            "source_policy": source_policy,
            "context_policy": {"synthetic_history_messages": len(CASES[case].get("history", [])),
                               "test_input_budget_tokens": CASES[case].get("context_input_budget_tokens")}}


def read_private_file(path, *, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("recovery input must be a bounded private owned regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("recovery input exceeded its size limit")
    return data


def write_private_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def fixture_payload():
    return {"message": {"message_id": group_example.INCOMING,
                        "is_topic_message": True, "message_thread_id": 1}}


def validate_recovery_database(path):
    """Inspect only the copied fixture database, never open external SQLite."""
    allowed = {"instance_binding", "model_retries", "auth_recoveries", "model_attempts", "requests",
               "schedule", "source_events", "turn_order", "job_progress", "checkpoints", "write_receipts",
               "delivery_parts", "completed_updates", "documents", "turns", "turn_results", "turn_sources",
               "message_sources", "current_sources", "request_audit", "request_audit_events",
               "conversation_context"}
    nonempty = {"instance_binding", "model_retries", "model_attempts", "requests", "schedule",
                "job_progress", "checkpoints", "request_audit", "request_audit_events"}
    with sqlite3.connect(str(path)) as connection:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("recovery database integrity failed")
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        tables = {name for kind, name, _ in schema if kind == "table"}
        if (not {"requests", "schedule", "checkpoints"}.issubset(tables) or tables - allowed
                or any(kind not in ("table", "index") or "VIRTUAL TABLE" in (sql or "").upper()
                       for kind, _, sql in schema)):
            raise ValueError("recovery database schema is outside the synthetic fixture")
        for table in tables:
            if table not in nonempty and connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                raise ValueError("recovery contains non-composition data")
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for column, expected in (("uid", group_example.MEMBER), ("request_id", group_example.UPDATE)):
                if column in columns and connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL OR "{column}"!=? LIMIT 1',
                        (expected,)).fetchone():
                    raise ValueError("recovery contains another participant or request")
        requests = connection.execute("SELECT id,uid,body,state FROM requests").fetchall()
        if (len(requests) != 1 or requests[0][:2] != (group_example.UPDATE, group_example.MEMBER)
                or json.loads(requests[0][2]) != fixture_payload() or requests[0][3] not in ("queued", "running")):
            raise ValueError("recovery request is not the synthetic composition job")
        if connection.execute("SELECT request_id,topic,message_id,reply_id FROM schedule").fetchall() != [
                (group_example.UPDATE, 1, group_example.INCOMING, None)]:
            raise ValueError("recovery schedule is not synthetic")
        if "instance_binding" in tables and connection.execute("SELECT * FROM instance_binding").fetchall() not in (
                [], [(1, group_example.BOT, group_example.GROUP)]):
            raise ValueError("recovery is bound to another instance")
        if "request_audit" in tables:
            for key, uid, topic, message_id, provenance, turn_id in connection.execute(
                    "SELECT request_key,uid,topic,message_id,provenance,turn_id FROM request_audit"):
                if ((key, uid, topic, message_id, turn_id) !=
                        ("update:" + str(group_example.UPDATE), group_example.MEMBER, 1, group_example.INCOMING, None)):
                    raise ValueError("recovery audit is not the synthetic composition job")
                json.loads(provenance)
        if "request_audit_events" in tables:
            for key, details in connection.execute("SELECT request_key,details FROM request_audit_events"):
                if key != "update:" + str(group_example.UPDATE):
                    raise ValueError("recovery audit event belongs to another request")
                json.loads(details)
        count = 0
        for key, value in connection.execute("SELECT key,value FROM checkpoints"):
            if not (key.startswith("model:") or key.startswith("prose:")):
                raise ValueError("recovery contains non-model checkpoints")
            json.loads(value)
            count += 1
    return count


def restore_recovery(args, fixture, output):
    recovery_root = private_output(output / "recovery", ())
    path = private_output(args.resume_state, ())
    if path.parent != recovery_root:
        raise ValueError("resume-state must be a direct private child of output-root/recovery")
    if any((path / name).exists() for name in ("group.sqlite3-wal", "group.sqlite3-shm")):
        raise ValueError("recovery must be an atomic SQLite backup without WAL sidecars")
    manifest_bytes = read_private_file(path / "manifest.json", limit=1024 * 1024)
    manifest = json.loads(manifest_bytes)
    expected = recovery_manifest(args.case, args.variant, args.module_dir)
    if any(manifest.get(key) != expected[key] for key in ("format", "case", "variant", "input_sha256", "source_sha256")):
        raise ValueError("recovery provenance does not match the selected case and source version")
    if (("context_policy" in manifest or CASES[args.case].get("history"))
            and manifest.get("context_policy") != expected["context_policy"]):
        raise ValueError("recovery context policy does not match the selected case")
    database = read_private_file(path / "group.sqlite3", limit=64 * 1024 * 1024)
    if not database.startswith(b"SQLite format 3\0"):
        raise ValueError("recovery is not SQLite")
    copied = fixture / "group.sqlite3"
    write_private_bytes(copied, database)
    count = validate_recovery_database(copied)
    return {"snapshot": str(path), "database_sha256": hashlib.sha256(database).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "checkpoint_count": count,
            "manifest": manifest}


def export_recovery(store, fixture, output, label, args, model, history, provenance):
    """SQLite backup stays inside fixture, then only bytes leave the audit scope."""
    destination = output / "recovery" / (label + "-attempt-" + str(len(history)))
    destination.mkdir(mode=0o700)
    temporary = fixture / ("snapshot-" + secrets.token_hex(6) + ".sqlite3")
    with store.database() as source, sqlite3.connect(str(temporary)) as target:
        source.backup(target)
    temporary.chmod(0o600)
    validate_recovery_database(temporary)
    write_private_bytes(destination / "group.sqlite3", temporary.read_bytes())
    manifest = recovery_manifest(args.case, args.variant, args.module_dir, model=model,
                                 historical_attempts=history, resumed_from=provenance,
                                 source_policy=getattr(args, "source_policy", None))
    write_private(destination / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.unlink()
    return str(destination)


def claim_after_wait(store, fixture, *, sleep=None):
    sleep = time.sleep if sleep is None else sleep
    while True:
        delay = store.next_retry_delay(root=fixture)
        if delay is not None and delay > 0:
            sleep(min(delay, 30))
            continue
        claimed = store.claim_next(root=fixture)
        if claimed is None or claimed[0] != group_example.UPDATE:
            raise group_example.IsolationViolation("synthetic retry could not be claimed")
        return


def composition_options(codex, case):
    """Exercise context only when supported, without patching legacy model behavior."""
    parameters = inspect.signature(codex.respond).parameters
    options = {}
    if "context_scope" in parameters:
        options["context_scope"] = {"uid": group_example.MEMBER, "topic": 1, "source_id": None}
        if "context_input_budget_tokens" in case:
            if "input_budget_tokens" not in parameters:
                raise ValueError("candidate does not expose the isolated context budget")
            options["input_budget_tokens"] = case["context_input_budget_tokens"]
    return options


def compose_with_retries(codex, store, fixture, checkpoints, case, model, attempts, history, snapshot):
    options = composition_options(codex, case)
    for attempt in range(1, attempts + 1):
        try:
            return codex.respond(case_turns(case), research=False, draw=False,
                                 model=model, question=case["question"], checkpoints=checkpoints, **options)
        except codex.ModelIssue as error:
            record = {"attempt": attempt, "kind": error.kind, "stage": error.stage,
                      "retryable": bool(error.retryable), "failed_at": time.time()}
            if error.retryable:
                store.wait_for_model(group_example.UPDATE, error.kind, error.reset, root=fixture)
                record["retry_delay_seconds"] = store.next_retry_delay(root=fixture)
            history.append(record)
            record["snapshot"] = snapshot()
            print(json.dumps({"status": "model_retry" if error.retryable and attempt < attempts else "model_stopped",
                              "attempt": attempt, "kind": error.kind, "stage": error.stage}), flush=True)
            if not error.retryable or attempt == attempts:
                raise
            claim_after_wait(store, fixture)


def selected_modules(directory):
    required = ("psy_group_codex.py", "psy_group_client.py", "psy_group_store.py", "psy_source_evidence.py")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("module directory must be a regular directory")
    for name in required:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("module directory is missing a required regular module")
    package = ModuleType("quality_modules")
    package.__path__ = [str(directory)]
    sys.modules[package.__name__] = package
    return package.__name__


def case_turns(case):
    history = []
    for message in case.get("history", []):
        if message.get("role") not in ("user", "assistant") or not isinstance(message.get("content"), str):
            raise ValueError("case history must contain fixed user/assistant text")
        history.append({"role": message["role"], "content": message["content"]})
    content = [{"type": "input_text", "text": case["question"]}]
    if case.get("material"):
        content.append({"type": "input_text", "text": "Материал: Synthetic teaching case.txt\n" + case["material"]})
    return [*history, {"role": "user", "content": content}]


def blind_review(case, text):
    """Full common input and answer, without implementation/version/expected result."""
    history = ""
    if case.get("history"):
        history = "Предшествующий синтетический диалог\n" + "\n\n".join(
            ("Участник: " if message["role"] == "user" else "Ассистент: ") + message["content"]
            for message in case["history"]) + "\n\n"
    return (history + "Задание\n" + case["question"] + "\n\n" +
            ("Учебный материал: Synthetic teaching case.txt\n" + case["material"] + "\n\n" if case.get("material") else "") +
            "Ответ\n" + text + "\n")


def safe_quality_metadata(result):
    """The private compacted conversation is neither a grade nor technical metadata."""
    return {key: value for key, value in getattr(result, "quality_metadata", {}).items()
            if key != "context_state"}


def fixture_job(store, fixture, *, resumed=False):
    if resumed:
        if store.state(group_example.UPDATE, root=fixture) == "queued":
            claim_after_wait(store, fixture)
        return store.Checkpoints(group_example.UPDATE, root=fixture)
    store.pending(group_example.MEMBER, group_example.UPDATE, json.dumps(fixture_payload()), root=fixture)
    claimed = store.claim_next(root=fixture)
    if claimed is None or claimed[0] != group_example.UPDATE:
        raise group_example.IsolationViolation("synthetic job could not be claimed")
    return store.Checkpoints(group_example.UPDATE, root=fixture)


def generation_complete(result):
    """Only API audit evidence can identify whether generation completed."""
    metadata = getattr(result, "quality_metadata", {})
    for name in ("generation_audit", "generation"):
        evidence = metadata.get(name, {})
        if "generation_complete" in evidence:
            value = evidence["generation_complete"]
            return value if isinstance(value, bool) else None
    return None


def evaluate(args, fixture, guard):
    package = selected_modules(args.module_dir)
    evidence = importlib.import_module(package + ".psy_source_evidence")
    store = importlib.import_module(package + ".psy_group_store")
    codex = importlib.import_module(package + ".psy_group_codex")
    tokenizer = None
    if (args.module_dir / "psy_group_context.py").is_file():
        context = importlib.import_module(package + ".psy_group_context")
        tokenizer = context.prepare_tokenizer()
    import hermes_cli.config as config
    model = config.load_config_readonly()["model"]
    if model.get("provider") != "openai-codex":
        raise group_example.IsolationViolation("configured provider is not openai-codex")
    case = CASES[args.case]
    if args.resumed_from and args.resumed_from["manifest"].get("model_requested") not in (None, model["default"]):
        raise ValueError("recovery model does not match the selected configured model")
    checkpoints = fixture_job(store, fixture, resumed=bool(args.resumed_from))
    with ExitStack() as stack:
        # Defaults bound at import time cannot accidentally touch the actual vault.
        original_database = store.database
        stack.enter_context(patch.object(store, "ROOT", fixture))
        stack.enter_context(patch.object(store, "database", lambda *_args, **_kw: original_database(fixture)))
        exclusions = restrict_source_reader(evidence, guard.source_hosts, stack, allow_pdf=args.allow_pdf,
            authorize_source=guard.authorize_source if args.all_public_sources else None)
        args.source_exclusions = exclusions
        snapshot = lambda: export_recovery(store, fixture, args.output_root, args.label, args,
                                           model["default"], args.retry_history, args.resumed_from)
        result = compose_with_retries(codex, store, fixture, checkpoints, case, model["default"],
                                      args.max_attempts, args.retry_history, snapshot)
        with store.database() as connection:
            database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
            if not database_path.is_relative_to(fixture):
                raise group_example.IsolationViolation("database is outside evaluation fixture")
    if not guard.codex_http:
        raise AssertionError("no actual Codex HTTP request observed")
    if guard.denied:
        raise group_example.IsolationViolation("unexpected network attempt during composition")
    hashes = module_hashes(args.module_dir)
    metadata = {"case": args.case, "variant": args.variant, "real_codex": True,
                "tokenizer": tokenizer,
                "provider": model["provider"], "model_requested": model["default"],
                "composition_only": True, "delivery_tested": False, "real_telegram_requests": 0,
                "synthetic_inputs_only": True, "temporary_database_verified": True,
                "source_reader_mode": (("all public sources through production reader" if args.all_public_sources
                                        else "declared public hosts") +
                                       (", production HTML and sandboxed PDF parser" if args.allow_pdf
                                        else ", production HTML parser, PDF excluded")),
                "all_public_sources": args.all_public_sources,
                "source_hosts": sorted(guard.source_hosts), "source_exclusions": exclusions,
                "dynamically_authorized_source_hosts": sorted(guard.dynamic_source_hosts),
                "codex_http_requests": guard.codex_http, "network_guard_denials": len(guard.denied),
                "legacy_pipeline_complete": bool(result.complete),
                "generation_complete": generation_complete(result),
                "task_status": getattr(result, "task_status", "unknown"),
                "missing_requirements": getattr(result, "missing_requirements", []),
                "quality_metadata": safe_quality_metadata(result),
                "history_messages": len(case.get("history", [])),
                "context_options": composition_options(codex, case),
                "context_test_budget_only": case.get("context_input_budget_tokens"),
                "characters": len(result.text), "word_count": len(result.text.split()),
                "source_sha256": hashes, "retry_history": args.retry_history,
                "resumed_from": args.resumed_from}
    return result.text, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--module-dir", type=Path)
    parser.add_argument("--case", choices=sorted(CASES), required=True)
    parser.add_argument("--variant", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-host", action="append", default=[])
    parser.add_argument("--allow-pdf", action="store_true", help="use the exact production sandboxed PDF decoder")
    parser.add_argument("--all-public-sources", action="store_true",
                        help="let only the production source reader authorize additional public hosts")
    parser.add_argument("--max-attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--resume-state", type=Path, help="private synthetic snapshot below output-root/recovery")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    sys.dont_write_bytecode = True
    args.checkout = args.checkout.resolve(strict=True)
    args.module_dir = (args.module_dir or args.checkout / "plugins/platforms/telegram").resolve(strict=True)
    actual_home = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")).resolve()
    repository = Path(__file__).resolve().parents[2]
    output = private_output(args.output_root, (actual_home, args.checkout, args.module_dir, repository))
    for name in ("review", "keys", "recovery", "grading"):
        destination = output / name
        destination.mkdir(mode=0o700, exist_ok=True)
        private_output(destination, (actual_home, args.checkout, args.module_dir, repository))
    label = "answer-" + secrets.token_hex(6)
    args.output_root, args.label = output, label
    args.retry_history, args.resumed_from = [], None
    args.source_policy = {"all_public_sources": args.all_public_sources, "allow_pdf": args.allow_pdf,
                          "declared_hosts": args.source_host}
    args.source_exclusions = None
    guard = None
    for key in ("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "NOUS_API_KEY"):
        os.environ.pop(key, None)
    os.environ.update(PSY_OWNER_ID=str(group_example.OWNER), PSY_GROUP_ID=str(group_example.GROUP),
                      PSY_BOT_ID=str(group_example.BOT), PSY_BOT_USERNAME="PsyFixtureBot",
                      PSY_PUBLIC_URL="https://example.invalid")
    sys.path.insert(0, str(args.checkout))
    started = time.time()
    print(json.dumps({"status": "running", "label": label, "case": args.case}, ensure_ascii=False), flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix="psy-quality-") as temporary:
            fixture = Path(temporary).resolve()
            audit = group_example.install_storage_audit(fixture, actual_home, args.checkout,
                pdf_reader=args.module_dir / "psy_source_evidence.py" if args.allow_pdf else None)
            if args.resume_state:
                args.resumed_from = restore_recovery(args, fixture, output)
            from pm.environments import committed_venv, site_packages
            environment = committed_venv(args.checkout)
            if environment is None:
                raise group_example.IsolationViolation("no committed dependency environment")
            dependencies = site_packages(environment)
            if not dependencies.is_dir():
                raise group_example.IsolationViolation("committed dependencies missing")
            sys.path.insert(1, str(dependencies))
            # All native decoder output and scratch files remain in this process's
            # private fixture, even if the launcher exported another TMPDIR.
            tempfile.tempdir = str(fixture)
            os.environ.update(TMPDIR=str(fixture), TMP=str(fixture), TEMP=str(fixture))
            with QualityNetworkGuard(args.source_host, all_public_sources=args.all_public_sources) as guard:
                bootstrap = initialize_transport_without_startup(dependencies)
                text, metadata = evaluate(args, fixture, guard)
            metadata["bootstrap"] = bootstrap
            metadata["storage_audit"] = audit
            metadata["dependency_environment"] = str(environment.resolve())
        case = CASES[args.case]
        review = blind_review(case, text)
        write_private(output / "review" / (label + ".txt"), review)
        grading = {"label": label, "checks": case.get("review_checks", []),
                   "dimensions": ["выполнение задания", "точность", "глубина", "связность", "язык", "границы интерпретации"],
                   "blockers": ["выдуманная цитата или атрибуция", "частичный результат выдан за полный"],
                   "status": "not_reviewed"}
        write_private(output / "grading" / (label + ".json"), json.dumps(grading, ensure_ascii=False, indent=2) + "\n")
        metadata.update(label=label, status="generated", started_at=started,
                        elapsed_seconds=round(time.time() - started, 2))
        write_private(output / "keys" / (label + ".json"), json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "generated", "label": label, "case": args.case,
                          "characters": len(text), "real_codex": True, "delivery_tested": False,
                          "elapsed_seconds": metadata["elapsed_seconds"]}, ensure_ascii=False))
    except Exception as error:
        metadata = {"status": "failed", "label": label, "case": args.case, "variant": args.variant,
                    "error_class": type(error).__name__, "started_at": started,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "retry_history": args.retry_history, "resumed_from": args.resumed_from,
                    "source_policy": args.source_policy, "source_exclusions": args.source_exclusions,
                    "dynamically_authorized_source_hosts": sorted(guard.dynamic_source_hosts) if guard else []}
        frames = []
        cursor = error.__traceback__
        while cursor:
            frames.append({"file": Path(cursor.tb_frame.f_code.co_filename).name,
                           "function": cursor.tb_frame.f_code.co_name, "line": cursor.tb_lineno})
            cursor = cursor.tb_next
        metadata["error_frames"] = frames[-5:]
        for name in ("kind", "stage"):
            value = getattr(error, name, None)
            if isinstance(value, str) and re.fullmatch(r"[a-z_]+", value):
                metadata[name] = value
        write_private(output / "keys" / (label + ".json"), json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        public_keys = ("status", "label", "case", "error_class", "elapsed_seconds", "error_frames", "kind", "stage")
        print(json.dumps({key: metadata[key] for key in public_keys if key in metadata}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
