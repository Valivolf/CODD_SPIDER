"""Что уже сделано по комплексу, пока монтёр(ы) на нём: текст отчёта, замена оборудования/SIM, итоги проверок.

Хранится на сервере (а не в памяти бота): монтёр может выйти из комплекса и зайти снова — работа продолжится с того
же места, прогресс переживает перезапуск бота, а двое монтёров одной бригады видят и меняют одно и то же. Число фото
сюда не входит — его считают по таблице visit_photos. Запись удаляется, когда отчёт по заданию сдан."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.db.database import get_connection

# Только эти ключи принимаются от бота; значения — как в сессии бота (bot/service/mechanic.py).
BOOL_KEYS = {
    "check_confirmed", "demontage_completed", "no_access", "repair_mode", "switched_to_repair", "sim_asked",
}
TEXT_KEYS = {
    "report_text", "equipment_installed", "equipment_removed", "power_status", "work_type", "point_id", "demontage_note",
    "fault_text", "needs_text", "shot_text",
}
# {"old","new","reason"}; {"kind","id"} — запрос диспетчеру, на ответ которого ещё ждём;
# {"mode": whole|parts, "ids": [...], "labels": "...", "other": "..."} — что именно демонтировано.
OBJECT_KEYS = {"sim_change", "pending", "demontage_scope"}
STATE_KEYS = BOOL_KEYS | TEXT_KEYS | OBJECT_KEYS
MAX_TEXT = 4000


def clean_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Оставляет только известные ключи с допустимыми типами; None очищает значение."""
    out: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key in BOOL_KEYS:
            out[key] = bool(value)
        elif key in TEXT_KEYS:
            out[key] = None if value is None else str(value)[:MAX_TEXT]
        elif key in OBJECT_KEYS:
            if value is None:
                out[key] = None
            elif isinstance(value, dict):
                out[key] = {
                    str(k): (
                        v if isinstance(v, (int, float, bool)) or v is None
                        else [i for i in v if isinstance(i, int)] if isinstance(v, list)
                        else str(v)[:MAX_TEXT]
                    )
                    for k, v in value.items()
                }
    return out


async def get(task_id: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select state, updated_at, updated_by from bot_task_progress where task_id = ?", (task_id,))
        row = await cur.fetchone()
    return _row(row) if row else None


def _row(row) -> dict[str, Any]:
    try:
        state = json.loads(row[0] or "{}")
    except ValueError:
        state = {}
    return {"state": state if isinstance(state, dict) else {}, "updated_at": row[1], "updated_by": row[2]}


async def get_many(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    ids = [i for i in task_ids if i]
    async with get_connection() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                f"select task_id, state, updated_at, updated_by from bot_task_progress where task_id in ({marks})", chunk
            )
            for r in await cur.fetchall():
                result[r[0]] = _row(r[1:])
    return result


async def patch(task_id: str, changes: dict[str, Any], updated_by: str | None) -> dict[str, Any]:
    """Сливает изменения в сохранённое состояние (ключ, которого нет в `changes`, остаётся как был — так двое
    монтёров не затирают друг другу разные поля); возвращает итоговую запись."""
    changes = clean_patch(changes)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        cur = await conn.execute("select state from bot_task_progress where task_id = ?", (task_id,))
        row = await cur.fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except ValueError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        await conn.execute(
            "insert into bot_task_progress (task_id, state, updated_at, updated_by) values (?, ?, ?, ?) "
            "on conflict(task_id) do update set state = excluded.state, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (task_id, json.dumps(state, ensure_ascii=False), now, updated_by),
        )
        await conn.commit()
    return {"state": state, "updated_at": now, "updated_by": updated_by}


def report_parts(state: dict[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    """(текст отчёта, что сняли, что поставили) из сохранённого прогресса — то же, что бот собирает в
    `_equipment_for_report` при обычном завершении: свободный текст «Замены оборудования» и демонтаж.
    SIM-карта в этот текст НЕ входит — она идёт отдельными полями отчёта (см. `sim_parts`)."""
    removed = [state.get("equipment_removed")]
    installed = [state.get("equipment_installed")]
    scope = state.get("demontage_scope") or {}
    if scope.get("labels") or scope.get("other"):
        removed.append("; ".join(p for p in (scope.get("labels"), scope.get("other")) if p))
    join = lambda parts: "; ".join(p for p in parts if p) or None  # noqa: E731
    return (state.get("report_text") or "").strip(), join(removed), join(installed)


def sim_parts(state: dict[str, Any]) -> tuple[Optional[str], Optional[str], str]:
    """(SIM снята, SIM установлена, состояние снятой: working / defective / "") из сохранённого прогресса."""
    change = state.get("sim_change") or {}
    if not change.get("new"):
        return None, None, ""
    old = str(change.get("old") or "").strip() or None
    state_code = change.get("old_state") or ""
    return old, str(change["new"]).strip() or None, state_code if (old and state_code in ("working", "defective")) else ""


def fault_parts(state: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(что неисправно, что нужно для исправления) из сохранённого прогресса — для итога «Не работает»."""
    clean = lambda v: (str(v).strip() or None) if v else None  # noqa: E731
    return clean(state.get("fault_text")), clean(state.get("needs_text"))


async def delete(task_id: str) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from bot_task_progress where task_id = ?", (task_id,))
        await conn.commit()
