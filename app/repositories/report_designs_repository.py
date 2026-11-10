"""Отчёты из онлайн-конструктора (таблица report_designs): имя, формат файла
и сам шаблон — JSON, нормализованный app/models/report_design.py."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.db.database import get_connection

_LIST_SELECT = "select id, name, format, created_at, updated_at, created_by, visibility, shared_users from report_designs"


def _brief(row) -> dict[str, Any]:
    return {
        "id": row[0], "name": row[1], "format": row[2],
        "created_at": row[3], "updated_at": row[4], "created_by": row[5],
        "visibility": row[6] or "public", "shared_users": json.loads(row[7] or "[]"),
    }


async def list_designs() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_LIST_SELECT + " order by updated_at desc, id desc")
        return [_brief(r) for r in await cur.fetchall()]


async def get_design(design_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, name, format, created_at, updated_at, created_by, visibility, shared_users, design from report_designs where id = ?",
            (design_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {**_brief(row[:8]), "design": json.loads(row[8])}


async def create_design(name: str, fmt: str, design: dict[str, Any], created_by: str | None, visibility: str = "private", shared_users: list[int] | None = None) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into report_designs (name, format, design, created_at, updated_at, created_by, visibility, shared_users) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, fmt, json.dumps(design, ensure_ascii=False), now, now, created_by, visibility, json.dumps(shared_users or [])),
        )
        await conn.commit()
        new_id = cur.lastrowid
    return await get_design(new_id)  # type: ignore[return-value]


async def update_design(design_id: int, name: str, design: dict[str, Any]) -> Optional[dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "update report_designs set name = ?, design = ?, updated_at = ? where id = ?",
            (name, json.dumps(design, ensure_ascii=False), now, design_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
    return await get_design(design_id)


async def delete_design(design_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from report_designs where id = ?", (design_id,))
        await conn.commit()
        return cur.rowcount > 0


async def set_access(design_id: int, visibility: str, shared_users: list[int]) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "update report_designs set visibility = ?, shared_users = ?, updated_at = ? where id = ?",
            (visibility, json.dumps(shared_users), dt.datetime.now(dt.timezone.utc).isoformat(), design_id),
        )
        await conn.commit()
        if not cur.rowcount:
            return None
    return await get_design(design_id)
