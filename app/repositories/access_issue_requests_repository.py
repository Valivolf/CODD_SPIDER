"""Ремонт/Установка: SIM указана (или только что введена), но доступа к
комплексу всё ещё нет — монтёр звонит диспетчеру напрямую, а диспетчер уже
с веб-панели решает: отпустить бригаду без ремонта в этот раз, или оформить
демонтаж (указав, что именно демонтировать). См. routes_bot.py (bot-key:
create/get) и routes_access_issues.py (session-authed: list/resolve).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.point_credentials import POINT_EXTRA_KEYS, finish_rows
from app.db.database import get_connection

_COLUMNS = "id, task_id, point_id, created_at, status, resolved_at, resolved_by, demontage_note"
_KEYS = ["id", "task_id", "point_id", "created_at", "status", "resolved_at", "resolved_by", "demontage_note"]


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


async def create(task_id: str, point_id: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into access_issue_requests (task_id, point_id, created_at, status) values (?, ?, ?, 'pending')",
            (task_id, point_id, now),
        )
        await conn.commit()
        request_id = cur.lastrowid
        cur = await conn.execute(f"select {_COLUMNS} from access_issue_requests where id = ?", (request_id,))
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[arg-type]


async def get(request_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from access_issue_requests where id = ?", (request_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_pending() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select a.id, a.task_id, a.point_id, a.created_at, a.status, a.resolved_at, a.resolved_by, a.demontage_note,
                   p.address, p.ip_address, p.port, p.login, p.password, coalesce(pc.is_legacy, 0)
            from access_issue_requests a
            left join points p on p.id = a.point_id
            left join point_categories pc on pc.id = p.category_id
            where a.status = 'pending'
            order by a.created_at
            """
        )
        rows = await cur.fetchall()
    return finish_rows(_KEYS + ["point_address", "ip_address", "port"] + POINT_EXTRA_KEYS, rows)


async def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select a.id, a.task_id, a.point_id, a.created_at, a.status, a.resolved_at, a.resolved_by, a.demontage_note,
                   p.address
            from access_issue_requests a
            left join points p on p.id = a.point_id
            order by a.created_at desc
            limit ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    keys = _KEYS + ["point_address"]
    return [dict(zip(keys, r)) for r in rows]


async def resolve(request_id: int, *, decision: str, demontage_note: Optional[str], resolved_by: Optional[str]) -> Optional[dict[str, Any]]:
    """decision: 'released' (отпустить бригаду без ремонта) | 'demontage'."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "update access_issue_requests set status = ?, resolved_at = ?, resolved_by = ?, demontage_note = ? "
            "where id = ? and status = 'pending'",
            (decision, now, resolved_by, demontage_note if decision == "demontage" else None, request_id),
        )
        await conn.commit()
    return await get(request_id)
