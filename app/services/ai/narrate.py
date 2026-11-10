"""Ответ человеческими словами: модель пишет короткий ответ на вопрос ПО ДАННЫМ, которые программа уже достала из базы по правам роли.

Раньше ответом была сама выдача: «Количество (выездов): 4» или «Не понял вопрос. Примеры: …» — формально верно, но человеку, спросившему «почему не работает P-2 и что нужно», такое
ничего не говорит. Теперь вопрос и данные попадают к модели вместе, и она отвечает на ЭТОТ вопрос («P-2 не работает с 3 октября: сгорел блок питания; нужны блок 12 В и автовышка»).
Числа остаются за программой: цифры из текста модели сверяются с данными, и если модель что-то выдумала, берётся простой текст, собранный программой (fallback_text), — неправды не будет.

Три режима: narrate_data (ответ по данным), converse (приветствие, «что ты умеешь», благодарность, просьбы не по данным), clarify (запрос не удалось превратить в данные — модель
честно говорит, что поняла и чего не хватает, а не выдаёт шаблон с примерами). Таблица с записями при этом всегда показывается рядом — любое утверждение можно сверить."""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Optional

from app.services.ai import catalog as cat
from app.services.ai import client

logger = logging.getLogger(__name__)

MAX_ROWS = 10
ROW_CHARS = 190
FACT_CHARS = 2400

SYSTEM_DATA = (
    "Ты — помощник диспетчера системы «Календарь работ» (обслуживание комплексов видеофиксации, АПК). Тебе дают вопрос и ДАННЫЕ из системы; напиши ответ на этот вопрос по-русски, простыми словами, "
    "как коллега, 1–4 коротких предложения. Начни сразу с сути (что именно спросили), потом добавь важные детали из данных: какие комплексы, когда, почему, что нужно. "
    "Используй только данные; числа, номера комплексов и даты копируй точно, ничего не выдумывай и не округляй. Описывай ровно то, что написано в строке «Как посчитано»: "
    "если там нет условия (статуса, питания, периода), не приписывай его — примеры записей показывают лишь несколько строк, а не всю картину; общее число — в строке «Всего найдено» или «Результат». "
    "Согласуй слово с числом («1 комплекс, 2 комплекса, 5 комплексов»; «не работает 1», «не работают 5»). "
    "Если данных нет — так и скажи и подскажи, как уточнить вопрос. Не повторяй вопрос, не пиши «согласно данным», не извиняйся."
)
SHOTS_DATA = [
    ("Вопрос: Сколько комплексов не работает?\n\nДанные:\nКак посчитано: Комплексы · Статус = «Не работает».\nРезультат: Количество (комплексов): 2.\nИз каких записей (всего 2):\n"
     "1) Номер комплекса: P-2; Адрес: ул. Вторая 2; Статус: Не работает; Почему не работает: сгорел блок питания; Что нужно для восстановления: блок питания 12 В, автовышка\n"
     "2) Номер комплекса: P-7; Адрес: ул. Седьмая 7; Статус: Не работает; Почему не работает: обрыв линии",
     "Сейчас не работает 2 комплекса: P-2 (сгорел блок питания, нужны блок 12 В и автовышка) и P-7 (обрыв линии)."),
    ("Вопрос: Нужна ли автовышка?\n\nДанные:\nВсего найдено записей: 0.",
     "По отчётам нигде не указано, что нужна автовышка."),
    ("Вопрос: Кто в отпуске?\n\nДанные:\nВсего найдено записей: 1.\n1) Кто отсутствует: Иванов И.; С какого числа: 28.09.2026; По какое число: 05.10.2026",
     "В отпуске Иванов И. — с 28.09.2026 по 05.10.2026."),
    ("Вопрос: Сколько всего комплексов?\n\nДанные:\nКак посчитано: Комплексы.\nРезультат: Количество (комплексов): 122.",
     "Всего в системе 122 комплекса."),
    ("Вопрос: Когда был последний выезд на P-7?\n\nДанные:\nКомплекс P-7 (Камера), адрес: ул. Седьмая 7.\nСтатус сейчас: Не работает (нет связи с 2026-10-03, 13 дн.).\n"
     "Причина в отчётах не указана.\nВыездов по отчётам не было.\nОписание в карточке: Инвентаризация.",
     "По P-7 выездов в отчётах нет, поэтому дату последнего выезда назвать не могу. Сейчас комплекс не работает: нет связи с 03.10.2026 (13 дн.), причина в отчётах не указана."),
    ("Вопрос: Что с комплексом P-5?\n\nДанные:\nКомплекс P-5 (Камера), адрес: ул. Пятая 5.\nСтатус сейчас: Не работает (нет связи с 2026-10-12, 4 дн.).\nПитание: СЭС.\n"
     "Почему не работает (из отчёта): сгорел блок питания.\nЧто нужно для восстановления: блок питания 12 В, автовышка.\nВыездов по отчётам: 2. Последние:\n"
     "  - 11.09.2026, Ремонт, итог «Работает», Петров П.; сделано: заменили кабель",
     "P-5 (ул. Пятая 5) не работает уже 4 дня, с 12.10.2026: сгорел блок питания. Для восстановления нужны блок питания 12 В и автовышка. Питание комплекса — солнечное (СЭС). "
     "Последний выезд был 11.09.2026 (ремонт, заменили кабель)."),
]
SYSTEM_CHAT = (
    "Ты — помощник диспетчера системы «Календарь работ» (обслуживание комплексов видеофиксации, АПК). Ты отвечаешь коротко, дружелюбно и по делу, по-русски. "
    "Если человек здоровается — поздоровайся в ответ и спроси, чем помочь. Если благодарит — ответь одной короткой фразой («Пожалуйста! Обращайтесь.»), без списков и примеров. "
    "Если спрашивает, что ты умеешь, — своими словами перечисли, о чём можешь рассказать по данным системы (список в сообщении), и приведи 2–3 примера вопросов. "
    "Если просит то, чего ты не умеешь (менять данные, составлять расписание, прогноз погоды, советы вне системы), — честно скажи, что этого не умеешь, и предложи, что можешь сделать по данным. "
    "Ничего не выдумывай про конкретные комплексы и числа — у тебя нет данных в этом сообщении."
)
SYSTEM_CLARIFY = (
    "Ты — помощник диспетчера системы «Календарь работ». Тебе задали вопрос, который не удалось превратить в запрос к данным. Ответь по-человечески: скажи, что именно ты поняла из вопроса, "
    "чего не хватает (какого комплекса, периода, какой информации), и предложи 2–3 конкретных переформулировки, подходящих к ИМЕННО этому вопросу. Не выдавай шаблонный список примеров. "
    "Ничего не выдумывай про данные: их у тебя в этом сообщении нет."
)


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def row_line(columns: list[dict[str, Any]], row: list[Any]) -> str:
    parts = []
    for i, (col, value) in enumerate(zip(columns, row)):
        text = _short(value, 110)
        if not text and i == 0:
            text = "(не указано)"                      # пустое значение ключа («статус не задан») — иначе строка теряет название, и модель домысливает его
        if text:
            parts.append(f"{str(col.get('label') or col.get('key') or '').split(' (')[0]}: {text}")
    return _short("; ".join(parts), ROW_CHARS)


