"""CRUD for the people table (монтёры/водители/операторы)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core import field_crypto as fc
from app.db.database import get_connection

_COLUMNS = "id, full_name, phone, role_type, vk_id, active, notes, created_at, driver_kind"
_KEYS = ["id", "full_name", "phone", "role_type", "vk_id", "active", "notes", "created_at", "driver_kind"]


def _kind(role_type: str, driver_kind: str | None) -> str:
    """Вид водителя имеет смысл только у роли «водитель»."""
    return driver_kind if role_type == "driver" and driver_kind in ("tower", "cover") else ""


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(zip(_KEYS, row))
    d["active"] = bool(d["active"])
    return d


async def list_people(*, role_type: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
    query = f"select {_COLUMNS} from people"
    clauses = []
    params: list[Any] = []
    if role_type:
        clauses.append("role_type = ?")
        params.append(role_type)
    if active_only:
        clauses.append("active = 1")
    if clauses:
        query += " where " + " and ".join(clauses)
    query += " order by full_name"
    async with get_connection() as conn:
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def get_person(person_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from people where id = ?", (person_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def get_by_vk_id(vk_id: int) -> Optional[dict[str, Any]]:
    """Auth lookup for the монтёр bot — see routes_bot.py's /me."""
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from people where vk_id = ?", (vk_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def create_person(
    *, full_name: str, phone: str | None, role_type: str, notes: str | None, vk_id: int | None = None, driver_kind: str | None = None
) -> dict[str, Any]:
    async with get_connection() as conn:
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = await conn.execute(
            "insert into people (full_name_enc, phone_enc, role_type, vk_id, active, notes, created_at, driver_kind) "
            "values (?, ?, ?, ?, 1, ?, ?, ?)",
            (fc.encrypt("people.full_name", full_name), fc.encrypt("people.phone", phone), role_type, vk_id, notes, created_at, _kind(role_type, driver_kind)),
        )
        await conn.commit()
        person_id = cur.lastrowid
    return await get_person(person_id)  # type: ignore[return-value]


async def update_person(
    person_id: int,
    *,
    full_name: str,
    phone: str | None,
    role_type: str,
    active: bool,
    notes: str | None,
    vk_id: int | None = None,
    driver_kind: str | None = None,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "update people set full_name_enc = ?, phone_enc = ?, role_type = ?, active = ?, notes = ?, vk_id = ?, driver_kind = ? where id = ?",
            (fc.encrypt("people.full_name", full_name), fc.encrypt("people.phone", phone), role_type, int(active), notes, vk_id, _kind(role_type, driver_kind), person_id),
        )
        await conn.commit()


async def delete_person(person_id: int) -> None:
    """Удаляет человека. Из бригад (всех дней) он убирается сам: в таблице crew_members внешний ключ на people создан без каскада, и без этого удаление любого, кто хоть раз стоял
    в составе бригады, падало с «FOREIGN KEY constraint failed». Отсутствия и чат уходят каскадом, оборудование на нём не числится (проверяет маршрут), оборудование в журнале остаётся."""
    async with get_connection() as conn:
        await conn.execute("delete from crew_members where person_id = ?", (person_id,))
        await conn.execute("delete from people where id = ?", (person_id,))
        await conn.commit()


_ABSENCE_COLUMNS = "id, person_id, start_date, end_date, reason, created_at"
_ABSENCE_KEYS = ["id", "person_id", "start_date", "end_date", "reason", "created_at"]


async def list_absences(person_id: int) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_ABSENCE_COLUMNS} from people_absences where person_id = ? order by start_date desc",
            (person_id,),
        )
        rows = await cur.fetchall()
    return [dict(zip(_ABSENCE_KEYS, r)) for r in rows]


async def create_absence(person_id: int, *, start_date: str, end_date: str, reason: str | None) -> dict[str, Any]:
    async with get_connection() as conn:
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = await conn.execute(
            "insert into people_absences (person_id, start_date, end_date, reason, created_at) "
            "values (?, ?, ?, ?, ?)",
            (person_id, start_date, end_date, reason, created_at),
        )
        await conn.commit()
        absence_id = cur.lastrowid
        cur = await conn.execute(
            f"select {_ABSENCE_COLUMNS} from people_absences where id = ?", (absence_id,)
        )
        row = await cur.fetchone()
    return dict(zip(_ABSENCE_KEYS, row))  # type: ignore[arg-type]


async def delete_absence(absence_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from people_absences where id = ?", (absence_id,))
        await conn.commit()


async def list_absent_on(date: str) -> dict[int, str]:
    """person_id -> reason, for everyone with an absence period covering `date`."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select person_id, reason from people_absences where start_date <= ? and end_date >= ?",
            (date, date),
        )
        rows = await cur.fetchall()
    return {person_id: (reason or "") for person_id, reason in rows}
