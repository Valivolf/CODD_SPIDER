"""Солнечные панели (раздел склада): список, карточка, привязка к комплексу, Excel-выгрузка."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.core.deps import require_warehouse, require_warehouse_edit
from app.models.user import UserOut
from app.repositories import solar_panels_repository as repo
from app.services import equipment_xlsx as xlsx

router = APIRouter(prefix="/api/solar-panels", tags=["solar-panels"], dependencies=[Depends(require_warehouse)])


def _fail(exc: repo.PanelError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


class PanelIn(BaseModel):
    panel_number: str
    location: Optional[str] = None
    notes: Optional[str] = None
    at_warehouse: bool = False
    point_id: Optional[str] = None


@router.get("")
async def list_panels(search: str = "", at_warehouse: bool = False, linked: str = ""):
    return await repo.list_panels(search, at_warehouse, linked)


@router.get("/export.xlsx")
async def export_panels(search: str = "", at_warehouse: bool = False, linked: str = ""):
    data = xlsx.panels_workbook(await repo.list_panels(search, at_warehouse, linked))
    return Response(content=data, media_type=xlsx.XLSX_MEDIA, headers={"Content-Disposition": 'attachment; filename="solnechnye_paneli.xlsx"'})


@router.get("/{panel_id}")
async def get_panel(panel_id: int):
    panel = await repo.get_panel(panel_id)
    if not panel:
        raise HTTPException(404, "Панель не найдена")
    return panel


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_panel(payload: PanelIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await repo.create_panel(
            payload.panel_number, location=payload.location, notes=payload.notes, at_warehouse=payload.at_warehouse,
            point_id=payload.point_id, performed_by=user.username,
        )
    except repo.PanelError as exc:
        raise _fail(exc)


@router.put("/{panel_id}")
async def update_panel(panel_id: int, payload: PanelIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await repo.update_panel(
            panel_id, panel_number=payload.panel_number, location=payload.location, notes=payload.notes,
            at_warehouse=payload.at_warehouse, point_id=payload.point_id, performed_by=user.username,
        )
    except repo.PanelError as exc:
        raise _fail(exc)


@router.delete("/{panel_id}")
async def delete_panel(panel_id: int, user: UserOut = Depends(require_warehouse_edit)):
    try:
        await repo.delete_panel(panel_id, user.username)
    except repo.PanelError as exc:
        raise _fail(exc)
    return {"ok": True}