def grouped_text(answer: dict[str, Any]) -> Optional[str]:
    """Сводка «сколько по каждому …» (4–15 строк, последний столбец — количество) пишется программой: перечислить все значения и сложить итог она умеет точнее и мгновенно, а модель
    пропускала строки и придумывала названия пустым значениям. None — таблица не такого вида (тогда ответ пишет модель)."""
    cols, rows = answer.get("columns") or [], answer.get("rows") or []
    if answer.get("kind") != "table" or len(cols) < 2 or not 4 <= len(rows) <= 15 or answer.get("truncated") or (answer.get("total") or len(rows)) > len(rows):
        return None
    label = str(cols[-1].get("label") or "")
    if not label.startswith("Количество") or not all(isinstance(r[-1], (int, float)) and not isinstance(r[-1], bool) for r in rows):
        return None
    unit = label.partition("(")[2].rstrip(") ").strip()
    keys = ", ".join(f"«{str(c.get('label') or '')}»" for c in cols[:-1])
    items = "; ".join(f"{' / '.join(str(v) if v not in (None, '') else '(не указано)' for v in r[:-1])} — {r[-1]}" for r in rows)
    total = sum(r[-1] for r in rows)
    return f"Количество {unit} по {keys}: {items}. Всего: {total}."


_DATE_CELL = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def order_note(columns: list[dict[str, Any]], rows: list[list[Any]]) -> str:
    """Как упорядочены записи по дате: модель на «когда был ПОСЛЕДНИЙ выезд» берёт первую попавшуюся строку, если ей не сказать, где самая новая."""
    idx = next((i for i, c in enumerate(columns) if str(c.get("label") or "").lower().startswith("дата")), None)
    if idx is None or len(rows) < 2:
        return ""
    keys = []
    for row in rows:
        m = _DATE_CELL.match(str(row[idx] if idx < len(row) else ""))
        if not m:
            return ""
        keys.append((m.group(3), m.group(2), m.group(1)))
    if keys == sorted(keys, reverse=True):
        return " Записи идут от самой новой к самой старой: первая — самая новая."
    if keys == sorted(keys):
        return " Записи идут от самой старой к самой новой: последняя — самая новая."
    return ""


