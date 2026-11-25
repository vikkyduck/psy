#!/usr/bin/env python3
"""Real Codex composition on fixed synthetic cases, with blinded local output.

Run each variant in a separate process using the installed Hermes interpreter as
hermes. --checkout selects the runtime and its existing shared OAuth controller.
--module-dir optionally selects candidate group modules from a separate checkout.
No Telegram transport is created. Queue/delivery acceptance is a separate test.

The source reader retains its public-IP checks and production parser. Only
explicit --source-host domains can be fetched locally. --allow-pdf permits only
the exact production PDF decoder command with bounded input, private output and
the decoder's existing seccomp/Landlock sandbox. No general subprocess is allowed.

Use an external process deadline for a one-shot evaluation. It must not change
production model settings. All generated files stay outside the source checkout.
"""
import argparse
from contextlib import ExitStack
import hashlib
import importlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
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

CASES = {
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
    """No Telegram or loopback receiver. Explicit public source domains only."""
    def __init__(self, hosts):
        self.source_hosts = frozenset(source_host(host) for host in hosts)
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

    def allowed_url(self, value):
        parsed = urlsplit(str(value))
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.hostname not in self.HOSTS):
            self.denied.append("http")
            raise group_example.IsolationViolation("HTTP outside declared evaluation hosts")
        return super().allowed_url(value)


def restrict_source_reader(module, hosts, stack, *, allow_pdf=False):
    """Keep production retrieval/SSRF/parser checks and narrow allowed origins."""
    allowed = frozenset(hosts)
    excluded = {"host": 0, "pdf": 0}
    original_connection = module.PublicConnection
    original_retrieve = module.retrieve

    class DeclaredConnection(original_connection):
        def __init__(self, host, *args, **kwargs):
            if host.lower().rstrip(".") not in allowed:
                excluded["host"] += 1
                # Expected source coverage limit, not an actual network request.
                raise ValueError("source hostname outside evaluation allowlist")
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
    content = [{"type": "input_text", "text": case["question"]}]
    if case.get("material"):
        content.append({"type": "input_text", "text": "Материал: Synthetic teaching case.txt\n" + case["material"]})
    return [{"role": "user", "content": content}]


def fixture_job(store, fixture):
    store.pending(group_example.MEMBER, group_example.UPDATE, json.dumps({"message": {
        "message_id": group_example.INCOMING, "is_topic_message": True,
        "message_thread_id": 1}}), root=fixture)
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
    import hermes_cli.config as config
    model = config.load_config_readonly()["model"]
    if model.get("provider") != "openai-codex":
        raise group_example.IsolationViolation("configured provider is not openai-codex")
    case = CASES[args.case]
    checkpoints = fixture_job(store, fixture)
    with ExitStack() as stack:
        # Defaults bound at import time cannot accidentally touch the actual vault.
        original_database = store.database
        stack.enter_context(patch.object(store, "ROOT", fixture))
        stack.enter_context(patch.object(store, "database", lambda *_args, **_kw: original_database(fixture)))
        exclusions = restrict_source_reader(evidence, guard.source_hosts, stack, allow_pdf=args.allow_pdf)
        result = codex.respond(case_turns(case), research=False, draw=False,
                               model=model["default"], question=case["question"], checkpoints=checkpoints)
        with store.database() as connection:
            database_path = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
            if not database_path.is_relative_to(fixture):
                raise group_example.IsolationViolation("database is outside evaluation fixture")
    if not guard.codex_http:
        raise AssertionError("no actual Codex HTTP request observed")
    if guard.denied:
        raise group_example.IsolationViolation("unexpected network attempt during composition")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(args.module_dir.glob("psy_*.py")) if not path.is_symlink()}
    metadata = {"case": args.case, "variant": args.variant, "real_codex": True,
                "provider": model["provider"], "model_requested": model["default"],
                "composition_only": True, "delivery_tested": False, "real_telegram_requests": 0,
                "synthetic_inputs_only": True, "temporary_database_verified": True,
                "source_reader_mode": ("declared public hosts, production HTML and sandboxed PDF parser"
                                       if args.allow_pdf else "declared public hosts, production HTML parser, PDF excluded"),
                "source_hosts": sorted(guard.source_hosts), "source_exclusions": exclusions,
                "codex_http_requests": guard.codex_http, "network_guard_denials": len(guard.denied),
                "legacy_pipeline_complete": bool(result.complete),
                "generation_complete": generation_complete(result),
                "task_status": getattr(result, "task_status", "unknown"),
                "missing_requirements": getattr(result, "missing_requirements", []),
                "quality_metadata": getattr(result, "quality_metadata", {}),
                "characters": len(result.text), "word_count": len(result.text.split()),
                "source_sha256": hashes}
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
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    sys.dont_write_bytecode = True
    args.checkout = args.checkout.resolve(strict=True)
    args.module_dir = (args.module_dir or args.checkout / "plugins/platforms/telegram").resolve(strict=True)
    actual_home = Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")).resolve()
    repository = Path(__file__).resolve().parents[2]
    output = private_output(args.output_root, (actual_home, args.checkout, args.module_dir, repository))
    for name in ("review", "keys"):
        destination = output / name
        destination.mkdir(mode=0o700, exist_ok=True)
        private_output(destination, (actual_home, args.checkout, args.module_dir, repository))
    label = "answer-" + secrets.token_hex(6)
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
            with QualityNetworkGuard(args.source_host) as guard:
                bootstrap = initialize_transport_without_startup(dependencies)
                text, metadata = evaluate(args, fixture, guard)
            metadata["bootstrap"] = bootstrap
            metadata["storage_audit"] = audit
            metadata["dependency_environment"] = str(environment.resolve())
        case = CASES[args.case]
        review = ("Задание\n" + case["question"] + "\n\n" +
                  ("Учебный материал\n" + case["material"] + "\n\n" if case.get("material") else "") +
                  "Ответ\n" + text + "\n")
        write_private(output / "review" / (label + ".txt"), review)
        metadata.update(label=label, status="generated", started_at=started,
                        elapsed_seconds=round(time.time() - started, 2))
        write_private(output / "keys" / (label + ".json"), json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": "generated", "label": label, "case": args.case,
                          "characters": len(text), "real_codex": True, "delivery_tested": False,
                          "elapsed_seconds": metadata["elapsed_seconds"]}, ensure_ascii=False))
    except Exception as error:
        metadata = {"status": "failed", "label": label, "case": args.case, "variant": args.variant,
                    "error_class": type(error).__name__, "started_at": started,
                    "elapsed_seconds": round(time.time() - started, 2)}
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
        print(json.dumps({key: value for key, value in metadata.items() if key != "variant"}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
