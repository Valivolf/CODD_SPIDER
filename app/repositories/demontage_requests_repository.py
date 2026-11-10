"""Монтёр сам просит демонтаж ИЛИ отпустить без ремонта (меню Ремонт/
Установка → «Запрос на демонтаж», либо кнопки «Отпустить»/«Демонтаж» при
«Завершить», если комплекс так и остался недоступен), диспетчер явно
одобряет/отклоняет — kind различает, что именно запрошено ('demontage' —
по умолчанию, для обратной совместимости, или 'release'). Кнопка
«Демонтаж» на /monitoring «Сейчас в работе» создаёт такую же заявку
(kind='demontage'), но initiated_by='dispatcher' и сразу approved (см.
routes_monitoring.py) — для "отпустить" у диспетчера есть свой прямой
/monitoring-эндпоинт (release_active_session), не через эту таблицу,
поскольку там решение уже дispetчерское, ждать одобрения не у кого.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.point_credentials import POINT_EXTRA_KEYS, finish_rows
from app.db.database import get_connection

_COLUMNS = "id, task_id, point_id, reason, initiated_by, created_at, status, resolved_at, resolved_by, decline_comment, kind"
_KEYS = ["id", "task_id", "point_id", "reason", "initiated_by", "created_at", "status", "resolved_at", "resolved_by", "decline_comment", "kind"]


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


async def create(task_id: str, point_id: str, reason: str, *, initiated_by: str = "fitter", kind: str = "demontage") -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into demontage_requests (task_id, point_id, reason, initiated_by, created_at, status, kind) "
            "values (?, ?, ?, ?, ?, 'pending', ?)",
            (task_id, point_id, reason, initiated_by, now, kind),
        )
        await conn.commit()
        request_id = cur.lastrowid
        cur = await conn.execute(f"select {_COLUMNS} from demontage_requests where id = ?", (request_id,))
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[arg-type]


async def create_resolved(task_id: str, point_id: str, reason: str, *, resolved_by: str) -> dict[str, Any]:
    """Диспетчер сразу решает демонтировать из /monitoring — минуя
    ожидание одобрения (это уже его собственное решение)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into demontage_requests (task_id, point_id, reason, initiated_by, created_at, status, resolved_at, resolved_by, kind) "
            "values (?, ?, ?, 'dispatcher', ?, 'approved', ?, ?, 'demontage')",
            (task_id, point_id, reason, now, now, resolved_by),
        )
        await conn.commit()
        request_id = cur.lastrowid
        cur = await conn.execute(f"select {_COLUMNS} from demontage_requests where id = ?", (request_id,))
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[arg-type]


async def get(request_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from demontage_requests where id = ?", (request_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_pending() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select d.id, d.task_id, d.point_id, d.reason, d.initiated_by, d.created_at, d.status,
                   d.resolved_at, d.resolved_by, d.decline_comment, d.kind,
                   p.address, p.ip_address, p.port, p.login, p.password, coalesce(pc.is_legacy, 0)
            from demontage_requests d
            left join points p on p.id = d.point_id
            left join point_categories pc on pc.id = p.category_id
            where d.status = 'pending'
            order by d.created_at
            """
        )
        rows = await cur.fetchall()
    return finish_rows(_KEYS + ["point_address", "ip_address", "port"] + POINT_EXTRA_KEYS, rows)


async def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select d.id, d.task_id, d.point_id, d.reason, d.initiated_by, d.created_at, d.status,
                   d.resolved_at, d.resolved_by, d.decline_comment, d.kind,
                   p.address
            from demontage_requests d
            left join points p on p.id = d.point_id
            order by d.created_at desc
            limit ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    keys = _KEYS + ["point_address"]
    return [dict(zip(keys, r)) for r in rows]


async def get_latest_dispatcher_initiated(task_id: str) -> Optional[dict[str, Any]]:
    """Used by the bot's per-task background watcher (see bot/service/
    mechanic.py::_task_watcher) — only ever looks at initiated_by='dispatcher'
    rows, so it never overlaps with a fitter-initiated request the bot is
    already polling directly by its own id."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from demontage_requests "
            "where task_id = ? and initiated_by = 'dispatcher' order by created_at desc limit 1",
            (task_id,),
        )
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def resolve(request_id: int, *, approve: bool, decline_comment: Optional[str], resolved_by: Optional[str]) -> Optional[dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    new_status = "approved" if approve else "declined"
    async with get_connection() as conn:
        await conn.execute(
            "update demontage_requests set status = ?, resolved_at = ?, resolved_by = ?, decline_comment = ? "
            "where id = ? and status = 'pending'",
            (new_status, now, resolved_by, decline_comment if not approve else None, request_id),
        )
        await conn.commit()
    return await get(request_id)
