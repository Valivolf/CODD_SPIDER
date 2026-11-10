"""Справочник типов выездов (таблица work_types) — «Настройки → Типы
выездов». Каждый тип: название и признак «связан с ремонтом» (в боте такой
тип получает меню ремонта, а в отчёте может нести «что сняли/поставили»).
Кеш, который читают синхронные места (app/core/work_types.py), обновляется
после каждой правки — см. load()."""
from __future__ import annotations

from typing import Any, Optional

from app.core import work_types
from app.db.database import get_connection


async def load() -> None:
    """Reads the table into app.core.work_types's in-memory copy."""
    async with get_connection() as conn:
        cur = await conn.execute("select name, is_repair, bot_tools from work_types order by sort_order, rowid")
        full = await cur.fetchall()
    rows = [(r[0], bool(r[1])) for r in full]
    if rows:
        work_types.set_work_types(rows)
        work_types.set_bot_tools_types([r[0] for r in full if r[2]])


async def list_all() -> list[dict[str, Any]]:
    """Types with how many tasks and visit reports currently use each — the
    settings screen shows it so removing a type that's still in use is a
    deliberate choice."""
    async with get_connection() as conn:
        cur = await conn.execute("select name, is_repair, bot_tools from work_types order by sort_order, rowid")
        types = [(r[0], bool(r[1]), bool(r[2])) for r in await cur.fetchall()]
        cur = await conn.execute("select type, count(*) from tasks group by type")
        task_counts = {r[0]: r[1] for r in await cur.fetchall()}
        cur = await conn.execute("select visit_type, count(*) from task_visit_reports group by visit_type")
        report_counts = {r[0]: r[1] for r in await cur.fetchall()}
        outcomes = await _all_outcomes(conn)
    return [
        {
            "name": n, "is_repair": r, "bot_tools": t, "tasks": task_counts.get(n, 0), "reports": report_counts.get(n, 0),
            "outcomes": outcomes.get(n, []),
        }
        for n, r, t in types
    ]


async def _all_outcomes(conn) -> dict[str, list[str]]:
    cur = await conn.execute("select type_name, label from work_type_outcomes order by sort_order, id")
    result: dict[str, list[str]] = {}
    for type_name, label in await cur.fetchall():
        result.setdefault(type_name, []).append(label)
    return result


async def outcomes_for(type_name: str) -> list[str]:
    async with get_connection() as conn:
        return (await _all_outcomes(conn)).get(type_name, [])


async def set_outcomes(type_name: str, labels: list[str]) -> list[str]:
    """Replaces the type's list of «статусов выезда» (the result buttons the
    bot shows at the end of a visit). Only a text record for reports and
    statistics — never touches the point's own status."""
    async with get_connection() as conn:
        await conn.execute("delete from work_type_outcomes where type_name = ?", (type_name,))
        await conn.executemany(
            "insert into work_type_outcomes (type_name, label, sort_order) values (?, ?, ?)",
            [(type_name, label, i) for i, label in enumerate(labels, start=1)],
        )
        await conn.commit()
    return labels


async def rename(old: str, new: str) -> bool:
    """Renames a type everywhere it is stored — the dictionary, existing
    tasks, visit reports (including the combined «<тип> + Ремонт» ones) and
    the type's own statuses — so nothing is orphaned under the old name."""
    from app.core.work_types import COMBINED_SEPARATOR, REPAIR_TYPE_NAME
    from app.repositories import state_repository

    async with get_connection() as conn:
        cur = await conn.execute("update work_types set name = ? where name = ?", (new, old))
        if cur.rowcount == 0:
            return False
        await conn.execute("update tasks set type = ? where type = ?", (new, old))
        await conn.execute("update task_visit_reports set visit_type = ? where visit_type = ?", (new, old))
        suffix = COMBINED_SEPARATOR + REPAIR_TYPE_NAME
        await conn.execute(
            "update task_visit_reports set visit_type = ? where visit_type = ?", (new + suffix, old + suffix)
        )
        await conn.execute("update work_type_outcomes set type_name = ? where type_name = ?", (new, old))
        await conn.commit()
    await state_repository.bump_rev()  # задания в календаре сменили тип
    await load()
    return True


async def get(name: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select name, is_repair, bot_tools from work_types where pylower(trim(name)) = pylower(trim(?))", (name,)
        )
        row = await cur.fetchone()
    return {"name": row[0], "is_repair": bool(row[1]), "bot_tools": bool(row[2])} if row else None


async def create(name: str, is_repair: bool, bot_tools: bool = False) -> dict[str, Any]:
    async with get_connection() as conn:
        cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from work_types")
        (next_order,) = await cur.fetchone()
        await conn.execute(
            "insert into work_types (name, is_repair, bot_tools, sort_order) values (?, ?, ?, ?)",
            (name, int(is_repair), int(bot_tools and not is_repair), next_order),
        )
        await conn.commit()
    await load()
    return {"name": name, "is_repair": is_repair, "bot_tools": bot_tools and not is_repair}


async def set_bot_tools(name: str, enabled: bool) -> bool:
    """«Проверка комплекса и SIM в боте» — кнопки простого меню бота у не связанного с ремонтом типа."""
    async with get_connection() as conn:
        cur = await conn.execute("update work_types set bot_tools = ? where name = ?", (int(enabled), name))
        await conn.commit()
        changed = cur.rowcount > 0
    await load()
    return changed


async def set_repair(name: str, is_repair: bool) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("update work_types set is_repair = ? where name = ?", (int(is_repair), name))
        await conn.commit()
        changed = cur.rowcount > 0
    await load()
    return changed


async def delete(name: str) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from work_types where name = ?", (name,))
        await conn.execute("delete from work_type_outcomes where type_name = ?", (name,))
        await conn.commit()
        removed = cur.rowcount > 0
    await load()
    return removed


async def count() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from work_types")
        (n,) = await cur.fetchone()
    return n
