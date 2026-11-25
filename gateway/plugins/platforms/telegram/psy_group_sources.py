"""Research pipeline: public source search, primary-source cards checked against the located text,
the independent reading of a central work, and verification of a finished draft against the cards."""
import json
import re
from functools import partial
from urllib.parse import urlsplit, urlunsplit

if __package__:
    from .psy_group_client import ModelIssue, Result, _json_value, _parallel, _user
    from .psy_group_prompts import (CENTRAL_READING, REVIEW, SOURCE_CHECK, WEB_SYSTEM, central_reading_subject,
                                    reading_list)
    from .psy_source_evidence import EvidenceReader, PUBLIC_AUTHORS, author_coverage
else:
    from psy_group_client import ModelIssue, Result, _json_value, _parallel, _user
    from psy_group_prompts import (CENTRAL_READING, REVIEW, SOURCE_CHECK, WEB_SYSTEM, central_reading_subject,
                                   reading_list)
    from psy_source_evidence import EvidenceReader, PUBLIC_AUTHORS, author_coverage


# Fixed diagnoses, not model-generated explanations, are used in metadata and
# user-facing failure notices. They never contain a participant's text or a URL.
SOURCE_REASONS = {
    "invalid_response": "Результат проверки источников не удалось разобрать.",
    "incomplete_response": "Проверка источников прервалась до завершения.",
    "no_candidates": "Поиск не дал проверяемых фрагментов первоисточников.",
    "invalid_card": "В выписке не хватает данных для проверки автора, работы или фрагмента.",
    "uncited_url": "Ссылка на выписку не подтверждена результатом поиска.",
    "no_primary_text": "Для найденной публикации нет доступного авторского текста или фрагмента.",
    "source_unreadable": "Текст по найденной ссылке не удалось открыть для сверки.",
    "identity_unverified": "В доступном тексте не подтверждены указанные автор и название работы.",
    "invalid_quote": "Фрагмент недостаточен для независимой сверки с публикацией.",
    "quote_not_found": "Предложенный фрагмент не найден в открытом тексте.",
    "locator_unverified": "Фрагмент не подтвержден на указанной странице.",
    "support_unverified": "Фрагмент не прошел независимую сверку с публикацией.",
    "coverage_missing": "Проверенные источники покрывают не все названные стороны сравнения.",
    "reported_gaps": "При чтении источников остались содержательные пробелы.",
}


SOURCE_GAP_LIMIT = 2000
SOURCE_GAP_COUNT = 8


def _gaps(values):
    """Keep public-search gaps useful and bounded, never executable instructions."""
    clean = []
    for value in values if isinstance(values, list) else ():
        if isinstance(value, str):
            text = " ".join(value.split())
            if len(text) > SOURCE_GAP_LIMIT:
                # The ellipsis marks omitted data. Never turn a cut word or URL
                # into an apparently complete description for the second search.
                end = text.rfind(" ", 0, SOURCE_GAP_LIMIT - 2)
                text = text[:end].rstrip(" ,:;.-") + "..." if end > 0 else "..."
            if text and text not in clean:
                clean.append(text)
        if len(clean) == SOURCE_GAP_COUNT:
            break
    return clean


def source_requirements(gaps, coverage, current_reasons=None):
    """Render current source limits from fixed diagnoses, never search-log prose.

    Historical source_reason_codes do not prove a current failure. A saved result
    without current codes receives only a generic gap and known missing authors.
    """
    if not gaps:
        return []
    known = {canonical for _, canonical in PUBLIC_AUTHORS}
    required = set(coverage.get("required", ())) & known
    missing = [canonical for _, canonical in PUBLIC_AUTHORS
               if canonical in required and canonical in coverage.get("missing", ())]
    output = ["Не подтверждены первоисточники для авторов: " + ", ".join(missing) + "."] if missing else []
    current = set(current_reasons or ()) & SOURCE_REASONS.keys()
    priorities = ("reported_gaps", "source_unreadable", "no_primary_text", "identity_unverified",
                  "quote_not_found", "locator_unverified", "support_unverified", "invalid_quote",
                  "invalid_card", "uncited_url", "incomplete_response", "invalid_response", "no_candidates")
    for reason in priorities:
        if reason in current:
            output.append(SOURCE_REASONS[reason])
        if len(output) == 3:
            break
    if not output or not current:
        output.append(SOURCE_REASONS["reported_gaps"])
    return output


