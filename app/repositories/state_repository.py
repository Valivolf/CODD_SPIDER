"""Optimistic-concurrency access to the calendar's task list — the one
piece of the calendar domain still edited as a whole-document compare-
and-swap (see app.js's `save()`).

Points ("комплексы") are not part of this — they're a per-row REST
resource now (app/repositories/points_repository.py), since they're
edited from two independent places (the calendar's point form and the
dedicated /warehouse page) and a wipe-and-reinsert-everything write would
let one silently clobber the other's concurrent change. Бригады (crews)
are likewise their own resource (app/repositories/crew_repository.py).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from app.core import map_events
from app.core.point_ref import load_refs
from app.core.text import decode_entities
from app.core.work_types import AUX_TYPE_NAME, is_aux_type
from app.core import stats_cache
from app.db.database import get_connection

# Guards the read-modify-write below within this process. SQLite's own
# `begin immediate` still protects against a second process (e.g. a
# future bot opening the same file directly).
_write_lock = asyncio.Lock()


async def read_rev() -> int:
    """Just the revision counter, no tasks query — used by GET /api/state's
    since_rev short-circuit so a poll that finds nothing changed doesn't
    pay for reading/serializing the whole task list every 5 seconds."""
    async with get_connection() as conn:
        cur = await conn.execute("select rev from state_rev where id = 1")
        (rev,) = await cur.fetchone()
        return rev


def _aux_coord(t: dict[str, Any], key: str) -> str:
    """Координата вспомогательной точки — только у задания этого типа, числом с точкой."""
    if not is_aux_type(t.get("type")):
        return ""
    return str(t.get(key) or "").strip().replace(",", ".")


async def read_state(include_aux: bool = True) -> tuple[int, dict[str, Any]]:
    """`include_aux=False` — без «вспомогательных точек» (метки на карте, не работа): для планов,
    отчётов и статистики; календарь и карты бригад читают всё."""
    async with get_connection() as conn:
        cur = await conn.execute("select rev from state_rev where id = 1")
        (rev,) = await cur.fetchone()

        cur = await conn.execute("select task_id from task_visit_reports where imported = 0")
        reported_ids = {r[0] for r in await cur.fetchall()}
        cur = await conn.execute("select task_id from aux_task_done")
        aux_done_ids = {r[0] for r in await cur.fetchall()}

        cur = await conn.execute(
            "select id, date, team, point, work, address, hours, travel, type, note, trip_id, date_to, priority, "
            "aux_lat, aux_lon from tasks" + ("" if include_aux else " where type != ?"),
            () if include_aux else (AUX_TYPE_NAME,),
        )
        tasks = [
            {
                "id": r[0],
                "date": r[1],
                "team": r[2],
                "point": r[3],
                "work": r[4],
                "address": r[5],
                "hours": r[6],
                "travel": r[7],
                "type": r[8],
                "note": r[9],
                "trip_id": r[10],
                "date_to": r[11],
                "priority": bool(r[12]),
                "aux_lat": r[13] or "",
                "aux_lon": r[14] or "",
                # Read-only, not part of the CAS write (write_state below
                # only ever persists the columns it explicitly lists) —
                # drives the green "Выполнено" mark on the calendar, see
                # static/js/app.js's crewCardHtml.
                "reported": r[0] in reported_ids or (r[8] == AUX_TYPE_NAME and r[0] in aux_done_ids),
            }
            for r in await cur.fetchall()
        ]

    return rev, {"tasks": tasks}


async def bump_rev() -> int:
    """Advances state_rev without touching the tasks table itself — for
    anything that changes calendar-visible task metadata out of band (a
    visit report toggling a task's "reported" flag, see
    task_visit_reports_repository.upsert/delete_for_task), so the
    dispatcher's periodic /api/state poll (see app.js's load()) notices
    and re-renders instead of only picking it up on the next full reload."""
    async with _write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            cur = await conn.execute("select rev from state_rev where id = 1")
            (current_rev,) = await cur.fetchone()
            new_rev = current_rev + 1
            await conn.execute("update state_rev set rev = ? where id = 1", (new_rev,))
            await conn.commit()
        map_events.kick()   # отчёт сдан / изменилась пометка «выполнено» — карты бригад пересчитать сразу
        return new_rev


async def mark_aux_done(task_id: str, done_by: str | None) -> None:
    """Монтёр нажал в боте «Принято» у вспомогательной точки — она «выполнена» (зелёная на карте бригады и в
    календаре). В статистику не попадает: отчёта о выезде у такой точки нет."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "insert into aux_task_done (task_id, done_at, done_by) values (?, ?, ?) "
            "on conflict(task_id) do update set done_at = excluded.done_at, done_by = excluded.done_by",
            (task_id, now, done_by),
        )
        await conn.commit()
    await bump_rev()   # календарь перерисует точку, а карта бригады перекрасит её в зелёный


