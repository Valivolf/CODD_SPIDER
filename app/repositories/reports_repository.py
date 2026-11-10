"""Дневные/недельные сводки для страницы /reports — по бригадам, за
выбранный период: план (назначенные задания) против факта (сданных
отчётов о выезде), с разбивкой по исходам и типам выезда.

Everything here is derived from tasks + crews + crew_members + people +
task_visit_reports (see task_visit_reports_repository.py for the outcome
vocabulary). No writes happen here.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any, Optional

from app.config import get_settings
from app.core.dates import format_date
from app.core.work_types import AUX_TYPE_NAME
from app.db.database import get_connection
from app.models.report_fields import DEFAULT_PLAN_TITLE, PLAN_TEMPLATE_TYPE
from app.repositories import report_values_repository
from app.repositories.task_visit_reports_repository import RESULT_STATUSES, visit_types

# How each RESULT_STATUSES value reads as a completed-visit outcome in a
# dispatcher-facing report (a verb, matching the style of the hand-written
# daily reports this page replaces), rather than the raw status name.
_RESULT_LABELS = {
    "Работает": "Восстановлено",
    "Не работает": "Осталось неисправно",
    "Демонтирован": "Демонтировано",
    "Установлен": "Установлено",
}

_MEMBERS_SELECT = (
    "select p.full_name, p.role_type from crew_members cm "
    "join people p on p.id = cm.person_id where cm.crew_id = ? "
    "order by case p.role_type when 'fitter' then 0 when 'driver' then 1 else 2 end, p.full_name"
)


async def _crew_members(conn, crew_id: int) -> list[dict[str, str]]:
    cur = await conn.execute(_MEMBERS_SELECT, (crew_id,))
    return [{"full_name": r[0], "role_type": r[1]} for r in await cur.fetchall()]


def _crew_label(crew_id: Optional[int], crew_name: Optional[str]) -> str:
    if crew_name:
        return crew_name
    return f"Бригада без названия (№{crew_id})" if crew_id else "Без бригады"


def _new_group(date: str, crew_id: int, label: str) -> dict[str, Any]:
    return {
        "date": date,
        "crew_id": crew_id,
        "crew_label": label,
        "assigned": 0,
        "reported": 0,
        "visit_types": Counter(),
        "result_statuses": Counter(),
        "outcomes": Counter(),
    }


def _report_local_day(created_at: str, tz_offset_minutes: int) -> str:
    """День, в который сдан отчёт, по часам пользователя: время отчёта хранится в UTC, а `tz_offset_minutes` —
    как отдаёт JavaScript (getTimezoneOffset): UTC минус местное (для Москвы −180)."""
    moment = dt.datetime.fromisoformat(created_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (moment - dt.timedelta(minutes=tz_offset_minutes)).date().isoformat()


async def _trip_day_groups(conn, date_from: str, date_to: str, tz_offset_minutes: int) -> dict[tuple[str, int], dict[str, Any]]:
    """Бригады в командировке — по ФАКТИЧЕСКИМ дням работы. Задания командировки стоят на день, когда их создали
    (обычно — начало поездки), а отчёты по ним сдают и в следующие дни; раньше всё писалось на дату начала, и за
    «сегодня» бригада в командировке в отчёте не появлялась. Теперь у каждого дня поездки (у бригады есть строка на
    этот день) считаем: `reported` — отчёты, сданные именно в этот день; `assigned` — они же плюс задания, которые
    к концу этого дня ещё остались открытыми (созданные не позже этого дня)."""
    cur = await conn.execute(
        "select id, date, name, trip_id from crews where trip_id is not null and date >= ? and date <= ? order by date, id",
        (date_from, date_to),
    )
    crew_days = await cur.fetchall()
    trip_tasks: dict[str, list[tuple]] = {}
    for trip_id in sorted({r[3] for r in crew_days}):
        cur = await conn.execute(
            """
            select t.date, r.visit_type, r.result_status, r.outcome, r.created_at
            from tasks t
            join crews c on c.id = t.team
            left join task_visit_reports r on r.task_id = t.id
            where c.trip_id = ? and t.type != ?
            """,
            (trip_id, AUX_TYPE_NAME),
        )
        trip_tasks[trip_id] = await cur.fetchall()
    groups: dict[tuple[str, int], dict[str, Any]] = {}
    for crew_id, day, name, trip_id in crew_days:
        g = _new_group(day, crew_id, _crew_label(crew_id, name))
        for task_date, visit_type, result_status, outcome, created_at in trip_tasks.get(trip_id, []):
            if task_date > day:
                continue                      # задание появится позже этого дня
            if not visit_type:
                g["assigned"] += 1            # к концу дня остаётся без отчёта
                continue
            report_day = _report_local_day(created_at, tz_offset_minutes)
            if report_day < day:
                continue                      # закрыто в один из прошлых дней
            g["assigned"] += 1
            if report_day == day:
                g["reported"] += 1
                g["visit_types"][visit_type] += 1
                g["result_statuses"][result_status] += 1
                if outcome:
                    g["outcomes"][(visit_type, outcome)] += 1
        if g["assigned"]:
            groups[(day, crew_id)] = g
    return groups


async def crew_day_breakdown(date_from: str, date_to: str, tz_offset_minutes: int = 0) -> list[dict[str, Any]]:
    """One row per (date, crew) in [date_from, date_to]: assigned/reported
    task counts plus visit_type/result_status breakdowns and crew
    composition. `assigned` counts every task for that crew that day;
    `reported` counts only those with a submitted visit report (a task
    without one contributes to neither breakdown below).

    Обычные бригады — по дате задания. Бригады в командировке — по фактическим дням работы
    (см. _trip_day_groups), `tz_offset_minutes` нужен, чтобы определить «день отчёта» по часам пользователя."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select t.date, t.team, c.name, r.visit_type, r.result_status, r.outcome
            from tasks t
            left join crews c on c.id = t.team
            left join task_visit_reports r on r.task_id = t.id
            where t.date >= ? and t.date <= ? and t.type != ? and c.trip_id is null
            order by t.date, t.team
            """,
            (date_from, date_to, AUX_TYPE_NAME),
        )
        rows = await cur.fetchall()

        grouped: dict[tuple[str, int], dict[str, Any]] = await _trip_day_groups(conn, date_from, date_to, tz_offset_minutes)
        for date, team, crew_name, visit_type, result_status, outcome in rows:
            key = (date, team)
            if key not in grouped:
                grouped[key] = _new_group(date, team, _crew_label(team, crew_name))
            g = grouped[key]
            g["assigned"] += 1
            if visit_type:
                g["reported"] += 1
                g["visit_types"][visit_type] += 1
                g["result_statuses"][result_status] += 1
                if outcome:
                    g["outcomes"][(visit_type, outcome)] += 1

        result = []
        for (date, team), g in sorted(grouped.items()):
            g["members"] = await _crew_members(conn, team) if team else []
            result.append(g)
        return result


def build_text_report(rows: list[dict[str, Any]], date_from: str, date_to: str) -> str:
    """Renders crew_day_breakdown() rows as a plain-text report meant to be
    copy-pasted into a chat, matching the style of the hand-written daily
    reports dispatchers already send."""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    multi_day = date_from != date_to
    lines: list[str] = []
    period_label = f"{_fmt(date_from)} г." if not multi_day else f"{_fmt(date_from)} — {_fmt(date_to)} г."
    lines.append(f"Отчёт о проделанной работе за {period_label}")
    lines.append("_" * 47)

    total_reported = total_assigned = 0
    total_results: Counter = Counter()

    for date in sorted(by_date):
        day_rows = by_date[date]
        if multi_day:
            lines.append("")
            lines.append(_fmt(date))
            lines.append("_" * 47)
        for i, crew in enumerate(day_rows, start=1):
            members = ", ".join(m["full_name"] for m in crew["members"])
            header = crew["crew_label"] + (f" ({members})" if members else "")
            lines.append(f"{i}) {header}")
            lines.append(f" Обследовано: {crew['reported']}")
            not_done = crew["assigned"] - crew["reported"]
            for status in RESULT_STATUSES:
                count = crew["result_statuses"].get(status, 0)
                if count:
                    lines.append(f" {_RESULT_LABELS[status]}: {count}")
            known_types = visit_types()
            extra_types = sorted(t for t in crew["visit_types"] if t not in known_types)
            for vt in [*known_types, *extra_types]:
                count = crew["visit_types"].get(vt, 0)
                if count:
                    lines.append(f" {vt}: {count}")
            for (vt, outcome), count in sorted(crew.get("outcomes", {}).items()):
                lines.append(f" {vt} — {outcome}: {count}")
            if not_done > 0:
                lines.append(f" Не выполнено (нет отчёта): {not_done}")
            lines.append("")

        day_reported = sum(c["reported"] for c in day_rows)
        day_assigned = sum(c["assigned"] for c in day_rows)
        day_results: Counter = Counter()
        for c in day_rows:
            day_results.update(c["result_statuses"])
        total_reported += day_reported
        total_assigned += day_assigned
        total_results.update(day_results)

        if multi_day:
            lines.append("_" * 47)
            lines.append(_totals_line("Итого за день", day_reported, day_results))

    if multi_day:
        lines.append("")
        lines.append("=" * 47)
        lines.append(_totals_line("Всего за период", total_reported, total_results))
    else:
        lines.append("_" * 47)
        lines.append(_totals_line("Всего", total_reported, total_results))

    return "\n".join(lines).strip() + "\n"


def _totals_line(label: str, reported: int, results: Counter) -> str:
    parts = [f"обследовано АПК: {reported}"]
    for status in RESULT_STATUSES:
        count = results.get(status, 0)
        if count:
            parts.append(f"{_RESULT_LABELS[status].lower()}: {count}")
    return f"{label} — " + ", ".join(parts)


async def daily_report_computed(day: str, tz_offset_minutes: int = 0) -> dict[str, int]:
    """The computed half of the "ежедневный отчёт" form (see
    app/models/report_fields.py::DAILY_REPORT_FIELDS) — everything the app
    can derive on its own for one calendar date. Everything else on that
    form (staff headcount, vehicles, solar panels, modems) isn't tracked
    anywhere in the app and stays operator-entered, see
    report_values_repository.py.
    """
    crew_rows = await crew_day_breakdown(day, day, tz_offset_minutes)
    tasks_total = sum(c["assigned"] for c in crew_rows)
    tasks_done = sum(c["reported"] for c in crew_rows)

    async with get_connection() as conn:
        # Итоги выездов за день — из тех же строк по бригадам (бригада в командировке — по дню сдачи отчёта).
        result_counts: Counter = Counter()
        for crew in crew_rows:
            result_counts.update(crew["result_statuses"])

        cur = await conn.execute(
            "select count(*) from point_status_history "
            "where old_status in ('На складе', 'Частично на складе') and new_status in ('Работает', 'Установлен') "
            "and substr(changed_at, 1, 10) = ?",
            (day,),
        )
        (restored_after_removal,) = await cur.fetchone()

        cur = await conn.execute("select count(*) from crews where date = ?", (day,))
        (crews_count,) = await cur.fetchone()

        cur = await conn.execute(
            "select coalesce(p.role_type, ''), count(distinct p.id) from crews c "
            "join crew_members cm on cm.crew_id = c.id "
            "join people p on p.id = cm.person_id "
            "where c.date = ? group by p.role_type",
            (day,),
        )
        role_counts = {k: v for k, v in await cur.fetchall()}

    return {
        "restored_onsite": result_counts.get("Работает", 0),
        "serviced_onsite": sum(result_counts.values()),
        "removed_for_repair": result_counts.get("Демонтирован", 0),
        "restored_after_removal": restored_after_removal,
        "tasks_total": tasks_total,
        "tasks_done": tasks_done,
        "tasks_not_done": tasks_total - tasks_done,
        "crews_count": crews_count,
        "staff_involved_electricians": role_counts.get("fitter", 0),
        "staff_involved_drivers": role_counts.get("driver", 0),
    }


def _fmt(iso_date: str) -> str:
    return format_date(iso_date)


async def plan_template_values() -> dict[str, str]:
    """Letterhead fields for the printable «План-задание» (see
    routes_plan_print.py) — saved values (report_field_values, see
    routes_reports.py's PUT /plan-template) win, falling back to the .env
    plan_* settings so a fresh install still has something sensible."""
    settings = get_settings()
    defaults = {
        "plan_title": DEFAULT_PLAN_TITLE,
        "org_title": settings.plan_org_title,
        "approver_name": settings.plan_approver_name,
        "route_label": settings.plan_route_label,
        "safety_officer": settings.plan_safety_officer,
        "second_approver_title": settings.plan_second_approver_title,
        "second_approver_name": settings.plan_second_approver_name,
        "plan_title_size_pt": "15",
        "plan_header_shift_mm": "0",
        "plan_title_align": "center",
    }
    saved = await report_values_repository.get_values(PLAN_TEMPLATE_TYPE, report_values_repository.GLOBAL_PERIOD)
    if not (saved.get("plan_title") or "").strip():
        saved.pop("plan_title", None)      # пустой заголовок бланка бессмыслен — остаётся стандартный
    return {**defaults, **saved}


async def build_template_context(
    date_from: str, date_to: str, days: list[str] | None = None, tz_offset_minutes: int = 0
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The fill-in data for an operator-uploaded .docx/.xlsx report
    template (see app/services/template_engine.py and
    app/models/report_fields.py::template_variable_catalog) — a flat
    scalar context for {{key}} placeholders, plus one row-record per
    (date, crew) in the period for a repeating {{row.*}} table row."""
    rows = await crew_day_breakdown(date_from, date_to, tz_offset_minutes)
    if days is not None:
        # Выбраны отдельные дни (не все между крайними) — итоги только по ним.
        selected = set(days)
        rows = [r for r in rows if r["date"] in selected]
    total_assigned = sum(r["assigned"] for r in rows)
    total_reported = sum(r["reported"] for r in rows)
    total_results: Counter = Counter()
    for r in rows:
        total_results.update(r["result_statuses"])
    crews_active = len({r["crew_id"] for r in rows if r["crew_id"]})

    context: dict[str, Any] = {
        "date_from": _fmt(date_from),
        "date_to": _fmt(date_to),
        "period_label": _fmt(date_from) if date_from == date_to else f"{_fmt(date_from)} — {_fmt(date_to)}",
        "text_report": build_text_report(rows, date_from, date_to),
        "total_assigned": total_assigned,
        "total_reported": total_reported,
        "total_not_done": total_assigned - total_reported,
        "total_restored": total_results.get("Работает", 0),
        "total_dismantled": total_results.get("Демонтирован", 0),
        "total_installed": total_results.get("Установлен", 0),
        "total_not_working": total_results.get("Не работает", 0),
        "crews_active": crews_active,
    }
    if date_from == date_to:
        context["date"] = _fmt(date_from)
    context.update(await plan_template_values())

    row_records = []
    for i, r in enumerate(rows, start=1):
        members = ", ".join(m["full_name"] for m in r["members"])
        row_records.append({
            "index": i,
            "date": _fmt(r["date"]),
            "crew": r["crew_label"],
            "members": members,
            "assigned": r["assigned"],
            "reported": r["reported"],
            "not_done": r["assigned"] - r["reported"],
            "restored": r["result_statuses"].get("Работает", 0),
            "dismantled": r["result_statuses"].get("Демонтирован", 0),
            "installed": r["result_statuses"].get("Установлен", 0),
            "not_working": r["result_statuses"].get("Не работает", 0),
        })
    return context, row_records
