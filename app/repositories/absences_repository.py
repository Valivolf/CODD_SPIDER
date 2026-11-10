"""Отсутствия людей (отпуск, больничный…) для страницы «Отсутствия» — тот же источник, что и «Персонал →
Отсутствия» в календаре (таблица people_absences): отметили здесь — человек сразу серый при сборе бригады."""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.db.database import get_connection

_COLUMNS = "a.id, a.person_id, a.start_date, a.end_date, a.reason, a.created_at, a.created_by"
_KEYS = ["id", "person_id", "start_date", "end_date", "reason", "created_at", "created_by"]


def _row(r) -> dict[str, Any]:
    d = dict(zip(_KEYS, r))
    d["reason"] = d["reason"] or ""
    return d


async def list_people(include_inactive: bool = False) -> list[dict[str, Any]]:
    """Только то, что нужно странице (имя и роль): телефоны, VK ID и примечания этой роли не отдаём."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, full_name, role_type, active from people "
            + ("" if include_inactive else "where active = 1 ")
            + "order by full_name collate nocase"
        )
        return [
            {"id": r[0], "full_name": r[1], "role_type": r[2], "active": bool(r[3])}
            for r in await cur.fetchall()
        ]


async def list_absences(date_from: str | None = None, date_to: str | None = None, person_id: int | None = None) -> list[dict[str, Any]]:
    """Периоды, которые пересекаются с [date_from, date_to] (границы необязательны)."""
    where, params = [], []
    if date_from:
        where.append("a.end_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("a.start_date <= ?")
        params.append(date_to)
    if person_id is not None:
        where.append("a.person_id = ?")
        params.append(person_id)
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from people_absences a join people p on p.id = a.person_id"
            + (" where " + " and ".join(where) if where else "")
            + " order by a.start_date, a.id",
            params,
        )
        return [_row(r) for r in await cur.fetchall()]


async def get(absence_id: int) -> dict[str, Any] | None:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from people_absences a where a.id = ?", (absence_id,))
        r = await cur.fetchone()
    return _row(r) if r else None


async def find_same(person_id: int, start_date: str, end_date: str, reason: str) -> dict[str, Any] | None:
    """Уже есть точно такой же период (повторное нажатие «Сохранить») — второй раз не заводим."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from people_absences a "
            "where a.person_id = ? and a.start_date = ? and a.end_date = ? and coalesce(a.reason, '') = ?",
            (person_id, start_date, end_date, reason),
        )
        r = await cur.fetchone()
    return _row(r) if r else None


async def create(person_id: int, start_date: str, end_date: str, reason: str, created_by: str | None) -> dict[str, Any]:
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into people_absences (person_id, start_date, end_date, reason, created_at, created_by) "
            "values (?, ?, ?, ?, ?, ?)",
            (person_id, start_date, end_date, reason or None, created_at, created_by),
        )
        await conn.commit()
        absence_id = cur.lastrowid
    return await get(absence_id)  # type: ignore[arg-type,return-value]


async def update(absence_id: int, start_date: str, end_date: str, reason: str) -> dict[str, Any] | None:
    async with get_connection() as conn:
        await conn.execute(
            "update people_absences set start_date = ?, end_date = ?, reason = ? where id = ?",
            (start_date, end_date, reason or None, absence_id),
        )
        await conn.commit()
    return await get(absence_id)


async def delete(absence_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from people_absences where id = ?", (absence_id,))
        await conn.commit()


async def crew_conflicts(person_id: int, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Бригады, в которые человек уже назначен в дни отсутствия: календарь его из бригады не убирает сам —
    диспетчеру об этом надо сказать."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select c.date, c.name from crews c join crew_members m on m.crew_id = c.id "
            "where m.person_id = ? and c.date >= ? and c.date <= ? order by c.date, c.id",
            (person_id, start_date, end_date),
        )
        return [{"date": r[0], "crew": r[1] or "Бригада без названия"} for r in await cur.fetchall()]
