"""Учёт оборудования по серийным номерам (веб-панель склада): справочники, приёмка, выдача и приём
от монтёров, оборудование на комплексах, журнал, отчёты. Доступ — админ и роль «склад»
(app/core/deps.py::require_warehouse). Операции — app/services/equipment_service.py."""
from __future__ import annotations

import sqlite3
from typing import Optional

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.core import equipment as eq
from app.core import permissions as P
from app.core.deps import require_equipment_move, require_equipment_view, require_warehouse_edit
from app.models.user import UserOut
from app.repositories import equipment_repository as repo
from app.repositories import people_repository, point_links_repository, points_repository, solar_panels_repository
from app.services import equipment_service as svc
from app.services import equipment_xlsx as xlsx
from app.db.database import get_connection

router = APIRouter(prefix="/api/equipment", tags=["equipment"], dependencies=[Depends(require_equipment_view)])

_MAX_LIST = 5000


def _fail(exc: Exception) -> HTTPException:
    return HTTPException(getattr(exc, "status_code", 400), getattr(exc, "detail", str(exc)))


def _xlsx(data: bytes, filename: str) -> Response:
    return Response(content=data, media_type=xlsx.XLSX_MEDIA, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _who(user: UserOut) -> str:
    return user.username


# ------------------------------------------------------------------ справочники

class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ModelIn(BaseModel):
    category_id: int
    name: str = Field(min_length=1, max_length=120)


class StatusIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = eq.DEFAULT_STATUS_COLOR
    usable: bool = False


@router.get("/meta")
async def meta(user: UserOut = Depends(require_equipment_view)):
    """Всё, что нужно формам одним запросом."""
    people = await people_repository.list_people(active_only=True) if P.has(user.permissions, "warehouse.view") else []
    return {
        "categories": await repo.list_categories(),
        "models": await repo.list_models(),
        "statuses": await repo.list_statuses(),
        "fields": await repo.list_fields(),
        "removal_reasons": eq.REMOVAL_REASONS,
        "places": [{"key": k, "label": v} for k, v in eq.PLACE_LABEL.items()],
        "people": [
            {"id": p["id"], "full_name": p["full_name"], "role_type": p["role_type"], "phone": p["phone"]}
            for p in people if p["role_type"] != "warehouse"
        ],
    }


@router.get("/categories")
async def list_categories():
    return await repo.list_categories()


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        if await repo.category_by_name_conn(conn, name, create=False):
            raise HTTPException(409, "Категория с таким названием уже существует")
        cid = await repo.category_by_name_conn(conn, name)
        await conn.commit()
    return {"id": cid, "name": name, "model_count": 0, "equipment_count": 0}


@router.put("/categories/{category_id}")
async def update_category(category_id: int, payload: CategoryIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        cur = await conn.execute("select kind from equipment_categories where id = ?", (category_id,))
        row = await cur.fetchone()
        if row and row[0]:
            raise HTTPException(409, "Это служебная категория — её нельзя переименовать")
        cur = await conn.execute("select id from equipment_categories where pylower(name) = pylower(?) and id != ?", (name, category_id))
        if await cur.fetchone():
            raise HTTPException(409, "Категория с таким названием уже существует")
        cur = await conn.execute("update equipment_categories set name = ? where id = ?", (name, category_id))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Категория не найдена")
    return {"id": category_id, "name": name}


@router.delete("/categories/{category_id}")
async def delete_category(category_id: int):
    async with get_connection() as conn:
        cur = await conn.execute("select kind from equipment_categories where id = ?", (category_id,))
        row = await cur.fetchone()
        if row and row[0]:
            raise HTTPException(409, "Это служебная категория — её нельзя удалить")
        cur = await conn.execute("select count(*) from equipment_models where category_id = ?", (category_id,))
        (n,) = await cur.fetchone()
        if n:
            raise HTTPException(409, "Нельзя удалить категорию: с ней связаны модели оборудования")
        cur = await conn.execute("delete from equipment_categories where id = ?", (category_id,))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Категория не найдена")
    return {"ok": True}


@router.get("/models")
async def list_models(category_id: Optional[int] = None):
    return await repo.list_models(category_id)


@router.post("/models", status_code=status.HTTP_201_CREATED)
async def create_model(payload: ModelIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from equipment_categories where id = ?", (payload.category_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "Категория не найдена")
        if await repo.model_by_name_conn(conn, payload.category_id, name, create=False):
            raise HTTPException(409, "Такая модель уже есть в этой категории")
        mid = await repo.model_by_name_conn(conn, payload.category_id, name)
        await conn.commit()
    return {"id": mid, "category_id": payload.category_id, "name": name}


@router.put("/models/{model_id}")
async def update_model(model_id: int, payload: ModelIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        cur = await conn.execute(
            "select 1 from equipment_models where category_id = ? and pylower(name) = pylower(?) and id != ?",
            (payload.category_id, name, model_id),
        )
        if await cur.fetchone():
            raise HTTPException(409, "Такая модель уже есть в этой категории")
        try:
            cur = await conn.execute("update equipment_models set category_id = ?, name = ? where id = ?", (payload.category_id, name, model_id))
        except (aiosqlite.IntegrityError, sqlite3.IntegrityError):
            raise HTTPException(404, "Категория не найдена")
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Модель не найдена")
    return {"id": model_id, "category_id": payload.category_id, "name": name}


@router.delete("/models/{model_id}")
async def delete_model(model_id: int):
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from equipment where model_id = ?", (model_id,))
        (n,) = await cur.fetchone()
        if n:
            raise HTTPException(409, "Нельзя удалить модель: с ней связано оборудование")
        cur = await conn.execute("delete from equipment_models where id = ?", (model_id,))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Модель не найдена")
    return {"ok": True}


@router.get("/statuses")
async def list_statuses():
    return await repo.list_statuses()


@router.post("/statuses", status_code=status.HTTP_201_CREATED)
async def create_status(payload: StatusIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        if await repo.status_by_name_conn(conn, name, create=False):
            raise HTTPException(409, "Такое состояние уже есть")
        sid = await repo.status_by_name_conn(conn, name, color=payload.color, usable=int(payload.usable))
        await conn.commit()
    return next(s for s in await repo.list_statuses() if s["id"] == sid)


@router.put("/statuses/{status_id}")
async def update_status(status_id: int, payload: StatusIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        st = await repo.get_status_conn(conn, status_id)
        if not st:
            raise HTTPException(404, "Состояние не найдено")
        cur = await conn.execute("select 1 from equipment_statuses where pylower(name) = pylower(?) and id != ?", (name, status_id))
        if await cur.fetchone():
            raise HTTPException(409, "Такое состояние уже есть")
        # Служебные: «исправно» всегда годное, «дефектное» всегда негодное — от этого зависят выдача и приёмка.
        usable = {eq.ROLE_OK: True, eq.ROLE_DEFECT: False}.get(st["role"], payload.usable)
        await conn.execute(
            "update equipment_statuses set name = ?, color = ?, usable = ? where id = ?",
            (name, (payload.color or "").strip() or eq.DEFAULT_STATUS_COLOR, int(usable), status_id),
        )
        await conn.commit()
    return next(s for s in await repo.list_statuses() if s["id"] == status_id)


@router.delete("/statuses/{status_id}")
async def delete_status(status_id: int):
    async with get_connection() as conn:
        st = await repo.get_status_conn(conn, status_id)
        if not st:
            raise HTTPException(404, "Состояние не найдено")
        if st["protected"] or st["role"]:
            raise HTTPException(409, f"«{st['name']}» — служебное состояние, его можно переименовать и перекрасить, но не удалить")
        cur = await conn.execute("select count(*) from equipment where status_id = ?", (status_id,))
        (n,) = await cur.fetchone()
        if n:
            raise HTTPException(409, f"Нельзя удалить: состояние стоит у {n} ед. оборудования — сначала смените его у них")
        await conn.execute("delete from equipment_statuses where id = ?", (status_id,))
        await conn.commit()
    return {"ok": True}


# ------------------------------------------------------------------ SIM: один номер у нескольких комплексов

@router.get("/sim-duplicates")
async def sim_duplicates():
    """Номера SIM, записанные в карточках сразу у нескольких комплексов (ошибка данных: одна SIM физически стоит в одном
    месте). В учёт оборудования такая SIM попадает один раз — у комплекса, которого коснулась сверка первым."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select p.id, p.address, p.status, p.sim_number from points p where trim(coalesce(p.sim_number, '')) != '' order by p.id"
        )
        points = await cur.fetchall()
        cur = await conn.execute(
            "select e.serial_number, e.point_id from equipment e join equipment_models m on m.id = e.model_id "
            "join equipment_categories c on c.id = m.category_id where c.kind = ? and e.place = 'point'", (eq.CATEGORY_KIND_SIM,),
        )
        installed = {s: pid for s, pid in await cur.fetchall()}
        link_of = {pid: gid for gid, ids in (await point_links_repository.groups_conn(conn)).items() for pid in ids}
    groups: dict[str, list[dict]] = {}
    for pid, address, status_, sim in points:
        key = eq.sim_key(sim)
        if eq.sim_key_valid(key):
            groups.setdefault(key, []).append({"id": pid, "address": address or "", "status": status_ or "", "raw": sim})
    dups = [
        {"sim": k, "installed_point": installed.get(k, ""), "points": v}
        for k, v in sorted(groups.items())
        # комплексы, которые диспетчер связал общей SIM («Уведомления → Ошибки данных»), ошибкой не считаются
        if len(v) > 1 and not (len({link_of.get(p["id"]) for p in v}) == 1 and link_of.get(v[0]["id"]) is not None)
    ]
    return {"groups": dups[:500], "total_groups": len(dups), "total_points": sum(len(g["points"]) for g in dups)}


# ------------------------------------------------------------------ дополнительные поля

class FieldIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    category_id: Optional[int] = None   # поле только для одной категории; пусто — для всего оборудования


@router.get("/fields")
async def list_fields():
    return await repo.list_fields()


@router.post("/fields", status_code=status.HTTP_201_CREATED)
async def create_field(payload: FieldIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        if await repo.field_by_name_conn(conn, name, create=False):
            raise HTTPException(409, "Поле с таким названием уже есть")
        if payload.category_id:
            cur = await conn.execute("select 1 from equipment_categories where id = ?", (payload.category_id,))
            if not await cur.fetchone():
                raise HTTPException(404, "Категория не найдена")
        fid = await repo.field_by_name_conn(conn, name, category_id=payload.category_id)
        await conn.commit()
    return next(f for f in await repo.list_fields() if f["id"] == fid)


@router.put("/fields/{field_id}")
async def update_field(field_id: int, payload: FieldIn):
    name = " ".join(payload.name.split())
    async with get_connection() as conn:
        cur = await conn.execute("select id from equipment_fields where pylower(name) = pylower(?) and id != ?", (name, field_id))
        if await cur.fetchone():
            raise HTTPException(409, "Поле с таким названием уже есть")
        cur = await conn.execute(
            "update equipment_fields set name = ?, category_id = ? where id = ?", (name, payload.category_id, field_id)
        )
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Поле не найдено")
    return next(f for f in await repo.list_fields() if f["id"] == field_id)


@router.delete("/fields/{field_id}")
async def delete_field(field_id: int):
    async with get_connection() as conn:
        cur = await conn.execute("delete from equipment_fields where id = ?", (field_id,))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Поле не найдено")
    return {"ok": True}


# ------------------------------------------------------------------ оборудование

def _item_filters(
    search: str, category_id: Optional[int], model_id: Optional[int], place: str, status_id: Optional[int],
    point_id: str, person_id: Optional[int], date_from: str, date_to: str, awaiting: bool,
) -> dict:
    f: dict = dict(
        search=search, category_id=category_id, model_id=model_id, place=place, status_id=status_id,
        point_id=point_id, person_id=person_id, date_from=date_from, date_to=date_to,
    )
    if awaiting:
        f["place"] = eq.PLACE_PERSON
        f["held_reason"] = eq.HELD_REMOVED
    return f


@router.get("/pending-installs")
async def pending_installs(all: bool = False):
    """Оборудование, которое монтёр поставил на комплекс по последним символам номера, а в учёте его ещё нет: привяжется само, когда его добавят (приёмка, импорт).
    all=true — вместе с уже привязанным."""
    return await svc.pending_list(only_open=not all)


@router.get("/items")
async def list_items(
    response: Response, search: str = "", category_id: Optional[int] = None, model_id: Optional[int] = None,
    place: str = "", status_id: Optional[int] = None, point_id: str = "", person_id: Optional[int] = None,
    date_from: str = "", date_to: str = "", awaiting: bool = False, usable: Optional[bool] = None,
    limit: int = Query(300, ge=1, le=_MAX_LIST), offset: int = 0,
):
    f = _item_filters(search, category_id, model_id, place, status_id, point_id, person_id, date_from, date_to, awaiting)
    if usable is not None:
        f["usable"] = usable
    items, total = await repo.list_items(limit, offset, **f)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    item = await repo.get_item(item_id, with_extra=True)
    if not item:
        raise HTTPException(404, "Оборудование не найдено")
    history, _, _ = await repo.list_history(limit=None, equipment_id=item_id)
    return {**item, "history": history}


class ReceiveIn(BaseModel):
    model_id: int
    serial_numbers: list[str]
    received_date: Optional[str] = None
    notes: Optional[str] = None
    status_id: Optional[int] = None


@router.post("/receive")
async def receive(payload: ReceiveIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.receive(
            payload.model_id, payload.serial_numbers, received_date=payload.received_date, notes=payload.notes,
            performed_by=_who(user), status_id=payload.status_id,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class ItemUpdateIn(BaseModel):
    notes: Optional[str] = None
    # {id дополнительного поля: значение}; пустое значение очищает поле.
    extra: Optional[dict[int, Optional[str]]] = None


@router.put("/items/{item_id}")
async def update_item(item_id: int, payload: ItemUpdateIn):
    """Примечание и/или дополнительные поля (что не передано — не меняется)."""
    try:
        await svc.update_item(item_id, notes=payload.notes, set_notes="notes" in payload.model_fields_set, extra=payload.extra)
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return {"ok": True}


class RenameIn(BaseModel):
    serial_number: str
    comment: Optional[str] = None


@router.post("/items/{item_id}/rename")
async def rename_item(item_id: int, payload: RenameIn, user: UserOut = Depends(require_warehouse_edit)):
    """Исправить серийный номер (был записан неверно). У SIM-карты на комплексе следом меняется поле «Номер SIM»."""
    try:
        return await svc.rename_serial(item_id, payload.serial_number, performed_by=_who(user), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)


class SetStatusIn(BaseModel):
    status_id: int
    comment: Optional[str] = None


@router.post("/items/{item_id}/set-status")
async def set_status(item_id: int, payload: SetStatusIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.set_status(item_id, payload.status_id, performed_by=_who(user), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)


class BulkStatusIn(BaseModel):
    ids: list[int]
    status_id: int
    comment: Optional[str] = None


@router.post("/items/bulk-status")
async def bulk_status(payload: BulkStatusIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.bulk_set_status(payload.ids, payload.status_id, performed_by=_who(user), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)


class BulkDeleteIn(BaseModel):
    ids: list[int]
    comment: Optional[str] = None


@router.post("/items/bulk-delete")
async def bulk_delete(payload: BulkDeleteIn, user: UserOut = Depends(require_warehouse_edit)):
    return await svc.delete_items(payload.ids, performed_by=_who(user), comment=payload.comment)


@router.delete("/items/{item_id}")
async def delete_item(item_id: int, user: UserOut = Depends(require_warehouse_edit)):
    res = await svc.delete_items([item_id], performed_by=_who(user))
    if res["skipped"]:
        raise HTTPException(409, res["skipped"][0]["reason"])
    return {"ok": True}


class NoteIn(BaseModel):
    comment: str
    date: Optional[str] = None


@router.post("/items/{item_id}/note")
async def item_note(item_id: int, payload: NoteIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        await svc.add_note(equipment_id=item_id, comment=payload.comment, date=payload.date, performed_by=_who(user))
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return {"ok": True}


# ------------------------------------------------------------------ выдача и приём

class IssueIn(BaseModel):
    person_id: int
    equipment_ids: list[int]
    date: Optional[str] = None
    comment: Optional[str] = None


@router.post("/issue")
async def issue(payload: IssueIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.issue(payload.person_id, payload.equipment_ids, date=payload.date, performed_by=_who(user), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)


class AcceptIn(BaseModel):
    equipment_ids: list[int]
    status_id: Optional[int] = None
    comment: Optional[str] = None
    date: Optional[str] = None


@router.post("/accept")
async def accept(payload: AcceptIn, user: UserOut = Depends(require_warehouse_edit)):
    """Принять на склад оборудование, числящееся на монтёре (снятое — как дефектное, невыбранное — вернуть)."""
    try:
        return await svc.accept_from_person(
            payload.equipment_ids, performed_by=_who(user), comment=payload.comment, status_id=payload.status_id, date=payload.date,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class AcceptSubstituteIn(BaseModel):
    expected_id: int
    actual_equipment_id: Optional[int] = None
    actual_serial: Optional[str] = None
    create_missing: bool = False
    status_id: Optional[int] = None
    comment: Optional[str] = None


@router.post("/accept-substitute")
async def accept_substitute(payload: AcceptSubstituteIn, user: UserOut = Depends(require_warehouse_edit)):
    """Монтёр сдал другой номер, чем числилось: сданное принимается на склад (как снятое), а то, что числилось
    на нём, получает «неизвестное местонахождение»."""
    try:
        return await svc.accept_substitute(
            payload.expected_id, actual_equipment_id=payload.actual_equipment_id, actual_serial=payload.actual_serial,
            create_missing=payload.create_missing, performed_by=_who(user), comment=payload.comment, status_id=payload.status_id,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.post("/items/{item_id}/found")
async def item_found(item_id: int, user: UserOut = Depends(require_warehouse_edit)):
    """Оборудование с неизвестным местонахождением нашлось — вернуть на склад."""
    try:
        return await svc.found_unknown(item_id, performed_by=_who(user))
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.get("/people-summary")
async def people_summary(person_id: Optional[int] = None, category_id: Optional[int] = None, date_from: str = "", date_to: str = ""):
    return await repo.people_summary(person_id, category_id, date_from, date_to)


@router.get("/awaiting")
async def awaiting():
    return await repo.awaiting_accept_people()


# ------------------------------------------------------------------ комплексы

@router.get("/points")
async def list_points(
    response: Response, search: str = "", category_id: Optional[int] = None, has_equipment: Optional[bool] = None,
    has_panel: Optional[bool] = None, limit: int = Query(200, ge=1, le=_MAX_LIST), offset: int = 0,
):
    items, total = await repo.list_points_with_equipment(search, category_id, has_equipment, has_panel, limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/points/{point_id}")
async def get_point(point_id: str):
    point = await points_repository.get_point(point_id)
    if not point:
        raise HTTPException(404, "Комплекс не найден")
    async with get_connection() as conn:
        items = await repo.point_items_conn(conn, point_id)
    history, _, _ = await repo.list_history(limit=None, point_id=point_id)
    panel = await solar_panels_repository.get_for_point(point_id)
    return {
        "id": point["id"], "address": point["address"], "inventory_number": point["inventory_number"],
        "category_name": point["category_name"], "category_color": point["category_color"], "status": point["status"],
        "equipment": items, "panel": panel, "history": history,
    }


class AttachIn(BaseModel):
    equipment_id: int
    person_id: Optional[int] = None
    installed_by: Optional[str] = None
    date: Optional[str] = None
    comment: Optional[str] = None


@router.post("/points/{point_id}/attach")
async def attach(point_id: str, payload: AttachIn, user: UserOut = Depends(require_equipment_move)):
    try:
        await svc.attach(
            point_id, payload.equipment_id, person_id=payload.person_id, installed_by=payload.installed_by,
            date=payload.date, performed_by=_who(user), comment=payload.comment,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return {"ok": True}


class ReplaceIn(BaseModel):
    old_equipment_id: int
    new_equipment_id: int
    person_id: Optional[int] = None
    installed_by: Optional[str] = None
    date: Optional[str] = None
    comment: Optional[str] = None
    removed_status_id: Optional[int] = None
    reason: Optional[str] = None   # почему снимается старое (какая проблема)
    # Реально стояло оборудование с другим номером: id найденного либо набранный номер (+ создать, если его нет в базе).
    actual_equipment_id: Optional[int] = None
    actual_serial: Optional[str] = None
    create_missing: bool = False


@router.post("/points/{point_id}/replace")
async def replace(point_id: str, payload: ReplaceIn, user: UserOut = Depends(require_equipment_move)):
    try:
        return await svc.replace(
            point_id, payload.old_equipment_id, payload.new_equipment_id, person_id=payload.person_id,
            installed_by=payload.installed_by, date=payload.date, performed_by=_who(user), comment=payload.comment,
            removed_status_id=payload.removed_status_id, actual_equipment_id=payload.actual_equipment_id,
            actual_serial=payload.actual_serial, create_missing=payload.create_missing, reason=payload.reason,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class DetachIn(BaseModel):
    to_person_id: Optional[int] = None
    status_id: Optional[int] = None
    date: Optional[str] = None
    comment: Optional[str] = None
    reason: Optional[str] = None
    actual_equipment_id: Optional[int] = None
    actual_serial: Optional[str] = None
    create_missing: bool = False


@router.post("/points/{point_id}/detach/{equipment_id}")
async def detach(point_id: str, equipment_id: int, payload: DetachIn, user: UserOut = Depends(require_equipment_move)):
    try:
        res = await svc.detach(
            point_id, equipment_id, to_person_id=payload.to_person_id, date=payload.date,
            performed_by=_who(user), comment=payload.comment, status_id=payload.status_id,
            actual_equipment_id=payload.actual_equipment_id, actual_serial=payload.actual_serial,
            create_missing=payload.create_missing, reason=payload.reason,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return {"ok": True, **res}


class SimIn(BaseModel):
    serial_number: str
    person_id: Optional[int] = None
    reason: Optional[str] = None
    comment: Optional[str] = None


@router.post("/points/{point_id}/sim/replace")
async def sim_replace(point_id: str, payload: SimIn, user: UserOut = Depends(require_equipment_move)):
    """Заменить SIM-карту комплекса (новую можно ввести номером — если её нет в базе, она заведётся)."""
    try:
        return await svc.sim_replace(
            point_id, payload.serial_number, person_id=payload.person_id, performed_by=_who(user),
            reason=payload.reason, comment=payload.comment,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.post("/points/{point_id}/sim/fix")
async def sim_fix(point_id: str, payload: SimIn, user: UserOut = Depends(require_equipment_move)):
    """Исправить номер SIM комплекса (раньше был записан не тот) — без «замены» и без причины снятия."""
    try:
        return await svc.sim_fix(point_id, payload.serial_number, performed_by=_who(user), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.post("/points/{point_id}/note")
async def point_note(point_id: str, payload: NoteIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        await svc.add_note(point_id=point_id, comment=payload.comment, date=payload.date, performed_by=_who(user))
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return {"ok": True}


class LinkPanelIn(BaseModel):
    panel_id: Optional[int] = None
    date: Optional[str] = None


@router.post("/points/{point_id}/link-panel")
async def link_panel(point_id: str, payload: LinkPanelIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        await solar_panels_repository.link_to_point(point_id, payload.panel_id, _who(user), payload.date)
    except solar_panels_repository.PanelError as exc:
        raise _fail(exc)
    return {"ok": True}


# ------------------------------------------------------------------ журнал

@router.get("/history")
async def history(
    search: str = "", action_type: str = "", date_from: str = "", date_to: str = "",
    limit: int = Query(200, ge=1, le=2000), offset: int = 0,
):
    rows, total, actions = await repo.list_history(limit, offset, search=search, action_type=action_type, date_from=date_from, date_to=date_to)
    return {"items": rows, "total": total, "action_types": actions}


class HistoryEditIn(BaseModel):
    event_date: Optional[str] = None
    comment: Optional[str] = None
    performed_by: Optional[str] = None


@router.put("/history/{entry_id}")
async def edit_history(entry_id: int, payload: HistoryEditIn):
    """Только свободные поля записи (дата события, комментарий, кто выполнил); тип операции и ссылки — факт."""
    if not await repo.edit_history_entry(entry_id, payload.event_date, payload.comment, payload.performed_by):
        raise HTTPException(404, "Запись истории не найдена")
    return {"ok": True}


@router.delete("/history/{entry_id}")
async def delete_history(entry_id: int):
    if not await repo.delete_history_entry(entry_id):
        raise HTTPException(404, "Запись истории не найдена")
    return {"ok": True}


@router.delete("/history")
async def delete_history_bulk(search: str = "", action_type: str = "", date_from: str = "", date_to: str = ""):
    """Массовое удаление по фильтру; пустой фильтр запрещён, чтобы случайно не стереть весь журнал."""
    if not any([search, action_type, date_from, date_to]):
        raise HTTPException(400, "Укажите период или другой фильтр для удаления")
    return {"ok": True, "deleted": await repo.delete_history_bulk(search=search, action_type=action_type, date_from=date_from, date_to=date_to)}


# ------------------------------------------------------------------ отчёты и выгрузки

@router.get("/reports/summary")
async def report_summary(category_id: Optional[int] = None, status_id: Optional[int] = None, date_from: str = "", date_to: str = ""):
    return await repo.summary_by_category(category_id=category_id, status_id=status_id, date_from=date_from, date_to=date_to)


@router.get("/export/summary.xlsx")
async def export_summary(category_id: Optional[int] = None, status_id: Optional[int] = None, date_from: str = "", date_to: str = ""):
    f = dict(category_id=category_id, status_id=status_id, date_from=date_from, date_to=date_to)
    items, _ = await repo.list_items(**f)
    return _xlsx(xlsx.summary_workbook(await repo.summary_by_category(**f), items), "sklad_otchet.xlsx")


async def _used_fields() -> list[dict]:
    return [f for f in await repo.list_fields() if f["value_count"]]


class ItemsExportIn(BaseModel):
    ids: list[int]
    fields: list[str] = []


@router.post("/export/items.xlsx")
async def export_items_selected(payload: ItemsExportIn):
    if not payload.ids:
        raise HTTPException(400, "Не выбрано ни одного оборудования")
    items, _ = await repo.list_items(ids=payload.ids, with_extra=True)
    if not items:
        raise HTTPException(404, "Выбранное оборудование не найдено")
    return _xlsx(xlsx.items_workbook(items, payload.fields, await _used_fields()), "oborudovanie_vybrannoe.xlsx")


@router.get("/export/items.xlsx")
async def export_items(
    search: str = "", category_id: Optional[int] = None, model_id: Optional[int] = None, place: str = "",
    status_id: Optional[int] = None, point_id: str = "", person_id: Optional[int] = None, date_from: str = "",
    date_to: str = "", awaiting: bool = False,
):
    f = _item_filters(search, category_id, model_id, place, status_id, point_id, person_id, date_from, date_to, awaiting)
    items, _ = await repo.list_items(with_extra=True, **f)
    return _xlsx(xlsx.items_workbook(items, None, await _used_fields()), "oborudovanie.xlsx")


@router.get("/export/people.xlsx")
async def export_people(person_id: Optional[int] = None, category_id: Optional[int] = None, date_from: str = "", date_to: str = ""):
    return _xlsx(xlsx.people_workbook(await repo.people_summary(person_id, category_id, date_from, date_to)), "oborudovanie_na_montjorah.xlsx")


@router.get("/export/history.xlsx")
async def export_history(search: str = "", action_type: str = "", date_from: str = "", date_to: str = ""):
    rows, _, _ = await repo.list_history(limit=None, search=search, action_type=action_type, date_from=date_from, date_to=date_to)
    return _xlsx(xlsx.history_workbook(rows), "istoriya_oborudovaniya.xlsx")


@router.get("/export/points.xlsx")
async def export_points(search: str = "", category_id: Optional[int] = None, has_equipment: Optional[bool] = None, has_panel: Optional[bool] = None):
    points, _ = await repo.list_points_with_equipment(search, category_id, has_equipment, has_panel, limit=None)
    by_point: dict[str, list] = {}
    if points:
        items, _ = await repo.list_items(place=eq.PLACE_POINT)
        for it in items:
            by_point.setdefault(it["point_id"], []).append(it)
    return _xlsx(xlsx.points_workbook(points, by_point), "kompleksy_oborudovanie.xlsx")
