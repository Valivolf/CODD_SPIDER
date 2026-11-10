"""Вопрос на обычном языке → запрос к данным → ответ. Модель только ПЛАНИРУЕТ запрос (какой набор, период, условия, группировка, подсчёт),
по схеме JSON — она физически не может ответить чем-то другим. Считает и выбирает данные программа (query_engine.py) по правам роли
человека; ответ — число или таблица плюс подпись «как посчитано», без пересказа моделью (так цифры не искажаются).

Подсказка модели одна для всех ролей (чтобы кэш подсказки на процессоре не сбрасывался), права проверяются при выполнении запроса."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from typing import Any, Optional

from app.services.ai import agent, narrate, progress, skills, understand
from app.services.ai import catalog as cat
from app.services.ai import client
from app.services.ai import plan_cache
from app.services.ai import query_engine as qe

logger = logging.getLogger(__name__)

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
OPS = ["=", "!=", ">", ">=", "<", "<=", "between", "contains", "not_contains", "starts", "in", "is_empty", "not_empty"]


def _all_fields() -> list[str]:
    names: list[str] = []
    for ds in cat.DATASETS.values():
        for f in ds.fields:
            if f.name not in names:
                names.append(f.name)
    return names


def _group_keys() -> list[str]:
    keys: list[str] = []
    for ds in cat.DATASETS.values():
        for f in ds.fields:
            if not f.groupable:
                continue
            keys.append(f.name)
            if f.type == cat.DATE:
                keys.extend(f"{f.name}:{b}" for b in ("day", "week", "month", "year"))
    return list(dict.fromkeys(keys))


def build_schema() -> dict:
    fields = _all_fields()
    return {
        "type": "object",
        "properties": {
            "can_answer": {"type": "boolean"},
            "reason": {"type": "string"},
            "dataset": {"type": "string", "enum": list(cat.DATASETS)},
            "period": {
                "type": "object",
                "properties": {"preset": {"type": "string", "enum": ["", *qe.PRESETS]}, "from": {"type": "string"}, "to": {"type": "string"}},
                "additionalProperties": False,
            },
            "filters": {"type": "array", "items": {
                "type": "object",
                "properties": {"field": {"type": "string", "enum": fields}, "op": {"type": "string", "enum": OPS}, "value": {"type": "string"},
                               "values": {"type": "array", "items": {"type": "string"}}},
                "required": ["field", "op"], "additionalProperties": False}},
            "aggregates": {"type": "array", "items": {
                "type": "object",
                "properties": {"fn": {"type": "string", "enum": sorted(qe.AGG_FNS)}, "field": {"type": "string", "enum": ["", *fields]}},
                "required": ["fn"], "additionalProperties": False}},
            "group_by": {"type": "array", "items": {"type": "string", "enum": _group_keys()}},
            "select": {"type": "array", "items": {"type": "string", "enum": fields}},
            "order_by": {"type": "array", "items": {
                "type": "object",
                "properties": {"field": {"type": "string", "enum": [*fields, "count"]}, "dir": {"type": "string", "enum": ["asc", "desc"]}},
                "required": ["field", "dir"], "additionalProperties": False}},
            "limit": {"type": "integer"},
        },
        # Обязательны только can_answer и dataset: пустые поля модель не пишет — ответ втрое короче, а на процессоре время ответа почти целиком уходит на выдачу слов.
        "required": ["can_answer", "dataset"],
        "additionalProperties": False,
    }


_TYPE_NAMES = {cat.TEXT: "текст", cat.ENUM: "список", cat.DATE: "дата", cat.NUMBER: "число", cat.BOOL: "да/нет"}

EXAMPLES = [
    ("Сколько было выездов по инвентаризации в сентябре?",
     {"can_answer": True, "reason": "", "dataset": "visits", "period": {"preset": "", "from": "2026-09", "to": "2026-09"},
      "filters": [{"field": "visit_type", "op": "contains", "value": "Инвентаризация", "values": []}],
      "aggregates": [{"fn": "count", "field": ""}], "group_by": [], "select": [], "order_by": [], "limit": 0}),
    ("Покажи выезды за прошлую неделю, где нужна автовышка",
     {"can_answer": True, "reason": "", "dataset": "visits", "period": {"preset": "last_week", "from": "", "to": ""},
      "filters": [{"field": "needs", "op": "contains", "value": "автовышк", "values": []}],
      "aggregates": [], "group_by": [], "select": ["date", "point_id", "address", "fault", "needs"],
      "order_by": [{"field": "date", "dir": "desc"}], "limit": 50}),
    ("Сколько ремонтов по каждой бригаде за этот месяц",
     {"can_answer": True, "reason": "", "dataset": "visits", "period": {"preset": "this_month", "from": "", "to": ""},
      "filters": [{"field": "repair", "op": "=", "value": "да", "values": []}],
      "aggregates": [{"fn": "count", "field": ""}], "group_by": ["crew"], "select": [], "order_by": [{"field": "count", "dir": "desc"}], "limit": 0}),
    ("Сколько комплексов сейчас не работает?",
     {"can_answer": True, "reason": "", "dataset": "points", "period": {"preset": "", "from": "", "to": ""},
      "filters": [{"field": "status", "op": "=", "value": "Не работает", "values": []}],
      "aggregates": [{"fn": "count", "field": ""}], "group_by": [], "select": [], "order_by": [], "limit": 0}),
    ("Кто в отпуске в октябре?",
     {"can_answer": True, "reason": "", "dataset": "absences", "period": {"preset": "", "from": "2026-10", "to": "2026-10"},
      "filters": [{"field": "reason", "op": "contains", "value": "отпуск", "values": []}], "aggregates": [], "group_by": [],
      "select": ["person", "date_from", "date_to", "reason"], "order_by": [{"field": "date_from", "dir": "asc"}], "limit": 0}),
    ("Когда последний раз был выезд на комплекс 1005?",
     {"can_answer": True, "reason": "", "dataset": "points", "period": {"preset": "", "from": "", "to": ""},
      "filters": [{"field": "point_id", "op": "contains", "value": "1005", "values": []}],
      "aggregates": [], "group_by": [], "select": ["point_id", "address", "last_visit"], "order_by": [], "limit": 0}),
    ("Какой пароль у комплекса 1005?",
     {"can_answer": True, "reason": "", "dataset": "points", "period": {"preset": "", "from": "", "to": ""},
      "filters": [{"field": "point_id", "op": "contains", "value": "1005", "values": []}],
      "aggregates": [], "group_by": [], "select": ["point_id", "login", "password"], "order_by": [], "limit": 0}),
    ("Какая сегодня погода?",
     {"can_answer": False, "reason": "Я отвечаю только на вопросы по данным системы: выезды, задания, комплексы, оборудование, отсутствия, бригады.",
      "dataset": "visits", "period": {"preset": "", "from": "", "to": ""}, "filters": [], "aggregates": [], "group_by": [], "select": [], "order_by": [], "limit": 0}),
]


def _compact(obj: Any) -> Any:
    """Без пустых полей: так модель и видит примеры, и отвечает (короче подсказка, короче ответ → быстрее на процессоре)."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            value = _compact(value)
            if key != "can_answer" and (value is None or value == "" or value == [] or value == {} or (value == 0 and not isinstance(value, bool))):
                continue
            out[key] = value
        return out
    if isinstance(obj, list):
        return [_compact(x) for x in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(_compact(obj), ensure_ascii=False, separators=(",", ":"))


def build_system(enums: dict[str, list[str]]) -> str:
    """Постоянная часть подсказки (без даты — иначе кэш подсказки в модели сбрасывался бы каждый день; дата идёт в конце сообщения)."""
    lines = [
        "Ты — помощник диспетчера системы «Календарь работ» (обслуживание комплексов видеофиксации, АПК). Превращай вопрос пользователя в описание "
        "запроса к данным. Сам ничего не считаешь и не отвечаешь по памяти: ты только выбираешь набор данных, период, условия, группировку и подсчёт — "
        "расчёт выполнит программа.",
        "",
        "НАБОРЫ ДАННЫХ И ПОЛЯ — «имя (тип): что это»:",
    ]
    for ds in cat.DATASETS.values():
        lines.append(f"[{ds.name}] {ds.label}")
        for f in ds.fields:
            lines.append(f"  {f.name} ({_TYPE_NAMES[f.type]}): {f.label}")
    lines += ["", "ЗНАЧЕНИЯ ПОЛЕЙ-СПИСКОВ:"]
    for key, label in (("work_types", "visit_type и type — типы работ"), ("visit_statuses", "status — итог выезда"), ("point_statuses", "статусы комплекса (status у points)"),
                       ("point_categories", "category — категории комплексов"), ("equipment_categories", "category у equipment"),
                       ("equipment_statuses", "status у equipment"), ("crew_equip", "equip у crews")):
        values = enums.get(key) or []
        if values:
            lines.append(f"  {label}: " + ", ".join(values[:20]))
    lines += [
        "",
        "ПРАВИЛА",
        "1. dataset: выезды, ремонты, инвентаризации, «что делали», причина, что нужно для восстановления → visits; задания, план, не выполнено, бригады на день → tasks; "
        "статус комплекса сейчас, адрес, SIM, питание, когда последний раз были → points; «когда упал», сколько раз падал, когда заработал → status_changes; "
        "оборудование и SIM-карты по номерам → equipment; отпуск, больничный → absences; составы бригад по дням → crews; подгруппа АПК, подборка, что нужно на выезде в подгруппе → apk_groups. "
        "Почему комплекс не работает сейчас и что нужно для восстановления (последний отчёт) — поля fault_reason и fault_need у points.",
        "2. period.preset — одно из: " + ", ".join(qe.PRESETS) + " — ТОЛЬКО для «сегодня, вчера, этот/прошлый месяц/неделя/квартал/год, последние N дней». "
        "Месяц или год, названный по имени («в сентябре», «за октябрь», «в 2025») — ВСЕГДА from и to (ГГГГ-ММ-ДД или ГГГГ-ММ), а preset пустой; preset и from/to вместе не заполняй. "
        "Месяц без года — ближайший прошедший или текущий. Без периода preset, from и to — пустые строки.",
        "3. «Сколько» → aggregates [{fn: count}]. «Сколько РАЗНЫХ комплексов» → count_distinct по point_id. «Сколько … в каждой / по каждой …» → count и group_by по этому полю "
        "(count_distinct тут не нужен). «По месяцам / типам / бригадам / исполнителям» → group_by; по времени — date:month, date:week, date:year.",
        "4. «Покажи / какие / перечисли / выведи список» → select с нужными полями, без aggregates и group_by. «Причины неисправностей» → select fault с условием fault not_empty.",
        "5. В условиях пиши значения как в списках; «содержит» (contains) — для слов в тексте (comment, fault, needs). Ремонт при любом типе выезда → repair = да.",
        "6. Если вопрос не про эти данные (погода, советы, шутки) или нужных данных нет — can_answer=false и в reason одно предложение; остальные поля заполни по умолчанию.",
        "7. Поля, которых нет в списках, не придумывай. Пароль и логин комплекса — обычные поля (login, password): если их просят, составь запрос как обычно и НЕ отказывай сам — разрешена ли роли выдача, проверит программа.",
        "8. «Запланировано», «сколько заданий», «часов в плане» — набор tasks БЕЗ условий по done; условие done ставь, только если прямо спросили про выполненные или невыполненные. "
        "Лишних условий, о которых не спрашивали, не добавляй.",
        "9. Пустые поля НЕ пиши: period, filters, aggregates, group_by, select, order_by, limit, reason — только те, что нужны этому вопросу. Для «сколько» не пиши select и order_by.",
        "",
    ]
    return "\n".join(lines)


def date_note(today: dt.date) -> str:
    """Дата идёт в конце сообщения, а не в постоянной подсказке: так кэш подсказки в модели не сбрасывается каждый день."""
    return f"\n(Сегодня {today.isoformat()}, {WEEKDAYS[today.weekday()]}.)"


EXAMPLE_TODAY = dt.date(2026, 10, 14)       # «сегодня» в примерах: у всех примеров такая же приписка — модель видит, что дата сама по себе не период


def _history_messages() -> list[dict]:
    messages: list[dict] = []
    for question, answer in EXAMPLES:
        messages.append({"role": "user", "content": question + date_note(EXAMPLE_TODAY)})
        messages.append({"role": "assistant", "content": _dumps(answer)})
    return messages


def _schema_form(spec: dict) -> dict:
    """Разобранный запрос (с None) → вид схемы модели (с пустыми строками): им показываем модели её же прошлый разбор."""
    period = spec.get("period") or {}
    return {
        "can_answer": True, "reason": "", "dataset": spec["dataset"],
        "period": {"preset": period.get("preset") or "", "from": period.get("from") or "", "to": period.get("to") or ""},
        "filters": [{"field": f.get("field", ""), "op": f.get("op", "="), "value": str(f.get("value") or ""), "values": [str(v) for v in f.get("values") or []]}
                    for f in spec.get("filters") or []],
        "aggregates": [{"fn": a.get("fn", "count"), "field": a.get("field") or ""} for a in spec.get("aggregates") or []],
        "group_by": list(spec.get("group_by") or []), "select": list(spec.get("select") or []),
        "order_by": [{"field": o.get("field", ""), "dir": o.get("dir", "asc")} for o in spec.get("order_by") or []], "limit": int(spec.get("limit") or 0),
    }


def _fix_dataset(spec: dict) -> dict:
    """Модель иногда берёт поле из другого набора («последний выезд» — это поле комплекса, а не выезда): если все названные поля есть ровно
    в одном другом наборе — переключаем набор. Иначе оставляем как есть (движок скажет, чего не хватает)."""
    ds = cat.DATASETS.get(spec.get("dataset"))
    if ds is None:
        return spec
    names = {f.get("field") for f in spec.get("filters") or []} | set(spec.get("select") or []) | {a.get("field") for a in spec.get("aggregates") or []}
    names |= {g.partition(":")[0] for g in spec.get("group_by") or []}
    names |= {o.get("field") for o in spec.get("order_by") or [] if o.get("field") != "count"}
    names = {n for n in names if n}
    if not names or all(ds.field(n) is not None for n in names):
        return spec
    fits = [name for name, other in cat.DATASETS.items() if name != spec["dataset"] and all(other.field(n) is not None for n in names)]
    if len(fits) == 1:
        spec = {**spec, "dataset": fits[0]}
    return spec


def _fix_filter(flt: dict) -> dict:
    """Условие без значения («причина содержит ничего») — это «причина указана»: небольшие модели так выражают «покажи причины»."""
    if flt.get("value") or flt.get("values"):
        return flt
    op = flt.get("op")
    if op in ("contains", "starts", "!="):
        return {**flt, "op": "not_empty"}
    if op == "=":
        return {**flt, "op": "is_empty"}
    return flt


def _clean_spec(data: dict) -> dict:
    """Пустые строки схемы («нет значения») → «нет значения» движка. Заодно исправляет типичные промахи небольших моделей: явные даты вместе с
    названным периодом («в сентябре» + «этот месяц») — побеждают даты; «сколько X в каждой Y» с подсчётом РАЗНЫХ по самому Y — это обычный подсчёт."""
    spec = dict(data)
    period = dict(spec.get("period") or {})
    frm, to, preset = period.get("from") or None, period.get("to") or None, period.get("preset") or None
    if (frm or to) and preset:
        preset = None
    spec["period"] = {"preset": preset, "from": frm, "to": to}
    group_keys = {str(g).partition(":")[0] for g in spec.get("group_by") or []}
    aggregates = []
    for a in spec.get("aggregates") or []:
        fn, field = a.get("fn"), a.get("field") or None
        if fn == "count_distinct" and (field in group_keys or not field):
            fn, field = "count", None
        aggregates.append({"fn": fn, "field": field, "alias": ""})
    spec["aggregates"] = aggregates
    spec["filters"] = [_fix_filter(f) for f in spec.get("filters") or []]
    for key in ("group_by", "select", "order_by"):          # модель пустые поля не пишет
        spec[key] = list(spec.get(key) or [])
    spec["limit"] = int(spec.get("limit") or 0)
    return spec


# ---- «страховка» после модели: небольшие модели (3B) иногда путают период и добавляют условие, о котором не спрашивали. Эти исправления строго
# по тексту вопроса, поэтому безопасны и для больших моделей.
_MONTH_RE = re.compile(r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яйе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b")
_MONTH_NUM = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "мае": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_DONE_WORDS = re.compile(r"выполн|сделан|закрыт|остал|не сдела|без отч|отчет|отчёт|висит|невыполн")


# Пресет периода допустим, только если вопрос его называет (небольшая модель любит подставлять «сегодня» и «этот месяц» без всякого повода).
_PRESET_WORDS = {
    "today": r"сегодня|сегодняшн", "yesterday": r"вчера|вчерашн",
    "this_week": r"эт(ой|у|а) недел|на неделе|текущ\w* недел", "last_week": r"прошл\w* недел|прошедш\w* недел",
    "this_month": r"эт(ом|от) месяц|текущ\w* месяц", "last_month": r"прошл\w* месяц|прошедш\w* месяц",
    "this_quarter": r"эт(ом|от) квартал|текущ\w* квартал", "last_quarter": r"прошл\w* квартал|прошедш\w* квартал",
    "this_year": r"эт(ом|от) год|текущ\w* год", "last_year": r"прошл\w* год|прошедш\w* год",
    "last_7_days": r"послед\w* (7|сем|недел)", "last_30_days": r"послед\w* (30|тридцат|месяц)",
    "last_90_days": r"послед\w* (90|девяност|квартал)", "last_365_days": r"послед\w* (365|год)",
}
_LIST_INTENT = re.compile(r"^(какие|какой|какая|какое|каких|кто|кого|покажи|показать|перечисли|выведи|выгрузи|список)\b")


_NUM_WORDS = {"один": 1, "одного": 1, "одному": 1, "одной": 1, "два": 2, "двух": 2, "двум": 2, "три": 3, "трех": 3, "трём": 3, "трем": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5,
              "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10}
_MIN_COUNT_RE = re.compile(r"(больше|более|не менее|не меньше|хотя бы|минимум|как минимум)\s+(?:чем\s+)?(\d+|[а-я]+)\b")


def min_count_in_question(question: str) -> Optional[int]:
    """«больше одного выезда» → 2, «не меньше трёх ремонтов» → 3, «хотя бы 2 раза» → 2; нет такого условия — None. Больше/более — строго больше, остальное — «не меньше»."""
    m = _MIN_COUNT_RE.search(plan_cache.normalize(question))
    if not m:
        return None
    word, num = m.group(1), m.group(2)
    value = int(num) if num.isdigit() else _NUM_WORDS.get(num)
    if not value:
        return None
    return value + 1 if word in ("больше", "более") else value


def month_in_question(question: str, today: dt.date) -> Optional[str]:
    """Единственный месяц, названный по имени («в сентябре», «за октябрь 2025»), как ГГГГ-ММ; нет или несколько — None. Год: названный, «прошлого года»,
    иначе ближайший прошедший или текущий месяц."""
    q = plan_cache.normalize(question)
    found = {_MONTH_NUM[m.group(1)[:3]] for m in _MONTH_RE.finditer(q)}
    if len(found) != 1:
        return None
    month = found.pop()
    year_m = re.search(r"\b(20\d\d)\b", q)
    if year_m:
        year = int(year_m.group(1))
    elif re.search(r"прошл\w* год", q):
        year = today.year - 1
    else:
        year = today.year if month <= today.month else today.year - 1
    return f"{year}-{month:02d}"


def apply_guardrails(spec: dict, question: str, today: dt.date) -> dict:
    """Месяц, названный в вопросе, всегда становится периодом; условие «выполнено» у заданий — только если о выполнении спросили."""
    spec = dict(spec)
    month = month_in_question(question, today)
    if month:
        period = spec.get("period") or {}
        if not (period.get("from") == month and period.get("to") == month and not period.get("preset")):
            spec["period"] = {"preset": None, "from": month, "to": month}
    q = plan_cache.normalize(question)
    when = understand.relative_period(q, today)                   # «завтра», «послезавтра», «на следующей неделе»: готовых периодов для будущего нет — даты считает программа
    months = understand.named_months(q, today) if month is None else []
    if when:
        spec["period"] = {"preset": None, "from": when[0], "to": when[1]}
    elif len(months) >= 2:                                        # «сравни сентябрь и октябрь»: период от первого до последнего названного месяца, по месяцам
        spec["period"] = {"preset": None, "from": months[0], "to": months[-1]}
        ds_name = spec.get("dataset")
        date_field = cat.DATASETS[ds_name].date_field if ds_name in cat.DATASETS else None
        wants_months = bool(understand._COMPARE.search(q) or re.search(r"по месяц", q))  # noqa: SLF001
        if date_field and wants_months and spec.get("aggregates") and not spec.get("group_by"):
            spec["group_by"] = [f"{date_field}:month"]
    period = spec.get("period") or {}
    preset = period.get("preset")
    if preset and not (period.get("from") or period.get("to")):
        words = _PRESET_WORDS.get(preset)
        if words is None or not re.search(words, q):
            spec["period"] = {"preset": None, "from": None, "to": None}             # вопрос период не называет — без периода
    if spec.get("dataset") == "tasks" and not _DONE_WORDS.search(q):
        spec["filters"] = [f for f in spec.get("filters") or [] if f.get("field") != "done"]
    mc = min_count_in_question(question)
    if mc and spec.get("group_by") and any(a.get("fn") in ("count", "count_distinct", None) for a in spec.get("aggregates") or [{"fn": "count"}]):
        spec["min_count"] = mc
    # «Какие комплексы не работают» / «покажи выезды…» — это список; модель иногда отвечает числом (подсчётом) — убираем подсчёт
    aggs = spec.get("aggregates") or []
    wants_list = bool(_LIST_INTENT.match(q) or understand._LIST_WORDS.search(q))      # noqa: SLF001 — «на каких», «кто», «покажи»… в любом месте вопроса
    if wants_list and "сколько" not in q and aggs and not spec.get("group_by") and all(a.get("fn") == "count" and not a.get("field") for a in aggs):
        spec = _drop_count(spec)
    # «Сколько ремонтов было в сентябре и на каких комплексах» — человек просит и число, и перечень: отвечаем списком (число — «всего записей»)
    if aggs and not spec.get("group_by") and _COUNT_AND_LIST.search(q) and all(a.get("fn") == "count" and not a.get("field") for a in aggs):
        spec = _drop_count(spec)
    # «Покажи выезды» без «первые N / последний / самый» — не ограничиваем одной строкой (модель иногда ставит limit 1 просто так)
    if spec.get("limit") == 1 and not re.search(r"(послед|перв|самый|самая|самое|наибольш|наименьш|максимальн|минимальн|один |одну |одного )", q):
        spec["limit"] = 0
    spec = _enforce_last_visit(spec, q)
    spec = _drop_unmentioned_equals(spec, q)
    # «Сколько комплексов питаются от солнечных панелей» — ОДНО число; группировка комплексов по их же номеру (у каждого «1») бессмысленна — модель так иногда пишет
    if (spec.get("dataset") == "points" and spec.get("group_by") in (["point_id"], ["address"]) and spec.get("aggregates") and understand._COUNT_WORDS.search(q)  # noqa: SLF001
            and not re.search(r"(по каждому|каждый|каждого|по комплексам|по адресам|на каких|у каких)", q)):
        spec = _drop_count_order(spec)
    # «Кто сейчас в отпуске» — отсутствия, действующие СЕГОДНЯ: без периода модель показывает и давно закончившиеся
    period = spec.get("period") or {}
    if spec.get("dataset") == "absences" and not (period.get("preset") or period.get("from") or period.get("to")) and _NOW_WORDS.search(q):
        spec["period"] = {"preset": "today", "from": None, "to": None}
    return _enforce_named_values(spec, q)


_NOW_WORDS = re.compile(r"\b(сейчас|в данный момент|на данный момент|в настоящее время|на текущий момент|нынче)\b")


_LAST_VISIT_Q = re.compile(r"\b(какой|какого|каком|какому|каким)\s+(комплекс|апк)\w*\b.*(послед|недавн|свеж)|(послед|недавн|свеж)\w*.*\b(какой|какого|каком|какому|каким)\s+(комплекс|апк)")
_LAST_VISIT_VERB = re.compile(r"(выезд|ездил|ехал|посещ|обслуж|был[аи]?\b|приезж)")


def _enforce_last_visit(spec: dict, q: str) -> dict:
    """«У какого комплекса последним был выезд?» — ОДИН комплекс с самой поздней датой выезда. Небольшие модели отвечают списком всех комплексов без порядка."""
    if not (_LAST_VISIT_Q.search(q) and _LAST_VISIT_VERB.search(q)) or spec.get("group_by"):
        return spec
    if spec.get("dataset") == "points":
        spec.update(aggregates=[], select=["point_id", "address", "last_visit"], order_by=[{"field": "last_visit", "dir": "desc"}], limit=1)
        spec["filters"] = [f for f in spec.get("filters") or [] if f.get("field") != "last_visit"] + [{"field": "last_visit", "op": "not_empty", "value": "", "values": []}]
    elif spec.get("dataset") == "visits":
        spec.update(aggregates=[], select=["date", "point_id", "address"], order_by=[{"field": "date", "dir": "desc"}], limit=1)
    return spec


def _drop_unmentioned_equals(spec: dict, q: str) -> dict:
    """«Сколько комплексов в каждой категории» — модель перечисляет категории условиями «категория = ARENA И категория = POST…» (в результате пусто). Условия «=» по полю, которое
    группируется или повторяется с разными значениями, оставляются только те, чьё значение названо в вопросе."""
    filters = spec.get("filters") or []
    group_fields = {str(g).partition(":")[0] for g in spec.get("group_by") or []}
    by_field: dict[str, list[dict]] = {}
    for f in filters:
        if f.get("op") == "=":
            by_field.setdefault(str(f.get("field")), []).append(f)
    drop: set[int] = set()
    for field, items in by_field.items():
        if field in group_fields or len({str(f.get("value")) for f in items}) > 1:
            for f in items:
                value = plan_cache.normalize(str(f.get("value") or ""))
                if not value or value[:6] not in q:
                    drop.add(id(f))
    if drop:
        spec["filters"] = [f for f in filters if id(f) not in drop]
    return spec


def _drop_count_order(spec: dict) -> dict:
    """Группировка снята (нужно одно общее число): сортировка по «count» и лимит теряют смысл."""
    spec["group_by"] = []
    spec["order_by"] = []
    spec["limit"] = 0
    return spec


def _drop_count(spec: dict) -> dict:
    """Подсчёт убран (нужен список): вместе с ним уходят сортировка и столбцы по «count» — такого поля у записей нет, и движок отвечал бы ошибкой."""
    ds = cat.DATASETS.get(spec.get("dataset", ""))
    known = (lambda name: ds is not None and ds.field(str(name).partition(":")[0]) is not None) if ds else (lambda name: True)
    spec["aggregates"] = []
    spec["order_by"] = [o for o in spec.get("order_by") or [] if known(o.get("field", ""))]
    spec["select"] = [s for s in spec.get("select") or [] if known(s)]
    return spec


_COUNT_AND_LIST = re.compile(r"\bсколько\b.*\b(?:и|а также|а) (?:на каких|у каких|в каких|по каким|у кого|кто|какие|какой|где|когда|какого числа|в какие дни)\b")


def _set_filter(filters: list[dict], field: str, op: str, value: str) -> list[dict]:
    """Условие по полю заменяется (или добавляется) заданным: то, что человек назвал словами, важнее догадки модели."""
    kept = [f for f in filters if f.get("field") != field]
    kept.append({"field": field, "op": op, "value": value, "values": []})
    return kept


def _enforce_named_values(spec: dict, q: str) -> dict:
    """Названное в вопросе словами («неработающие», «на солнечных панелях», «нужна автовышка») всегда становится условием, а условие «статус = …», о котором не спрашивали, убирается:
    небольшие модели то теряют первое, то выдумывают второе, а подсказка в тексте запроса их не всегда удерживает."""
    ds = spec.get("dataset")
    filters = list(spec.get("filters") or [])
    status, power = understand.named_status(q), understand.named_power(q)
    if ds == "points":
        if status:
            filters = _set_filter(filters, "status", "=", status)
        elif not understand.mentions_status(q):
            filters = [f for f in filters if f.get("field") != "status"]
        if power:
            filters = _set_filter(filters, "power_source", "=", power)
    if re.search(r"автовышк|вышк", q):
        need_field = {"visits": "needs", "points": "fault_need"}.get(ds or "")
        if need_field and not any(f.get("field") == need_field and f.get("op") in ("contains", "=", "in") for f in filters):
            filters = _set_filter(filters, need_field, "contains", "автовышк")
    spec["filters"] = filters
    return spec


async def plan(question: str, *, history: Optional[list[dict]] = None, today: Optional[dt.date] = None, priority: int = client.PRIORITY_USER,
               hints: Optional[list[str]] = None, feedback: str = "") -> dict:
    """Вопрос → нормализованное описание запроса либо {"can_answer": False, "reason": …}. hints — проверенные программой подсказки (точные номера комплексов, месяц, что нужно человеку:
    число, список, рейтинг); feedback — почему не сработала прошлая попытка (текст ошибки движка): модель исправляет запрос, а не повторяет его."""
    today = today or dt.date.today()
    enums = await qe.enum_values()
    msgs = _history_messages()
    for item in (history or [])[-2:]:                 # предыдущие вопросы и их разбор — чтобы работало «а за прошлый месяц?»
        previous_q, previous_spec = str(item.get("question") or "").strip()[:600], item.get("spec")
        if previous_q and isinstance(previous_spec, dict) and previous_spec.get("dataset") in cat.DATASETS:
            msgs.append({"role": "user", "content": previous_q + date_note(today)})
            msgs.append({"role": "assistant", "content": _dumps(_schema_form(previous_spec))})
    user = question.strip()[:600]
    if hints:
        user += "\nПодсказки (проверены по базе): " + " ".join(hints[:5])
    if feedback:
        user += f"\nПрошлая попытка не сработала: {feedback[:300]} Составь запрос заново, исправив это."
    user += date_note(today)
    data = await client.llm_json(build_system(enums), user, build_schema(), schema_name="query_plan", max_tokens=500, priority=priority, history=msgs)
    if not data.get("can_answer"):
        return {"can_answer": False, "reason": str(data.get("reason") or "").strip()}
    return {"can_answer": True, "spec": apply_guardrails(_fix_dataset(_clean_spec(data)), question, today)}


PLAN_REV = "8"      # поднять, когда меняются правила подсказки модели: запомненные разборы со старой версией перестают использоваться
PLAN_VERSION = PLAN_REV + hashlib.sha1((json.dumps(build_schema(), sort_keys=True) + json.dumps(EXAMPLES, ensure_ascii=False, sort_keys=True)).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def _spec(dataset: str, *, preset: str = "", frm: str = "", to: str = "", filters: tuple = (), aggregates: tuple = (), group_by: tuple = (),
          select: tuple = (), order_by: tuple = (), limit: int = 0) -> dict:
    """Описание запроса в виде схемы модели (с пустыми строками) — так же, как отвечает модель."""
    return {"can_answer": True, "reason": "", "dataset": dataset, "period": {"preset": preset, "from": frm, "to": to},
            "filters": [{"field": f, "op": op, "value": value, "values": []} for f, op, value in filters],
            "aggregates": [{"fn": fn, "field": field} for fn, field in aggregates], "group_by": list(group_by), "select": list(select),
            "order_by": [{"field": f, "dir": d} for f, d in order_by], "limit": limit}


# Вопросы-кнопки на странице помощника (routes_assistant.EXAMPLES) разобраны заранее: ответ мгновенный, модель не нужна. Формулировка должна
# совпадать с кнопкой (регистр и знаки препинания не важны).
PRESET_PLANS: list[tuple[str, dict]] = [
    ("Сколько было выездов по инвентаризации за прошлый месяц?",
     _spec("visits", preset="last_month", filters=(("visit_type", "contains", "Инвентаризация"),), aggregates=(("count", ""),))),
    ("Какие комплексы сейчас не работают?",
     _spec("points", filters=(("status", "=", "Не работает"),), select=("point_id", "address", "status", "down_since", "last_visit"), order_by=(("point_id", "asc"),))),
    ("Покажи выезды за прошлую неделю, где нужна автовышка",
     _spec("visits", preset="last_week", filters=(("needs", "contains", "автовышк"),), select=("date", "point_id", "address", "fault", "needs"), order_by=(("date", "desc"),), limit=50)),
    ("Сколько ремонтов у каждой бригады в этом месяце?",
     _spec("visits", preset="this_month", filters=(("repair", "=", "да"),), aggregates=(("count", ""),), group_by=("crew",), order_by=(("count", "desc"),))),
    ("Кто сейчас в отпуске?",
     _spec("absences", preset="today", filters=(("reason", "contains", "отпуск"),), select=("person", "role", "date_from", "date_to", "reason"), order_by=(("date_from", "asc"),))),
    ("Кто отсутствует на этой неделе?",
     _spec("absences", preset="this_week", select=("person", "role", "date_from", "date_to", "reason"), order_by=(("date_from", "asc"),))),
    ("Сколько дней отсутствовал каждый человек в этом месяце?",
     _spec("absences", preset="this_month", aggregates=(("sum", "days"),), group_by=("person",), order_by=(("count", "desc"),))),
    ("Сколько роутеров на складе?",
     _spec("equipment", filters=(("category", "contains", "роутер"), ("place", "=", "на складе")), aggregates=(("count", ""),))),
    ("Какое оборудование сейчас у монтёров?",
     _spec("equipment", filters=(("place", "=", "у монтёра"),), select=("holder", "category", "model", "serial", "held_reason"), order_by=(("holder", "asc"),))),
    ("Покажи оборудование, которое снято с комплексов и ждёт приёмки",
     _spec("equipment", filters=(("held_reason", "=", "снято с комплекса, ждёт приёмки"),), select=("serial", "category", "model", "removed_point", "removed_date", "holder", "removal_reason"))),
]

_DAY_TASKS_RE = re.compile(r"^какие задани\w* (?:стоят |запланированы |есть )?на (?:завтра|послезавтра|сегодня)$")

# Вопросы с подставляемым значением: «когда последний раз были на комплексе P-3» → любой номер
_LAST_VISIT_RE = re.compile(r"^когда последний раз (?:\w+ )?(?:на|к) комплекс\w* ([\w\-+.]+)$")


_ENTITY_RE = re.compile(r"(комплекс\w*|апк|брига\w*|монтер\w*|исполнител\w*)")
_NOUN_RE = re.compile(r"(выезд\w*|ремонт\w*|задани\w*|поездк\w*)")


def _min_count_plan(question: str, today: dt.date) -> Optional[dict]:
    """«По каким комплексам было больше одного выезда?», «у каких бригад не меньше 3 ремонтов в сентябре» — сводка по комплексам/бригадам/исполнителям с отсечением по числу."""
    q = plan_cache.normalize(question)
    mc = min_count_in_question(q)
    ent, noun = _ENTITY_RE.search(q), _NOUN_RE.search(q)
    if not (mc and ent and noun) or not re.match(r"^(по каким|какие|у каких|на каких|у кого|кто)\b", q):
        return None
    group = "point_id" if ent.group(1).startswith(("комплекс", "апк")) else "crew" if ent.group(1).startswith("брига") else "executor"
    tasks_ds = noun.group(1).startswith("задани")
    spec = _spec("tasks" if tasks_ds else "visits", filters=(("repair", "=", "да"),) if noun.group(1).startswith("ремонт") else (), aggregates=(("count", ""),), group_by=(group,),
                 order_by=(("count", "desc"),))
    month = month_in_question(q, today)
    if month:
        spec["period"] = {"preset": "", "from": month, "to": month}
    else:
        for preset, words in _PRESET_WORDS.items():
            if re.search(words, q):
                spec["period"] = {"preset": preset, "from": "", "to": ""}
                break
    out = _fix_dataset(_clean_spec(spec))
    out["min_count"] = mc
    return out


def preset_plan(question: str, today: Optional[dt.date] = None) -> Optional[dict]:
    """Готовый разбор для частого вопроса (None — вопрос не из заранее разобранных)."""
    norm = plan_cache.normalize(question)
    for text, spec in PRESET_PLANS:
        if plan_cache.normalize(text) == norm:
            return _fix_dataset(_clean_spec(spec))
    mcp = _min_count_plan(question, today or dt.date.today())
    if mcp is not None:
        return mcp
    day = _DAY_TASKS_RE.match(norm)
    if day:                                                    # «какие задания стоят на завтра» — дата считается от сегодняшнего дня, поэтому разбор собирается здесь, а не лежит готовым
        when = understand.relative_period(norm, today or dt.date.today())
        return _fix_dataset(_clean_spec(_spec("tasks", preset="" if when else "today", frm=when[0] if when else "", to=when[1] if when else "",
                                              select=("date", "crew", "point_id", "address", "type"), order_by=(("crew", "asc"), ("point_id", "asc")))))
    m = _LAST_VISIT_RE.match(norm)
    if m:
        spec = _spec("points", filters=(("point_id", "contains", m.group(1)),), select=("point_id", "address", "status", "last_visit", "visits_count"))
        return _fix_dataset(_clean_spec(spec))
    return None


UNSUPPORTED_HINT = (
    "Примеры: «сколько было выездов по инвентаризации за сентябрь», «какие комплексы сейчас не работают», «покажи выезды за прошлую неделю, где нужна автовышка», "
    "«сколько ремонтов у каждой бригады в этом месяце», «кто в отпуске в октябре»."
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def execute(spec: dict, role: str, *, today: dt.date, source: str, max_rows: int = qe.MAX_ROWS_LIST) -> dict[str, Any]:
    """Готовый разбор → данные из базы → ответ (число или таблица), без участия модели. Вызывается и при новом вопросе, и при «Обновить» (повтор по тому же разбору):
    данные ВСЕГДА читаются заново, поэтому ответ показывает состояние системы на момент `computed_at`. `raw_spec` (с настоящими значениями условий) нужен для повтора и
    «из чего посчитано»; клиенту он не отдаётся (chat.py), `as_of` — день, на который считался период («сегодня» остаётся тем же днём при повторе)."""
    try:
        result = await qe.run(spec, role, today=today, max_rows=max_rows)
    except qe.AccessDenied as exc:
        return {"kind": "denied", "text": str(exc), "spec": _public_spec(spec), "source": source}
    except qe.QueryError as exc:
        return {"kind": "error", "text": str(exc), "spec": _public_spec(spec), "source": source}
    min_count = spec.get("min_count")
    if min_count and result.rows and len(result.columns) >= 2:
        result.rows = [r for r in result.rows if isinstance(r[-1], (int, float)) and r[-1] >= min_count]
        result.total = len(result.rows)
        result.description = f"{result.description} · только с числом не меньше {min_count}" + (" (из первых строк выборки)" if result.truncated else "")
    out = qe.render(result)
    out["spec"] = _public_spec(result.spec)
    out["dataset"] = result.dataset
    out["source"] = source
    out["computed_at"] = _now_iso()
    out["as_of"] = today.isoformat()
    out["raw_spec"] = spec
    return out


_DETAIL_SELECT = {
    "points": ("point_id", "address", "status", "power_source", "fault_reason", "fault_need", "down_since", "last_visit"),
    "visits": ("date", "point_id", "address", "visit_type", "status", "executor", "comment", "fault", "needs"),
}


def _context_line(history: Optional[list[dict]]) -> str:
    """Предыдущий обмен одной строкой — чтобы «а за прошлый месяц?» и «а почему?» были понятны и модели, пишущей ответ."""
    for item in reversed(history or []):
        q, a = str(item.get("question") or "").strip(), str(item.get("answer") or "").strip()
        if q and a:
            return f"«{q[:200]}» → «{a[:260]}»"
    return ""


def _understood(u: "understand.Understanding") -> str:
    bits = []
    if u.points:
        bits.append("комплексы: " + ", ".join(u.point_ids))
    if u.unknown_ids:
        bits.append("номера, которых нет в справочнике: " + ", ".join(i["token"] for i in u.unknown_ids))
    if u.month:
        bits.append(f"месяц: {u.month}")
    if u.intents:
        names = {"count": "нужно число", "list": "нужен список", "ranking": "нужен рейтинг", "compare": "нужно сравнение", "why": "нужна причина",
                 "when": "нужна дата", "aggregate": "нужна сводка"}
        bits.append("; ".join(names[i] for i in sorted(u.intents) if i in names))
    return "; ".join(b for b in bits if b)


def _when_phrase(u: "understand.Understanding", today: dt.date) -> str:
    n = u.norm
    return ("на послезавтра" if understand._DAY_AFTER.search(n) else "на завтра" if understand._TOMORROW.search(n) else "на следующей неделе" if understand._NEXT_WEEK.search(n)  # noqa: SLF001
            else "на сегодня" if "сегодня" in n else "на этой неделе")


_POINT_WORDS = re.compile(r"(комплекс|апк|точк)")
_NOT_POINT_WORDS = re.compile(r"(бригад|экипаж|монт[её]р|исполнител|\bсим\b|sim)")


def _asks_about_unknown_point(u: "understand.Understanding") -> bool:
    """Назван номер, которого нет в справочнике, и речь именно о комплексе («что с комплексом P-9», короткое «P-9?»); «Бр-3» (бригада) и подобное сюда не попадает."""
    return bool(u.unknown_ids) and not u.points and not _NOT_POINT_WORDS.search(u.norm) and bool(_POINT_WORDS.search(u.norm) or len(u.norm.split()) <= 3)


def _unknown_point(u: "understand.Understanding", today: dt.date) -> dict[str, Any]:
    """Номера нет в справочнике: модель на такое пишет «переформулировки» про несуществующий комплекс; программа говорит прямо и подсказывает похожие номера."""
    names = ", ".join(f"«{i['token']}»" for i in u.unknown_ids[:2])
    similar = [h for i in u.unknown_ids[:2] for h in i["hints"]][:4]
    text = (f"В справочнике нет комплекса {names}." + (f" Похожие номера: {', '.join(similar)}." if similar else "")
            + " Проверьте номер и спросите ещё раз — найду по правильному.")
    return {"kind": "text", "text": text, "narration": text, "source": "chat", "unsupported": True, "computed_at": _now_iso(), "as_of": today.isoformat()}


def _cannot_do(u: "understand.Understanding", today: dt.date) -> dict[str, Any]:
    """Просьба составить или изменить (план, задания, бригады): помощник только читает данные. Ответ собирает программа, а не модель: модель на такие просьбы пересказывает служебные
    подсказки и предлагает «переформулировки», повторяющие сам вопрос."""
    when = _when_phrase(u, today)
    text = (f"Составлять и менять план, задания и бригады я не умею — я только читаю то, что уже есть в системе. Могу показать, какие задания уже стоят {when} "
            f"(спросите «Какие задания {when}?»), или какие комплексы сейчас не работают и что им нужно для восстановления.")
    return {"kind": "text", "text": text, "narration": text, "source": "chat", "unsupported": True, "computed_at": _now_iso(), "as_of": today.isoformat()}


async def _chat_answer(question: str, role: str, u: "understand.Understanding", today: dt.date, context: str) -> dict[str, Any]:
    if understand._THANKS.match(u.norm) and len(u.norm.split()) <= 4:                # noqa: SLF001 — «спасибо» не требует модели (она путала падежи)
        text = "Пожалуйста! Если понадобится что-то ещё по комплексам, выездам или бригадам — спрашивайте."
        return {"kind": "text", "text": text, "narration": text, "source": "chat", "computed_at": _now_iso(), "as_of": today.isoformat()}
    if u.help_request:                                                                # «что ты умеешь» — по данным этой роли, без модели
        text = ("Здравствуйте! " if re.match(r"(привет|здравств|добр\w+ (день|утро|вечер)|хай|хелло)", u.norm) else "") + narrate.help_text(role)
        return {"kind": "text", "text": text, "narration": text, "source": "chat", "computed_at": _now_iso(), "as_of": today.isoformat()}
    progress.report(stage="Отвечаю…")
    text = await narrate.converse(question, role, today=today, context=context)
    if not text:
        thanks = understand._THANKS.match(u.norm)       # noqa: SLF001
        text = ("Пожалуйста! Спрашивайте ещё." if thanks else
                "Я помощник по данным системы: могу рассказать про комплексы (статус, причины неисправности, выезды), задания и бригады, оборудование и отсутствия. "
                "Спросите, например, «что с комплексом P-2» или «сколько комплексов сейчас не работает».")
    return {"kind": "text", "text": text, "narration": text, "source": "chat", "computed_at": _now_iso(), "as_of": today.isoformat()}


async def _dossier_answer(question: str, role: str, u: "understand.Understanding", today: dt.date, context: str) -> Optional[dict[str, Any]]:
    if not cat.role_can(role, "points"):
        return None
    progress.report(stage="Собираю сведения по комплексу…")
    card = await skills.dossier(u.point_ids, role)
    if not card["items"]:
        return None
    progress.report(stage="Пишу ответ…")
    if "when" in u.intents and all(not i.get("visits_count") for i in card["items"]):
        # «Когда был последний выезд» там, где выездов в отчётах нет: фразу собирает программа — модель выдавала «последний выезд не был»
        text = " ".join(f"По {i['point_id']} выездов в отчётах нет, поэтому дату последнего выезда назвать не могу." + (f" Сейчас статус комплекса: {i['status']}." if i.get("status") else "")
                        for i in card["items"][:3])
    else:
        text = await narrate.narrate_data(question, card["facts"], today=today, context=context)
    fallback = "\n".join(i["summary"] for i in card["items"])
    return {"kind": "dossier", "text": text or fallback, "narration": text or fallback, "narrated": bool(text), "cards": [_card_view(i) for i in card["items"]],
            "description": "Карточка комплекса, отчёты выездов, план", "source": "dossier", "computed_at": _now_iso(), "as_of": today.isoformat()}


def _card_view(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("point_id", "address", "category", "status", "down_since", "down_days", "power_source", "fault_reason", "fault_need", "fault_source", "visits_count", "planned", "groups", "summary")
    card = {k: item.get(k) for k in keep}
    last = item.get("last_visit")
    card["last_visit"] = (last.get("date") if isinstance(last, dict) else last) or ""          # в карточке проверки это целая запись выезда, странице нужна только дата
    return card


async def _evidence_rows(spec: dict, role: str, today: dt.date, limit: int = 12) -> Optional[dict[str, Any]]:
    """Записи под числом/сводкой — модель отвечает на «и на каких комплексах?», а человек видит, из чего получилось."""
    ds = cat.DATASETS.get(spec.get("dataset", ""))
    if ds is None:
        return None
    ev = {k: v for k, v in spec.items() if k != "min_count"}
    ev.update(aggregates=[], group_by=[], order_by=[], select=list(_DETAIL_SELECT.get(ds.name) or ds.default_select), limit=limit)
    try:
        result = await qe.run(ev, role, today=today, max_rows=limit)
    except qe.QueryError:
        return None
    if not result.rows:
        return None
    return {"columns": result.columns, "rows": result.rows, "total": result.total}


async def _narrate_answer(question: str, role: str, out: dict[str, Any], spec: dict, today: dt.date, context: str, source: str) -> dict[str, Any]:
    """Дописывает к ответу движка ответ словами: по существу вопроса, с деталями (кто/какие/почему), числа сверены с данными."""
    fallback = narrate.fallback_text(out)
    out["narration"] = fallback
    out["narrated"] = False
    if source == "preset":                                  # частые вопросы-кнопки отвечаются мгновенно, без модели
        return out
    grouped = narrate.grouped_text(out) if spec.get("group_by") else None
    if grouped:                                             # «сколько по каждому статусу» — перечень и итог точнее и быстрее собирает программа
        out["narration"], out["narrated"] = grouped, True
        return out
    try:
        evidence = None
        # Примеры записей под числом — только когда просят перечень, причину или дату: к вопросу «сколько всего» они лишние, и модель по ним домысливала («все 12 примеров не работают → не работают все»)
        wants_rows = bool(understand._LIST_WORDS.search(plan_cache.normalize(question)) or re.search(r"(когда|почему|причин|\bи (на|у|в|по) )", plan_cache.normalize(question)))  # noqa: SLF001
        if (out.get("kind") == "number" or (out.get("kind") == "table" and spec.get("group_by"))) and wants_rows:
            evidence = await _evidence_rows(spec, role, today)
        progress.report(stage="Пишу ответ…")
        facts = narrate.facts_from_answer(out, evidence)
        text = await narrate.narrate_data(question, facts, today=today, context=context)
    except client.AiUnavailable:
        return out
    if text:
        out["narration"], out["narrated"] = text, True
    return out


_POINT_REF = re.compile(r"\b(у него|у нее|у неё|него|нее|неё|его|ее|её|этот комплекс|этого комплекса|этом комплексе|этому комплексу|тот комплекс|того комплекса|тот же|этот же|там|туда|оттуда)\b")


async def _resolve_point_reference(question: str, u: "understand.Understanding", history: Optional[list[dict]], today: dt.date) -> tuple[str, "understand.Understanding"]:
    """«А когда там был последний выезд?», «а у него какое питание?» — номер комплекса не назван, но речь о том, о котором только что спрашивали. Номер берём из прошлого вопроса (условие point_id
    в его разборе или номер в тексте) и дописываем к вопросу — дальше он понятен и программе, и модели. Нет прошлого комплекса или в вопросе уже есть номер — вопрос не меняется."""
    if (u.points or u.unknown_ids or not history or not _POINT_REF.search(u.norm) or len(u.norm.split()) > 10
            or understand._AGG_WORDS.search(u.norm)):          # noqa: SLF001 — «сколько там выездов за месяц» про все комплексы, а не про прошлый
        return question, u
    for item in reversed(history):
        pid = ""
        for flt in (item.get("spec") or {}).get("filters") or []:
            if flt.get("field") == "point_id" and str(flt.get("value") or "").strip():
                pid = str(flt["value"]).strip()
                break
        if not pid:
            prev = await understand.analyze(str(item.get("question") or ""), today)
            if len(prev.points) == 1:
                pid = prev.points[0]["id"]
        if pid:
            resolved = f"{question.rstrip(' ?.!')} (речь о комплексе {pid})"
            return resolved, await understand.analyze(resolved, today)
    return question, u


FILE_KINDS = ("table", "number", "empty", "agent", "dossier", "text")      # виды ответов, из которых можно сделать файл (отказ и ошибка — нет)


async def answer(question: str, role: str, *, history: Optional[list[dict]] = None, today: Optional[dt.date] = None, mode: str = "auto") -> dict[str, Any]:
    """Ответ на вопрос (см. `_answer`); если в вопросе просят файл («выгрузи в Excel», «сделай справку в Word») — вопрос разбирается без слов про формат (память разборов общая с обычным
    вопросом), а в ответе стоит `file_request` ('xlsx' | 'docx'): файл собирает страница ответа на сервере (routes_assistant → export_doc.py) из тех же данных."""
    fmt = understand.wanted_file(question)
    out = await _answer(understand.strip_file_words(question) if fmt else question, role, history=history, today=today, mode=mode)
    if fmt and out.get("kind") in FILE_KINDS and not out.get("unsupported"):
        out["file_request"] = fmt
    return out


async def _answer(question: str, role: str, *, history: Optional[list[dict]] = None, today: Optional[dt.date] = None, mode: str = "auto") -> dict[str, Any]:
    """Полный ответ на вопрос: {"kind": text|dossier|number|table|agent|empty|denied|error, "text", "narration" (ответ словами), "description", "spec", "source", "computed_at", …}.
    Порядок: программа понимает вопрос (комплексы, период, цель — understand.py) → приветствие и «что умеешь» — обычный разговор; «что с P-2», «почему не работает P-2» — досье комплекса
    (skills.py); остальное — модель планирует запрос к данным (готовый разбор / память / модель, с подсказками программы и одной исправляющей попыткой при ошибке), данные считает программа,
    а модель отвечает на вопрос словами по этим данным (narrate.py; числа сверяются). Вопросы-объяснения («почему…») — режим «агент» (agent.py).
    source: preset — готовый разбор частого вопроса, cache — разбор запомнен, model — разобрала модель, dossier/chat/agent — см. выше. Данные считаются заново всегда."""
    today = today or dt.date.today()
    context = _context_line(history)
    progress.report(stage="Понимаю вопрос…")
    u = await understand.analyze(question, today)
    question, u = await _resolve_point_reference(question, u, history, today)         # «а когда там был последний выезд?» → о каком комплексе, берём из прошлого вопроса
    if u.write_request:
        return _cannot_do(u, today)
    if _asks_about_unknown_point(u):
        return _unknown_point(u, today)
    if u.smalltalk:
        return await _chat_answer(question, role, u, today, context)
    if u.dossier and mode != "plain" and preset_plan(question, today) is None:          # готовый разбор («когда последний раз были на P-3») мгновенный — досье ему не нужно
        card = await _dossier_answer(question, role, u, today, context)
        if card is not None:
            return card
    follow = plan_cache.is_followup(question)          # «а за прошлый месяц?» зависит от прошлого вопроса — только через модель с историей
    spec: Optional[dict] = None
    source = "model"
    if not follow:
        spec = preset_plan(question, today)
        source = "preset"
        if spec is None:
            spec = await plan_cache.lookup(question, PLAN_VERSION, today)
            source = "cache"
            if spec is not None:
                spec = apply_guardrails(spec, question, today)             # запомненный разбор проходит страховку заново: правила дополняются, а даты («завтра») считаются от сегодняшнего дня
    hints = understand.planner_hints(u)
    if spec is None:
        source = "model"
        progress.report(stage="Составляю запрос к данным…")
        # Самостоятельному вопросу прошлые разборы не нужны (и подсказка короче → быстрее: кэш подсказки в модели не ломается чужими вопросами)
        planned = await plan(question, history=history if follow else None, today=today, hints=hints)
        if not planned["can_answer"]:
            return await _unsupported(question, role, u, planned.get("reason", ""), today)
        spec = planned["spec"]
    progress.report(stage="Достаю данные…")
    out = await execute(spec, role, today=today, source=source)
    if out["kind"] == "error" and source == "model":
        # одна исправляющая попытка: модель видит, чем кончилась прошлая («не нашёл значение …, бывает: …»), и пишет запрос заново
        progress.report(stage="Исправляю запрос…")
        retry = await plan(question, history=history if follow else None, today=today, hints=hints, feedback=out.get("text") or "")
        if retry["can_answer"]:
            out2 = await execute(retry["spec"], role, today=today, source=source)
            if out2["kind"] not in ("error", "denied"):
                spec, out = retry["spec"], out2
    if out["kind"] == "empty":
        alt_spec = _empty_fallback(spec)
        if alt_spec is not None:
            progress.report(stage="Ищу в отчётах о выездах…")
            alt = await execute(alt_spec, role, today=today, source=source)
            if alt["kind"] in ("number", "table"):
                spec, out = alt_spec, alt
                out["description"] = ((out.get("description") or "").rstrip(".") + " (в истории смен статуса записей нет — посчитано по отчётам о выездах с итогом «Не работает»)").strip()
    if out["kind"] in ("denied", "error"):
        return out
    if source == "model" and not follow:
        try:
            await plan_cache.store(question, spec, PLAN_VERSION)
        except Exception:  # noqa: BLE001 — запомнить не удалось: ответ от этого не страдает
            logger.exception("Не удалось запомнить разбор вопроса")
    if agent.wanted(question, mode, out):
        try:
            return await agent.refine(question, role, out, today=today)
        except client.AiUnavailable:
            raise
        except client.AiError as exc:                  # разбор записей не удался на полпути — дальше обычный ответ по этим же записям
            logger.warning("Агент не закончил разбор: %s", exc)
            out["agent_error"] = f"Подробный разбор не удался ({exc}) — вот сами записи."
    return await _narrate_answer(question, role, out, spec, today, context, source)


def _empty_fallback(spec: dict) -> Optional[dict]:
    """История смен статуса пуста («какой комплекс чаще всего ломается» на базе без истории) — тот же вопрос по отчётам о выездах с итогом «Не работает»: такие данные есть всегда."""
    if spec.get("dataset") != "status_changes":
        return None
    visits = cat.DATASETS["visits"]
    group_by = list(spec.get("group_by") or [])
    if any(visits.field(str(g).partition(":")[0]) is None for g in group_by):
        return None
    filters = [f for f in spec.get("filters") or [] if f.get("field") in ("point_id", "category", "date") and visits.field(f["field"]) is not None]
    filters.append({"field": "status", "op": "=", "value": "Не работает", "values": []})
    aggregated = bool(spec.get("aggregates") or group_by)
    return {"dataset": "visits", "period": spec.get("period") or {"preset": None, "from": None, "to": None}, "filters": filters,
            "aggregates": [{"fn": "count", "field": None, "alias": ""}] if aggregated else [], "group_by": group_by,
            "select": [] if aggregated else ["date", "point_id", "address"],
            "order_by": [{"field": "count", "dir": "desc"}] if aggregated and group_by else [{"field": "date", "dir": "desc"}] if not aggregated else [],
            "limit": int(spec.get("limit") or 0)}


async def _unsupported(question: str, role: str, u: "understand.Understanding", reason: str, today: dt.date) -> dict[str, Any]:
    """Запрос не получился: вместо шаблона с примерами модель объясняет, что поняла и чего не хватает, предлагая переформулировки именно к этому вопросу."""
    progress.report(stage="Думаю, чем помочь…")
    text = None
    try:
        text = await narrate.clarify(question, role, understood=_understood(u), reason=reason, today=today)
    except client.AiUnavailable:
        raise
    if not text:
        text = (reason + " " if reason else "") + "Уточните, пожалуйста: о каком комплексе, бригаде или периоде идёт речь и что именно нужно — число, список или причина?"
    return {"kind": "text", "text": text, "narration": text, "source": "model", "unsupported": True, "computed_at": _now_iso(), "as_of": today.isoformat()}


def _sig_rows(answer: dict[str, Any]) -> list[str]:
    return [" | ".join("" if c is None else str(c) for c in row) for row in (answer.get("rows") or [])]


def compare(old: dict[str, Any], new: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Что изменилось в данных между двумя ответами на один и тот же запрос: {"changed", "was", "now", "added", "removed"} или None (виды ответов не сравнить).
    Число — само значение; таблица — число строк и какие строки (по первому столбцу) появились или исчезли."""
    ok, nk = old.get("kind"), new.get("kind")
    if ok == "number" and nk == "number":
        return {"changed": str(old.get("value")) != str(new.get("value")), "was": old.get("value"), "now": new.get("value"), "added": [], "removed": []}
    if ok in ("table", "empty") and nk in ("table", "empty"):
        a, b = _sig_rows(old), _sig_rows(new)
        was, now = (old.get("total") or len(a)) if ok == "table" else 0, (new.get("total") or len(b)) if nk == "table" else 0
        if a == b and was == now:
            return {"changed": False, "was": was, "now": now, "added": [], "removed": []}
        sa, sb = set(a), set(b)
        return {"changed": True, "was": was, "now": now, "added": [x.split(" | ")[0] for x in b if x not in sa][:10],
                "removed": [x.split(" | ")[0] for x in a if x not in sb][:10]}
    return None


def evidence_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """«Из чего посчитано»: тот же запрос без подсчёта и группировки — сами записи (основные столбцы набора, до 200 строк)."""
    ds = cat.DATASETS.get(spec.get("dataset", ""))
    out = {k: v for k, v in spec.items() if k != "min_count"}
    out.update(aggregates=[], group_by=[], order_by=[], select=list(ds.default_select) if ds else [], limit=200)
    return out


async def evidence(raw_spec: dict[str, Any], role: str, *, today: dt.date) -> dict[str, Any]:
    out = await execute(evidence_spec(raw_spec), role, today=today, source="evidence")
    out.pop("raw_spec", None)
    return out


def _public_spec(spec: dict) -> dict:
    """Описание запроса для показа/истории: значения чувствительных полей (пароли) скрыты."""
    ds = cat.DATASETS.get(spec.get("dataset", ""))
    out = json.loads(json.dumps(spec, ensure_ascii=False))
    if ds:
        for f in out.get("filters") or []:
            fd = ds.field(f.get("field", ""))
            if fd is not None and fd.sensitive:
                f["value"], f["values"] = "…", []
    return out
