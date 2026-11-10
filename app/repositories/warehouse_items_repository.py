"""Warehouse-only per-point data (see database.py's warehouse_items table):
the "статус паутины" registry field, the receipt date and free info for a
point that is (fully or partly) on the shelf. What exactly is on the shelf
is the point's list of components — see point_components_repository.

The row is meaningful only while the point's status is «На складе»:
points_repository deletes it (and un-stocks the components) as soon as the
status changes away from it. The old fixed
box_state/camera_state/board_state columns are left in the table but no
longer read or written — their data was converted into components once, at
startup (database.py).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.warehouse import WAREHOUSE_STATUS
from app.db.database import get_connection
from app.repositories import point_components_repository

FIELDS = ["web_status", "received_at", "info"]

_BASE_SELECT = """
    select p.id, p.address, p.description, p.inventory_number, p.status, p.category_id,
           pc.name as category_name, pc.color as category_color,
           w.web_status, w.received_at, w.info
    from points p
    left join point_categories pc on pc.id = p.category_id
    left join warehouse_items w on w.point_id = p.id
"""
_IN_WAREHOUSE = f"p.status = '{WAREHOUSE_STATUS}'"


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "address": row[1],
        "description": row[2],
        "inventory_number": row[3],
        "status": row[4],
        "category_id": row[5],
        "category_name": row[6] or "",
        "category_color": row[7] or "",
        "web_status": row[8],
        "received_at": row[9],
        "info": row[10],
    }


async def _with_components(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_point = await point_components_repository.list_for_points([i["id"] for i in items])
    for item in items:
        comps = by_point.get(item["id"], [])
        item["components"] = comps
        item["components_total"] = len(comps)
        item["components_in_stock"] = sum(1 for c in comps if c["in_stock"])
    return items


async def list_in_warehouse(
    search: str = "", category_ids: Optional[list[int]] = None, uncategorized: bool = False
) -> list[dict[str, Any]]:
    """Points currently «На складе» (all or only some of their components —
    the page shows which), with their components."""
    # pylower(...) (database.py) instead of a bare LIKE — SQLite's own LIKE
    # only case-folds ASCII, and addresses/descriptions here are Cyrillic.
    sql = _BASE_SELECT + f" where {_IN_WAREHOUSE}"
    params: list[Any] = []
    if search:
        s = f"%{search.strip()}%"
        sql += (
            " and (pylower(p.id) like pylower(?) or pylower(p.description) like pylower(?) "
            "or pylower(p.inventory_number) like pylower(?) or pylower(p.address) like pylower(?) "
            "or p.id in (select point_id from point_components where pylower(name) like pylower(?) "
            "or pylower(description) like pylower(?)))"
        )
        params.extend([s, s, s, s, s, s])
    category_ids = [c for c in (category_ids or []) if c]
    if uncategorized or category_ids:
        branches = []
        if uncategorized:
            branches.append("p.category_id is null")
        if category_ids:
            branches.append(f"p.category_id in ({', '.join('?' for _ in category_ids)})")
            params.extend(category_ids)
        sql += " and (" + " or ".join(branches) + ")"
    sql += " order by p.id"
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        items = [_row_to_dict(r) for r in await cur.fetchall()]
    return await _with_components(items)


async def get_for_point(point_id: str) -> Optional[dict[str, Any]]:
    """The warehouse card of ANY existing point — on the shelf or not (a
    point that's still in service can get a component put on the shelf from
    here, which makes it «На складе», shown as «Частично на складе» while
    something of it is not on the shelf). None only if the point doesn't
    exist."""
    async with get_connection() as conn:
        cur = await conn.execute(_BASE_SELECT + " where p.id = ?", (point_id,))
        row = await cur.fetchone()
    if not row:
        return None
    item = (await _with_components([_row_to_dict(row)]))[0]
    item["in_warehouse"] = item["status"] == WAREHOUSE_STATUS
    return item


def _upsert_sql() -> str:
    cols = ", ".join(FIELDS)
    placeholders = ", ".join("?" for _ in FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in FIELDS)
    return (
        f"insert into warehouse_items (point_id, {cols}, updated_at) "
        f"values (?, {placeholders}, ?) "
        f"on conflict(point_id) do update set {updates}, updated_at = excluded.updated_at"
    )


async def upsert(point_id: str, data: dict[str, Any]) -> None:
    """Full replace of the warehouse fields — used by the manual edit form
    (PUT /api/warehouse/points/{id}), which always submits the complete
    current state of the block."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    values = tuple((data.get(k) or None) for k in FIELDS)
    async with get_connection() as conn:
        await conn.execute(_upsert_sql(), (point_id, *values, now))
        await conn.commit()


async def bulk_upsert_from_import(point_id: str, data: dict[str, Any]) -> None:
    """Merges only the non-empty fields in `data` onto point_id's existing
    row (blank cells don't overwrite an already-set value) — used by the
    warehouse Excel import and the demontage report."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(f"select {', '.join(FIELDS)} from warehouse_items where point_id = ?", (point_id,))
        row = await cur.fetchone()
        existing = dict(zip(FIELDS, row)) if row else {k: None for k in FIELDS}
        merged = {k: (data.get(k) or existing.get(k)) for k in FIELDS}
        await conn.execute(_upsert_sql(), (point_id, *(merged[k] for k in FIELDS), now))
        await conn.commit()


async def delete_for_point(point_id: str) -> None:
    async with get_connection() as conn:
        await delete_for_point_conn(conn, point_id)
        await conn.commit()


async def delete_for_point_conn(conn, point_id: str) -> None:
    """On an already-open connection/transaction — called from
    points_repository when a point's status changes away from the warehouse
    statuses: the warehouse fields go, and every component is marked as no
    longer in stock (the list of components itself stays — it's the complex's
    composition). A point-id rename doesn't need an equivalent helper: both
    tables' point_id are real FKs with ON UPDATE CASCADE."""
    await conn.execute("delete from warehouse_items where point_id = ?", (point_id,))
    await point_components_repository.set_all_in_stock_conn(conn, point_id, False)
