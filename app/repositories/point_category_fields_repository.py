"""Дополнительные поля комплексов, общие и привязанные к категории."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection


FIELD_TYPES = frozenset({"text", "number", "currency", "date", "textarea"})


def _field(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "category_id": row[1],
        "category_name": row[2] or "",
        "name": row[3],
        "field_type": row[4],
        "accounting_only": bool(row[5]),
        "sort_order": row[6],
        "value_count": row[7],
    }


async def list_fields(category_id: Optional[int] = None, *, exact_category: bool = False) -> list[dict[str, Any]]:
    where = ""
    params: tuple[Any, ...] = ()
    if category_id is not None:
        where = "where f.category_id = ?" if exact_category else "where f.category_id is null or f.category_id = ?"
        params = (category_id,)
    elif exact_category:
        where = "where f.category_id is null"
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""select f.id, f.category_id, c.name, f.name, f.field_type, f.accounting_only,
                       f.sort_order, count(v.point_id)
                from point_category_fields f
                left join point_categories c on c.id = f.category_id
                left join point_category_field_values v on v.field_id = f.id
                {where}
                group by f.id
                order by f.category_id is not null, f.sort_order, f.name collate nocase""",
            params,
        )
        return [_field(row) for row in await cur.fetchall()]


async def get_field(field_id: int) -> Optional[dict[str, Any]]:
    items = await list_fields()
    return next((item for item in items if item["id"] == field_id), None)


async def create_field(
    category_id: Optional[int], name: str, field_type: str, accounting_only: bool
) -> dict[str, Any]:
    name = " ".join(name.split())
    accounting_only = bool(accounting_only and category_id is None)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id from point_category_fields where category_id is ? and pylower(name) = pylower(?)",
            (category_id, name),
        )
        if await cur.fetchone():
            raise ValueError("Поле с таким названием уже есть в этой категории")
        cur = await conn.execute(
            "select coalesce(max(sort_order), 0) + 1 from point_category_fields where category_id is ?",
            (category_id,),
        )
        (order,) = await cur.fetchone()
        cur = await conn.execute(
            """insert into point_category_fields
               (category_id, name, field_type, accounting_only, sort_order, created_at)
               values (?, ?, ?, ?, ?, ?)""",
            (category_id, name, field_type, int(accounting_only), order, now),
        )
        await conn.commit()
        field_id = cur.lastrowid
    return await get_field(field_id)


async def update_field(
    field_id: int, name: str, field_type: str, accounting_only: bool
) -> bool:
    name = " ".join(name.split())
    async with get_connection() as conn:
        cur = await conn.execute("select category_id from point_category_fields where id = ?", (field_id,))
        row = await cur.fetchone()
        if not row:
            return False
        category_id = row[0]
        accounting_only = bool(accounting_only and category_id is None)
        cur = await conn.execute(
            "select id from point_category_fields where category_id is ? and pylower(name) = pylower(?) and id != ?",
            (category_id, name, field_id),
        )
        if await cur.fetchone():
            raise ValueError("Поле с таким названием уже есть в этой категории")
        await conn.execute(
            "update point_category_fields set name = ?, field_type = ?, accounting_only = ? where id = ?",
            (name, field_type, int(accounting_only), field_id),
        )
        await conn.commit()
    return True


async def delete_field(field_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from point_category_fields where id = ?", (field_id,))
        await conn.commit()
        return cur.rowcount > 0


async def fields_for_point(point_id: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """select f.id, f.category_id, c.name, f.name, f.field_type, f.accounting_only,
                      f.sort_order, coalesce(v.value, '')
               from points p
               join point_category_fields f on f.category_id is null or f.category_id = p.category_id
               left join point_categories c on c.id = f.category_id
               left join point_category_field_values v on v.point_id = p.id and v.field_id = f.id
               where p.id = ?
               order by f.accounting_only, f.category_id is not null, f.sort_order, f.name collate nocase""",
            (point_id,),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": row[0], "category_id": row[1], "category_name": row[2] or "", "name": row[3],
            "field_type": row[4], "accounting_only": bool(row[5]), "sort_order": row[6], "value": row[7],
        }
        for row in rows
    ]


async def point_exists(point_id: str) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from points where id = ?", (point_id,))
        return await cur.fetchone() is not None


async def save_values(point_id: str, values: dict[int, str]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "select category_id from points where id = ?", (point_id,)
        )
        point = await cur.fetchone()
        if not point:
            raise LookupError("Комплекс не найден")
        for field_id, value in values.items():
            cur = await conn.execute(
                "select 1 from point_category_fields where id = ? and (category_id is null or category_id = ?)",
                (field_id, point[0]),
            )
            if not await cur.fetchone():
                raise ValueError("Поле не относится к категории этого комплекса")
            if value:
                await conn.execute(
                    """insert into point_category_field_values (point_id, field_id, value, updated_at)
                       values (?, ?, ?, ?)
                       on conflict(point_id, field_id) do update set value = excluded.value, updated_at = excluded.updated_at""",
                    (point_id, field_id, value, now),
                )
            else:
                await conn.execute(
                    "delete from point_category_field_values where point_id = ? and field_id = ?",
                    (point_id, field_id),
                )
        await conn.commit()
