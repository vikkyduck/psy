"""Lecture manuscript: an editorial plan, sections drafted in parallel, a completeness review of each
section with a supplement for what it missed, and assembly with the planned timings."""
import json
import math
from functools import partial

if __package__:
    from .psy_group_client import ModelIssue, Result, _json_value, _parallel, _user
    from .psy_group_continuation import _complete
    from .psy_group_prompts import ATTRIBUTION, SYSTEM
else:
    from psy_group_client import ModelIssue, Result, _json_value, _parallel, _user
    from psy_group_continuation import _complete
    from psy_group_prompts import ATTRIBUTION, SYSTEM


def _lecture(call, turns, minutes, *, write_prose=None):
    # A real manuscript needs an explicit division of labour, with usable
    # examples and cross-references assigned once before parallel drafting.
    planning = SYSTEM + "\n" + ATTRIBUTION + f"""
Спроектируй сценарий именно для исходного запроса на {minutes} минут. Сейчас нужен внутренний редакционный
план. Верни строго JSON с полями title, brief, sections. sections - упорядоченный массив объектов с
полями title, minutes (целое), activity_minutes (целое), brief (тезис, источник, связка с предыдущей частью
и отличающийся от остальных пример). Сумма minutes ровно {minutes}, в каждом разделе 10-25 минут для
многочасовой лекции. Содержательные примеры и переходы заранее закрепи за конкретными разделами, исключи
повторные вступления и пересказы. Если заказан текст лекции, выделяй основное время на речь лектора.
Минуты взаимодействия допустимы только там, где слушатели действительно будут выполнять задание.
Если специально заказано непрерывное выступление, activity_minutes всегда 0. В brief укажи единые для
авторов понятия, атрибуцию и учебную цель. Если участник сам задал объем всей рукописи, распредели его
между разделами в плане. В остальных случаях планируй по содержанию. Ссылки должны быть из проверенных материалов."""
    if write_prose is None:
        write_prose = partial(_complete, call)
    plan = None
    for attempt in range(2):
        response = call(turns, planning)
        parsed = _json_value(response.text) if response.complete else None
        if _valid_plan(parsed, minutes):
            plan = parsed
            break
        planning += "\nПроверь валидность JSON, структуру sections и арифметику времени перед ответом."
    if plan is None:
        raise ModelIssue("unavailable", "lecture_plan")
    plan_text = json.dumps(plan, ensure_ascii=False)
    sections = plan["sections"]
    def write(index):
        section = sections[index]
        start = sum(s["minutes"] for s in sections[:index])
        end = start + section["minutes"]
        talk = section["minutes"] - section["activity_minutes"]
        draft_turns = [*turns, _user("Общий редакционный план по исходному запросу\n" + plan_text)]
        instruction = SYSTEM + "\n" + ATTRIBUTION + f"""
Напиши целиком только раздел {index+1} "{section['title']}" для минут {start}-{end} по этому плану.
По расписанию {talk} мин отведено на речь преподавателя. Пиши ее целиком, так, чтобы преподаватель мог
прочитать раздел вслух. Дай понятия, ход рассуждения, разбор материала примера,
вопросы к примеру и переход, соразмерно теме и уровню слушателей. Отработай тезис этого раздела
и связки из плана, не пересказывай соседние части. Не добавляй внешнего вступления, оглавления
и служебной переписки. Оформи упражнения (если по плану {section['activity_minutes']} мин)
отдельными вставками [Практика: ...] с конкретным материалом, действиями и проверяемым результатом.
При нуле минут на практику такие вставки не нужны. Ссылки у соответствующих тезисов.
Различай работу самого автора и позднейшие интерпретации.
Не утверждай, что материал занимает заказанное время, лишь на основании таймкодов плана."""
        result = write_prose(draft_turns, instruction)
        if not result.complete:
            return result
        # Review the actual manuscript in the context of the original request.
        # Never infer coverage or running time from its length.
        review_turns = [*draft_turns, {"role": "assistant", "content": result.text}]
        try:
            review = call(review_turns, SYSTEM + f"""
Проверь содержание раздела "{section['title']}" по исходному запросу и его задаче в плане.
Есть ли готовая речь преподавателя, раскрыт ли ход рассуждения и разобран ли назначенный пример?
Если раздел закончен по существу, верни строго JSON {{"missing":""}}. Если конкретное обязательство
по плану пропущено или лишь объявлено, верни {{"missing":"что именно требуется развернуть"}}.
Не придумывай пожеланий сверх запроса. Не суди по длине и не выводи длительность чтения.""")
            verdict = _json_value(review.text) if review.complete else None
            if not isinstance(verdict, dict) or not isinstance(verdict.get("missing"), str):
                raise ModelIssue("unavailable", "lecture_review")
            missing = verdict["missing"].strip()
            if missing:
                supplement = write_prose([*draft_turns, _user(
                    "Напиши только недостающий фрагмент в форме речи преподавателя. "
                    "Дополнение должно читаться сразу после готового текста, без повтора и служебных отметок. "
                    "Вопрос для раскрытия:\n" + missing)], SYSTEM + "\n" + ATTRIBUTION, previous_chapter=result.text)
                addition = supplement.text.strip()
                if not addition or addition in result.text:
                    raise ModelIssue("unavailable", "lecture_supplement")
                result.text += "\n\n" + addition
                result.complete = supplement.complete
        except ModelIssue as exc:
            result.text += "\n\n[Проверка полноты раздела не завершена. " + exc.public() + "]"
            result.complete = False
        return result

    parts = []
    complete = True
    first_failure = None
    missing = []
    # A section that ends with a model issue leaves a gap; a deferred 429 or any other
    # failure stops the remaining sections and parks the job with the written ones saved.
    for result, issue in _parallel(call, write, range(len(sections)), tolerate=ModelIssue):
        if issue is not None:
            first_failure = first_failure or issue
            complete = False
            parts.append(None)
            missing.append("Раздел не сформирован: " + sections[len(parts) - 1]["title"])
        else:
            parts.append(result.text)
            complete &= result.complete
            if not result.complete:
                missing.append("Раздел требует завершения или проверки: " + sections[len(parts) - 1]["title"])
    if not any(parts):
        if first_failure:
            raise first_failure
        raise ModelIssue("unavailable", "lecture_sections")
    out = [plan["title"]]
    elapsed = 0
    practice = sum(s["activity_minutes"] for s in sections)
    for index, (section, part) in enumerate(zip(sections, parts), 1):
        end = elapsed + section["minutes"]
        if part is None:
            part = "[Этот раздел не сформирован.]"
        out.append(f"{index}. {section['title']} ({elapsed}-{end} мин по плану)\n\n{part}")
        elapsed = end
    schedule = f"План занятия - {minutes} мин."
    if practice:
        schedule += f" Из них на работу слушателей отведено {practice} мин."
    out.insert(1, schedule)
    if not complete:
        out.append("Работа над рукописью завершена частично. " + (
            first_failure.public() if first_failure else "Доступный текст сохранен."))
    result = Result("\n\n".join(out), [], 0, complete)
    result.task_status = "unknown" if complete else "partial"
    result.missing_requirements = missing
    result.quality_metadata = {"lecture_sections_planned": len(sections),
                               "lecture_sections_present": sum(part is not None for part in parts),
                               "planned_minutes": minutes, "planned_activity_minutes": practice,
                               "duration_basis": "schedule_not_measured"}
    return result


def _valid_plan(plan, minutes):
    if not isinstance(plan, dict) or not isinstance(plan.get("title"), str) or not plan["title"].strip():
        return False
    sections = plan.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= max(4, math.ceil(minutes / 5)):
        return False
    for s in sections:
        if not isinstance(s, dict) or not all(isinstance(s.get(k), str) and s[k].strip() for k in ("title", "brief")):
            return False
        if type(s.get("minutes")) is not int or type(s.get("activity_minutes")) is not int:
            return False
        if not (0 <= s["activity_minutes"] <= s["minutes"] and 0 < s["minutes"] <= max(30, math.ceil(minutes / 4))):
            return False
    return sum(s["minutes"] for s in sections) == minutes
