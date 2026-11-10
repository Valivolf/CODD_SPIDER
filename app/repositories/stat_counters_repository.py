"""Сохранённые счётчики страницы «Статистика» (таблица stat_counters, см. app/db/database.py): название и набор условий (JSON).
Сам подсчёт и проверка условий — app/services/stat_counters.py; здесь только хранение."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.core.text import decode_entities
from app.db.database import get_connection
from app.services.stat_counters import NAME_MAX

MAX_COUNTERS = 200


def clean_name(name: Any) -> str:
    return " ".join(decode_entities(str(name or "")).split())[:NAME_MAX]


def _row(r) -> dict[str, Any]:
    try:
        filters = json.loads(r[2] or "{}")
    except ValueError:
        filters = {}
    return {"id": r[0], "name": r[1], "filters": filters if isinstance(filters, dict) else {}, "sort_order": r[3], "created_at": r[4], "created_by": r[5] or ""}


_COLS = "id, name, filters, sort_order, created_at, created_by"


async def list_counters() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLS} from stat_counters order by sort_order, id")
        return [_row(r) for r in await cur.fetchall()]


async def get(counter_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLS} from stat_counters where id = ?", (counter_id,))
        row = await cur.fetchone()
    return _row(row) if row else None


async def count() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from stat_counters")
        return (await cur.fetchone())[0]


async def create(name: str, filters: dict[str, Any], created_by: str) -> dict[str, Any]:
    async with get_connection() as conn:
        cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from stat_counters")
        order = (await cur.fetchone())[0]
        cur = await conn.execute(
            "insert into stat_counters (name, filters, sort_order, created_at, created_by) values (?, ?, ?, ?, ?)",
            (name, json.dumps(filters, ensure_ascii=False), order, dt.datetime.now(dt.timezone.utc).isoformat(), created_by))
        await conn.commit()
        new_id = cur.lastrowid
    return await get(new_id)  # type: ignore[return-value]


async def update(counter_id: int, name: Optional[str] = None, filters: Optional[dict[str, Any]] = None) -> bool:
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if filters is not None:
        sets.append("filters = ?")
        args.append(json.dumps(filters, ensure_ascii=False))
    if not sets:
        return await get(counter_id) is not None
    async with get_connection() as conn:
        cur = await conn.execute(f"update stat_counters set {', '.join(sets)} where id = ?", (*args, counter_id))
        await conn.commit()
        return cur.rowcount > 0


async def delete(counter_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from stat_counters where id = ?", (counter_id,))
        await conn.commit()
        return cur.rowcount > 0


async def move(counter_id: int, direction: int) -> bool:
    """Поменять счётчик местами с соседним (direction: -1 — выше, 1 — ниже). Порядок задаётся только переставляемыми двумя."""
    rows = await list_counters()
    ids = [r["id"] for r in rows]
    if counter_id not in ids:
        return False
    i = ids.index(counter_id)
    j = i + (1 if direction > 0 else -1)
    if not 0 <= j < len(ids):
        return True
    ids[i], ids[j] = ids[j], ids[i]
    async with get_connection() as conn:
        for order, cid in enumerate(ids, start=1):
            await conn.execute("update stat_counters set sort_order = ? where id = ?", (order, cid))
        await conn.commit()
    return True
