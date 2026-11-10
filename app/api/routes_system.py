"""Состояние системы: GET /api/system/health (администратор), /api/system/summary (значок в шапке) и /healthz — лёгкая проверка «приложение живо и база отвечает» для Docker."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core.deps import require_system
from app.db.database import get_connection
from app.services import point_rename, system_health

router = APIRouter(tags=["system"])


@router.get("/healthz", include_in_schema=False)
async def healthz():
    """Для healthcheck контейнера: отвечает, только если приложение живо и файл базы читается. Без входа и без подробностей."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute("select 1")
            await cur.fetchone()
    except Exception:  # noqa: BLE001
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "база данных не отвечает")
    return {"ok": True}


@router.get("/api/system/health", dependencies=[Depends(require_system)])
async def health(refresh: bool = False):
    return await system_health.collect(force=refresh)


@router.get("/api/system/summary", dependencies=[Depends(require_system)])
async def summary():
    return await system_health.summary()


@router.get("/api/system/openapi", dependencies=[Depends(require_system)], include_in_schema=False)
async def openapi_schema(request: Request):
    """Описание API (OpenAPI). Страницы /docs, /redoc и /openapi.json отключены: описание всех адресов сервера не должно быть доступно без входа; администратору оно отдаётся здесь."""
    return request.app.openapi()


class RelinkIn(BaseModel):
    old: str
    new: str


@router.post("/api/system/relink-point", dependencies=[Depends(require_system)])
async def relink_point(payload: RelinkIn):
    """Привязать «осиротевшие» задания, фото, отчёты и историю с прежнего номера (такого комплекса уже нет — его переименовали) к существующему комплексу."""
    try:
        result = await point_rename.relink(payload.old, payload.new)
    except point_rename.RelinkError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    system_health._cache.update(at=0.0)           # noqa: SLF001 — следующий опрос возьмёт свежий результат
    return result


@router.post("/api/system/integrity", dependencies=[Depends(require_system)])
async def run_integrity():
    """Проверить целостность базы сейчас (десятки секунд на большой базе)."""
    result = await system_health.run_integrity_check()
    system_health._cache.update(at=0.0)           # noqa: SLF001 — следующий опрос возьмёт свежий результат
    return result
