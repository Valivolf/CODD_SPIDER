"""API бота по оборудованию — тот же server-to-server ключ, что у остальных /api/bot/*.

Монтёр: что стоит на комплексе по категориям, что на нём числится, замена / установка / снятие.
Ответственный за склад (роль «склад» в персонале): принять снятое и возвращённое, выдать монтёру,
поставить состояние, остатки. Доступ к складским вызовам — только у активного человека с этой ролью."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core import equipment as eq
from app.core.deps import require_bot_key
from app.repositories import equipment_repository as repo
from app.repositories import people_repository
from app.services import equipment_service as svc

router = APIRouter(prefix="/api/bot", tags=["bot-equipment"], dependencies=[Depends(require_bot_key)])


async def _actor(vk_id: int, *, warehouse: bool = False) -> dict[str, Any]:
    person = await people_repository.get_by_vk_id(vk_id)
    if not person or not person["active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найден или неактивен")
    if warehouse and person["role_type"] != "warehouse":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Раздел доступен только роли «склад»")
    return person


def _who(person: dict[str, Any]) -> str:
    return f"{person['full_name']} (бот)"


def _short(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"], "serial_number": item["serial_number"], "model_name": item["model_name"],
        "category_id": item["category_id"], "category_name": item["category_name"], "status": item["status"] or "",
        "status_id": item["status_id"], "status_usable": item["status_usable"], "place": item["place"], "point_id": item["point_id"] or "",
        "person_name": item["person_name"] or "", "held_reason": item["held_reason"] or "",
        "removed_point_id": item["removed_point_id"] or "", "removed_by": item["removed_by"] or "",
        "removed_date": item["removed_date"] or "", "accepted_by": item["accepted_by"] or "",
        "removal_reason": item.get("removal_reason") or "", "category_kind": item.get("category_kind") or "",
    }


def _fail(exc: svc.EquipmentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


# ------------------------------------------------------------------ монтёр

@router.get("/equipment/point/{point_id:path}")
async def bot_point_equipment(point_id: str):
    """Оборудование на комплексе по категориям + все категории (чтобы поставить то, чего на комплексе ещё нет)."""
    groups = await svc.point_equipment_by_category(point_id)
    return {
        "groups": [
            {"category_id": g["category_id"], "category_name": g["category_name"], "items": [_short(i) for i in g["items"]]}
            for g in groups
        ],
        "categories": [{"id": c["id"], "name": c["name"]} for c in await repo.list_categories()],
    }


@router.get("/equipment/held")
async def bot_held(vk_id: int, category_id: Optional[int] = None):
    """Всё, что числится на монтёре: выданное (годится для установки) и снятое (ждёт сдачи на склад)."""
    person = await _actor(vk_id)
    return [_short(i) for i in await repo.person_items(person["id"], category_id)]


class ActualIn(BaseModel):
    """«Стоит другой номер»: id найденного оборудования либо набранный номер (+ создать, если его нет в базе)."""
    actual_equipment_id: Optional[int] = None
    actual_serial: Optional[str] = None
    create_missing: bool = False


class ReplaceIn(ActualIn):
    vk_id: int
    point_id: str
    old_equipment_id: int
    new_equipment_id: int
    reason: Optional[str] = None   # почему снимается старое


@router.post("/equipment/replace")
async def bot_replace(payload: ReplaceIn):
    person = await _actor(payload.vk_id)
    try:
        return await svc.replace(
            payload.point_id, payload.old_equipment_id, payload.new_equipment_id, person_id=person["id"],
            performed_by=_who(person), comment="через бота", actual_equipment_id=payload.actual_equipment_id,
            actual_serial=payload.actual_serial, create_missing=payload.create_missing, reason=payload.reason,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class InstallIn(BaseModel):
    vk_id: int
    point_id: str
    equipment_id: int


@router.post("/equipment/install")
async def bot_install(payload: InstallIn):
    person = await _actor(payload.vk_id)
    try:
        await svc.attach(
            payload.point_id, payload.equipment_id, person_id=person["id"], performed_by=_who(person), comment="через бота",
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)
    item = await repo.get_item(payload.equipment_id)
    return {"installed": item["serial_number"] if item else ""}


class RemoveIn(ActualIn):
    vk_id: int
    point_id: str
    equipment_id: int
    reason: Optional[str] = None   # почему снимается


@router.post("/equipment/remove")
async def bot_remove(payload: RemoveIn):
    """Снять с комплекса без замены: числится на монтёре как дефектное, пока не сдано на склад."""
    person = await _actor(payload.vk_id)
    try:
        return await svc.detach(
            payload.point_id, payload.equipment_id, to_person_id=person["id"], performed_by=_who(person), comment="через бота",
            actual_equipment_id=payload.actual_equipment_id, actual_serial=payload.actual_serial,
            create_missing=payload.create_missing, reason=payload.reason,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.get("/equipment/by-suffix")
async def bot_by_suffix(vk_id: int, suffix: str, category_id: Optional[int] = None):
    """Поиск оборудования по последним символам серийного номера (6 и больше), по всему учёту: что найдено, можно ли это поставить и где оно сейчас."""
    await _actor(vk_id)
    try:
        items = await svc.candidates_by_suffix(suffix, category_id=category_id)
    except svc.EquipmentError as exc:
        raise _fail(exc)
    return [{**_short(i), "usable": i["usable"], "why": i["why"]} for i in items[:30]]


class SuffixInstallIn(BaseModel):
    vk_id: int
    point_id: str
    suffix: str
    old_equipment_id: Optional[int] = None     # что снимается (замена); пусто — просто установка
    new_equipment_id: Optional[int] = None     # выбранная единица, если по символам нашлось несколько
    category_id: Optional[int] = None
    reason: Optional[str] = None               # почему снимается старое


@router.post("/equipment/install-by-suffix")
async def bot_install_by_suffix(payload: SuffixInstallIn):
    """Поставить оборудование, указанное последними символами номера (даже если его не выдавали или в учёте его ещё нет — тогда привяжется само, когда его добавят)."""
    person = await _actor(payload.vk_id)
    try:
        return await svc.install_by_suffix(
            payload.point_id, payload.suffix, person_id=person["id"], performed_by=_who(person), old_equipment_id=payload.old_equipment_id,
            new_equipment_id=payload.new_equipment_id, category_id=payload.category_id, reason=payload.reason, comment="через бота",
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class BotSimIn(BaseModel):
    vk_id: int
    point_id: str
    serial_number: str
    reason: Optional[str] = None
    removed_working: Optional[bool] = None    # снятая SIM рабочая (True) или нет (False); None — не указано


@router.post("/equipment/sim/replace")
async def bot_sim_replace(payload: BotSimIn):
    """Монтёр заменил SIM-карту на комплексе: новая (номер набран или выдана ему) встаёт на комплекс, старая числится
    на нём как снятая — с причиной. Поле «Номер SIM» комплекса и IP из реестра обновляются."""
    person = await _actor(payload.vk_id)
    try:
        return await svc.sim_replace(
            payload.point_id, payload.serial_number, person_id=person["id"], performed_by=_who(person),
            reason=payload.reason, comment="через бота", removed_working=payload.removed_working,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.post("/equipment/sim/fix")
async def bot_sim_fix(payload: BotSimIn):
    """Исправление номера SIM: на комплексе был указан не тот номер. Это не замена — оборудование не снимается."""
    person = await _actor(payload.vk_id)
    try:
        return await svc.sim_fix(payload.point_id, payload.serial_number, performed_by=_who(person), comment="исправлено через бота")
    except svc.EquipmentError as exc:
        raise _fail(exc)


class DemontageIn(BaseModel):
    vk_id: int
    point_id: str
    whole: bool = False
    equipment_ids: list[int] = []
    reason: Optional[str] = None


@router.post("/equipment/demontage")
async def bot_demontage(payload: DemontageIn):
    """Демонтаж: весь комплекс (`whole`) или отдельные элементы — снимаются на монтёра и ждут приёмки на склад."""
    person = await _actor(payload.vk_id)
    try:
        return await svc.demontage_remove(
            payload.point_id, equipment_ids=None if payload.whole else payload.equipment_ids, person_id=person["id"],
            performed_by=_who(person), reason=payload.reason,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.get("/equipment/find")
async def bot_find(vk_id: int, q: str):
    """Поиск оборудования по номеру среди всего (для «стоит другой номер»): совпадение целиком, иначе по части."""
    await _actor(vk_id)
    q = q.strip()
    if not q:
        return []
    exact = await repo.find_by_serial(q)
    if exact:
        return [_short(exact)]
    items, _ = await repo.list_items(10, 0, search=q)
    return [_short(i) for i in items]


# ------------------------------------------------------------------ склад

@router.get("/warehouse/summary")
async def wh_summary(vk_id: int):
    await _actor(vk_id, warehouse=True)
    return {
        "stock": await repo.stock_totals_by_category(), "awaiting": await repo.awaiting_accept_people(),
        "unknown": await repo.unknown_count(),
    }


@router.get("/warehouse/awaiting")
async def wh_awaiting(vk_id: int):
    await _actor(vk_id, warehouse=True)
    return await repo.awaiting_accept_people()


@router.get("/warehouse/person/{person_id}/items")
async def wh_person_items(person_id: int, vk_id: int):
    await _actor(vk_id, warehouse=True)
    return [_short(i) for i in await repo.person_items(person_id)]


class AcceptIn(BaseModel):
    vk_id: int
    equipment_ids: list[int]
    person_id: Optional[int] = None
    status_id: Optional[int] = None
    comment: Optional[str] = None


@router.post("/warehouse/accept")
async def wh_accept(payload: AcceptIn):
    person = await _actor(payload.vk_id, warehouse=True)
    try:
        return await svc.accept_from_person(
            payload.equipment_ids, performed_by=_who(person), comment=payload.comment, status_id=payload.status_id,
            person_id=payload.person_id,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class AcceptSubstituteIn(ActualIn):
    vk_id: int
    expected_id: int
    person_id: Optional[int] = None


@router.post("/warehouse/accept-substitute")
async def wh_accept_substitute(payload: AcceptSubstituteIn):
    """Монтёр сдал другой номер, чем числилось на нём: сданное принимается, числившееся — «неизвестное местонахождение»."""
    person = await _actor(payload.vk_id, warehouse=True)
    try:
        return await svc.accept_substitute(
            payload.expected_id, actual_equipment_id=payload.actual_equipment_id, actual_serial=payload.actual_serial,
            create_missing=payload.create_missing, performed_by=_who(person), comment="через бота", person_id=payload.person_id,
        )
    except svc.EquipmentError as exc:
        raise _fail(exc)


class FoundIn(BaseModel):
    vk_id: int
    equipment_id: int


@router.post("/warehouse/found")
async def wh_found(payload: FoundIn):
    person = await _actor(payload.vk_id, warehouse=True)
    try:
        return await svc.found_unknown(payload.equipment_id, performed_by=_who(person), comment="через бота")
    except svc.EquipmentError as exc:
        raise _fail(exc)


@router.get("/warehouse/fitters")
async def wh_fitters(vk_id: int, q: str = "", limit: int = Query(50, ge=1, le=200)):
    await _actor(vk_id, warehouse=True)
    people = await people_repository.list_people(active_only=True)
    key = q.strip().casefold()
    out = [
        {"id": p["id"], "full_name": p["full_name"]}
        for p in people if p["role_type"] == "fitter" and (not key or all(t in p["full_name"].casefold() for t in key.split()))
    ]
    return out[:limit]


@router.get("/warehouse/categories")
async def wh_categories(vk_id: int):
    """Категории, у которых на складе есть годное для выдачи оборудование."""
    await _actor(vk_id, warehouse=True)
    items, _ = await repo.list_items(place=eq.PLACE_STOCK, usable=True)
    counts: dict[int, dict[str, Any]] = {}
    for it in items:
        c = counts.setdefault(it["category_id"], {"id": it["category_id"], "name": it["category_name"], "count": 0})
        c["count"] += 1
    return sorted(counts.values(), key=lambda c: c["name"].casefold())


@router.get("/warehouse/stock")
async def wh_stock(vk_id: int, category_id: Optional[int] = None, q: str = "", limit: int = Query(30, ge=1, le=200), offset: int = 0):
    await _actor(vk_id, warehouse=True)
    items, total = await repo.list_items(limit, offset, place=eq.PLACE_STOCK, usable=True, category_id=category_id, search=q)
    return {"items": [_short(i) for i in items], "total": total}


class WhIssueIn(BaseModel):
    vk_id: int
    person_id: int
    equipment_ids: list[int] = []
    serials: list[str] = []


@router.post("/warehouse/issue")
async def wh_issue(payload: WhIssueIn):
    """Выдать монтёру: по id (выбор из списка) и/или по набранным вручную серийным номерам."""
    actor = await _actor(payload.vk_id, warehouse=True)
    ids = list(payload.equipment_ids)
    not_found: list[str] = []
    for serial in payload.serials:
        found = await repo.find_by_serial(serial)
        if found:
            ids.append(found["id"])
        elif serial.strip():
            not_found.append(serial.strip())
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не найдено ни одного оборудования" + (f": {', '.join(not_found)}" if not_found else ""))
    try:
        res = await svc.issue(payload.person_id, ids, performed_by=_who(actor), comment="через бота")
    except svc.EquipmentError as exc:
        raise _fail(exc)
    serials_by_id = {}
    for eid in res["issued"]:
        item = await repo.get_item(eid)
        if item:
            serials_by_id[eid] = item["serial_number"]
    return {"issued": [serials_by_id.get(e, str(e)) for e in res["issued"]], "skipped": res["skipped"], "not_found": not_found}


@router.get("/warehouse/find")
async def wh_find(vk_id: int, q: str):
    await _actor(vk_id, warehouse=True)
    q = q.strip()
    if not q:
        return []
    exact = await repo.find_by_serial(q)
    if exact:
        return [_short(exact)]
    items, _ = await repo.list_items(10, 0, search=q)
    return [_short(i) for i in items]


@router.get("/warehouse/statuses")
async def wh_statuses(vk_id: int):
    await _actor(vk_id, warehouse=True)
    return [{"id": s["id"], "name": s["name"], "usable": s["usable"], "role": s["role"]} for s in await repo.list_statuses()]


class WhStatusIn(BaseModel):
    vk_id: int
    equipment_id: int
    status_id: int
    comment: Optional[str] = None


@router.post("/warehouse/set-status")
async def wh_set_status(payload: WhStatusIn):
    actor = await _actor(payload.vk_id, warehouse=True)
    try:
        return await svc.set_status(payload.equipment_id, payload.status_id, performed_by=_who(actor), comment=payload.comment)
    except svc.EquipmentError as exc:
        raise _fail(exc)
