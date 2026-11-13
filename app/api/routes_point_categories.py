"""CRUD for point categories ("АПК") — free-form, colour-tagged, shared
by the calendar's point form, the warehouse page's complex form, and the
"Все АПК" directory page."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.repositories import point_categories_repository, point_category_fields_repository as fields_repo

router = APIRouter(prefix="/api/point-categories", tags=["point-categories"], dependencies=[Depends(require_password_ok)])


class CategoryIn(BaseModel):
    name: str
    color: str = point_categories_repository.DEFAULT_COLOR
    match_pattern: str = ""
    # Старый/новый комплекс — decides how "Зайти на веб-интерфейс" connects
    # for points in this category (see ui.js::openPointWebInterface).
    is_legacy: bool = False


class CategoryFieldIn(BaseModel):
    category_id: int | None = None
    name: str
    field_type: str = "text"


def _validate_field(payload: CategoryFieldIn) -> tuple[str, str]:
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название поля не может быть пустым")
    if len(name) > 80:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название поля длиннее 80 знаков")
    if payload.field_type not in fields_repo.FIELD_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный тип поля")
    return name, payload.field_type


@router.get("/fields")
async def list_category_fields(category_id: int | None = None):
    return await fields_repo.list_fields(category_id)


@router.post("/fields", status_code=status.HTTP_201_CREATED)
async def create_category_field(payload: CategoryFieldIn):
    name, field_type = _validate_field(payload)
    if payload.category_id is not None and not any(
        c["id"] == payload.category_id for c in await point_categories_repository.list_categories()
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Категория не найдена")
    try:
        return await fields_repo.create_field(payload.category_id, name, field_type, False)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.put("/fields/{field_id}")
async def update_category_field(field_id: int, payload: CategoryFieldIn):
    name, field_type = _validate_field(payload)
    current = await fields_repo.get_field(field_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поле не найдено")
    if current["accounting_only"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бухгалтерские поля нельзя менять в конструкторе категорий")
    if payload.category_id != current["category_id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Категорию существующего поля менять нельзя")
    try:
        await fields_repo.update_field(field_id, name, field_type, False)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return await fields_repo.get_field(field_id)


@router.delete("/fields/{field_id}")
async def delete_category_field(field_id: int):
    current = await fields_repo.get_field(field_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поле не найдено")
    if current["accounting_only"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бухгалтерские поля нельзя удалять в конструкторе категорий")
    if not await fields_repo.delete_field(field_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поле не найдено")
    return {"ok": True}


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
