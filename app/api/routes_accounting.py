"""Поиск комплексов для отдельной страницы бухгалтерии."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from app.core.deps import require_password_ok
from app.repositories import points_repository

router = APIRouter(
    prefix="/api/accounting",
    tags=["accounting"],
    dependencies=[Depends(require_password_ok)],
)


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
