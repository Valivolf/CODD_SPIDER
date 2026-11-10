"""Режим «агент» помощника: модель сама читает данные по частям и дописывает ответ.

Обычный путь (qa.py) — модель только ПЛАНИРУЕТ запрос, а ответ — число или таблица, посчитанные программой. Но на вопросы «почему не работает…», «что нужно для восстановления…»,
«кратко опиши, что было на этих комплексах» таблицы мало: нужно прочитать записи (причины, комментарии) и сложить из них связный ответ. Записей может быть много, а модель на процессоре
читает медленно и помнит ограниченно, поэтому записи отдаются МАЛЕНЬКИМИ ПАЧКАМИ, как делает человек: прочитал пачку — написал ответ; прочитал следующую — дополнил ответ. Если
модели не хватает данных, она сама просит их словами («какие причины у комплекса P-3 за сентябрь») — запрос превращается в обычный (qa.plan → query_engine, по правам роли) и
следующая пачка берётся уже из него. Не больше MAX_BATCHES пачек и MAX_EXTRA_QUERIES дополнительных запросов.

Числа и записи программа не отдаёт на откуп модели: рядом с текстом всегда «Просмотрено записей: N из M» и таблица самих записей, чтобы любое утверждение можно было сверить.
Ход (какую пачку читает, что уже написано) виден человеку в переписке сразу (progress.py)."""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections import deque
from typing import Any, Optional

from app.services.ai import client, plan_cache, progress
from app.services.ai import query_engine as qe

logger = logging.getLogger(__name__)

BATCH_ROWS = 8
MAX_BATCHES = 6
MAX_EXTRA_QUERIES = 2
CELL_MAX = 220
ROW_MAX = 520
DRAFT_MAX = 2600

# Вопросы, на которые нужен связный рассказ по записям, а не таблица («почему», «что нужно», «кратко опиши», «проанализируй»…)
EXPLAIN_RE = re.compile(r"(почему|причин|что нужно|что надо|что делать|что случилось|что с |расскаж|опиш|объясн|сводк|кратко|проанализир|анализ|итог по|вывод|подробн|разбер)")

STEP_SYSTEM = (
    "Ты помощник диспетчера системы «Календарь работ». Тебе по частям показывают записи из системы, а ты пишешь по ним ответ на вопрос. На каждом шаге ты получаешь: ВОПРОС, "
    "ЧЕРНОВИК (что уже написано; на первом шаге пусто) и НОВУЮ ПАЧКУ записей. Верни обновлённый черновик: сохрани важное из прежнего и добавь то, что видно в новой пачке. "
    "Пиши по-русски, коротко, по пунктам. Номера комплексов, даты и числа бери ТОЧНО из записей, ничего не выдумывай; то, что к вопросу не относится, не добавляй. "
    "Если для полного ответа не хватает данных, которых нет в записях, в поле need опиши одной фразой, что запросить у системы (как вопрос, например: «какие причины неисправности у комплекса P-3»); "
    "если данных хватает — need пустой."
)
STEP_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}, "need": {"type": "string"}},
    "required": ["draft"],
    "additionalProperties": False,
}


def wanted(question: str, mode: str, out: dict[str, Any]) -> bool:
    """Нужен ли агент: только когда первый запрос дал таблицу записей; mode «agent» — человек просит подробный разбор, «plain» — не нужен, «auto» — по словам вопроса."""
    if out.get("kind") != "table" or not out.get("rows"):
        return False
    if mode == "agent":
        return True
    if mode == "plain":
        return False
    return bool(EXPLAIN_RE.search(plan_cache.normalize(question)))


def _label(column: dict[str, Any]) -> str:
    return str(column.get("label") or column.get("key") or "").split(" (")[0]


def row_text(columns: list[dict[str, Any]], row: list[Any]) -> str:
    parts = []
    for col, value in zip(columns, row):
        text = " ".join(str(value if value is not None else "").split())
        if text:
            parts.append(f"{_label(col)}: {text[:CELL_MAX]}")
    return " | ".join(parts)[:ROW_MAX]


