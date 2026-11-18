"""Партионный учёт расходных материалов и движения между складом, людьми и АПК."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

import aiosqlite
from fastapi import status

from app.db.database import get_connection


class ConsumablesError(ValueError):
    def __init__(self, detail: str, status_code: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _clean_name(value: str, label: str, limit: int) -> str:
    result = " ".join((value or "").split())
    if not result:
        raise ConsumablesError(f"Укажите {label}")
    if len(result) > limit:
        raise ConsumablesError(f"{label.capitalize()} длиннее {limit} знаков")
    return result


async def list_items(*, active_only: bool = False) -> list[dict[str, Any]]:
    where = "where c.active = 1" if active_only else ""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""select c.id, c.name, c.unit, c.active, c.created_at, c.updated_at,
                       coalesce(sum(case
                         when m.action in ('receipt', 'return') then m.quantity
                         when m.action = 'issue' then -m.quantity else 0 end), 0) as stock_qty,
                       coalesce(sum(case
                         when m.action = 'issue' then m.quantity
                         when m.action in ('install', 'return') then -m.quantity else 0 end), 0) as held_qty
                from consumables c left join consumable_movements m on m.consumable_id = c.id
                {where}
                group by c.id order by c.active desc, c.name collate nocase""",
        )
        return [
            {"id": r[0], "name": r[1], "unit": r[2], "active": bool(r[3]), "created_at": r[4],
             "updated_at": r[5], "stock_quantity": r[6], "held_quantity": r[7]}
            for r in await cur.fetchall()
        ]


async def list_movements(limit: int = 500) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """select m.id, m.consumable_id, c.name, c.unit, m.action, m.quantity,
                      m.person_id, p.full_name, m.point_id, m.task_id, m.actor, m.note, m.created_at
               from consumable_movements m
               join consumables c on c.id = m.consumable_id
               left join people p on p.id = m.person_id
               order by m.id desc limit ?""",
            (max(1, min(int(limit), 2000)),),
        )
        return [
            {"id": r[0], "consumable_id": r[1], "name": r[2], "unit": r[3], "action": r[4],
             "quantity": r[5], "person_id": r[6], "person_name": r[7] or "", "point_id": r[8] or "",
             "task_id": r[9] or "", "actor": r[10], "note": r[11], "created_at": r[12]}
            for r in await cur.fetchall()
        ]


async def _stock_balance(conn, item_id: int) -> int:
    cur = await conn.execute(
        """select coalesce(sum(case
                 when action in ('receipt', 'return') then quantity
                 when action = 'issue' then -quantity else 0 end), 0)
           from consumable_movements where consumable_id = ?""",
        (item_id,),
    )
    (quantity,) = await cur.fetchone()
    return int(quantity)


async def _person_balance(conn, item_id: int, person_id: int) -> int:
    cur = await conn.execute(
        """select coalesce(sum(case
                 when action = 'issue' then quantity
                 when action in ('install', 'return') then -quantity else 0 end), 0)
           from consumable_movements where consumable_id = ? and person_id = ?""",
        (item_id, person_id),
    )
    (quantity,) = await cur.fetchone()
    return int(quantity)


async def _require_active_item(conn, item_id: int) -> tuple[str, str]:
    cur = await conn.execute("select name, unit from consumables where id = ? and active = 1", (item_id,))
    row = await cur.fetchone()
    if not row:
        raise ConsumablesError("Расходный материал не найден или отключён", status.HTTP_404_NOT_FOUND)
    return row[0], row[1]


async def _require_person(conn, person_id: int) -> str:
    cur = await conn.execute("select full_name from people where id = ? and active = 1", (person_id,))
    row = await cur.fetchone()
    if not row:
        raise ConsumablesError("Сотрудник не найден или неактивен", status.HTTP_404_NOT_FOUND)
    return row[0]


