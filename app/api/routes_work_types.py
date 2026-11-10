"""Справочник типов выездов — «Настройки → Типы выездов»: добавить и убрать
тип, у каждого признак «связан с ремонтом» (меню ремонта в боте, «что
сняли/поставили» в отчёте). Читать список могут все страницы (из него
собираются выпадающие списки типа работы), править — тот же доступ, что и у
остальных справочников."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.core.work_types import LOCKED_TYPE_NAMES, MAX_OUTCOME_LABEL, MAX_OUTCOMES
from app.repositories import work_types_repository

router = APIRouter(prefix="/api/work-types", tags=["work-types"], dependencies=[Depends(require_password_ok)])

_MAX_NAME = 60


class WorkTypeIn(BaseModel):
    name: str
    is_repair: bool = False
    bot_tools: bool = False


class WorkTypeUpdateIn(BaseModel):
    is_repair: bool | None = None
    bot_tools: bool | None = None
    name: str | None = None


class OutcomesIn(BaseModel):
    outcomes: list[str]


@router.get("")
async def list_work_types():
    return await work_types_repository.list_all()


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Название типа не может быть пустым")
    if len(name) > _MAX_NAME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Название длиннее {_MAX_NAME} символов")
    if "+" in name:
        # «Тип + Ремонт» — служебная запись для визита, переведённого в режим
        # ремонта; в самом названии типа знак «+» не нужен.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В названии типа нельзя использовать знак «+»")
    if any(ch in name for ch in ("/", "\\", "?", "#", "%")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В названии типа нельзя использовать символы / ? # % и обратную косую черту")
    return name


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_work_type(payload: WorkTypeIn):
    name = _clean_name(payload.name)
    if await work_types_repository.get(name):
        raise HTTPException(status.HTTP_409_CONFLICT, "Такой тип уже есть")
    try:
        return await work_types_repository.create(name, payload.is_repair, payload.bot_tools)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Такой тип уже есть")


@router.put("/{name}")
async def update_work_type(name: str, payload: WorkTypeUpdateIn):
    """Переименовать тип и/или переключить «связан с ремонтом» и «проверка комплекса и SIM в боте»."""
    current = await work_types_repository.get(name)
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тип не найден")
    current_name = current["name"]
    if payload.name is not None:
        new_name = _clean_name(payload.name)
        if new_name != current_name:
            if current_name in LOCKED_TYPE_NAMES:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Тип «{current_name}» системный — у него встроенное поведение, переименовать его нельзя",
                )
            clash = await work_types_repository.get(new_name)
            if clash and clash["name"] != current_name:
                raise HTTPException(status.HTTP_409_CONFLICT, "Такой тип уже есть")
            await work_types_repository.rename(current_name, new_name)
            current_name = new_name
    if payload.is_repair is not None:
        await work_types_repository.set_repair(current_name, payload.is_repair)
    if payload.bot_tools is not None:
        await work_types_repository.set_bot_tools(current_name, payload.bot_tools)
    return {"ok": True, "name": current_name}


@router.put("/{name}/outcomes")
async def set_work_type_outcomes(name: str, payload: OutcomesIn):
    """«Статусы выезда» типа — кнопки итога в боте при завершении. Только у не
    связанных с ремонтом типов (у ремонта итоги стандартные), только запись в
    отчётах и статистике — на статус комплекса не влияют."""
    current = await work_types_repository.get(name)
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тип не найден")
    if current["is_repair"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У связанных с ремонтом типов итоги стандартные")
    labels: list[str] = []
    seen: set[str] = set()
    for raw in payload.outcomes:
        label = " ".join(raw.split())
        if not label:
            continue
        if len(label) > MAX_OUTCOME_LABEL:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Статус «{label[:20]}…» длиннее {MAX_OUTCOME_LABEL} символов")
        if label.lower() in seen:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Статус «{label}» указан дважды")
        seen.add(label.lower())
        labels.append(label)
    if len(labels) > MAX_OUTCOMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не больше {MAX_OUTCOMES} статусов на тип")
    return {"outcomes": await work_types_repository.set_outcomes(current["name"], labels)}


@router.delete("/{name}")
async def delete_work_type(name: str):
    """Removing a type only takes it out of the choices — tasks and reports
    that already carry it keep their text. At least one type must remain
    (the task form can't work with an empty list)."""
    if await work_types_repository.count() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить последний тип")
    if not await work_types_repository.delete(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тип не найден")
    return {"ok": True}
