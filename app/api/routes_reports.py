"""Отчёты за день/неделю — текстовая сводка для рассылки, .xlsx свод по
бригадам (оба построены из reports_repository.crew_day_breakdown(), тот
же источник данных, что и /stats: tasks + task_visit_reports), плюс
editable-report templates (ежедневный отчёт, план-наряд — see
app/models/report_fields.py and report_values_repository.py)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.models.report_fields import (
    DAILY_REPORT_FIELDS,
    DAILY_REPORT_TYPE,
    PLAN_TEMPLATE_FIELDS,
    PLAN_TEMPLATE_TYPE,
    manual_keys,
)
from app.models.user import UserOut
from app.repositories import report_values_repository, reports_repository
from app.services import excel_service

router = APIRouter(prefix="/api/reports", tags=["reports"], dependencies=[Depends(require_password_ok)])


class FieldValuesIn(BaseModel):
    values: dict[str, str]


def _validate_dates(date_from: str, date_to: str) -> None:
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный формат даты")
    if date_from > date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Начало периода позже конца")


# tz_offset — часовой пояс браузера (getTimezoneOffset): по нему определяется «день, когда сдан отчёт» у бригад в командировке.
_TzOffset = Query(default=0, ge=-900, le=900)


@router.get("/text")
async def get_text_report(date_from: str, date_to: str, tz_offset: int = _TzOffset):
    _validate_dates(date_from, date_to)
    rows = await reports_repository.crew_day_breakdown(date_from, date_to, tz_offset)
    text = reports_repository.build_text_report(rows, date_from, date_to)
    return {"text": text}


@router.get("/crew-summary/export")
async def export_crew_summary(date_from: str, date_to: str, tz_offset: int = _TzOffset):
    _validate_dates(date_from, date_to)
    rows = await reports_repository.crew_day_breakdown(date_from, date_to, tz_offset)
    out = excel_service.build_crew_summary_export(rows)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="crew-summary-{format_date(date_from)}_{format_date(date_to)}.xlsx"'},
    )


@router.get("/daily/{day}")
async def get_daily_report(day: str, tz_offset: int = _TzOffset):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    computed = await reports_repository.daily_report_computed(day, tz_offset)
    manual = await report_values_repository.get_values_with_carry_forward(
        DAILY_REPORT_TYPE, day, manual_keys(DAILY_REPORT_FIELDS)
    )
    return {"fields": DAILY_REPORT_FIELDS, "computed": computed, "manual": manual}


@router.put("/daily/{day}")
async def put_daily_report(day: str, payload: FieldValuesIn, user: UserOut = Depends(require_password_ok)):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    allowed = set(manual_keys(DAILY_REPORT_FIELDS))
    values = {k: v for k, v in payload.values.items() if k in allowed}
    await report_values_repository.save_values(DAILY_REPORT_TYPE, day, values, user.username)
    return {"ok": True}


@router.get("/daily/{day}/export")
async def export_daily_report(day: str, tz_offset: int = _TzOffset):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    computed = await reports_repository.daily_report_computed(day, tz_offset)
    manual = await report_values_repository.get_values_with_carry_forward(
        DAILY_REPORT_TYPE, day, manual_keys(DAILY_REPORT_FIELDS)
    )
    values = {**manual, **{k: str(v) for k, v in computed.items()}}
    out = excel_service.build_daily_report_export(day, DAILY_REPORT_FIELDS, values)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="daily-report-{format_date(day)}.xlsx"'},
    )


@router.get("/plan-template")
async def get_plan_template():
    values = await reports_repository.plan_template_values()
    return {"fields": PLAN_TEMPLATE_FIELDS, "values": values}


@router.put("/plan-template")
async def put_plan_template(payload: FieldValuesIn, user: UserOut = Depends(require_password_ok)):
    allowed = {f["key"] for f in PLAN_TEMPLATE_FIELDS}
    values = {k: v for k, v in payload.values.items() if k in allowed}
    await report_values_repository.save_values(
        PLAN_TEMPLATE_TYPE, report_values_repository.GLOBAL_PERIOD, values, user.username
    )
    return {"ok": True}