async def point_ids_on(day: str, team: int) -> set[str]:
    """Normalised (trimmed, lower-cased — same rule as app.js's normId)
    numbers of the points that are already added when a task for `team` on
    `day` is about to be: any task that day (whichever бригада has it), plus
    every task of the same командировка (the crew's sibling days, see
    app/core/trips.py) — so a point isn't taken twice, and one already done
    earlier in a trip isn't counted as a fresh task on a later day. A bulk
    add skips these instead of duplicating them."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select point from tasks where date = ? and type != ? "
            "union select point from tasks where type != ? and team in ("
            "  select id from crews where trip_id is not null "
            "  and trip_id = (select trip_id from crews where id = ?))",
            (day, AUX_TYPE_NAME, AUX_TYPE_NAME, team),
        )
        return {(r[0] or "").strip().lower() for r in await cur.fetchall()}


def _point_text(refs, t: dict[str, Any]) -> str:
    """Что записать в задание как комплекс: если набранное не совпало с номером комплекса точно, но однозначно совпало «без разбора» (регистр, пробел, латинская буква
    вместо русской — app/core/point_ref.py), пишем настоящий номер: иначе у задания не будет данных комплекса, а фото и отчёты лягут «не на тот» номер.
    Вспомогательная точка — исключение: в этом поле её собственная подпись."""
    point = t.get("point") or ""
    if point and t.get("type") != AUX_TYPE_NAME and point not in refs.ids:
        return refs.resolve(point) or point
    return point


@stats_cache.invalidates
async def add_tasks(new_tasks: list[dict[str, Any]]) -> int:
    """Appends tasks without touching (or needing to know) the existing
    ones — for bulk actions that don't go through the calendar's own CAS
    edit loop (see app.js's save()), e.g. converting several selected
    «план» entries into tasks for one бригада/day at once (see
    routes_point_plans.py's /assign). Plain INSERT, so there's no
    expected_rev/conflict to resolve on the caller's side."""
    if not new_tasks:
        return await read_rev()
    async with _write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            cur = await conn.execute("select rev from state_rev where id = 1")
            (current_rev,) = await cur.fetchone()
            refs = await load_refs(conn)
            for t in new_tasks:
                await conn.execute(
                    "insert into tasks (id, date, team, point, work, address, hours, travel, type, note, "
                    "trip_id, date_to, priority, aux_lat, aux_lon) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(t["id"]),
                        t["date"],
                        int(t["team"]),
                        _point_text(refs, t),
                        decode_entities(t.get("work")),
                        decode_entities(t.get("address")),
                        float(t.get("hours") or 0),
                        float(t.get("travel") or 0),
                        t.get("type") or "",
                        decode_entities(t.get("note")),
                        t.get("trip_id") or None,
                        t.get("date_to") or None,
                        int(bool(t.get("priority"))),
                        _aux_coord(t, "aux_lat"),
                        _aux_coord(t, "aux_lon"),
                    ),
                )
            new_rev = current_rev + 1
            await conn.execute("update state_rev set rev = ? where id = 1", (new_rev,))
            await conn.commit()
        map_events.kick()
        return new_rev


@stats_cache.invalidates
async def write_state(expected_rev: int, data: dict[str, Any]) -> int:
    """Compare-and-swap write. Returns the new revision.

    The frontend always sends its complete in-memory task list back
    (never a partial patch — see app.js's `save()`), so this replaces the
    full contents of `tasks` from `data` in one transaction.

    Raises ValueError('conflict') if expected_rev is stale.
    """
    async with _write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            cur = await conn.execute("select rev from state_rev where id = 1")
            (current_rev,) = await cur.fetchone()
            if current_rev != expected_rev:
                await conn.rollback()
                raise ValueError("conflict")

            await conn.execute("delete from tasks")
            refs = await load_refs(conn)
            for t in data["tasks"]:
                await conn.execute(
                    "insert into tasks (id, date, team, point, work, address, hours, travel, type, note, trip_id, date_to, priority, "
                    "aux_lat, aux_lon) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(t["id"]),
                        t["date"],
                        int(t["team"]),
                        _point_text(refs, t),
                        decode_entities(t.get("work")),
                        decode_entities(t.get("address")),
                        float(t.get("hours") or 0),
                        float(t.get("travel") or 0),
                        t.get("type") or "",
                        decode_entities(t.get("note")),
                        t.get("trip_id") or None,
                        t.get("date_to") or None,
                        int(bool(t.get("priority"))),
                        _aux_coord(t, "aux_lat"),
                        _aux_coord(t, "aux_lon"),
                    ),
                )

            await conn.execute("delete from aux_task_done where task_id not in (select id from tasks)")
            await conn.execute("delete from bot_task_progress where task_id not in (select id from tasks)")
            new_rev = current_rev + 1
            await conn.execute("update state_rev set rev = ? where id = 1", (new_rev,))
            await conn.commit()
        map_events.kick()
        return new_rev
