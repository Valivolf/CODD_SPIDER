"""Поиск комплексов для отдельной страницы бухгалтерии."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core.deps import require_perm
from app.repositories import points_repository
from app.services.accounting_xlsx import export_accounting_xlsx

router = APIRouter(
    prefix="/api/accounting",
    tags=["accounting"],
    dependencies=[Depends(require_perm("accounting.view"))],
)


class AccountingExportIn(BaseModel):
    point_ids: list[str] = Field(min_length=1, max_length=5000)


@router.post("/export")
async def accounting_export(payload: AccountingExportIn):
    ids = list(dict.fromkeys(payload.point_ids))
    if any(not pid or len(pid) > 4000 for pid in ids):
        raise HTTPException(400, "Некорректный номер АПК")
    items, _ = await points_repository.search_accounting_points(point_ids=ids, limit=len(ids))
    if len(items) != len(ids):
        raise HTTPException(409, "Часть выбранных АПК удалена или больше не содержит бухгалтерских сведений. Обновите список и выберите комплексы повторно.")
    content = await run_in_threadpool(export_accounting_xlsx, items)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="accounting-apk.xlsx"',
                             "Cache-Control": "private, no-store", "X-Points-Exported": str(len(items))})


@router.get("/points")
async def accounting_points(
    response: Response,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    items, total = await points_repository.search_accounting_points(q.strip(), limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/candidates")
async def accounting_candidates(response: Response, inventory: str = Query(min_length=1, max_length=200),
                                offset: int = Query(default=0, ge=0)):
    if not inventory.strip():
        raise HTTPException(400, "Введите инвентарный номер")
    items, total = await points_repository.search_accounting_points(
        limit=50, offset=offset, registered_only=False, inventory=inventory)
    response.headers["X-Total-Count"] = str(total)
    # Selection never returns credentials or silently picks the first match.
    return [{k: v for k, v in p.items() if k not in ("accounting_fields", "accounting_revision", "lat", "lon")} for p in items]


@router.get("/point")
async def accounting_point(point_id: str = Query(min_length=1)):
    items, _ = await points_repository.search_accounting_points(registered_only=False, point_id=point_id, limit=1)
    if not items:
        raise HTTPException(404, "Комплекс не найден")
    return items[0]
