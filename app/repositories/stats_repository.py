"""Read-only aggregates for the /stats dashboard — everything here is a
GROUP BY over task_visit_reports (per-visit reports), point_status_history
(every status change, tagged with its source) or the live points table
(current snapshot). No writes happen here.
"""
from __future__ import annotations

from typing import Any

from app.db.database import get_connection
from app.repositories.task_visit_reports_repository import RESULT_STATUSES, visit_types


def _date_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    # changed_at/created_at are ISO datetimes; date_to's day should be
    # inclusive, so bound it at the start of the following day.
    return date_from, date_to + "T23:59:59.999999"


async def visit_type_breakdown(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """All current work types, in dictionary order, zero-filled — so a type
    with no reports this period (e.g. "Опиловка") still shows as an explicit
    0 rather than silently disappearing from the list. Visit types that are
    not in the dictionary (a combined «Инвентаризация + Ремонт», or a type
    removed from the settings later) follow, so the counts always add up."""
    frm, to = _date_bounds(date_from, date_to)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select visit_type, count(*) from task_visit_reports "
            "where created_at >= ? and created_at <= ? group by visit_type",
            (frm, to),
        )
        counts = {r[0]: r[1] for r in await cur.fetchall()}
    known = visit_types()
    rows = [{"visit_type": t, "count": counts.get(t, 0)} for t in known]
    extras = sorted((t for t in counts if t and t not in known), key=lambda t: (-counts[t], t))
    return rows + [{"visit_type": t, "count": counts[t]} for t in extras]


async def outcome_breakdown(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """«Статусы выезда» (итоги работ из «Настройки → Типы выездов»: «Найден» /
    «Не найден»…) за период — по типу выезда, сколько раз какой. Это только
    записи в отчётах, на статус комплекса они не влияют."""
    frm, to = _date_bounds(date_from, date_to)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select visit_type, outcome, count(*) from task_visit_reports "
            "where outcome != '' and created_at >= ? and created_at <= ? "
            "group by visit_type, outcome order by visit_type, count(*) desc, outcome",
            (frm, to),
        )
        return [{"visit_type": r[0], "outcome": r[1], "count": r[2]} for r in await cur.fetchall()]


async def result_status_breakdown(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """All RESULT_STATUSES, fixed order, zero-filled — same reasoning as
    visit_type_breakdown."""
    frm, to = _date_bounds(date_from, date_to)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select result_status, count(*) from task_visit_reports "
            "where created_at >= ? and created_at <= ? group by result_status",
            (frm, to),
        )
        counts = {r[0]: r[1] for r in await cur.fetchall()}
    return [{"result_status": s, "count": counts.get(s, 0)} for s in RESULT_STATUSES]


async def current_status_distribution() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select case when status = '' then 'Не известно' else status end as s, count(*) "
            "from points group by s order by count(*) desc"
        )
        return [{"status": r[0], "count": r[1]} for r in await cur.fetchall()]


async def currently_down_since() -> list[tuple[str, str]]:
    """Every point that is *currently* "Не работает" with the moment its ongoing down streak began — (point_id, since).
    Считается один раз на запрос сводки и дальше режется по периодам (_in_range): «отвалилось» и дневной график берут из
    одного и того же списка."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select p.id,
                   (select max(h.changed_at) from point_status_history h
                    where h.point_id = p.id and h.new_status = p.status) as since
            from points p
            where p.status = 'Не работает'
            """
        )
        rows = await cur.fetchall()
    return [(point_id, since) for point_id, since in rows if since]


def _in_range(down: list[tuple[str, str]], date_from: str, date_to: str) -> list[tuple[str, str]]:
    """Points that are *currently* "Не работает" whose ongoing down streak
    began within [date_from, date_to] — (point_id, since) pairs. This is
    the shared basis for both the "отвалилось" tile and the daily trend,
    and it's what makes flapping not inflate either: only the latest
    transition into the point's *current* status counts (a point that
    dropped and came back by now has status='Работает' again, so it's
    simply not in this list at all — it isn't "un-counted" after the fact,
    it was never eligible once it recovered)."""
    frm, to = _date_bounds(date_from, date_to)
    return [(point_id, since) for point_id, since in down if frm <= since <= to]