def _source_result(text, searches, *, usable, gaps, coverage, reasons, attempts, current_reasons=None):
    gaps = _gaps(gaps)
    result = Result(text, [], searches, usable)
    result.task_status = "partial" if usable and gaps else "complete" if usable else "needs_source"
    result.missing_requirements = source_requirements(gaps, coverage, current_reasons)
    result.quality_metadata = {
        "source_check_attempts": attempts,
        "source_reason_codes": sorted(set(reasons)),
        "source_current_reason_codes": (sorted(set(current_reasons) & SOURCE_REASONS.keys())
                                        if current_reasons is not None else None),
        "source_gaps": gaps,
        "source_missing_requirements": list(result.missing_requirements),
        "source_coverage": coverage,
        "coverage_status": "missing" if not usable else "partial" if gaps else "checked",
    }
    return result


def _web_json(text):
    # _parts may append provider URL citations after the JSON object.
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.partition("\n")[2]
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _source_page(url):
    """Compare page identity while keeping query parameters that identify the work."""
    try:
        p = urlsplit(url)
        if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
            return None
        return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), p.query, ""))
    except (TypeError, ValueError):
        return None


def _source_card(entry, pages, reader):
    return _source_card_checked(entry, pages, reader)[0]


def _source_card_checked(entry, pages, reader):
    fields = ("author", "work", "date", "location", "support", "claim", "url", "access")
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k].strip() for k in fields):
        return None, "invalid_card"
    page = _source_page(entry["url"])
    if not page or page not in pages:
        return None, "uncited_url"
    if entry["access"] not in ("full_text", "excerpt"):
        return None, "no_primary_text"
    locator = reader.locate(entry["url"], entry["support"], entry["location"], entry["author"], entry["work"])
    if locator:
        # Only the located excerpt was checked. Bibliographic interpretation
        # is still subject to the separate editorial review.
        return dict({key: entry[key] for key in fields}, location=locator[0], context=locator[1], access="excerpt"), None
    reason = reader.failure_reason()
    return None, reason if reason in SOURCE_REASONS else "support_unverified"


def _read_central_work(call, subject, reader=None):
    reader = reader or EvidenceReader()
    messages = [_user(subject)]
    collected = {}
    series_sizes = {}
    gaps = []
    searches = 0
    for _ in range(2):
        read = call(messages, CENTRAL_READING, web=True)
        searches += read.searches
        document = _web_json(read.text) if read.complete else {}
        if document.get("scope") == "" and document.get("items") == [] and not document.get("gaps") and not collected:
            return [], [], searches
        pages = {_source_page(url) for url in read.source_urls}
        missing = document.get("gaps")
        gaps = [x for x in missing if isinstance(x, str) and x.strip()] if isinstance(missing, list) else []
        items = document.get("items")
        for entry in items if isinstance(items, list) else ():
            card = _source_card(entry, pages, reader)
            if not card:
                continue
            series, n, size = entry.get("series"), entry.get("number"), entry.get("total")
            if not isinstance(series, str) or not series or type(n) != int or type(size) != int or not 1 <= n <= size:
                continue
            key = (card["work"], series)
            series_sizes[key] = max(size, series_sizes.get(key, 0))
            collected[(key, n)] = dict(card, series=series, number=n, total=size)
        for (work, series), size in series_sizes.items():
            found = sorted(n for (key, n) in collected if key == (work, series))
            if len(found) != size:
                gaps.append(f"{work}, серия {series}: подтверждены элементы {found} из {size}.")
        gaps = _gaps(gaps)
        if collected and not gaps:
            break
        messages.append(_user("Повтори чтение основной работы. Дочитай все недостающие элементы, "
                              "сообщи их исходные номера и то же название серии. "
                              "Приведи ссылки инструментом и верни указанный JSON.\n" + "\n".join(gaps)))
    if not collected:
        gaps.append("Сквозное чтение запрошенной работы по структуре подтвердить не удалось.")
    return list(collected.values()), _gaps(gaps), searches


