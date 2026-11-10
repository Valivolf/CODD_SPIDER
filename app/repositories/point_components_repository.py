"""Компоненты комплекса (камера, ящик, плата, …) — сколько угодно у каждой
точки, каждый со своей отметкой «на складе» и описанием. Заменяют прежние
готовые поля «Ящик / Камера / Плата» на странице «Склад».

Статус точки по компонентам (recompute_status):
  - хотя бы один компонент на складе    → «На складе» (если не все — интерфейс
                                          подписывает «Частично на складе»; это
                                          подпись, не статус, см. core/warehouse.py)
  - ни один не на складе (а точка была «На складе»)
                                        → статус сбрасывается (фоновая проверка
                                          доступности выставит настоящий)
У точки без компонентов статус остаётся тем, что поставили вручную.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.warehouse import WAREHOUSE_STATUS
from app.db.database import get_connection

_CHUNK = 500


def _row(r) -> dict[str, Any]:
    return {"id": r[0], "name": r[1], "in_stock": bool(r[2]), "description": r[3] or ""}


async def list_for_points(point_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    ids = list(point_ids)
    async with get_connection() as conn:
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                f"select id, name, in_stock, description, point_id from point_components "
                f"where point_id in ({marks}) order by sort_order, id",
                chunk,
            )
            for r in await cur.fetchall():
                result.setdefault(r[4], []).append(_row(r))
    return result


async def list_for_point(point_id: str) -> list[dict[str, Any]]:
    return (await list_for_points([point_id])).get(point_id, [])


async def names() -> list[str]:
    """Names already used, for the autocomplete in the card (plus the usual
    suggestions) — the field itself stays free text."""
    async with get_connection() as conn:
        cur = await conn.execute("select distinct name from point_components order by name collate nocase limit 300")
        return [r[0] for r in await cur.fetchall()]


async def replace_for_point(point_id: str, items: list[dict[str, Any]]) -> None:
    """Full replace — what the card's editor saves (it always submits the
    whole list)."""
    async with get_connection() as conn:
        await conn.execute("delete from point_components where point_id = ?", (point_id,))
        await conn.executemany(
            "insert into point_components (point_id, name, in_stock, description, sort_order) values (?, ?, ?, ?, ?)",
            [
                (point_id, it["name"], int(bool(it.get("in_stock"))), it.get("description") or "", i)
                for i, it in enumerate(items, start=1)
            ],
        )
        await conn.commit()


async def merge_for_point(point_id: str, items: list[dict[str, Any]]) -> tuple[int, int]:
    """Excel import: each item updates the component with the same name
    (case-insensitive) or adds a new one; components not mentioned stay as
    they are. An empty description doesn't wipe an existing one. Returns
    (added, updated)."""
    added = updated = 0
    async with get_connection() as conn:
        cur = await conn.execute("select coalesce(max(sort_order), 0) from point_components where point_id = ?", (point_id,))
        (order,) = await cur.fetchone()
        for it in items:
            cur = await conn.execute(
                "select id, description from point_components "
                "where point_id = ? and pylower(trim(name)) = pylower(trim(?)) order by id limit 1",
                (point_id, it["name"]),
            )
            existing = await cur.fetchone()
            if existing:
                description = it.get("description") or existing[1] or ""
                await conn.execute(
                    "update point_components set in_stock = ?, description = ? where id = ?",
                    (int(bool(it.get("in_stock"))), description, existing[0]),
                )
                updated += 1
            else:
                order += 1
                await conn.execute(
                    "insert into point_components (point_id, name, in_stock, description, sort_order) values (?, ?, ?, ?, ?)",
                    (point_id, it["name"], int(bool(it.get("in_stock"))), it.get("description") or "", order),
                )
                added += 1
        await conn.commit()
    return added, updated


async def set_all_in_stock_conn(conn, point_id: str, in_stock: bool) -> None:
    """On an open connection — the whole complex went to the warehouse / left it."""
    await conn.execute("update point_components set in_stock = ? where point_id = ?", (int(in_stock), point_id))


async def recompute_status(point_id: str, source: str = "warehouse") -> Optional[str]:
    """Sets the point's status from its components (see the module docstring)
    and logs the change in point_status_history. Returns the resulting status
    (None if the point doesn't exist)."""
    async with get_connection() as conn:
        cur = await conn.execute("select status from points where id = ?", (point_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        old = row[0] or ""
        cur = await conn.execute(
            "select count(*), coalesce(sum(in_stock), 0) from point_components where point_id = ?", (point_id,)
        )
        total, stocked = await cur.fetchone()
        if total == 0:
            return old
        if stocked > 0:
            new = WAREHOUSE_STATUS
        else:
            new = "" if old == WAREHOUSE_STATUS else old
        if new != old:
            await conn.execute("update points set status = ? where id = ?", (new, point_id))
            if old == WAREHOUSE_STATUS and new != WAREHOUSE_STATUS:
                await conn.execute("delete from warehouse_items where point_id = ?", (point_id,))
            await conn.execute(
                "insert into point_status_history (point_id, old_status, new_status, source, changed_at, task_id) "
                "values (?, ?, ?, ?, ?, null)",
                (point_id, old, new, source, dt.datetime.now(dt.timezone.utc).isoformat()),
            )
        await conn.commit()
        return new
