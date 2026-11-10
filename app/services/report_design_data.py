"""Данные для отчётов из конструктора: что подставлять в переменные.

Три уровня, как в самом шаблоне (см. app/models/report_design.py):
  - общие — {{period_label}}, {{total_assigned}} и сводные итоги;
    (тот же набор, что у загружаемых .docx/.xlsx, см. reports_repository.
    build_template_context);
  - бригада — {{crew.*}}: один повтор секции «для каждой бригады» на каждую
    бригаду каждого дня периода;
  - задание — {{task.*}}: строка таблицы «для каждого задания» (задания
    бригады внутри секции бригады, а вне неё — все задания периода).

Итог задания считается так же, как в выгрузке «Выезды бригад по дням»
(excel_service.period_outcome): есть отчёт — его статус; нет отчёта и день
прошёл — «Не выехали»; день ещё не наступил или сегодня — «Ещё не было выезда».
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from app.core.dates import format_date, format_datetime
from app.core.day_selection import MAX_DAYS, describe_days
from app.core.text import decode_entities
from app.models.report_fields import template_variable_catalog
from app.repositories import (
    crew_repository,
    points_repository,
    reports_repository,
    state_repository,
    task_visit_reports_repository,
)
from app.services.excel_service import PERIOD_NOT_VISITED, PERIOD_NOT_YET, period_outcome, reporter_name

_ROLE = {"fitter": "монтёр", "driver": "водитель"}

# Что показывает палитра переменных в конструкторе (ключ без «{{ }}»).
CREW_VARIABLES = [
    ("crew.index", "№ бригады по порядку в отчёте"),
    ("crew.date", "Дата работы бригады"),
    ("crew.name", "Название бригады"),
    ("crew.place", "Место работ"),
    ("crew.equip", "Оснащение (лестница / автовышка)"),
    ("crew.members", "Состав с должностями: «Иванов И.И. (монтёр), …»"),
    ("crew.fitters", "Монтёры (через запятую)"),
    ("crew.drivers", "Водители (через запятую)"),
    ("crew.map_link", "Ссылка на карту бригады"),
    ("crew.trip", "Командировка: «с … по …» (если бригада в командировке)"),
    ("crew.tasks_total", "Заданий у бригады"),
    ("crew.tasks_done", "Заданий с отчётом о выезде"),
    ("crew.tasks_missed", "Не выехали (день прошёл, отчёта нет)"),
    ("crew.tasks_pending", "Ещё не было выезда (день не наступил / сегодня)"),
    ("crew.restored", "Восстановлено («Работает»)"),
    ("crew.dismantled", "Демонтировано"),
    ("crew.installed", "Установлено"),
    ("crew.not_working", "Осталось неисправно («Не работает»)"),
]
TASK_VARIABLES = [
    ("task.index", "№ задания по порядку"),
    ("task.date", "Дата задания"),
    ("task.crew", "Бригада"),
    ("task.point", "Комплекс (номер)"),
    ("task.address", "Адрес комплекса"),
    ("task.type", "Тип работы"),
    ("task.work", "Задание (что нужно сделать)"),
    ("task.outcome", "Итог выезда: статус или «Не выехали» / «Ещё не было выезда»"),
    ("task.comment", "Комментарий из отчёта о выезде"),
    ("task.equipment_removed", "Снято оборудование"),
    ("task.equipment_installed", "Установлено оборудование"),
    ("task.status_now", "Текущий статус комплекса"),
    ("task.priority", "«Приоритет» для приоритетных заданий"),
    ("task.category", "Категория комплекса"),
    ("task.inventory_number", "Инвентарный номер комплекса"),
    ("task.note", "Заметка к заданию"),
    ("task.hours", "Часов работы по заданию"),
    ("task.travel", "Часов дороги по заданию"),
    ("task.reported_by", "Кто сдал отчёт о выезде"),
    ("task.reported_at", "Когда сдан отчёт"),
]
EXTRA_GLOBAL_VARIABLES = [
    ("generated_at", "Дата и время формирования отчёта"),
    ("today", "Сегодняшняя дата"),
]


def design_variable_catalog() -> list[dict[str, str]]:
    """Все переменные для палитры: общие (period/итоги/шаблон
    план-наряда — без {{row.*}}, это для загружаемых файлов), бригада, задание."""
    catalog = [
        {"key": k, "label": label, "group": "Период", "scope": "global"} for k, label in EXTRA_GLOBAL_VARIABLES
    ]
    for v in template_variable_catalog():
        if v["key"].startswith("row."):
            continue
        catalog.append({**v, "scope": "global"})
    catalog += [{"key": k, "label": label, "group": "Бригада", "scope": "crew"} for k, label in CREW_VARIABLES]
    catalog += [{"key": k, "label": label, "group": "Задание", "scope": "task"} for k, label in TASK_VARIABLES]
    return catalog


def _crew_members_text(crew: dict[str, Any]) -> str:
    parts = []
    for m in crew.get("members") or []:
        role = _ROLE.get(m.get("role_type") or "", "")
        name = decode_entities(m.get("full_name") or "")
        parts.append(f"{name} ({role})" if role else name)
    return ", ".join(p for p in parts if p)


def _names(crew: dict[str, Any], role: str) -> str:
    return ", ".join(decode_entities(m.get("full_name") or "") for m in crew.get("members") or [] if m.get("role_type") == role)


def _point_status_label(point: dict[str, Any]) -> str:
    status = point.get("status") or ""
    if not status:
        return "Не известно"
    return "Частично на складе" if status == "На складе" and point.get("stock_partial") else status


async def collect(
    date_from: str, date_to: str, today: str, now: str = "", days: list[str] | None = None
) -> dict[str, Any]:
    """{"context": общие переменные, "instances": [{"crew": {...}, "tasks": [...]}],
    "tasks": все задания периода по порядку}. Бригады — по дням периода (как в
    календаре), задания — по их дате и бригаде. `days` — отдельные выбранные дни
    (не обязательно подряд): тогда берутся только они, а date_from/date_to —
    крайние из них."""
    if days:
        date_from, date_to = days[0], days[-1]
    else:
        d_from, d_to = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
        days = [(d_from + dt.timedelta(days=i)).isoformat() for i in range((d_to - d_from).days + 1)]
    selected = set(days)

    _, state = await state_repository.read_state(include_aux=False)
    tasks = [t for t in state["tasks"] if (t.get("date") or "") in selected]
    crews_by_day = {day: await crew_repository.list_for_date(day) for day in days}
    crew_lookup = {c["id"]: c for crews in crews_by_day.values() for c in crews}
    for team in {t.get("team") for t in tasks}:
        if team in crew_lookup:
            continue
        try:
            crew = await crew_repository.get_crew(int(team))
        except (TypeError, ValueError):
            crew = None
        if crew:
            crew_lookup[crew["id"]] = crew
    points = {p["id"]: p for p in await points_repository.list_points() if "id" in p}
    reports = await task_visit_reports_repository.list_for_tasks([t.get("id") for t in tasks])

    context, _rows = await reports_repository.build_template_context(date_from, date_to, days)
    context["period_label"] = describe_days(days, format_date)
    context["today"] = format_date(today)
    # «now» — местное время браузера: серверное — в другом часовом поясе.
    context["generated_at"] = format_datetime(now) if now else format_datetime(dt.datetime.now().isoformat(timespec="seconds"))

    instances: list[dict[str, Any]] = []
    all_tasks: list[dict[str, Any]] = []
    for day in days:
        day_tasks = [t for t in tasks if t.get("date") == day]
        crews = list(crews_by_day.get(day, []))
        known = {str(c["id"]) for c in crews}
        for t in day_tasks:
            team = str(t.get("team"))
            if team not in known:
                known.add(team)
                crews.append(crew_lookup.get(t.get("team")) or crew_lookup.get(team) or {"id": team, "name": f"Бригада {team}", "members": []})
        for crew in crews:
            crew_name = decode_entities(crew.get("name") or "")
            rows = []
            for t in [x for x in day_tasks if str(x.get("team")) == str(crew["id"])]:
                p = points.get(t.get("point"), {})
                report = reports.get(t.get("id"))
                rows.append(
                    {
                        "date": format_date(day),
                        "crew": crew_name,
                        "point": decode_entities(t.get("point") or ""),
                        "address": decode_entities(p.get("address") or t.get("address") or ""),
                        "type": (report or {}).get("visit_type") or t.get("type") or "",
                        "work": decode_entities(t.get("work") or p.get("description") or ""),
                        "outcome": period_outcome(report, day, today),
                        "comment": decode_entities((report or {}).get("comment") or ""),
                        "equipment_removed": decode_entities((report or {}).get("equipment_removed") or ""),
                        "equipment_installed": decode_entities((report or {}).get("equipment_installed") or ""),
                        "status_now": _point_status_label(p),
                        "priority": "Приоритет" if t.get("priority") else "",
                        "category": decode_entities(p.get("category_name") or ""),
                        "inventory_number": decode_entities(p.get("inventory_number") or ""),
                        "note": decode_entities(t.get("note") or ""),
                        "hours": t.get("hours") or "",
                        "travel": t.get("travel") or "",
                        "reported_by": reporter_name((report or {}).get("created_by")),
                        "reported_at": format_datetime((report or {}).get("updated_at") or (report or {}).get("created_at")) if report else "",
                        "_reported": bool(report),
                        "_result": (report or {}).get("result_status") or "",
                    }
                )
            for i, row in enumerate(rows, start=1):
                row["index"] = i
            results = [r["_result"] for r in rows]
            instances.append(
                {
                    "crew": {
                        "index": len(instances) + 1,
                        "date": format_date(day),
                        "name": crew_name,
                        "place": decode_entities(crew.get("place") or ""),
                        "equip": crew.get("equip") or "",
                        "members": _crew_members_text(crew),
                        "fitters": _names(crew, "fitter"),
                        "drivers": _names(crew, "driver"),
                        "map_link": crew.get("map_link") or "",
                        "trip": f"{format_date(crew.get('trip_from'))} — {format_date(crew.get('trip_to'))}" if crew.get("trip_id") else "",
                        "tasks_total": len(rows),
                        "tasks_done": sum(1 for r in rows if r["_reported"]),
                        "tasks_missed": sum(1 for r in rows if r["outcome"] == PERIOD_NOT_VISITED),
                        "tasks_pending": sum(1 for r in rows if r["outcome"] == PERIOD_NOT_YET),
                        "restored": results.count("Работает"),
                        "dismantled": results.count("Демонтирован"),
                        "installed": results.count("Установлен"),
                        "not_working": results.count("Не работает"),
                    },
                    "tasks": rows,
                }
            )
            all_tasks.extend(rows)
    for i, row in enumerate(all_tasks, start=1):
        row["global_index"] = i
    return {"context": context, "instances": instances, "tasks": all_tasks}
