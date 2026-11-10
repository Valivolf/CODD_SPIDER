from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.core.work_types import is_aux_type
from app.repositories import crew_repository, points_repository, state_repository
from app.services import kml_service

router = APIRouter(prefix="/api/kml", tags=["kml"], dependencies=[Depends(require_password_ok)])


@router.get("/points")
async def kml_points():
    points = await points_repository.list_points()
    items = [
        {
            "name": p.get("id", ""),
            "description": p.get("description") or p.get("address", ""),
            "lat": p.get("lat"),
            "lon": p.get("lon"),
        }
        for p in points
    ]
    out = kml_service.build_kml(items, "Точки ремонта")
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет точек с корректными координатами")
    return StreamingResponse(
        out,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": 'attachment; filename="repair-points.kml"'},
    )


@router.get("/day/{day}")
async def kml_day(day: str):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    _, data = await state_repository.read_state()
    pts = {p.get("id"): p for p in await points_repository.list_points()}
    crew_names = {c["id"]: c["name"] for c in await crew_repository.list_for_date(day)}
    items = []
    for t in data["tasks"]:
        if t.get("date") != day:
            continue
        p = pts.get(t.get("point"), {})
        if is_aux_type(t.get("type")):
            p = {"lat": t.get("aux_lat"), "lon": t.get("aux_lon")}
        desc = (
            "Бригада: " + crew_names.get(t.get("team"), "")
            + "; Работа: " + str(t.get("work") or p.get("description", ""))
            + "; Статус: " + str(t.get("status", ""))
        )
        items.append({"name": t.get("point") or "Без номера", "description": desc, "lat": p.get("lat"), "lon": p.get("lon")})
    out = kml_service.build_kml(items, "План работ " + format_date(day))
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "На выбранную дату нет заданий с корректными координатами")
    return StreamingResponse(
        out,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f'attachment; filename="plan-{format_date(day)}.kml"'},
    )
