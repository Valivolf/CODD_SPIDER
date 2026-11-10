"""CRUD for point categories ("АПК") — free-form, colour-tagged, shared
by the calendar's point form, the warehouse page's complex form, and the
"Все АПК" directory page."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.repositories import point_categories_repository

router = APIRouter(prefix="/api/point-categories", tags=["point-categories"], dependencies=[Depends(require_password_ok)])


class CategoryIn(BaseModel):
    name: str
    color: str = point_categories_repository.DEFAULT_COLOR
    match_pattern: str = ""
    # Старый/новый комплекс — decides how "Зайти на веб-интерфейс" connects
    # for points in this category (see ui.js::openPointWebInterface).
    is_legacy: bool = False


@router.get("")
async def list_categories():
    return await point_categories_repository.list_categories()


@router.get("/search")
async def search_categories(q: str = "", limit: int = 10):
    return await point_categories_repository.search_categories(q, limit)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название категории не может быть пустым")
    try:
        return await point_categories_repository.create_category(
            name, payload.color, payload.match_pattern, payload.is_legacy
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такая категория уже существует")


@router.put("/{category_id}")
async def update_category(category_id: int, payload: CategoryIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название категории не может быть пустым")
    try:
        await point_categories_repository.update_category(
            category_id, name, payload.color, payload.match_pattern, payload.is_legacy
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такая категория уже существует")
    return {"ok": True}


@router.delete("/{category_id}")
async def delete_category(category_id: int):
    ok = await point_categories_repository.delete_category(category_id)
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "Нельзя удалить категорию: она используется у точек")
    return {"ok": True}


@router.post("/auto-assign")
async def auto_assign_categories():
    """Bulk backfill — assigns a category to every currently-uncategorized
    point whose id/description matches some category's match_pattern."""
    assigned = await point_categories_repository.auto_assign_uncategorized()
    return {"assigned": assigned}
