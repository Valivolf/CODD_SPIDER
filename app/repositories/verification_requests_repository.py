"""A ping/port recheck coming back "reachable" after a Ремонт/Установка
report isn't proof the complex actually works end to end — this is the
queue that makes a human (operator/admin) confirm or flag it, and lets the
bot's conversation with the fitter wait on the answer. See routes_bot.py
(bot-key endpoints) and routes_verification_requests.py (session-authed
endpoints for the web UI's live popup / /monitoring history).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.point_credentials import POINT_EXTRA_KEYS, finish_rows
from app.db.database import get_connection

_COLUMNS = "id, task_id, point_id, created_at, status, resolved_at, resolved_by, issue_comment"
_KEYS = ["id", "task_id", "point_id", "created_at", "status", "resolved_at", "resolved_by", "issue_comment"]


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


async def create(task_id: str, point_id: str) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into verification_requests (task_id, point_id, created_at, status) values (?, ?, ?, 'pending')",
            (task_id, point_id, now),
        )
        await conn.commit()
        request_id = cur.lastrowid
        cur = await conn.execute(f"select {_COLUMNS} from verification_requests where id = ?", (request_id,))
        row = await cur.fetchone()
    return _row_to_dict(row)  # type: ignore[arg-type]


async def get(request_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from verification_requests where id = ?", (request_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_pending() -> list[dict[str, Any]]:
    """Pending requests joined with the point's address/ip/port, newest
    first — for the live popup (address text, "Веб-интерфейс"/"Копировать
    IP:порт" buttons) and /monitoring."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select v.id, v.task_id, v.point_id, v.created_at, v.status, v.resolved_at, v.resolved_by, v.issue_comment,
                   p.address, p.ip_address, p.port, p.login, p.password, coalesce(pc.is_legacy, 0)
            from verification_requests v
            left join points p on p.id = v.point_id
            left join point_categories pc on pc.id = p.category_id
            where v.status = 'pending'
            order by v.created_at
            """
        )
        rows = await cur.fetchall()
    return finish_rows(_KEYS + ["point_address", "ip_address", "port"] + POINT_EXTRA_KEYS, rows)


async def list_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Resolved-and-pending history for /monitoring, newest first."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select v.id, v.task_id, v.point_id, v.created_at, v.status, v.resolved_at, v.resolved_by, v.issue_comment,
                   p.address
            from verification_requests v
            left join points p on p.id = v.point_id
            order by v.created_at desc
            limit ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
    keys = _KEYS + ["point_address"]
    return [dict(zip(keys, r)) for r in rows]


async def resolve(
    request_id: int, *, ok: bool, issue_comment: Optional[str], resolved_by: Optional[str], remark: bool = False
) -> Optional[dict[str, Any]]:
    """Решение диспетчера: `confirmed` — полностью работает; `remark` — работает, но с замечанием, бригаду
    отпускают (замечание — в issue_comment); `issue` — «продолжить работу» (комментарий необязателен)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    new_status = ("remark" if remark else "confirmed") if ok else "issue"
    comment = (issue_comment or "").strip() or None
    async with get_connection() as conn:
        await conn.execute(
            "update verification_requests set status = ?, resolved_at = ?, resolved_by = ?, issue_comment = ? "
            "where id = ? and status = 'pending'",
            (new_status, now, resolved_by, comment if (not ok or remark) else None, request_id),
        )
        await conn.commit()
    return await get(request_id)


async def delete(request_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from verification_requests where id = ?", (request_id,))
        await conn.commit()
