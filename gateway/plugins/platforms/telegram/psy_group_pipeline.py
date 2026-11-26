"""Group job pipeline: dispatch, mapping of an oversized material, primary sources, a lecture or prose
answer and its verification against the sources."""
from functools import partial

if __package__:
    from .psy_group_client import ModelIssue, Result, _dispatch_marker, _job_calls, _json_value, _parallel, _user
    from .psy_group_continuation import _complete
    from .psy_group_lecture import _lecture
    from .psy_group_prompts import ATTRIBUTION, CUT_NOTE, DISPATCH, SYSTEM
    from .psy_group_quality import annotate, contract_message, delivery_instructions, extract_contract, finish_task, mark_partial_text, recheck_edited_task
    from .psy_group_sources import _primary_sources, _recover_verified, _verify_draft
    from .psy_source_evidence import public_topic
else:
    from psy_group_client import ModelIssue, Result, _dispatch_marker, _job_calls, _json_value, _parallel, _user
    from psy_group_continuation import _complete
    from psy_group_lecture import _lecture
    from psy_group_prompts import ATTRIBUTION, CUT_NOTE, DISPATCH, SYSTEM
    from psy_group_quality import annotate, contract_message, delivery_instructions, extract_contract, finish_task, mark_partial_text, recheck_edited_task
    from psy_group_sources import _primary_sources, _recover_verified, _verify_draft
    from psy_source_evidence import public_topic


def _admit_long(checkpoints, call=None):
    # Admission is attached only to durable group jobs. Direct CLI uses and isolated
    # composition remain synchronous and have no shared group queue to reserve.
    # A long job also thinks deep from here on (psy_group_client.DEEP_EFFORT).
    if call is not None:
        call.deep = True
    admit = getattr(checkpoints, "admit_long", None)
    if admit is not None:
        admit()


def _compose(client, turns, research, model, question, *, checkpoints=None,
             context_scope=None, context_state=None, input_budget_tokens=None):
    call, continuation_call = _job_calls(client, model, checkpoints)
    prepared = None
    try:
        if context_scope is not None:
            if __package__:
                from .psy_group_context import prepare_context
            else:
                from psy_group_context import prepare_context
            prepared = prepare_context(turns[:-1], turns[-1], call, scope=context_scope,
                                       previous=context_state, input_budget_tokens=input_budget_tokens,
                                       model=model)
            turns = prepared.turns
        result = _compose_task(call, continuation_call, turns, research, model, question, checkpoints)
    except ModelIssue as exc:
        if prepared is not None:
            exc.context_state = prepared.state
            exc.quality_metadata.update(context_audit=prepared.metadata)
        audit = getattr(call, "audit", None)
        if callable(audit):
            exc.quality_metadata.update(audit())
        raise
    if prepared is not None:
        result.quality_metadata.update(context_state=prepared.state, context_audit=prepared.metadata)
    audit = getattr(call, "audit", None)
    if callable(audit):
        result.quality_metadata.update(audit())
    return mark_partial_text(result)