def facts_from_answer(answer: dict[str, Any], evidence: Optional[dict[str, Any]] = None) -> str:
    """Данные ответа движка для модели: число и/или записи, подпись «как посчитано», сколько всего."""
    lines: list[str] = []
    if answer.get("description"):
        lines.append(f"Как посчитано: {answer['description']}.")
    kind = answer.get("kind")
    if kind == "number":
        lines.append(f"Результат: {answer.get('label')}: {answer.get('value')}.")
    elif kind == "table":
        total = answer.get("total") or len(answer.get("rows") or [])
        shown = len(answer["rows"])
        lines.append(f"Всего найдено записей: {total}." + (f" Ниже для примера показаны только первые {shown} — это НЕ общее число." if total > shown else "")
                     + order_note(answer["columns"], answer["rows"]))
        for i, row in enumerate((answer.get("rows") or [])[:MAX_ROWS], 1):
            lines.append(f"{i}) {row_line(answer['columns'], row)}")
        if len(answer.get("rows") or []) > MAX_ROWS:
            lines.append(f"… и ещё {len(answer['rows']) - MAX_ROWS} (см. таблицу).")
    elif kind == "empty":
        lines.append("Результат: по этим условиям записей нет.")
    if evidence and evidence.get("rows"):
        total = evidence.get("total") or len(evidence["rows"])
        lines.append(f"Из каких записей получено (всего {total}):" + order_note(evidence["columns"], evidence["rows"]))
        for i, row in enumerate(evidence["rows"][:MAX_ROWS], 1):
            lines.append(f"{i}) {row_line(evidence['columns'], row)}")
        if len(evidence["rows"]) > MAX_ROWS:
            lines.append(f"… и ещё {len(evidence['rows']) - MAX_ROWS}.")
    text = "\n".join(lines)
    return text if len(text) <= FACT_CHARS else text[:FACT_CHARS] + "…"


_DIGITS = re.compile(r"\d+")


def numbers_in(text: str) -> set[str]:
    return set(_DIGITS.findall(text or ""))


def numbers_grounded(narration: str, facts: str, question: str) -> bool:
    """Все числа в тексте модели есть в данных (или в вопросе): иначе модель что-то придумала. Однозначные числа и годы допускаются свободно (счёт «один», «2026»)."""
    allowed = numbers_in(facts) | numbers_in(question)
    for n in numbers_in(narration):
        if n in allowed or len(n) == 1 or (len(n) == 4 and n.startswith(("19", "20"))):
            continue
        # число могло получиться как «0» + дата: 03 → 3
        if n.lstrip("0") in allowed or any(a.lstrip("0") == n.lstrip("0") for a in allowed):
            continue
        return False
    return True


_ID_IN_TEXT = re.compile(r"(?<![\w-])[A-Za-zА-Яа-яЁё]{1,6}-\d[\w\-]*")


def ids_grounded(narration: str, facts: str, question: str) -> bool:
    """Все номера вида «P-3», «Бр-2», названные моделью, есть в данных или в вопросе: модель любит «добавить» комплекс, которого в выдаче нет."""
    haystack = f"{facts}\n{question}".casefold()
    return all(token.casefold() in haystack for token in _ID_IN_TEXT.findall(narration or ""))


# Слова-условия, которые модель любит «приписывать» ответу (все примеры оказались неработающими → «не работают все»): появились в тексте — должны быть и в данных или в вопросе.
_QUALIFIERS = ("неработ", "в отпуск", "больнич", "на складе", "сэс", "солнечн", "электросет", "автовышк", "демонтаж", "демонтир", "инвентаризац")


