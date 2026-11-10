"""CRUD for бригады (crews) — an ad-hoc group of people plus a transport
choice, created for one date (typically while importing that day's KML
tasks), replacing the old persistent "Бригада №N" slots."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Optional

from app.core import map_events
from app.db.database import get_connection

_CREW_COLS = "id, date, name, equip, map_link, trip_id, place, contractor, contractor_note"


def _crew_row(r) -> dict[str, Any]:
    return {
        "id": r[0], "date": r[1], "name": r[2], "equip": r[3], "map_link": r[4], "trip_id": r[5], "place": r[6],
        "contractor": bool(r[7]), "contractor_note": r[8] or "",
    }


_MEMBERS_SELECT = (
    "select p.id, p.full_name, p.phone, p.role_type, p.driver_kind from crew_members cm "
    "join people p on p.id = cm.person_id where cm.crew_id = ? "
    "order by case p.role_type when 'fitter' then 0 when 'driver' then 1 else 2 end, p.full_name"
)


async def _members(conn, crew_id: int) -> list[dict[str, Any]]:
    cur = await conn.execute(_MEMBERS_SELECT, (crew_id,))
    return [{"id": r[0], "full_name": r[1], "phone": r[2], "role_type": r[3], "driver_kind": r[4] or ""} for r in await cur.fetchall()]


async def _trip_info(conn, trip_id: str) -> dict[str, Any]:
    """Every crew row sharing `trip_id` (one per day of the командировка,
    same composition) — the trip's date range is just min/max of their
    dates, no separate columns needed."""
    cur = await conn.execute("select id, date from crews where trip_id = ? order by date, id", (trip_id,))
    rows = await cur.fetchall()
    return {
        "trip_crew_ids": [r[0] for r in rows],
        "trip_from": rows[0][1] if rows else None,
        "trip_to": rows[-1][1] if rows else None,
    }


async def _attach_trip_info(conn, crew: dict[str, Any]) -> None:
    if crew.get("trip_id"):
        crew.update(await _trip_info(conn, crew["trip_id"]))


async def list_for_date(date: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_CREW_COLS} from crews where date = ? order by id", (date,))
        crews = [_crew_row(r) for r in await cur.fetchall()]
        for crew in crews:
            crew["members"] = await _members(conn, crew["id"])
            await _attach_trip_info(conn, crew)
        return crews


async def _list_where(conn, where: str, params: tuple) -> list[dict[str, Any]]:
    cur = await conn.execute(f"select {_CREW_COLS} from crews where {where} order by date, id", params)
    crews = [_crew_row(r) for r in await cur.fetchall()]
    for crew in crews:
        await _attach_trip_info(conn, crew)
    return crews


async def list_between(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Бригады за период (без состава) — с данными командировки (дни, даты)."""
    async with get_connection() as conn:
        return await _list_where(conn, "date >= ? and date <= ?", (date_from, date_to))


async def list_with_constructor_links() -> list[dict[str, Any]]:
    """Бригады, в поле ссылки которых лежит карта Конструктора Яндекса."""
    async with get_connection() as conn:
        return await _list_where(conn, "map_link like ?", ("%um=constructor%",))


async def get_crew(crew_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_CREW_COLS} from crews where id = ?", (crew_id,))
        row = await cur.fetchone()
        if not row:
            return None
        crew = _crew_row(row)
        crew["members"] = await _members(conn, crew_id)
        await _attach_trip_info(conn, crew)
        return crew


def _clean_note(note: str | None) -> str:
    return " ".join((note or "").split())[:120]


async def create_crew(
    *, date: str, name: str, equip: str, person_ids: list[int], map_link: str = "", place: str = "",
    contractor: bool = False, contractor_note: str = "",
) -> dict[str, Any]:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from crews where date = ?", (date,))
        (existing_count,) = await cur.fetchone()
        label = name.strip() if name and name.strip() else f"Бригада {existing_count + 1}"
        cur = await conn.execute(
            "insert into crews (date, name, equip, map_link, place, contractor, contractor_note) values (?, ?, ?, ?, ?, ?, ?)",
            (date, label, equip or "Лестница", map_link.strip(), place.strip(), int(bool(contractor)), _clean_note(contractor_note) if contractor else ""),
        )
        crew_id = cur.lastrowid
        for person_id in person_ids:
            await conn.execute(
                "insert or ignore into crew_members (crew_id, person_id) values (?, ?)", (crew_id, person_id)
            )
        await conn.commit()
    map_events.kick()
    return await get_crew(crew_id)  # type: ignore[return-value]


