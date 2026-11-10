"""«Планы» — точки, отмеченные оператором/админом как требующие выезда, но
ещё не привязанные к бригаде/дате (см. app/repositories/point_plans_repository.py).
Используется на /apk (кнопка «В план», а также «📋 План» — общий менеджер
плана с массовыми действиями, см. static/js/apk.js::planManagerForm) и в
форме задания на календаре (кнопка «Из плана»)."""
from __future__ import annotations
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.assign_common import check_assignment_target, check_work_type
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import point_plans_repository, points_repository, state_repository
from app.services import excel_service

router = APIRouter(prefix="/api/point-plans", tags=["point-plans"], dependencies=[Depends(require_password_ok)])

_MAX_IMPORT_SIZE = 5 * 1024 * 1024
_READ_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    """Read the upload in chunks, aborting as soon as it exceeds max_size
    instead of buffering an unbounded body before checking its length —
    same pattern as routes_excel.py's own _read_capped."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


class PointPlanIn(BaseModel):
    point_id: str
    description: str = ""
    priority: bool = False
    group_name: str = ""          # группа, в которую собирается точка (необязательно)
    work_type: str = ""           # тип работы заранее (необязательно; не зависит от группы)


class PointPlanBulkIn(BaseModel):
    """Несколько комплексов в план сразу (например, отмеченные на «Все АПК»): общая группа, тип работы и описание."""
    point_ids: list[str]
    description: str = ""
    priority: bool = False
    group_name: str = ""
    work_type: str = ""
    # У каждого комплекса свой тип работы (например, из подгруппы АПК): номер → тип. Общий work_type, если задан, главнее.
    types: dict[str, str] = {}


class PointPlanGroupIn(BaseModel):
    plan_ids: list[int]
    name: str = ""                # пустое — убрать из группы


class PointPlanTypeIn(BaseModel):
    plan_ids: list[int]
    type: str = ""                # пустое — снять тип


class PointPlanPriorityIn(BaseModel):
    plan_ids: list[int]
    priority: bool


class PointPlanAssignItem(BaseModel):
    plan_id: int
    type: str = ""                # пусто — берётся тип, заранее поставленный записи плана


class PointPlanAssignIn(BaseModel):
    items: list[PointPlanAssignItem]
    date: str
    team: int
    hours: float = 1.0
    travel: float = 0.0


@router.get("")
async def list_point_plans():
    return await point_plans_repository.list_all()


@router.get("/import-template")
async def point_plan_import_template():
    out = excel_service.build_point_plan_import_template()
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="plan-import-template.xlsx"'},
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_point_plan(payload: PointPlanIn, user: UserOut = Depends(require_password_ok)):
    if not payload.point_id.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не указан номер точки")
    if payload.work_type:
        check_work_type(payload.work_type)
    existing = await point_plans_repository.find_by_point(payload.point_id)
    if existing:
        # Already in the plan — not an error, just nothing new to add (но группа/тип, если их указали, достаются существующей записи).
        if payload.group_name.strip():
            await point_plans_repository.set_group([existing["id"]], payload.group_name)
        if payload.work_type:
            await point_plans_repository.set_work_type([existing["id"]], payload.work_type)
        return {**(await point_plans_repository.get(existing["id"])), "duplicate": True}
    return await point_plans_repository.create(
        payload.point_id.strip(), payload.description, user.username, payload.priority, payload.group_name, payload.work_type
    )


@router.post("/bulk")
async def create_point_plans_bulk(payload: PointPlanBulkIn, user: UserOut = Depends(require_password_ok)):
    """Добавляет в план сразу несколько комплексов — с общей группой и типом работы. Уже стоящие в плане пропускаются
    (но получают группу/тип, если они указаны); номера, которых нет в справочнике, не добавляются."""
    if not payload.point_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной точки")
    if payload.work_type:
        check_work_type(payload.work_type)
    own_types = {} if payload.work_type else {k.strip().lower(): v.strip() for k, v in payload.types.items() if v and v.strip()}
    for value in set(own_types.values()):
        check_work_type(value)
    created, existing_ids, unknown = 0, [], []
    seen: set[str] = set()
    for raw in payload.point_ids:
        pid = raw.strip()
        key = pid.lower()
        if not pid or key in seen:
            continue
        seen.add(key)
        if not await points_repository.get_point(pid):
            unknown.append(pid)
            continue
        existing = await point_plans_repository.find_by_point(pid)
        if existing:
            existing_ids.append(existing["id"])
            if own_types.get(key):
                await point_plans_repository.set_work_type([existing["id"]], own_types[key])
            continue
        await point_plans_repository.create(pid, payload.description, user.username, payload.priority, payload.group_name, payload.work_type or own_types.get(key, ""))
        created += 1
    if existing_ids:
        if payload.group_name.strip():
            await point_plans_repository.set_group(existing_ids, payload.group_name)
        if payload.work_type:
            await point_plans_repository.set_work_type(existing_ids, payload.work_type)
    return {"created": created, "already": len(existing_ids), "unknown": unknown}


@router.post("/import")
async def import_point_plans(file: UploadFile, user: UserOut = Depends(require_password_ok)):
    """Bulk-adds to «План» from Excel — just number + description (see
    excel_service.POINT_PLAN_HEADERS). Тип работы/приоритет are set
    afterwards, in bulk or one at a time, via /priority and /assign below —
    an import row never carries them, there's nowhere for a work type to
    make sense until the point is actually assigned to a бригада/day."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, _MAX_IMPORT_SIZE)

    parsed = excel_service.parse_point_plan_import(content)
    if not parsed.rows and not parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не найдено строк для импорта")

    created, skipped = 0, []
    seen: set[str] = set()
    row_errors = list(parsed.errors)
    for row in parsed.rows:
        key = row.point_id.strip().lower()
        if key in seen or await point_plans_repository.find_by_point(row.point_id):
            skipped.append(row.point_id)
            continue
        seen.add(key)
        work_type = row.work_type
        if work_type:
            try:
                check_work_type(work_type)
            except HTTPException:
                row_errors.append(f"Строка {row.row_number}: неизвестный тип работы «{work_type}» — комплекс добавлен без типа")
                work_type = ""
        await point_plans_repository.create(row.point_id, row.description, user.username, False, row.group, work_type)
        created += 1
    return {"created": created, "skipped": len(skipped), "skipped_points": skipped, "row_errors": row_errors}