def _compose_task(call, continuation_call, turns, research, model, question, checkpoints):
    contract = extract_contract(call, turns, question)
    def write_prose(messages, instructions, **options):
        transport = delivery_instructions(contract)
        return _complete(partial(call, prose=True), messages,
                         instructions if instructions.endswith(transport) else instructions + transport, model=model,
                         continuation_call=continuation_call if checkpoints is not None else None,
                         **options)
    if contract.get("contract_failure") == "context":
        _admit_long(checkpoints, call)
        turns = _map_source(call, turns, question)
        contract = extract_contract(call, turns, question)
    dispatch = SYSTEM + delivery_instructions(contract) + DISPATCH
    if research:
        _admit_long(checkpoints, call)
        dispatch += ("\nУчастник запустил /research. Выбери служебный PSY_TASK с обезличенной "
                     "публичной темой. Если искать приватно нельзя, верни пустую public_topic.")
    try:
        initial = call([*turns, contract_message(contract)], dispatch, prose=not research)
    except ModelIssue as exc:
        if exc.kind != "context":
            raise
        _admit_long(checkpoints, call)
        turns = _map_source(call, turns, question)
        contract = extract_contract(call, turns, question)
        dispatch = SYSTEM + delivery_instructions(contract) + DISPATCH
        if research:
            dispatch += ("\nУчастник запустил /research. Выбери служебный PSY_TASK с обезличенной "
                         "публичной темой. Если искать приватно нельзя, верни пустую public_topic.")
        initial = call([*turns, contract_message(contract)], dispatch, prose=not research)

    turns = [*turns, contract_message(contract)]

    if getattr(initial, "stop_reason", None) == "provider_refusal":
        return annotate(initial, contract, status="refused", review="provider_refusal")

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
    # Explicit long manuscript requirements are not optional model routing advice.
    # A completed first answer saying "part one" must not bypass the lecture path.
    if contract["kind"] == "lecture" and (contract["minutes"] or 0) >= 30:
        task = dict(task) if isinstance(task, dict) else {}
        task["minutes"] = contract["minutes"]
        if not task.get("public_topic") and not contract["local_only"]:
            topic = public_topic(question)
            task["public_topic"] = topic
    if not isinstance(task, dict):
        if research:
            raise ModelIssue("unavailable", "research_dispatch")
        if not initial.complete:
            _admit_long(checkpoints, call)
        result = write_prose(turns, SYSTEM, initial=initial)
        if not result.complete and "[Продолжение не завершено." not in result.text:
            result.text += CUT_NOTE
        return finish_task(call, write_prose, turns, SYSTEM, result, contract)

    # Dispatch is already checkpointed. A parked job can resume at this point.
    _admit_long(checkpoints, call)
    # Keep the pre-writing obligations. Routing advice cannot replace or widen them.
    if contract["kind"] == "lecture" and not contract["outline_only"] and not contract["limited_scope"]:
        if contract["minutes"] is None and type(task.get("minutes")) is int and task["minutes"] >= 30:
            contract["minutes"] = task["minutes"]
    minutes = contract["minutes"] if not contract["outline_only"] else None
    long_form = type(minutes) is int and minutes >= 30
    wants_sources = (bool(task.get("public_topic")) or research) and not contract["local_only"]
    topic = public_topic(question) if wants_sources else ""
    sources = None
    if topic.strip():
        sources = _primary_sources(call, topic.strip(), read_main=long_form or task.get("read_main") is True)
        if not sources.complete:
            return annotate(sources, contract, status=getattr(sources, "task_status", "unknown")
                            if getattr(sources, "task_status", "unknown") != "unknown" else "needs_source",
                            review="source_unavailable")
    elif wants_sources:
        return annotate(Result("Для проверки источников начните сообщение со строки \"Публичная тема: название работы или предмет поиска\". "
                               "Только текст этой строки будет использован для внешнего поиска.", [], 0), contract,
                        status="needs_clarification", missing=["Не задана безопасная публичная тема поиска."],
                        review="public_topic_missing")

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
    result = finish_task(call, write_prose, turns, SYSTEM + ("\n" + ATTRIBUTION if sources else ""),
                         result, contract)
    if result.task_status == "refused":
        return result
    if sources:
        previous_status = result.task_status
        previous_missing = result.missing_requirements
        previous_metadata = result.quality_metadata
        previous_text = result.text
        try:
            result = _verify_draft(call, result, sources.text)
        except ModelIssue as exc:
            result = _recover_verified(call, result, sources, exc)
        result.task_status = previous_status if result.complete else "partial"
        result.missing_requirements = list(dict.fromkeys([*previous_missing, *getattr(result, "missing_requirements", [])]))
        result.quality_metadata = {**previous_metadata, **getattr(result, "quality_metadata", {}),
                                   **getattr(sources, "quality_metadata", {})}
        if getattr(sources, "task_status", "unknown") == "partial":
            result.task_status = "partial"
            result.missing_requirements = list(dict.fromkeys([*result.missing_requirements,
                                                              *sources.missing_requirements]))
        if not result.complete and not result.missing_requirements:
            result.missing_requirements = ["Проверка созданного материала по источникам не завершена."]
        if result.text != previous_text:
            result = recheck_edited_task(call, turns, result, contract)
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