def qualifiers_grounded(narration: str, facts: str, question: str) -> bool:
    norm = lambda s: (s or "").casefold().replace("не работ", "неработ")  # noqa: E731 — «не работает» и «неработающие» — одно и то же
    hay, text = norm(f"{facts}\n{question}"), norm(narration)
    return all(q in hay for q in _QUALIFIERS if q in text)


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-z]*\n?|```$", "", text).strip()
    return text


def _usable(text: str) -> bool:
    return len(text) >= 6 and not text.lstrip().startswith(("{", "[")) and "<think>" not in text


def capabilities(role: str) -> str:
    return "\n".join(f"- {ds.label.split(' (')[0]}" for ds in cat.role_datasets(role))


async def _ask(system: str, user: str, *, max_tokens: int, priority: int, shots: Optional[list[tuple[str, str]]] = None) -> Optional[str]:
    history = [m for q, a in (shots or []) for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]
    try:
        text = _clean(await client.llm_chat(system, user, max_tokens=max_tokens, temperature=0.1, priority=priority, history=history or None))
    except client.AiUnavailable:
        raise
    except client.AiError as exc:
        logger.warning("Модель не написала ответ словами: %s", exc)
        return None
    return text if _usable(text) else None


def date_note(today: dt.date) -> str:
    return f"(Сегодня {today.strftime('%d.%m.%Y')}.)"


_AGREE = re.compile(r"(\d+)(\s+(?:комплекс\w*|апк|выезд\w*|задани\w*|бригад\w*)\s+(?:не\s+)?)работает\b", re.IGNORECASE)


def fix_agreement(text: str) -> str:
    """«2599 комплексов не работает» → «не работают»: небольшая модель согласует глагол с числом через раз; исправление точечное и не меняет смысл."""
    def sub(m: re.Match) -> str:
        n = int(m.group(1))
        singular = n % 10 == 1 and n % 100 != 11
        return m.group(0) if singular else f"{m.group(1)}{m.group(2)}работают"
    return _AGREE.sub(sub, text or "")


def data_prefix() -> tuple[str, list[dict[str, str]]]:
    """Постоянное начало запроса «ответь по данным» (инструкция + образцы): его «прогревает» warmup, чтобы первый ответ после запуска не читал его с нуля."""
    return SYSTEM_DATA, [m for q, a in SHOTS_DATA for m in ({"role": "user", "content": q}, {"role": "assistant", "content": a})]


async def narrate_data(question: str, facts: str, *, today: dt.date, context: str = "", priority: int = client.PRIORITY_USER) -> Optional[str]:
    """Ответ на вопрос по данным; None — модель ничего годного не написала или выдумала числа (вызывающий возьмёт fallback_text)."""
    # Пустая выдача: сегодняшняя дата ни к чему — модель иначе пишет «за сегодня нет данных», хотя период в вопросе не назывался
    # Дата нужна модели только когда вопрос про время («сегодня», «сейчас», «давно»); иначе она копирует «Сегодня 16.10.2026» в ответ
    needs_date = bool(re.search(r"(сегодня|вчера|завтра|сейчас|недел|месяц|давно|дней|дня\b|года|числа)", question.casefold()))
    note = "" if "записей нет" in facts or not needs_date else f"\n\n{date_note(today)}"
    user = (f"Вопрос: {question}\n" + (f"Предыдущий обмен (для понимания «а за прошлый месяц?» и подобного): {context}\n" if context else "")
            + f"\nДанные:\n{facts}{note}")
    known = f"{facts}\n{context}\n{date_note(today)}"            # что модель видела в запросе: числа из прошлого обмена и сегодняшней даты — не выдумка
    for attempt in range(2):
        text = await _ask(SYSTEM_DATA + ("" if attempt == 0 else " ВАЖНО: в прошлый раз ты написал то, чего нет в данных (число, номер комплекса или условие вроде «не работают»). Используй ТОЛЬКО числа, номера и условия из данных."), user,
                          max_tokens=320, priority=priority, shots=SHOTS_DATA)
        if text and numbers_grounded(text, known, question) and ids_grounded(text, known, question) and qualifiers_grounded(text, facts, question):
            return fix_agreement(text)
        if text:
            logger.info("Ответ модели отброшен: числа, номера или условия не из данных (попытка %s): %s", attempt + 1, text[:120])
    return None


SHOTS_CLARIFY = [
    ("Составь план работ на завтра",
     "Составлять и менять расписание я не умею — я только читаю данные системы. Зато могу показать, какие задания уже стоят на завтра у бригад (спросите «какие задания на завтра»), "
     "или рассказать, какие комплексы сейчас не работают и что им нужно для восстановления."),
]
SHOTS_CHAT = [
    ("Привет!", "Здравствуйте! Чем помочь: рассказать про комплексы, выезды, бригады или оборудование?"),
    ("Спасибо", "Пожалуйста! Обращайтесь."),
]


_HELP_EXAMPLES = {                                  # по одному примеру на набор данных, в порядке важности для диспетчера
    "points": "«Что с комплексом P-2?»",
    "visits": "«Сколько ремонтов было в сентябре и на каких комплексах?»",
    "tasks": "«Какие задания стоят на завтра?»",
    "absences": "«Кто сейчас в отпуске?»",
    "equipment": "«Сколько роутеров на складе?»",
}


def help_text(role: str) -> str:
    """«Что ты умеешь» — по списку данных, доступных именно этой роли, и с примерами к нему; программа собирает сама: быстрее модели и без выдумок."""
    datasets = cat.role_datasets(role)
    names = [ds.label.split(" (")[0] for ds in datasets]
    lowered = [n[:1].lower() + n[1:] for n in names]
    allowed = {ds.name for ds in datasets}
    examples = [text for name, text in _HELP_EXAMPLES.items() if name in allowed][:4]
    return ("Я помощник по данным системы «Календарь работ»: читаю то, что в ней есть, и отвечаю обычными словами. Могу рассказать про " + ", ".join(lowered) + ". "
            + ("Спросите, например: " + ", ".join(examples) + "." if examples else "Спросите о том, что есть в системе.")
            + " Результат могу отдать файлом: добавьте к вопросу «в Excel» или «в Word» — например, «выгрузи неработающие комплексы в Excel» или «сделай справку по неработающим комплексам в Word»."
            + " Составлять и менять данные я не умею.")


async def converse(question: str, role: str, *, today: dt.date, context: str = "", priority: int = client.PRIORITY_USER) -> Optional[str]:
    system = SYSTEM_CHAT + "\n\nЧто ты умеешь (данные системы, доступные собеседнику):\n" + capabilities(role)
    user = (f"(Предыдущий обмен: {context})\n" if context else "") + question
    return await _ask(system, user, max_tokens=260, priority=priority, shots=SHOTS_CHAT)


async def clarify(question: str, role: str, *, understood: str, reason: str = "", today: dt.date, priority: int = client.PRIORITY_USER) -> Optional[str]:
    system = (SYSTEM_CLARIFY + "\n\nЧто есть в системе (доступно собеседнику):\n" + capabilities(role)
              + f"\n\nСлужебная справка для тебя (собеседнику её не пересказывай): из вопроса распознано — {understood or 'ничего определённого'}."
              + (f" Запрос к данным не получился: {reason}." if reason else ""))
    return await _ask(system, question, max_tokens=300, priority=priority, shots=SHOTS_CLARIFY)


# ---------------------------------------------------------------- запасной текст, собранный программой (без модели)

def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    return many if 11 <= m100 <= 14 else one if m10 == 1 else few if 2 <= m10 <= 4 else many


def fallback_text(answer: dict[str, Any]) -> str:
    """Короткая фраза по результату — когда модель недоступна, ничего годного не написала или ответ мгновенный (готовый разбор / память): без шаблонов-примеров, по существу выдачи."""
    kind = answer.get("kind")
    how = (answer.get("description") or "").strip()
    suffix = f" ({how})" if how else ""
    if kind == "number":
        label = str(answer.get("label") or "").split(": ")[-1].replace("Количество (", "").rstrip(")")
        return f"Получилось {answer.get('value')}" + (f" — {label}" if label and label != str(answer.get("value")) else "") + suffix + "."
    if kind == "table":
        rows = answer.get("rows") or []
        total = answer.get("total") or len(rows)
        cols = answer.get("columns") or []
        head = f"Нашлось записей: {total}."
        if rows and cols:
            first = ", ".join(row_line(cols, r).split("; ")[0].split(": ", 1)[-1] for r in rows[:5])
            head += f" Первые: {first}."
        return head + (f" Показаны первые {len(rows)}." if answer.get("truncated") else "") + suffix
    if kind == "empty":
        return "По этим условиям ничего не нашлось" + suffix + ". Попробуйте изменить период или условия."
    return str(answer.get("text") or "")
