"""Страница «Отсутствия»: отметить и посмотреть, кто когда отсутствует (отпуск, больничный…).

Её ведёт отдельная роль «отсутствия» (только эта страница, больше ничего) и, как обычно, диспетчер с администратором;
«только просмотр» читает, но не меняет (граница — app/core/viewer_guard.py). Данные — те же отсутствия, что в
«Персонал → Отсутствия» в календаре, поэтому отмеченный здесь человек сразу недоступен при сборе бригады."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import absences_repository, people_repository

router = APIRouter(prefix="/api/absences", tags=["absences"], dependencies=[Depends(require_password_ok)])

_MAX_REASON = 200
# Период длиннее трёх лет — почти наверняка опечатка в году (2062 вместо 2026).
_MAX_SPAN_DAYS = 3 * 366


async def _viewer_of_absences(user: UserOut = Depends(require_password_ok)) -> UserOut:
    if not P.has(user.permissions, "absences.view"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: видеть отсутствия")
    return user


class AbsenceIn(BaseModel):
    person_id: int | None = None   # при создании обязателен, при правке не меняется
    start_date: str
    end_date: str
    reason: str = ""

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("Неверная дата")
        return v

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, v: str) -> str:
        v = " ".join((v or "").split())
        if len(v) > _MAX_REASON:
            raise ValueError(f"Причина длиннее {_MAX_REASON} символов")
        return v


def _check_span(payload: AbsenceIn) -> None:
    start, end = date.fromisoformat(payload.start_date), date.fromisoformat(payload.end_date)
    if end < start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Дата окончания раньше даты начала")
    if (end - start).days > _MAX_SPAN_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Период длиннее трёх лет — проверьте год")


@router.get("/people", dependencies=[Depends(_viewer_of_absences)])
async def list_people(include_inactive: bool = False):
    return await absences_repository.list_people(include_inactive)


@router.get("", dependencies=[Depends(_viewer_of_absences)])
async def list_absences(date_from: str | None = None, date_to: str | None = None):
    """Периоды, пересекающиеся с окном date_from…date_to (любая граница необязательна)."""
    for value in (date_from, date_to):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    return await absences_repository.list_absences(date_from or None, date_to or None)


@router.get("/person/{person_id}", dependencies=[Depends(_viewer_of_absences)])
async def person_absences(person_id: int):
    """Вся история отсутствий одного человека, новые сверху."""
    if not await people_repository.get_person(person_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Человек не найден")
    items = await absences_repository.list_absences(person_id=person_id)
    items.sort(key=lambda a: (a["start_date"], a["id"]), reverse=True)
    return items


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_absence(payload: AbsenceIn, user: UserOut = Depends(_viewer_of_absences)):
    if payload.person_id is None or not await people_repository.get_person(payload.person_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Человек не найден")
    _check_span(payload)
    same = await absences_repository.find_same(payload.person_id, payload.start_date, payload.end_date, payload.reason)
    item = same or await absences_repository.create(
        payload.person_id, payload.start_date, payload.end_date, payload.reason, user.username
    )
    return {
        **item,
        "duplicate": same is not None,
        "conflicts": await absences_repository.crew_conflicts(payload.person_id, payload.start_date, payload.end_date),
    }


@router.put("/{absence_id}")
async def update_absence(absence_id: int, payload: AbsenceIn, _: UserOut = Depends(_viewer_of_absences)):
    existing = await absences_repository.get(absence_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отсутствие не найдено")
    _check_span(payload)
    item = await absences_repository.update(absence_id, payload.start_date, payload.end_date, payload.reason)
    return {
        **item,
        "conflicts": await absences_repository.crew_conflicts(existing["person_id"], payload.start_date, payload.end_date),
    }


@router.delete("/{absence_id}")
async def delete_absence(absence_id: int, _: UserOut = Depends(_viewer_of_absences)):
    if not await absences_repository.get(absence_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отсутствие не найдено")
    await absences_repository.delete(absence_id)
    return {"ok": True}
