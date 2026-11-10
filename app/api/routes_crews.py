"""Бригады (crews) — an ad-hoc group of people plus a transport choice,
created for one date (typically while importing that day's KML tasks)."""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import require_password_ok
from app.models.crew import CrewCreate, CrewMoveIn, CrewOut, CrewTripIn, CrewUpdate, TasksMoveIn
from app.models.user import UserOut
from app.repositories import crew_move_repository, crew_repository

router = APIRouter(prefix="/api/crews", tags=["crews"], dependencies=[Depends(require_password_ok)])


def _check_date(day: str) -> None:
    try:
        date_cls.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")


@router.get("", response_model=list[CrewOut])
async def list_crews(date: str):
    _check_date(date)
    return await crew_repository.list_for_date(date)


@router.post("", response_model=CrewOut, status_code=status.HTTP_201_CREATED)
async def create_crew(payload: CrewCreate):
    _check_date(payload.date)
    return await crew_repository.create_crew(
        date=payload.date, name=payload.name, equip=payload.equip,
        person_ids=payload.person_ids, map_link=payload.map_link, place=payload.place,
        contractor=payload.contractor, contractor_note=payload.contractor_note,
    )


@router.put("/{crew_id}", response_model=CrewOut)
async def update_crew(crew_id: int, payload: CrewUpdate):
    if not await crew_repository.get_crew(crew_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Бригада не найдена")
    await crew_repository.update_crew(
        crew_id, name=payload.name, equip=payload.equip,
        person_ids=payload.person_ids, map_link=payload.map_link, place=payload.place,
        contractor=payload.contractor, contractor_note=payload.contractor_note,
    )
    return await crew_repository.get_crew(crew_id)


@router.delete("/{crew_id}")
async def delete_crew(crew_id: int):
    """A crew with tasks still attached (`tasks.team` not cleared) is
    refused — the client is expected to remove/reassign those tasks first
    (see app.js's deleteCrewCascade, which does that via the normal
    tasks CAS save() before calling this), same as it always had to. This
    endpoint itself never cascade-deletes tasks — /api/state's
    compare-and-swap write is the one place tasks.rev-consistently changes."""
    if not await crew_repository.get_crew(crew_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Бригада не найдена")
    if await crew_repository.count_tasks(crew_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "У бригады есть задания — сначала переназначьте или удалите их"
        )
    await crew_repository.delete_crew(crew_id)
    return {"ok": True}


@router.post("/move-tasks")
async def move_tasks(payload: TasksMoveIn, user: UserOut = Depends(require_password_ok)):
    """Перенести выбранные невыполненные задания в другую бригаду — того же дня, другого дня или новую (см. crew_move_repository.move_tasks) — либо в «План»
    (to_plan: задания убираются из бригады, комплексы встают в план без дня и бригады — crew_move_repository.move_tasks_to_plan)."""
    if payload.to_plan:
        try:
            return await crew_move_repository.move_tasks_to_plan(payload.task_ids, created_by=user.username, group_name=payload.plan_group)
        except crew_move_repository.MoveError as exc:
            raise HTTPException(exc.status_code, exc.message)
    if payload.crew_id is None:
        _check_date(payload.date or "")
    try:
        return await crew_move_repository.move_tasks(
            payload.task_ids, crew_id=payload.crew_id, date=payload.date, new_crew_name=payload.new_crew_name,
            copy_members=payload.copy_members, allow_duplicates=payload.allow_duplicates,
        )
    except crew_move_repository.MoveError as exc:
        raise HTTPException(exc.status_code, exc.message)


@router.get("/{crew_id}/move-check")
async def check_move_crew(crew_id: int, date: str):
    """Что будет при переносе бригады на `date`: сколько точек поедет, что уже выполнено и останется, какие точки
    на том дне уже есть, чьи люди заняты или в отпуске. Ничего не меняет — для окна подтверждения."""
    try:
        return await crew_move_repository.check(crew_id, date)
    except crew_move_repository.MoveError as exc:
        raise HTTPException(exc.status_code, exc.message)


@router.post("/{crew_id}/move")
async def move_crew(crew_id: int, payload: CrewMoveIn):
    """Перенос бригады с её невыполненными точками на другой день (см. crew_move_repository)."""
    try:
        return await crew_move_repository.move(crew_id, payload.date, allow_duplicates=payload.allow_duplicates)
    except crew_move_repository.MoveError as exc:
        raise HTTPException(exc.status_code, exc.message)


@router.post("/{crew_id}/trip", response_model=CrewOut)
async def start_trip(crew_id: int, payload: CrewTripIn):
    _check_date(payload.date_to)
    crew = await crew_repository.start_trip(crew_id, payload.date_to)
    if not crew:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Не удалось оформить командировку — бригада не найдена, уже в командировке, "
            "или дата окончания раньше даты начала",
        )
    return crew


@router.delete("/{crew_id}/trip")
async def cancel_trip(crew_id: int):
    if not await crew_repository.get_crew(crew_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Бригада не найдена")
    if not await crew_repository.cancel_trip(crew_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бригада не в командировке")
    return {"ok": True}