def _batch_user(question: str, draft: str, title: str, lines: list[str], left: int) -> str:
    return (f"ВОПРОС: {question}\n\nЧЕРНОВИК:\n{draft or '(пока пусто)'}\n\nНОВАЯ ПАЧКА ({title}):\n" + "\n".join(f"{i}) {t}" for i, t in enumerate(lines, 1))
            + f"\n\n(После этой пачки осталось пачек: {left}.)")


async def _fetch_more(need: str, role: str, today: dt.date) -> Optional[tuple[list[dict[str, Any]], list[list[Any]], str]]:
    """Модель попросила данные словами → обычный запрос по правам роли. Не получилось (непонятно, нет доступа) — None."""
    from app.services.ai import qa  # noqa: PLC0415 — qa импортирует этот модуль только внутри answer

    try:
        planned = await qa.plan(need, history=None, today=today)
        if not planned.get("can_answer"):
            return None
        result = await qe.run(planned["spec"], role, today=today, max_rows=BATCH_ROWS)
    except (qe.QueryError, client.AiError):
        return None
    if not result.rows:
        return None
    return result.columns, result.rows[:BATCH_ROWS], result.description


async def refine(question: str, role: str, out: dict[str, Any], *, today: dt.date) -> dict[str, Any]:
    """Прочитать записи первого запроса (out — ответ qa.execute, kind=table) пачками и написать по ним ответ. Возвращает ответ kind=agent."""
    columns, rows = out["columns"], out["rows"]
    total = out.get("total") or len(rows)
    rows = rows[: BATCH_ROWS * MAX_BATCHES]
    queue: deque[tuple[list[dict[str, Any]], list[list[Any]], str]] = deque(
        (columns, rows[i:i + BATCH_ROWS], f"записи {i + 1}–{min(i + BATCH_ROWS, len(rows))} из {len(rows)}") for i in range(0, len(rows), BATCH_ROWS))
    draft, extra, read, step = "", [], 0, 0
    read_columns, read_rows = columns, []
    while queue:
        cols, batch, title = queue.popleft()
        step += 1
        progress.report(stage=f"Читаю: {title}", step=step, steps=step + len(queue), draft=draft, extra=extra)
        lines = [row_text(cols, r) for r in batch]
        data = await client.llm_json(STEP_SYSTEM, _batch_user(question, draft, title, lines, len(queue)), STEP_SCHEMA, schema_name="agent_step",
                                     max_tokens=420, priority=client.PRIORITY_USER)
        new_draft = str(data.get("draft") or "").strip()
        if new_draft:
            draft = new_draft[:DRAFT_MAX]
        read += len(batch)
        if cols is columns:
            read_rows.extend(batch)
        need = str(data.get("need") or "").strip()
        if need and len(extra) < MAX_EXTRA_QUERIES and need.lower() not in (e.lower() for e in extra):
            extra.append(need)
            progress.report(stage=f"Запрашиваю нужные данные: «{need}»", step=step, steps=step + len(queue) + 1, draft=draft, extra=extra)
            more = await _fetch_more(need, role, today)
            if more:
                m_cols, m_rows, m_desc = more
                queue.appendleft((m_cols, m_rows, f"по запросу модели «{need}»: {len(m_rows)} зап."))
    progress.report(stage="Готовлю ответ", step=step, steps=step, draft=draft, extra=extra)
    if not draft:
        draft = "Модель не смогла составить связный ответ по этим записям — смотрите таблицу ниже."
    answer = dict(out)
    answer.update({
        "kind": "agent", "text": draft,
        "agent": {"steps": step, "rows_read": len(read_rows), "extra_rows": read - len(read_rows), "rows_total": total, "extra": extra, "truncated": total > len(read_rows), "batch": BATCH_ROWS},
        "records": {"columns": read_columns, "rows": read_rows},
        "source": "agent",
    })
    for key in ("columns", "rows"):         # таблица теперь внутри records — не дублируем
        answer.pop(key, None)
    return answer