@router.post("/priority")
async def set_point_plans_priority(payload: PointPlanPriorityIn):
    if not payload.plan_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной записи")
    for plan_id in payload.plan_ids:
        if not await point_plans_repository.get(plan_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Запись плана {plan_id} не найдена")
    for plan_id in payload.plan_ids:
        await point_plans_repository.set_priority(plan_id, payload.priority)
    return {"updated": len(payload.plan_ids)}


@router.post("/group")
async def set_point_plans_group(payload: PointPlanGroupIn):
    """Собирает отмеченные записи в группу с подписью (или убирает из группы, если имя пустое). Переименование группы — то же самое
    для всех её записей."""
    if not payload.plan_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной записи")
    for plan_id in payload.plan_ids:
        if not await point_plans_repository.get(plan_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Запись плана {plan_id} не найдена")
    name = await point_plans_repository.set_group(payload.plan_ids, payload.name)
    return {"updated": len(payload.plan_ids), "group_name": name}


@router.post("/work-type")
async def set_point_plans_work_type(payload: PointPlanTypeIn):
    """Ставит тип работы отмеченным записям заранее — независимо от того, в группе они или нет."""
    if not payload.plan_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной записи")
    if payload.type:
        check_work_type(payload.type)
    for plan_id in payload.plan_ids:
        if not await point_plans_repository.get(plan_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Запись плана {plan_id} не найдена")
    await point_plans_repository.set_work_type(payload.plan_ids, payload.type)
    return {"updated": len(payload.plan_ids)}


@router.get("/groups")
async def list_point_plan_groups():
    """Названия групп, которые уже есть в плане — для подсказки при вводе."""
    return await point_plans_repository.group_names()


@router.post("/assign")
async def assign_point_plans(payload: PointPlanAssignIn):
    """Converts several selected «план» entries into tasks for one
    бригада/day at once — each with its own type of work (the common "32
    identical инвентаризации" case is just the client filling the same type
    into every row). Consumes (deletes) each plan entry it turns into a
    task, same as picking a single one via jobForm's «Из плана»."""
    if not payload.items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной точки")
    await check_assignment_target(payload.date, payload.team)

    plans = []
    for item in payload.items:
        plan = await point_plans_repository.get(item.plan_id)
        if not plan:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Запись плана {item.plan_id} не найдена")
        work_type = item.type or plan["work_type"]            # тип не прислан — берём заранее поставленный в плане
        if not work_type:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не выбран тип работы для точки {plan['point_id']}")
        check_work_type(work_type)
        plans.append((plan, work_type))

    # A point that is already added (any бригада that day, or the same
    # командировка) is skipped, and its plan entry stays where it is — only
    # entries that really became a task are consumed.
    already = await state_repository.point_ids_on(payload.date, payload.team)
    new_tasks, consumed, skipped = [], [], []
    for plan, work_type in plans:
        key = plan["point_id"].strip().lower()
        if key in already:
            skipped.append(plan["point_id"])
            continue
        already.add(key)
        consumed.append(plan)
        new_tasks.append({
            "id": "t" + uuid.uuid4().hex,
            "date": payload.date,
            "team": payload.team,
            "point": plan["point_id"],
            "work": plan["description"],
            "address": "",
            "hours": payload.hours,
            "travel": payload.travel,
            "type": work_type,
            "note": "",
            "priority": plan["priority"],
        })
    await state_repository.add_tasks(new_tasks)
    for plan in consumed:
        await point_plans_repository.delete(plan["id"])
    return {"created": len(new_tasks), "skipped": len(skipped), "skipped_points": skipped}


@router.delete("/{plan_id}")
async def delete_point_plan(plan_id: int):
    if not await point_plans_repository.get(plan_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запись плана не найдена")
    await point_plans_repository.delete(plan_id)
    return {"ok": True}