async def _currently_down_since_in_range(date_from: str, date_to: str) -> list[tuple[str, str]]:
    return _in_range(await currently_down_since(), date_from, date_to)


async def transitions_summary(date_from: str, date_to: str, down_all: list[tuple[str, str]] | None = None) -> dict[str, int]:
    # "Отвалилось" is scoped to a single day (date_to — "текущий день" by
    # default, or whichever date the picker is set to), not the whole
    # date_from..date_to range: a wide period tile summing every day's
    # drops reads as a much scarier, less actionable number than "how many
    # are down as of the day I'm looking at" — see daily_failures below for
    # the actual day-by-day trend, which still needs the full range.
    down = _in_range(down_all if down_all is not None else await currently_down_since(), date_to, date_to)
    frm, to = _date_bounds(date_from, date_to)
    async with get_connection() as conn:
        # "recovered" counts a down→up transition only if it was either (a)
        # the team's own work — a Ремонт task happened sometime during that
        # down streak — or (b) the point was down for 2+ days and came back
        # on its own: a real, notable recovery either way. A shorter,
        # un-worked blip (ping flakiness that cleared itself within under
        # two days) doesn't count — it would just inflate the "мы починили
        # N точек" number without reflecting real work or a real outage.
        # Dates are compared as YYYY-MM-DD (substr on the ISO timestamps)
        # rather than full datetimes for the Ремонт-task check, since
        # tasks.date has no time component and a naive string compare
        # against a datetime would wrongly exclude a same-day task.
        cur = await conn.execute(
            """
            with recovered_events as (
                select h.point_id, h.changed_at,
                       (select max(h2.changed_at) from point_status_history h2
                        where h2.point_id = h.point_id and h2.new_status = 'Не работает'
                          and h2.changed_at <= h.changed_at) as down_since
                from point_status_history h
                where h.new_status = 'Работает' and h.old_status = 'Не работает'
                  and h.changed_at >= ? and h.changed_at <= ?
            )
            select count(*) from recovered_events e
            where e.down_since is not null
              and (
                julianday(e.changed_at) - julianday(e.down_since) >= 2
                or exists (
                  select 1 from tasks t
                  where t.point = e.point_id and t.type = 'Ремонт'
                    and t.date >= substr(e.down_since, 1, 10)
                    and t.date <= substr(e.changed_at, 1, 10)
                )
              )
            """,
            (frm, to),
        )
        (recovered,) = await cur.fetchone()
    return {"went_down": len(down), "recovered": recovered}


async def daily_failures(date_from: str, date_to: str, down_all: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    down = _in_range(down_all if down_all is not None else await currently_down_since(), date_from, date_to)
    counts: dict[str, int] = {}
    for _point_id, since in down:
        d = since[:10]
        counts[d] = counts.get(d, 0) + 1
    return [{"date": d, "count": c} for d, c in sorted(counts.items())]


async def points_status_detail() -> list[dict[str, Any]]:
    """Every point with what the «Выгрузка статусов АПК» second sheet needs:
    number, current status, when its port (plain ping if it has none) last
    started failing (null while up — see points_repository.search_points),
    IP, address and category. Same population as category_status_matrix, so
    the two sheets of the export reconcile."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select p.id,
                   case when p.status = '' then 'Не известно' else p.status end,
                   p.port_down_since, p.ip_address, p.address,
                   coalesce(pc.name, 'Без категории')
            from points p
            left join point_categories pc on pc.id = p.category_id
            order by p.id
            """
        )
        keys = ["point_id", "status", "port_down_since", "ip_address", "address", "category"]
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


async def category_status_matrix() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select coalesce(pc.name, 'Без категории') as category,
                   case when p.status = '' then 'Не известно' else p.status end as s,
                   count(*)
            from points p
            left join point_categories pc on pc.id = p.category_id
            group by category, s
            order by category, s
            """
        )
        return [{"category": r[0], "status": r[1], "count": r[2]} for r in await cur.fetchall()]
