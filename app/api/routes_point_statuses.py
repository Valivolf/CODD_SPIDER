"""CRUD for the point-status dictionary — mirrors routes_point_categories.py,
plus is_auto (read by point_health_service) and protected (blocks delete;
"На складе" specifically also blocks rename — enforced in the repository)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.repositories import point_statuses_repository

router = APIRouter(prefix="/api/point-statuses", tags=["point-statuses"], dependencies=[Depends(require_password_ok)])


class StatusIn(BaseModel):
    name: str
    color: str = point_statuses_repository.DEFAULT_COLOR
    is_auto: bool = False


@router.get("")
async def list_statuses():
    return await point_statuses_repository.list_statuses()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_status(payload: StatusIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название статуса не может быть пустым")
    try:
        return await point_statuses_repository.create_status(name, payload.color, payload.is_auto)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такой статус уже существует")


@router.put("/{status_id}")
async def update_status(status_id: int, payload: StatusIn):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название статуса не может быть пустым")
    try:
        await point_statuses_repository.update_status(status_id, name, payload.color, payload.is_auto)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такой статус уже существует")
    except PermissionError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Этот статус нельзя переименовать")
    except LookupError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Статус не найден")
    return {"ok": True}


@router.delete("/{status_id}")
async def delete_status(status_id: int):
    ok = await point_statuses_repository.delete_status(status_id)
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "Нельзя удалить этот статус")
    return {"ok": True}
