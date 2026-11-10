"""Read-only aggregates for the /monitoring page — live work sessions come
from bot_work_sessions_repository, photos from visit_photos_repository;
this is just the "требует проверки" reconciliation query (a fitter
reported a Ремонт/Установка as "Работает", but the automated ping/port
check still doesn't agree).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.work_types import REPAIR_TYPE_NAME, repair_type_names
from app.db.database import get_connection

_RECENT_WINDOW_HOURS = 48


async def needs_check() -> list[dict[str, Any]]:
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=_RECENT_WINDOW_HOURS)).isoformat()
    # Отчёты по связанным с ремонтом типам («Ремонт», «Установка», «Подключение
    # кабеля»… — «Настройки → Типы выездов») и их сочетания с ремонтом
    # («Инвентаризация + Ремонт»).
    repair_types = repair_type_names()
    type_marks = ",".join("?" for _ in repair_types) or "''"
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""
            select p.id, p.address, p.status, r.created_at
            from points p
            join task_visit_reports r on r.point_id = p.id
            where (r.visit_type in ({type_marks}) or r.visit_type like '% + ' || ?)
              and r.result_status = 'Работает'
              and r.imported = 0
              and r.created_at = (
                  select max(r2.created_at) from task_visit_reports r2 where r2.point_id = p.id and r2.imported = 0
              )
              and r.created_at >= ?
              and p.status != 'Работает'
            order by r.created_at desc
            """,
            (*repair_types, REPAIR_TYPE_NAME, cutoff),
        )
        rows = await cur.fetchall()
    keys = ["point_id", "point_address", "current_status", "reported_at"]
    return [dict(zip(keys, r)) for r in rows]


def _crew_label(crew_id: int | None, crew_name: str | None) -> str:
    if crew_name:
        return crew_name
    return f"Бригада без названия (№{crew_id})" if crew_id else "Без бригады"


def _local_day_bounds(date_from: str, date_to: str, tz_offset_minutes: int) -> tuple[str, str]:
    """Границы суток по ЧАСАМ БРАУЗЕРА в UTC — время отчётов хранится в UTC. `tz_offset_minutes` — как отдаёт
    JavaScript (getTimezoneOffset): UTC минус местное, в минутах (для Москвы −180)."""
    start = dt.datetime.fromisoformat(date_from) + dt.timedelta(minutes=tz_offset_minutes)
    end = dt.datetime.fromisoformat(date_to) + dt.timedelta(days=1, minutes=tz_offset_minutes)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


async def point_summaries(
    date_from: str, date_to: str, by_report_date: bool = False, tz_offset_minutes: int = 0,
) -> list[dict[str, Any]]:
    """Итоги по каждому комплексу за период — один отчёт монтёра на
    строку, отсортировано по бригаде (в порядке выезда внутри бригады).

    По умолчанию период и поле `date` — дата ЗАДАНИЯ (так работают выгрузки). С `by_report_date` — период берётся
    по дню, когда отчёт реально отправлен (`reported_at`): задание бригады в командировке стоит на день начала
    поездки, а отчёт по нему могут сдать на третий день — на странице мониторинга нужен именно этот день."""
    if by_report_date:
        low, high = _local_day_bounds(date_from, date_to, tz_offset_minutes)
        where, params, order = "r.created_at >= ? and r.created_at < ?", (low, high), "r.created_at, r.id"
    else:
        where, params, order = "t.date >= ? and t.date <= ?", (date_from, date_to), "t.team, t.date, p.address"
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""
            select t.team, c.name, t.date, t.point, p.address, t.work,
                   r.visit_type, r.result_status, r.outcome, r.comment, coalesce(pe.full_name, r.created_by), r.task_id, r.created_at,
                   r.fault, r.needs, p.power_source, r.equipment_removed, r.equipment_installed,
                   r.sim_removed, r.sim_installed, r.sim_removed_state, r.source, t.type, r.author_person_id
            from task_visit_reports r
            left join people pe on pe.id = r.author_person_id
            join tasks t on t.id = r.task_id
            left join crews c on c.id = t.team
            left join points p on p.id = t.point
            where {where}
            order by {order}
            """,
            params,
        )
        rows = await cur.fetchall()
    keys = ["crew_id", "crew_name", "date", "point_id", "point_address", "work", "visit_type", "result_status", "outcome", "comment", "created_by", "task_id", "reported_at", "fault", "needs",
            "power_source", "equipment_removed", "equipment_installed", "sim_removed", "sim_installed", "sim_removed_state", "source", "task_type", "author_person_id"]
    result = []
    for row in rows:
        d = dict(zip(keys, row))
        d["created_by"] = (d["created_by"] or "")[3:].strip() if (d["created_by"] or "").lower().startswith("vk:") else d["created_by"]
        d["crew_label"] = _crew_label(d["crew_id"], d.pop("crew_name"))
        d["power_source"] = d["power_source"] or ""
        d["sim_removed_state"] = d["sim_removed_state"] or ""
        result.append(d)
    return result


# Ordered (key, header label) pairs — the field picker on /reports' «Итоги
# по комплексам» export offers exactly these, in this order. Mirrors the
# same row shape point_summaries() above already returns, so no separate
# export-only query is needed.
EXPORT_FIELDS = [
    ("date", "Дата"),
    ("crew_label", "Бригада"),
    ("point_id", "Комплекс"),
    ("point_address", "Адрес"),
    ("power_source", "Тип питания"),
    ("work", "Что нужно сделать (план)"),
    ("visit_type", "Тип выезда"),
    ("comment", "Что сделано (комментарий)"),
    ("result_status", "Статус (работает/не работает)"),
    ("outcome", "Статус выезда"),
    ("equipment_removed", "Снято оборудование"),
    ("equipment_installed", "Установлено оборудование"),
    ("sim_removed", "SIM снята"),
    ("sim_removed_state", "Состояние снятой SIM"),
    ("sim_installed", "SIM установлена"),
    ("fault", "Что неисправно"),
    ("needs", "Что нужно для исправления"),
    ("created_by", "Исполнитель"),
]