async def create_item(name: str, unit: str, quantity: int, actor: str) -> dict[str, Any]:
    name = _clean_name(name, "название", 120)
    unit = _clean_name(unit or "шт.", "единицу измерения", 24)
    if quantity < 0:
        raise ConsumablesError("Количество не может быть отрицательным")
    now = _now()
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        cur = await conn.execute("select id from consumables where pylower(name) = pylower(?)", (name,))
        if await cur.fetchone():
            raise ConsumablesError("Расходный материал с таким названием уже есть", status.HTTP_409_CONFLICT)
        cur = await conn.execute(
            "insert into consumables (name, unit, created_at, updated_at) values (?, ?, ?, ?)",
            (name, unit, now, now),
        )
        item_id = cur.lastrowid
        if quantity:
            await conn.execute(
                """insert into consumable_movements
                   (consumable_id, action, quantity, actor, note, created_at)
                   values (?, 'receipt', ?, ?, 'Начальный остаток', ?)""",
                (item_id, quantity, actor, now),
            )
        await conn.commit()
    return next(item for item in await list_items() if item["id"] == item_id)


async def update_item(item_id: int, name: str, unit: str, active: bool) -> dict[str, Any]:
    name = _clean_name(name, "название", 120)
    unit = _clean_name(unit, "единицу измерения", 24)
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        cur = await conn.execute(
            "select id from consumables where pylower(name) = pylower(?) and id != ?", (name, item_id)
        )
        if await cur.fetchone():
            raise ConsumablesError("Расходный материал с таким названием уже есть", status.HTTP_409_CONFLICT)
        cur = await conn.execute(
            "update consumables set name = ?, unit = ?, active = ?, updated_at = ? where id = ?",
            (name, unit, int(active), _now(), item_id),
        )
        if cur.rowcount == 0:
            raise ConsumablesError("Расходный материал не найден", status.HTTP_404_NOT_FOUND)
        await conn.commit()
    return next(item for item in await list_items() if item["id"] == item_id)


async def receive(item_id: int, quantity: int, actor: str, note: str = "") -> dict[str, Any]:
    if quantity <= 0:
        raise ConsumablesError("Количество должно быть больше нуля")
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        await _require_active_item(conn, item_id)
        await conn.execute(
            """insert into consumable_movements
               (consumable_id, action, quantity, actor, note, created_at)
               values (?, 'receipt', ?, ?, ?, ?)""",
            (item_id, quantity, actor, (note or "").strip()[:500], _now()),
        )
        await conn.execute("update consumables set updated_at = ? where id = ?", (_now(), item_id))
        await conn.commit()
    return next(item for item in await list_items() if item["id"] == item_id)


async def issue(item_id: int, person_id: int, quantity: int, actor: str, note: str = "") -> dict[str, Any]:
    if quantity <= 0:
        raise ConsumablesError("Количество должно быть больше нуля")
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        await _require_active_item(conn, item_id)
        await _require_person(conn, person_id)
        stock = await _stock_balance(conn, item_id)
        if quantity > stock:
            raise ConsumablesError(f"На складе осталось {stock} шт.")
        await conn.execute(
            """insert into consumable_movements
               (consumable_id, action, quantity, person_id, actor, note, created_at)
               values (?, 'issue', ?, ?, ?, ?, ?)""",
            (item_id, quantity, person_id, actor, (note or "").strip()[:500], _now()),
        )
        await conn.execute("update consumables set updated_at = ? where id = ?", (_now(), item_id))
        await conn.commit()
    return next(item for item in await list_items() if item["id"] == item_id)


