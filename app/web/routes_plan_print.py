"""Printable «План-задание» work-order — one page per team, matching the
paper form, generated in bulk for every team with at least one task on
the given day.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, get_settings
from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.core.text import decode_entities
from app.core.trips import is_trip_first_day, trip_visible_crew_ids
from app.core.work_types import INVENTORY_TYPE, PLAN_TYPE_LABELS
from app.models.report_fields import DEFAULT_PLAN_TITLE, PLAN_TEMPLATE_TYPE
from app.repositories import crew_repository, report_values_repository, state_repository
from app.services import plan_docx

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_password_ok)])
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))

_ROLE_LABEL = {"fitter": "Электромонтер", "driver": "Водитель", "operator": "Оператор"}
_DRIVER_KIND_LABEL = {"tower": "Водитель вышки", "cover": "Водитель прикрытия"}


def _role_label(person: dict) -> str:
    """Подпись роли в составе бригады: у водителя — его вид (вышки / прикрытия). «Подрядчик» — отметка на бригаде, не человек из
    персонала, поэтому в состав (members) не попадает и в план-задание не печатается."""
    if person["role_type"] == "driver" and person.get("driver_kind") in _DRIVER_KIND_LABEL:
        return _DRIVER_KIND_LABEL[person["driver_kind"]]
    return _ROLE_LABEL.get(person["role_type"], person["role_type"])


_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _render_plan_title(text: str, day: str) -> str:
    """Подставляет в заголовок бланка {число} {месяц} {год} {дата} из даты плана; остальное не трогает."""
    d = date.fromisoformat(day)
    values = {
        "число": f"{d.day:02d}", "месяц": _MONTHS_GEN[d.month - 1], "год": str(d.year),
        "дата": f"{d.day:02d} {_MONTHS_GEN[d.month - 1]} {d.year}",
    }
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _place_phrase(place: str) -> str:
    """"г. Тихорецк" → "в г. Тихорецк" для строки «…на местах дислокации …»;
    то, что уже начинается с предлога ("по дороге А-147"), не трогаем."""
    p = (place or "").strip()
    if not p:
        return ""
    return p if p.lower().startswith(("по ", "в ", "на ", "вдоль ", "около ")) else f"в {p}"


def _plan_rows(day_tasks: list[dict]) -> list[dict]:
    """Строки таблицы бланка: «Вид работ / номер комплекса» — без адреса и
    описания работы. Все инвентаризации бригады сворачиваются в одну строку
    «Инвентаризация / Кол-во АПК: N шт» (встаёт на место первой из них)."""
    rows: list[dict] = []
    inventory: dict | None = None
    for t in day_tasks:
        kind = (t.get("type") or "").strip()
        if kind == INVENTORY_TYPE:
            if inventory is None:
                inventory = {"n": len(rows) + 1, "title": INVENTORY_TYPE, "sub": "", "count": 0}
                rows.append(inventory)
            inventory["count"] += 1
            continue
        rows.append(
            {
                "n": len(rows) + 1,
                "title": PLAN_TYPE_LABELS.get(kind) or kind or "Работы",
                "sub": decode_entities(t.get("point")),
            }
        )
    if inventory is not None:
        inventory["sub"] = f"Кол-во АПК: {inventory['count']}шт"
    return rows


@router.get("/plan-print/{day}", response_class=HTMLResponse)
async def plan_print(request: Request, day: str, route_label: str | None = None):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")

    _, data = await state_repository.read_state(include_aux=False)
    settings = get_settings()
    # Letterhead text is editable/saveable from /reports (see
    # routes_reports.py's /plan-template) — saved values win, the .env
    # plan_* settings are only the seed default for a fresh install.
    template = await report_values_repository.get_values(
        PLAN_TEMPLATE_TYPE, report_values_repository.GLOBAL_PERIOD
    )
    try:
        title_size = min(24, max(12, float(template.get("plan_title_size_pt", 15))))
    except (TypeError, ValueError):
        title_size = 15
    try:
        header_shift = min(15, max(-15, float(template.get("plan_header_shift_mm", 0))))
    except (TypeError, ValueError):
        header_shift = 0
    title_align = template.get("plan_title_align", "center")
    if title_align not in {"left", "center", "right"}:
        title_align = "center"

    team_blocks = []
    for crew in await crew_repository.list_for_date(day):
        # A trip crew's план-наряд is only printed on its first day, with
        # every task assigned to it across the whole trip (see
        # app/core/trips.py) — later days would just repeat it.
        if not is_trip_first_day(crew):
            continue
        if crew.get("trip_id"):
            visible_ids = {str(i) for i in trip_visible_crew_ids(crew)}
            day_tasks = [t for t in data["tasks"] if str(t.get("team")) in visible_ids]
        else:
            day_tasks = [
                t for t in data["tasks"] if t.get("date") == day and str(t.get("team")) == str(crew["id"])
            ]
        if not day_tasks:
            continue

        members = crew["members"]
        fitters = [p["full_name"] for p in members if p["role_type"] == "fitter"]
        drivers = [p["full_name"] for p in members if p["role_type"] == "driver"]

        # A single flat list drives the hanging-indent composition block in
        # the template: "Выездной бригаде в составе:" sits on the same row
        # as the first entry, and every entry's value starts at the same
        # left edge — see .comp-grid in plan_print.html.
        composition = []
        for p in members:
            name = p["full_name"]
            if p["role_type"] == "operator" and p["phone"]:
                name = f"{name} (тел. {p['phone']})"
            composition.append({"role": _role_label(p), "name": name})

        rows = _plan_rows(day_tasks)

        team_blocks.append(
            {
                "name": crew["name"],
                "fitters": fitters,
                "drivers": drivers,
                "composition": composition,
                "place_text": decode_entities(_place_phrase(crew.get("place"))),
                "rows": rows,
            }
        )

    return templates.TemplateResponse(
        request,
        "plan_print.html",
        {
            "day": day,
            "day_display": format_date(day),
            "teams": team_blocks,
            "plan_title": _render_plan_title((template.get("plan_title") or "").strip() or DEFAULT_PLAN_TITLE, day),
            "org_title": template.get("org_title") or settings.plan_org_title,
            "approver_name": template.get("approver_name") or settings.plan_approver_name,
            # A dispatcher can override this per printout (see the prompt
            # before "План-задание всем бригадам" in the UI); an empty/
            # missing override falls back to the saved/configured default.
            "route_label": (route_label or "").strip() or template.get("route_label") or settings.plan_route_label,
            "safety_officer": template.get("safety_officer") or settings.plan_safety_officer,
            "second_approver_title": template.get("second_approver_title") or settings.plan_second_approver_title,
            "second_approver_name": template.get("second_approver_name") or settings.plan_second_approver_name,
            "plan_title_size_pt": title_size,
            "plan_header_shift_mm": header_shift,
            "plan_title_align": title_align,
        },
    )


@router.post("/plan-print/docx")
async def plan_print_docx(payload: dict = Body(...)):
    """«Скачать Word» на странице плана-задания: страница присылает готовую
    раскладку листов (в том числе поправленный текст), здесь она
    превращается в .docx — см. app/services/plan_docx.py."""
    try:
        content = plan_docx.build_plan_docx(payload)
    except Exception as exc:  # noqa: BLE001 — любая ошибка сборки = понятный 400, не 500
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не удалось собрать Word-файл: {exc}")
    day = str(payload.get("day") or "")
    safe_day = day if len(day) == 10 and day.replace("-", "").isdigit() else "plan"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="plan-zadanie-{safe_day}.docx"'},
    )