async def update_crew(
    crew_id: int, *, name: str | None, equip: str | None, person_ids: list[int] | None,
    map_link: str | None = None, place: str | None = None,
    contractor: bool | None = None, contractor_note: str | None = None,
) -> None:
    """Правит бригаду. Для бригады в командировке то же самое применяется
    ко всем её дням (строкам с общим trip_id) — название, место, транспорт,
    ссылка и состав одни на всю командировку, иначе на других днях бригада
    осталась бы со старым названием/составом."""
    async with get_connection() as conn:
        cur = await conn.execute("select name, equip, map_link, place, trip_id, contractor, contractor_note from crews where id = ?", (crew_id,))
        row = await cur.fetchone()
        if not row:
            return
        trip_id = row[4]
        new_contractor = int(bool(contractor)) if contractor is not None else int(row[5] or 0)
        new_contractor_note = (_clean_note(contractor_note) if contractor_note is not None else (row[6] or "")) if new_contractor else ''
        new_name = name.strip() if name is not None and name.strip() else row[0]
        new_equip = equip if equip is not None else row[1]
        new_map_link = map_link.strip() if map_link is not None else row[2]
        new_place = place.strip() if place is not None else row[3]

        cur = await conn.execute(
            "select id from crews where trip_id = ?" if trip_id else "select id from crews where id = ?",
            (trip_id or crew_id,),
        )
        targets = [r[0] for r in await cur.fetchall()] or [crew_id]
        for target in targets:
            await conn.execute(
                "update crews set name = ?, equip = ?, map_link = ?, place = ?, contractor = ?, contractor_note = ? where id = ?",
                (new_name, new_equip, new_map_link, new_place, new_contractor, new_contractor_note, target),
            )
            if person_ids is not None:
                await conn.execute("delete from crew_members where crew_id = ?", (target,))
                for person_id in person_ids:
                    await conn.execute(
                        "insert or ignore into crew_members (crew_id, person_id) values (?, ?)", (target, person_id)
                    )
        await conn.commit()
    map_events.kick()


async def delete_crew(crew_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from crews where id = ?", (crew_id,))
        await conn.commit()
    map_events.kick()


async def start_trip(crew_id: int, date_to: str) -> Optional[dict[str, Any]]:
    """Marks `crew` as on a многодневная командировка through `date_to`:
    clones it (same name/equip/members) onto every following date in
    range, all sharing a fresh trip_id, and tags the original row too.
    Tasks are untouched — trip-awareness only changes which tasks count as
    "this crew's" on a given day, see app/core/trips.py. Returns None if
    the crew doesn't exist or is already on a trip."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select date, name, equip, map_link, trip_id, place, contractor, contractor_note from crews where id = ?", (crew_id,)
        )
        row = await cur.fetchone()
        if not row or row[4]:
            return None
        start_date, name, equip, map_link, place = row[0], row[1], row[2], row[3], row[5]
        contractor, contractor_note = int(row[6] or 0), row[7] or ""
        if date_to <= start_date:
            return None
        trip_id = uuid.uuid4().hex
        cur = await conn.execute(_MEMBERS_SELECT, (crew_id,))
        person_ids = [r[0] for r in await cur.fetchall()]

        d = dt.date.fromisoformat(start_date) + dt.timedelta(days=1)
        end = dt.date.fromisoformat(date_to)
        while d <= end:
            cur = await conn.execute(
                "insert into crews (date, name, equip, map_link, trip_id, place, contractor, contractor_note) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (d.isoformat(), name, equip, map_link, trip_id, place, contractor, contractor_note),
            )
            new_crew_id = cur.lastrowid
            for person_id in person_ids:
                await conn.execute(
                    "insert or ignore into crew_members (crew_id, person_id) values (?, ?)",
                    (new_crew_id, person_id),
                )
            d += dt.timedelta(days=1)

        await conn.execute("update crews set trip_id = ? where id = ?", (trip_id, crew_id))
        await conn.commit()
    map_events.kick()
    return await get_crew(crew_id)


async def cancel_trip(crew_id: int) -> bool:
    """Un-tags every crew row sharing this crew's trip_id — the day-rows
    themselves (and any tasks already assigned under them) are left alone,
    they just stop being cross-visible beyond their own day."""
    async with get_connection() as conn:
        cur = await conn.execute("select trip_id from crews where id = ?", (crew_id,))
        row = await cur.fetchone()
        if not row or not row[0]:
            return False
        await conn.execute("update crews set trip_id = null where trip_id = ?", (row[0],))
        await conn.commit()
    map_events.kick()
    return True


async def count_tasks(crew_id: int) -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from tasks where team = ?", (crew_id,))
        (n,) = await cur.fetchone()
        return n
