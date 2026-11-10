"""Self-service "link me to the bot" requests — instead of the dispatcher
having to ask a fitter for their VK ID and type it into "Персонал" by hand,
an unrecognised vk_id messaging the bot can submit a request here, and the
dispatcher approves it (or blocks it) from the web panel. See routes_bot.py
(bot-key: submit) and routes_join_requests.py (session-authed: list/block).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection

# How long a 'pending' request holds off a resubmission from the same
# vk_id — stops someone mashing "Отправить заявку" from flooding the panel.
JOIN_REQUEST_COOLDOWN_HOURS = 24

_COLUMNS = "id, vk_id, vk_name, status, requested_at, resolved_at, resolved_by"
_KEYS = ["id", "vk_id", "vk_name", "status", "requested_at", "resolved_at", "resolved_by"]


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


async def submit(vk_id: int, vk_name: Optional[str]) -> dict[str, Any]:
    """Called by the bot when someone taps "Отправить заявку". Returns
    {"status": "pending"|"blocked", "cooldown_active": bool} — the bot uses
    this to decide what to tell them, it never raises for a normal outcome."""
    now = dt.datetime.now(dt.timezone.utc)
    async with get_connection() as conn:
        cur = await conn.execute("select status, requested_at from bot_join_requests where vk_id = ?", (vk_id,))
        row = await cur.fetchone()
        if row and row[0] == "blocked":
            return {"status": "blocked", "cooldown_active": False}
        if row:
            requested_at = dt.datetime.fromisoformat(row[1])
            elapsed_hours = (now - requested_at).total_seconds() / 3600
            if elapsed_hours < JOIN_REQUEST_COOLDOWN_HOURS:
                return {"status": "pending", "cooldown_active": True}
        await conn.execute(
            """
            insert into bot_join_requests (vk_id, vk_name, status, requested_at)
            values (?, ?, 'pending', ?)
            on conflict(vk_id) do update set
                vk_name = excluded.vk_name, status = 'pending',
                requested_at = excluded.requested_at, resolved_at = null, resolved_by = null
            """,
            (vk_id, vk_name, now.isoformat()),
        )
        await conn.commit()
    return {"status": "pending", "cooldown_active": False}


async def list_all() -> list[dict[str, Any]]:
    """Pending + blocked, newest first — everything still needing a
    decision from the dispatcher/admin."""
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from bot_join_requests order by requested_at desc")
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def block(vk_id: int, *, resolved_by: Optional[str] = None) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            """
            insert into bot_join_requests (vk_id, status, requested_at, resolved_at, resolved_by)
            values (?, 'blocked', ?, ?, ?)
            on conflict(vk_id) do update set
                status = 'blocked', resolved_at = excluded.resolved_at, resolved_by = excluded.resolved_by
            """,
            (vk_id, now, now, resolved_by),
        )
        await conn.commit()


async def unblock(vk_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from bot_join_requests where vk_id = ?", (vk_id,))
        await conn.commit()


async def resolve_by_vk_id(vk_id: int) -> None:
    """A person got this vk_id linked (admin either approved a request or
    just typed the ID in directly) — any pending/blocked row is moot."""
    async with get_connection() as conn:
        await conn.execute("delete from bot_join_requests where vk_id = ?", (vk_id,))
        await conn.commit()
