"""Personnel (монтёры/водители/операторы) management."""
from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.clientip import client_ip
from app.core.deps import require_password_ok
from app.db.database import get_connection
from app.models.user import UserOut
from app.models.people import AbsenceCreate, AbsenceOut, PersonCreate, PersonOut, PersonUpdate, RoleType
from app.repositories import bot_join_requests_repository, people_repository
from app.services import person_erasure, security_log

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[Depends(require_password_ok)])


@router.get("", response_model=list[PersonOut])
async def list_people(role_type: RoleType | None = None, active_only: bool = False):
    people = await people_repository.list_people(role_type=role_type, active_only=active_only)
    return [PersonOut(**p) for p in people]


@router.get("/{person_id}/tasks")
async def person_tasks(person_id: int):
    """Задания бригад, где работал сотрудник, с итогами отчётов."""
    person = await people_repository.get_person(person_id)
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сотрудник не найден")
    async with get_connection() as conn:
        cur = await conn.execute(
            """select t.id, t.date, t.date_to, t.point, t.work, t.type, t.address, c.name,
                      r.result_status, r.outcome, r.comment, r.fault, r.needs, r.created_at
               from tasks t
               join crew_members cm on cm.crew_id = t.team and cm.person_id = ?
               left join crews c on c.id = t.team
               left join task_visit_reports r on r.task_id = t.id
               order by t.date desc, t.id desc limit 500""",
            (person_id,),
        )
        rows = await cur.fetchall()
    keys = ("id", "date", "date_to", "point", "work", "type", "address", "crew", "result", "outcome", "comment", "fault", "needs", "reported_at")
    return {"person": person["full_name"], "items": [dict(zip(keys, row)) for row in rows]}


@router.post("", response_model=PersonOut, status_code=status.HTTP_201_CREATED)
async def create_person(payload: PersonCreate):
    try:
        p = await people_repository.create_person(
            full_name=payload.full_name, phone=payload.phone, role_type=payload.role_type,
            notes=payload.notes, vk_id=payload.vk_id, driver_kind=payload.driver_kind,
        )
    except aiosqlite.IntegrityError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Этот VK ID уже привязан к другому человеку")
    if payload.vk_id:
        await bot_join_requests_repository.resolve_by_vk_id(payload.vk_id)
    return PersonOut(**p)


@router.put("/{person_id}", response_model=PersonOut)
async def update_person(person_id: int, payload: PersonUpdate):
    existing = await people_repository.get_person(person_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    try:
        await people_repository.update_person(
            person_id,
            full_name=payload.full_name,
            phone=payload.phone,
            role_type=payload.role_type,
            active=payload.active,
            notes=payload.notes,
            vk_id=payload.vk_id,
            driver_kind=payload.driver_kind,
        )
    except aiosqlite.IntegrityError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Этот VK ID уже привязан к другому человеку")
    if payload.vk_id:
        await bot_join_requests_repository.resolve_by_vk_id(payload.vk_id)
    return PersonOut(**await people_repository.get_person(person_id))


@router.get("/{person_id}/erase-preview")
async def erase_preview(person_id: int):
    """Что будет удалено и обезличено при удалении сотрудника — для окна подтверждения. Ничего не меняет."""
    try:
        return await person_erasure.preview(person_id)
    except person_erasure.ErasureError as exc:
        raise HTTPException(exc.status_code, exc.message)


@router.delete("/{person_id}")
async def delete_person(person_id: int, request: Request, user: UserOut = Depends(require_password_ok)):
    """Удаляет сотрудника и обезличивает связанные с ним записи (app/services/person_erasure.py). Окно «Персонал» вызывает это только после подтверждения."""
    try:
        result = await person_erasure.erase(person_id)
    except person_erasure.ErasureError as exc:
        raise HTTPException(exc.status_code, exc.message + (" Если человек просто уволился, деактивируйте его вместо удаления." if exc.status_code == 409 else ""))
    await security_log.record("person_erase", username=user.username, role=user.role, ip=client_ip(request),
                              details={"person_id": person_id, "deleted": result["deleted"], "anonymized": result["anonymized"]})   # ФИО в журнал не пишется
    return {"ok": True, **result}


@router.get("/absent-on/{day}")
async def absent_on(day: str):
    """person_id -> reason, for everyone marked absent that date — used by
    the "assemble a team" picker to grey out and explain unavailable people."""
    return await people_repository.list_absent_on(day)


@router.get("/{person_id}/absences", response_model=list[AbsenceOut])
async def list_absences(person_id: int):
    if not await people_repository.get_person(person_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    return await people_repository.list_absences(person_id)


@router.post("/{person_id}/absences", response_model=AbsenceOut, status_code=status.HTTP_201_CREATED)
async def create_absence(person_id: int, payload: AbsenceCreate):
    if not await people_repository.get_person(person_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    return await people_repository.create_absence(
        person_id, start_date=payload.start_date, end_date=payload.end_date, reason=payload.reason
    )


@router.delete("/{person_id}/absences/{absence_id}")
async def delete_absence(person_id: int, absence_id: int):
    if not await people_repository.get_person(person_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    await people_repository.delete_absence(absence_id)
    return {"ok": True}