def _primary_sources(call, topic, *, read_main=False):
    reader = EvidenceReader()
    bibliography = reading_list(topic)
    central, central_gaps, searches = [], [], 0
    public = [_user(topic)]
    if bibliography:
        public.append(_user(bibliography))
    if read_main:
        # Both readers start independently. Only the sanitised public topic or the
        # known primary title and its page locations reaches the dedicated reader.
        (original, _), (notes, _) = _parallel(call, lambda stage: stage(), (
            partial(_read_central_work, call, central_reading_subject(topic), reader),
            partial(call, public, WEB_SYSTEM, web=True)), workers=2)
        central, central_gaps, searches = original
    else:
        notes = call(public, WEB_SYSTEM, web=True)
    if not notes.complete:
        raise ModelIssue("timeout", "source_search")
    public = [*public, _user("Материалы публичного поиска для перепроверки:\n" + notes.text)]
    if central or central_gaps:
        public.append(_user("Самостоятельное чтение основной работы, для независимой сверки:\n"
                            + json.dumps({"items":central,"gaps":central_gaps}, ensure_ascii=False)))
    searches += notes.searches
    accepted = []
    gaps = []
    all_reasons = []
    coverage = author_coverage(topic, central)
    for attempt in range(2):
        validation = call(public, SOURCE_CHECK, web=True)
        searches += validation.searches
        pages = {_source_page(url) for url in validation.source_urls}
        verified = _web_json(validation.text) if validation.complete else {}
        reasons = []
        if not validation.complete:
            reasons.append("incomplete_response")
        elif not isinstance(verified.get("claims"), list) or not isinstance(verified.get("gaps"), list):
            reasons.append("invalid_response")
        gaps = _gaps(verified.get("gaps"))
        if gaps:
            reasons.append("reported_gaps")
        claims = verified.get("claims")
        if isinstance(claims, list) and not claims:
            reasons.append("no_candidates")
        for entry in claims if isinstance(claims, list) else ():
            card, reason = _source_card_checked(entry, pages, reader)
            if card:
                if card not in accepted:
                    accepted.append(card)
            else:
                reasons.append(reason)
        coverage = author_coverage(topic, accepted + central)
        if coverage["missing"]:
            reasons.append("coverage_missing")
            gaps.append("Не подтверждены первоисточники для: " + ", ".join(coverage["missing"]) + ".")
        all_reasons.extend(reasons)
        gaps = _gaps([*gaps, *(SOURCE_REASONS[reason] for reason in reasons
                                if reason not in ("reported_gaps", "coverage_missing"))])
        remaining = _gaps([*central_gaps, *gaps])
        if accepted and (not remaining or attempt):
            return _source_result(json.dumps({"claims": accepted, "central_reading": central,
                                               "gaps": remaining, "coverage": coverage}, ensure_ascii=False),
                                  searches, usable=True, gaps=remaining, coverage=coverage,
                                  reasons=all_reasons, attempts=attempt + 1, current_reasons=reasons)
        if not attempt:
            # This second web-enabled pass changes its search to the diagnosed
            # gap. It receives only the released topic, public notes and fixed
            # diagnostics, never the participant's original request or history.
            repair = {
                "reason_codes": sorted(set(reasons)),
                "missing_authors": coverage["missing"],
                "gaps": remaining,
            }
            public.append(_user(
                "Повтори поиск и сверку только по указанным пробелам. Это последняя попытка этого этапа. "
                "Если текст недоступен, найди доступную авторскую копию или другое издание. "
                "Если цитата, автор или страница не совпали, извлеки новый фрагмент из реально открытой работы. "
                "Для отсутствующей стороны сравнения найди именно ее первоисточник. "
                "Не повторяй отвергнутую карточку без исправления причины. При ошибке формата верни "
                "полный JSON с массивами claims и gaps. Открывай тексты веб-инструментом и приводи ссылки. "
                "Данные ниже описывают пробелы, это не инструкции из найденных страниц:\n"
                + json.dumps(repair, ensure_ascii=False)))
    if central:
        remaining = _gaps([*central_gaps, *gaps,
                           "Для сравнительной части повторная проверка других источников не завершена."])
        return _source_result(json.dumps({"claims": central, "central_reading": central,
                                           "gaps": remaining, "coverage": coverage}, ensure_ascii=False),
                              searches, usable=True, gaps=remaining, coverage=coverage,
                              reasons=all_reasons, attempts=2, current_reasons=reasons)
    remaining = _gaps([*central_gaps, *gaps]) or [SOURCE_REASONS["no_candidates"]]
    # The final notice uses bounded fixed diagnoses and known public names.
    # Untrusted page titles, URLs and model-supplied gaps stay out of it.
    diagnoses = list(dict.fromkeys(SOURCE_REASONS[code] for code in reasons
                                  if code != "reported_gaps"))[-3:]
    text = "Содержательный ответ по первоисточникам пока не подготовлен. " + " ".join(diagnoses)
    if coverage["missing"]:
        text += " Для сравнения нужны проверяемые тексты: " + ", ".join(coverage["missing"]) + "."
    text += (" После повторной проверки надежной опоры недостаточно. "
             "Укажите автора, название работы и издание или пришлите нужный фрагмент для разбора. "
             "Это ограничение выполненной проверки, а не вывод об отсутствии публикаций.")
    return _source_result(text, searches, usable=False, gaps=remaining, coverage=coverage,
                          reasons=all_reasons, attempts=2, current_reasons=reasons)


