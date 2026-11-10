"""«Статистика» → вкладка «Счётчики»: именованные счётчики с набором фильтров (см. app/services/stat_counters.py).

  GET    /api/stat-counters                 все счётчики сразу с посчитанными числами (один проход по данным)
  GET    /api/stat-counters/options         значения для списков редактора (категории, статусы, питание, типы работ, подгруппы…)
  POST   /api/stat-counters/preview         число по набору условий без сохранения («предпросмотр» в редакторе) — только чтение
  POST   /api/stat-counters/points          список комплексов, из которых сложилось число — только чтение
  GET    /api/stat-counters/{id}/export     тот же список в Excel (лист «Комплексы» и лист «Условия»)
  POST   /api/stat-counters                 создать счётчик — право «Счётчики в статистике»
  PUT    /api/stat-counters/{id}            изменить название и/или условия — то же право
  POST   /api/stat-counters/{id}/move       переставить выше / ниже — то же право
  DELETE /api/stat-counters/{id}            удалить — то же право

Видеть счётчики и их числа может каждый, кто видит «Статистику» (число — это счёт комплексов, а не их сведения; в списке комплексов нет ни IP, ни логинов и паролей);
создавать, менять и удалять — те, у кого есть право «stats.counters» (таблица адресов — app/core/permissions.py)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core import stats_cache
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import stat_counters_repository as repo
from app.services import stat_counters as svc

router = APIRouter(prefix="/api/stat-counters", tags=["stat-counters"], dependencies=[Depends(require_password_ok)])


class CounterIn(BaseModel):
    name: str
    filters: dict[str, Any] = {}


class CounterPatch(BaseModel):
    name: Optional[str] = None
    filters: Optional[dict[str, Any]] = None


class FiltersIn(BaseModel):
    filters: dict[str, Any] = {}


class MoveIn(BaseModel):
    direction: int


def _filters(raw: Any) -> dict[str, Any]:
    try:
        return svc.normalize(raw)
    except svc.CounterError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


def _name(raw: Any) -> str:
    name = repo.clean_name(raw)
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Впишите название счётчика")
    return name


async def _or_404(counter_id: int) -> dict[str, Any]:
    counter = await repo.get(counter_id)
    if not counter:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Счётчик не найден")
    return counter


async def _one(counter_id: int) -> dict[str, Any]:
    return (await svc.compute_many([await _or_404(counter_id)]))[0]


@router.get("")
async def list_counters():
    # числа держатся в общем коротком кэше статистики: сбрасывается любой записью, меняющей данные, и сохранением счётчиков
    key = ("stat-counters", dt.date.today().isoformat())
    return {"counters": await stats_cache.get_or_compute(key, lambda: _compute_all())}


async def _compute_all() -> list[dict[str, Any]]:
    return await svc.compute_many(await repo.list_counters())


@router.get("/options")
async def options():
    return await svc.options()


@router.post("/preview")
async def preview(payload: FiltersIn):
    f = _filters(payload.filters)
    return await svc.compute_one(f)


@router.post("/points")
async def points(payload: FiltersIn):
    f = _filters(payload.filters)
    return await svc.points_of(f)


@router.get("/{counter_id}/export")
async def export(counter_id: int):
    counter = await _or_404(counter_id)
    data = await svc.points_of(_filters(counter["filters"]))
    out = svc.build_xlsx(counter["name"], data)
    filename = f"schetchik-{dt.date.today().isoformat()}.xlsx"
    return StreamingResponse(
        out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote('Счётчик ' + counter['name'])}.xlsx"})


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_counter(payload: CounterIn, user: UserOut = Depends(require_password_ok)):
    name, f = _name(payload.name), _filters(payload.filters)
    if await repo.count() >= repo.MAX_COUNTERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Слишком много счётчиков (не больше {repo.MAX_COUNTERS}) — удалите ненужные")
    created = await repo.create(name, f, user.username)
    stats_cache.invalidate()
    return await _one(created["id"])


@router.put("/{counter_id}")
async def update_counter(counter_id: int, payload: CounterPatch):
    await _or_404(counter_id)
    name = _name(payload.name) if payload.name is not None else None
    f = _filters(payload.filters) if payload.filters is not None else None
    await repo.update(counter_id, name=name, filters=f)
    stats_cache.invalidate()
    return await _one(counter_id)


@router.post("/{counter_id}/move")
async def move_counter(counter_id: int, payload: MoveIn):
    await _or_404(counter_id)
    await repo.move(counter_id, payload.direction)
    stats_cache.invalidate()
    return {"ok": True}


@router.delete("/{counter_id}")
async def delete_counter(counter_id: int):
    await _or_404(counter_id)
    await repo.delete(counter_id)
    stats_cache.invalidate()
    return {"ok": True}