async def return_to_stock(item_id: int, person_id: int, quantity: int, actor: str, note: str = "") -> dict[str, Any]:
    if quantity <= 0:
        raise ConsumablesError("Количество должно быть больше нуля")
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        await _require_active_item(conn, item_id)
        await _require_person(conn, person_id)
        held = await _person_balance(conn, item_id, person_id)
        if quantity > held:
            raise ConsumablesError(f"У сотрудника числится {held} шт.")
        await conn.execute(
            """insert into consumable_movements
               (consumable_id, action, quantity, person_id, actor, note, created_at)
               values (?, 'return', ?, ?, ?, ?, ?)""",
            (item_id, quantity, person_id, actor, (note or "").strip()[:500], _now()),
        )
        await conn.execute("update consumables set updated_at = ? where id = ?", (_now(), item_id))
        await conn.commit()
    return next(item for item in await list_items() if item["id"] == item_id)


async def person_balances(person_id: int, *, active_only: bool = True) -> list[dict[str, Any]]:
    where = "and c.active = 1" if active_only else ""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""select c.id, c.name, c.unit,
                       coalesce(sum(case
                         when m.action = 'issue' then m.quantity
                         when m.action in ('install', 'return') then -m.quantity else 0 end), 0) as quantity
                from consumables c
                left join consumable_movements m on m.consumable_id = c.id and m.person_id = ?
                where 1 = 1 {where}
                group by c.id
                having quantity > 0
                order by c.name collate nocase""",
            (person_id,),
        )
        return [{"id": r[0], "name": r[1], "unit": r[2], "quantity": r[3]} for r in await cur.fetchall()]


async def record_task_installations(
    task_id: str, point_id: str, person_id: int, items: list[dict[str, int]], actor: str
) -> list[dict[str, int]]:
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        result = await record_task_installations_conn(conn, task_id, point_id, person_id, items, actor)
        await conn.commit()
    return result


async def record_task_installations_conn(
    conn: aiosqlite.Connection, task_id: str, point_id: str, person_id: int,
    items: list[dict[str, int]], actor: str,
) -> list[dict[str, int]]:
    aggregated: dict[int, int] = defaultdict(int)
    for item in items:
        item_id = int(item["item_id"])
        quantity = int(item["quantity"])
        if item_id <= 0 or quantity <= 0:
            raise ConsumablesError("Укажите расходный материал и количество больше нуля")
        aggregated[item_id] += quantity
    requested = dict(aggregated)
    if not requested:
        return []
    task_id = _clean_name(task_id, "номер задания", 120)
    point_id = _clean_name(point_id, "номер комплекса", 160)
    await _require_person(conn, person_id)
    cur = await conn.execute("select 1 from points where id = ?", (point_id,))
    if not await cur.fetchone():
        raise ConsumablesError("Комплекс не найден", status.HTTP_404_NOT_FOUND)
    cur = await conn.execute(
        "select consumable_id, quantity, person_id, point_id from consumable_movements "
        "where task_id = ? and action = 'install'",
        (task_id,),
    )
    existing_rows = await cur.fetchall()
    existing = {int(r[0]): int(r[1]) for r in existing_rows}
    if existing:
        if existing != requested or any(r[2] != person_id or r[3] != point_id for r in existing_rows):
            raise ConsumablesError(
                "Расходники этого выезда уже записаны с другими количествами",
                status.HTTP_409_CONFLICT,
            )
        return [{"item_id": item_id, "quantity": quantity} for item_id, quantity in sorted(requested.items())]
    for item_id, quantity in requested.items():
        await _require_active_item(conn, item_id)
        held = await _person_balance(conn, item_id, person_id)
        if quantity > held:
            raise ConsumablesError(f"У монтёра недостаточно материала №{item_id}: на руках {held} шт.")
    now = _now()
    for item_id, quantity in requested.items():
        await conn.execute(
            """insert into consumable_movements
               (consumable_id, action, quantity, person_id, point_id, task_id, actor, note, created_at)
               values (?, 'install', ?, ?, ?, ?, ?, 'Установка из отчёта бота', ?)""",
            (item_id, quantity, person_id, point_id, task_id, actor, now),
        )
        await conn.execute("update consumables set updated_at = ? where id = ?", (now, item_id))
    return [{"item_id": item_id, "quantity": quantity} for item_id, quantity in sorted(requested.items())]
