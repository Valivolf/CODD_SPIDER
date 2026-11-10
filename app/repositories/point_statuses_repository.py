"""Managed dictionary of point statuses — same get/create/update/delete
shape as point_categories_repository, plus two flags the status-CRUD UI
and the background health checker both care about:
  - is_auto: point_health_service is allowed to set/overwrite this status
    automatically (replaces the old hardcoded _AUTO_STATUSES set).
  - protected: can't be deleted; for "На складе" the name also can't be
    changed (enforced in update_status below) since the warehouse page and
    points_repository both key off that exact string.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.db.database import get_connection

DEFAULT_COLOR = "#64748b"
LOCKED_NAMES = {"На складе"}


async def list_statuses() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, name, color, is_auto, protected from point_statuses "
            "order by protected desc, name collate nocase"
        )
        return [
            {"id": r[0], "name": r[1], "color": r[2], "is_auto": bool(r[3]), "protected": bool(r[4])}
            for r in await cur.fetchall()
        ]


async def list_auto_names() -> set[str]:
    """Names the background ping/port checker may set/overwrite freely."""
    async with get_connection() as conn:
        cur = await conn.execute("select name from point_statuses where is_auto = 1")
        return {r[0] for r in await cur.fetchall()}


async def create_status(name: str, color: str, is_auto: bool) -> dict[str, Any]:
    name = name.strip()
    color = (color or "").strip() or DEFAULT_COLOR
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into point_statuses (name, color, is_auto, protected, created_at) values (?, ?, ?, 0, ?)",
            (name, color, int(is_auto), dt.datetime.now(dt.timezone.utc).isoformat()),
        )
        await conn.commit()
        return {"id": cur.lastrowid, "name": name, "color": color, "is_auto": is_auto, "protected": False}


async def update_status(status_id: int, name: str, color: str, is_auto: bool) -> None:
    name = name.strip()
    color = (color or "").strip() or DEFAULT_COLOR
    async with get_connection() as conn:
        cur = await conn.execute("select name, protected from point_statuses where id = ?", (status_id,))
        row = await cur.fetchone()
        if row is None:
            raise LookupError("status not found")
        current_name, protected = row[0], bool(row[1])
        if protected and current_name in LOCKED_NAMES and name != current_name:
            raise PermissionError("cannot rename this status")
        await conn.execute(
            "update point_statuses set name = ?, color = ?, is_auto = ? where id = ?",
            (name, color, int(is_auto), status_id),
        )
        await conn.commit()


async def delete_status(status_id: int) -> bool:
    """False (caller -> 409) if protected, or if any point still uses it."""
    async with get_connection() as conn:
        cur = await conn.execute("select name, protected from point_statuses where id = ?", (status_id,))
        row = await cur.fetchone()
        if row is None:
            return False
        name, protected = row[0], bool(row[1])
        if protected:
            return False
        cur = await conn.execute("select count(*) from points where status = ?", (name,))
        (in_use,) = await cur.fetchone()
        if in_use:
            return False
        await conn.execute("delete from point_statuses where id = ?", (status_id,))
        await conn.commit()
        return True
