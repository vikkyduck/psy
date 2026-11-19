"""Group job pipeline: dispatch, mapping of an oversized material, primary sources, a lecture or prose
answer and its verification against the sources."""
from functools import partial

if __package__:
    from .psy_group_client import ModelIssue, Result, _dispatch_marker, _job_calls, _json_value, _parallel, _user
    from .psy_group_continuation import _complete
    from .psy_group_lecture import _lecture
    from .psy_group_prompts import ATTRIBUTION, CUT_NOTE, DISPATCH, SYSTEM
    from .psy_group_sources import _primary_sources, _recover_verified, _verify_draft
    from .psy_source_evidence import public_topic
else:
    from psy_group_client import ModelIssue, Result, _dispatch_marker, _job_calls, _json_value, _parallel, _user
    from psy_group_continuation import _complete
    from psy_group_lecture import _lecture
    from psy_group_prompts import ATTRIBUTION, CUT_NOTE, DISPATCH, SYSTEM
    from psy_group_sources import _primary_sources, _recover_verified, _verify_draft
    from psy_source_evidence import public_topic


def _admit_long(checkpoints):
    # Admission is attached only to durable group jobs. Direct CLI uses and isolated
    # composition remain synchronous and have no shared group queue to reserve.
    admit = getattr(checkpoints, "admit_long", None)
    if admit is not None:
        admit()


def _compose(client, turns, research, model, question, *, checkpoints=None):
    call, continuation_call = _job_calls(client, model, checkpoints)
    write_prose = partial(_complete, partial(call, prose=True), model=model,
                          continuation_call=continuation_call if checkpoints is not None else None)
    dispatch = SYSTEM + DISPATCH
    if research:
        _admit_long(checkpoints)
        dispatch += ("\nУчастник запустил /research. Выбери служебный PSY_TASK с обезличенной "
                     "публичной темой. Если искать приватно нельзя, верни пустую public_topic.")
    try:
        initial = call(turns, dispatch, prose=not research)
    except ModelIssue as exc:
        if exc.kind != "context":
            raise
        _admit_long(checkpoints)
        turns = _map_source(call, turns, question)
        initial = call(turns, dispatch, prose=not research)

    task = None
    for attempt in range(2):
        if not _dispatch_marker(initial.text, complete=initial.complete):
            break
        if initial.complete:
            task = _json_value(initial.text.strip()[len("PSY_TASK"):])
            if isinstance(task, dict):
                break
        if attempt:
            raise ModelIssue("unavailable", "task_dispatch")
        # Reclassify the original input instead of continuing a broken internal
        # instruction as public prose. The distinct call also bypasses old drafts.
        initial = call(turns, dispatch + "\nВерни полный ответ или полную служебную инструкцию с корректным JSON.",
                       prose=not research)
    if not isinstance(task, dict):
        if research:
            raise ModelIssue("unavailable", "research_dispatch")
        if not initial.complete:
            _admit_long(checkpoints)
        result = write_prose(turns, SYSTEM, initial=initial)
        if not result.complete and "[Продолжение не завершено." not in result.text:
            result.text += CUT_NOTE
        return result

    # Dispatch is already checkpointed. A parked job can resume at this point.
    _admit_long(checkpoints)
    minutes = task.get("minutes")
    long_form = type(minutes) is int and minutes >= 30
    wants_sources = bool(task.get("public_topic")) or research
    topic = public_topic(question) if wants_sources else ""
    sources = None
    if topic.strip():
        sources = _primary_sources(call, topic.strip(), read_main=long_form or task.get("read_main") is True)
        if not sources.complete:
            return sources
    elif wants_sources:
        return Result("Для проверки источников начните сообщение со строки 'Публичная тема: название работы или предмет поиска'. "
                      "Только текст этой строки будет использован для внешнего поиска.", [], 0)

    if sources:
        # Source evidence is untrusted data, never privileged instructions.
        turns = [*turns, _user("Выписки из первоисточников и границы проверки:\n" + sources.text)]
    if long_form:
        result = _lecture(call, turns, minutes, write_prose=write_prose)
    else:
        result = write_prose(turns, SYSTEM + "\nВыполни исходный запрос целиком по доступным материалам."
                           + ("\n" + ATTRIBUTION if sources else ""))
        if not result.complete and "[Продолжение не завершено." not in result.text:
            result.text += CUT_NOTE
    if sources:
        try:
            result = _verify_draft(call, result, sources.text)
        except ModelIssue as exc:
            result = _recover_verified(call, result, sources, exc)
        result.searches = sources.searches
    elif wants_sources:
        result.text += "\n\nПоиск первоисточников не выполнялся. Для внешней сверки начните сообщение со строки Публичная тема: название работы или предмет поиска."
    return result


def _map_source(call, turns, question):
    current = turns[-1].get("content")
    if not isinstance(current, list):
        raise ModelIssue("context", "source_missing")
    materials = [part for part in current if part.get("type") == "input_text" and part.get("text", "").startswith("Материал: ")]
    if len(materials) != 1:
        raise ModelIssue("context", "source_missing")
    material = materials[0]
    name, sep, body = material["text"].partition("\n")
    if not sep or not body:
        raise ModelIssue("context", "source_empty")
    pieces = []
    start = 0
    while start < len(body):
        end = min(start + 48000, len(body))
        if end < len(body):
            boundary = max(body.rfind("\n", start + 24000, end), body.rfind(" ", start + 24000, end))
            if boundary != -1:
                end = boundary + 1
        pieces.append(body[start:end])
        start = end
    if len(pieces) == 1:
        raise ModelIssue("context", "source_overflow")
    request = question or current[0].get("text") or "Разбери материал"
    # Include the current instruction and short dialogue so a clarification such
    # as "чем это подтверждается?" is intelligible on every pass.
    context = [*turns[:-1], {"role": "user", "content": [part for part in current if part is not material]}]
    def extract(index):
        prompt = (f"Вопрос: {request}\nФрагмент {index+1} из {len(pieces)}\n"
                  f"{name}\n{pieces[index]}")
        result = call([*context, _user(prompt)], SYSTEM + """
Извлеки факты, реплики, таймкоды, исключения и альтернативные свидетельства, которые нужны для ответа
на исходный вопрос, включая начало и конец фрагмента. Выдели связи, для которых нужен соседний фрагмент.
Если релевантных сведений нет, сообщи об этом. Это промежуточные заметки для последующего сопоставления.""")
        if not result.complete:
            raise ModelIssue("timeout", "source_pass")
        return f"Фрагмент {index+1} из {len(pieces)}\n" + result.text
    notes = [note for note, _ in _parallel(call, extract, range(len(pieces)))]
    synthesis = ("Весь материал просмотрен по последовательным фрагментам. Ниже наблюдения из каждого, "
                 "сохрани локаторы, не выдавай их за полный дословный текст и отмечай ограничения вывода.\n"
                 + "\n\n".join(notes))
    return [*context, _user(synthesis)]
