"""«Планы» — точки, замеченные оператором как нуждающиеся в выезде, но ещё
не привязанные ни к бригаде, ни к дате. Отдельное хранилище от tasks: при
добавлении задания оператор выбирает точку из плана (см.
app/api/routes_point_plans.py), и запись удаляется — план "расходуется" в
задание, а не копируется/остаётся висеть.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core.text import decode_entities
from app.db.database import get_connection

_SELECT = (
    "select pp.id, pp.point_id, pp.description, pp.created_at, pp.created_by, pp.priority, pp.group_name, pp.work_type, "
    "p.address as point_address, p.status as point_status, "
    "p.category_id, pc.name as category_name, pc.color as category_color, p.verification_valid_until "
    "from point_plans pp left join points p on p.id = pp.point_id "
    "left join point_categories pc on pc.id = p.category_id"
)


def _row_to_dict(row) -> dict[str, Any]:
    keys = [
        "id", "point_id", "description", "created_at", "created_by", "priority", "group_name", "work_type", "point_address", "point_status",
        "category_id", "category_name", "category_color", "verification_valid_until",
    ]
    d = dict(zip(keys, row))
    d["priority"] = bool(d["priority"])
    d["group_name"] = d["group_name"] or ""
    d["work_type"] = d["work_type"] or ""
    # Only used by the plan lists' client-side filters (category chips,
    # certificate) — a plan entry whose point isn't in the directory (yet)
    # just has none of them.
    for k in ("category_name", "category_color", "verification_valid_until"):
        d[k] = d[k] or ""
    return d


async def list_all() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " order by pp.created_at desc")
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def get(plan_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " where pp.id = ?", (plan_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def find_by_point(point_id: str) -> Optional[dict[str, Any]]:
    """The plan entry already holding this point (same trimmed,
    case-insensitive number rule as everywhere else), if any."""
    key = decode_entities(point_id).strip().lower()
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " where pylower(trim(pp.point_id)) = ? limit 1", (key,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


def clean_group(name: str | None) -> str:
    """Название группы: лишние пробелы убраны, не длиннее 60 символов."""
    return " ".join(decode_entities(name or "").split())[:60]


async def group_names() -> list[str]:
    async with get_connection() as conn:
        cur = await conn.execute("select distinct group_name from point_plans where group_name != ''")
        return sorted((r[0] for r in await cur.fetchall()), key=lambda x: x.lower())


async def _canonical_group(name: str) -> str:
    """«рейд 1» и «Рейд 1» — одна группа: берём написание, которое уже есть."""
    name = clean_group(name)
    if not name:
        return ""
    for existing in await group_names():
        if existing.lower() == name.lower():
            return existing
    return name


async def create(point_id: str, description: str, created_by: str, priority: bool = False,
                 group_name: str = "", work_type: str = "") -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    group_name = await _canonical_group(group_name)
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into point_plans (point_id, description, created_at, created_by, priority, group_name, work_type) values (?, ?, ?, ?, ?, ?, ?)",
            (decode_entities(point_id).strip(), decode_entities(description).strip(), now, created_by, int(priority), group_name, (work_type or "").strip()),
        )
        plan_id = cur.lastrowid
        await conn.commit()
    return await get(plan_id)  # type: ignore[return-value]


async def set_priority(plan_id: int, priority: bool) -> None:
    async with get_connection() as conn:
        await conn.execute("update point_plans set priority = ? where id = ?", (int(priority), plan_id))
        await conn.commit()


async def delete(plan_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from point_plans where id = ?", (plan_id,))
        await conn.commit()


async def set_group(plan_ids: list[int], name: str) -> str:
    """Собирает записи в группу `name` (пустое имя — убирает из группы). Возвращает итоговое название."""
    name = await _canonical_group(name)
    async with get_connection() as conn:
        await conn.executemany("update point_plans set group_name = ? where id = ?", [(name, i) for i in plan_ids])
        await conn.commit()
    return name


async def set_work_type(plan_ids: list[int], work_type: str) -> None:
    async with get_connection() as conn:
        await conn.executemany("update point_plans set work_type = ? where id = ?", [((work_type or "").strip(), i) for i in plan_ids])
        await conn.commit()
