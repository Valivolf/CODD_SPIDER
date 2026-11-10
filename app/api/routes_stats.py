"""Single combined endpoint for the /stats dashboard — one request per
period change is simpler for the frontend than several small ones."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core import stats_cache
from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.repositories import stats_repository
from app.services import excel_service

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(require_password_ok)])


@router.get("/summary")
async def summary(date_from: str, date_to: str):
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный формат даты")
    if date_from > date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Начальная дата позже конечной")

    return await stats_cache.get_or_compute((date_from, date_to), lambda: _compute_summary(date_from, date_to))


async def _compute_summary(date_from: str, date_to: str) -> dict:
    down = await stats_repository.currently_down_since()       # один раз на сводку: нужен и плитке «Отвалилось», и графику
    return {
        "visit_types": await stats_repository.visit_type_breakdown(date_from, date_to),
        "result_statuses": await stats_repository.result_status_breakdown(date_from, date_to),
        "outcomes": await stats_repository.outcome_breakdown(date_from, date_to),
        "current_status": await stats_repository.current_status_distribution(),
        "transitions": await stats_repository.transitions_summary(date_from, date_to, down),
        "daily_failures": await stats_repository.daily_failures(date_from, date_to, down),
        "category_matrix": await stats_repository.category_status_matrix(),
    }


@router.get("/status-export")
async def status_export():
    """«Выгрузка статусов АПК» — текущий срез (не зависит от периода на
    странице): лист «Сводка» как таблица «По категориям» на /stats, лист
    «Детально» — каждый комплекс со статусом, сколькими днями простоя, IP и
    адресом (см. excel_service.build_status_export)."""
    out = excel_service.build_status_export(
        await stats_repository.category_status_matrix(),
        await stats_repository.points_status_detail(),
    )
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="status-apk-{format_date(date.today())}.xlsx"'},
    )