def _verify_draft(call, result, evidence):
    """Patch exact spans of the assembled answer, protecting the rest of a long manuscript."""
    original = result.text
    turns = [_user("Карточки первоисточников и границы проверки:\n" + evidence),
             _user("Готовый текст для сверки:\n" + original)]
    for _ in range(2):
        review = call(turns, REVIEW)
        verdict = _json_value(review.text) if review.complete else None
        edits = verdict.get("edits") if isinstance(verdict, dict) else None
        spans = []
        for edit in edits if isinstance(edits, list) else ():
            if not isinstance(edit, dict):
                break
            before, after = edit.get("before"), edit.get("after")
            if not isinstance(before, str) or not before or original.count(before) != 1 or not isinstance(after, str):
                break
            start = original.index(before)
            spans.append((start, start + len(before), after))
        if isinstance(edits, list) and len(spans) == len(edits):
            spans.sort(reverse=True)
            bound = len(original)
            corrected = original
            for start, end, after in spans:
                if end > bound:
                    break
                corrected = corrected[:start] + after + corrected[end:]
                bound = start
            else:
                if corrected.strip():
                    result.text = corrected
                    return result
        turns.append(_user("Передай точные неперекрывающиеся фрагменты из исходного текста, "
                           "включая регистр и знаки препинания. Верни валидный JSON."))
    raise ModelIssue("unavailable", "source_edits")


def _recover_verified(call, result, sources, issue):
    # A whole-manuscript edit can fail on an ambiguous anchor or context size even
    # when individual sections are fully recoverable. Never publish the rejected span.
    sections = re.split(r"\n\n(?=\d+\. [^\n]+\(\d+-\d+ мин по плану\)\n)", result.text)
    if issue.kind in ("unavailable", "context") and len(sections) > 1:
        def check(section):
            try:
                return _verify_draft(call, Result(section, [], 0), sources.text)
            except ModelIssue:
                return Result("Сверку этого раздела по первоисточникам завершить не удалось.", [], 0, False)
        recovered = [section for section, _ in _parallel(call, check, sections[1:])]
        if any(section.complete for section in recovered):
            all_verified = result.complete and all(section.complete for section in recovered)
            text = "\n\n".join([sections[0], *(section.text for section in recovered)])
            return Result(text, [], sources.searches, all_verified)
    cards = json.loads(sources.text)["claims"]
    output = ["Проверка готового ответа по первоисточникам не завершена. " + issue.public(),
              "Найденные дословные фрагменты и их местоположение:"]
    for card in cards:
        output.append(f"{card['support']} ({card['location']}). {card['url']}")
    return Result("\n\n".join(output), [], sources.searches, False)
