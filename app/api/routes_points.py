"""Points directory ("комплексы") — shared data: both the calendar's point
form and the dedicated /warehouse page create/edit every field here."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from app.api.assign_common import check_assignment_target, check_work_type
from app.core import points_version
from app.core.deps import require_password_ok
from app.core.names import compact_name, strip_dup_suffix
from app.models.points import PointIn, PointOut
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import point_links_repository, points_repository, state_repository
from app.services import apk_group_auto, check_service, network_check, point_health_service, point_merge
from app.services.point_matching import PointIndex

router = APIRouter(prefix="/api/points", tags=["points"], dependencies=[Depends(require_password_ok)])


class StatusIn(BaseModel):
    status: str = ""


class PointAssignItem(BaseModel):
    point_id: str
    type: str


class PointsAssignIn(BaseModel):
    items: list[PointAssignItem]
    date: str
    team: int
    hours: float = 1.0
    travel: float = 0.0


def _strip_credentials(point: dict, user: UserOut) -> dict:
    """Логин и пароль комплекса отдаются только тем, у кого есть право «Видеть и копировать логины и пароли» (points.secrets), IP-адрес и порт — тем, у кого есть право
    «Видеть IP-адрес и порт» (points.network); остальные получают пустые значения при том же адресе и том же составе остальных полей справочника."""
    if not P.secrets_ok(user.permissions):
        point = {**point, "login": "", "password": ""}
    if not P.network_ok(user.permissions):
        point = {**point, "ip_address": ""}
    return point


@router.get("")
async def list_points(
    request: Request,
    user: UserOut = Depends(require_password_ok),
    search: str = "",
    category_id: list[int] = Query(default=[]),
    limit: int | None = None,
    offset: int = 0,
    uncategorized: bool = False,
    status: list[str] = Query(default=[]),
    sort: str = "",
    certificate: str = "",
    group_id: int | None = None,
):
    # Plain `GET /api/points` (no params) stays the full, unfiltered list —
    # existing callers (jobForm's point datalist, KML/Excel import matching,
    # the warehouse page) rely on that. The "Все АПК" directory page passes
    # search/category_id/limit/offset (+ uncategorized/status/sort/certificate)
    # instead, and reads X-Total-Count for its "load more" — with ~3000
    # points that page can't afford to fetch everything on every open or
    # keystroke. category_id/status each repeat as a query param
    # (?category_id=1&category_id=2) for the multi-select filter chips —
    # see points_repository.search_points.
    #
    # Список и карта опрашиваются каждые несколько секунд из каждой вкладки, а комплексы меняются редко, поэтому ответ
    # кэшируется по «отпечатку» данных (app/core/points_version.py): тот же отпечаток — «304 Not Modified» без обращения
    # к базе, а тело одинаковых запросов берётся из памяти.
    if group_id is not None:
        await apk_group_auto.sync_if_stale()      # подгруппа «Требуют повторного выезда» ведётся сама: состав свежий к моменту чтения
    tag = await points_version.etag(P.profile(user.permissions), request.url.query)
    if tag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers={"ETag": tag, "Cache-Control": "no-cache"})
    key = (P.profile(user.permissions), request.url.query)
    cached = points_version.get_payload(key, tag)
    if cached is None:
        total = None
        paged = bool(search or category_id or limit is not None or uncategorized or status or sort or certificate or group_id is not None)
        if paged:
            items, total = await points_repository.search_points(
                search, category_id, limit, offset, uncategorized, status, sort, certificate, group_id
            )
            visits = await check_service.last_visits([p["id"] for p in items]) if items else {}      # карточка в списке: последний выезд и что нужно
            items = [{**p, "last_visit": visits.get(p["id"])} for p in items]
        else:
            items = await points_repository.list_points()
        # Поля подгруппы (group_*) нужны только при выборке по подгруппе; в общем списке на ~3000 комплексов они были бы лишними null-ами в каждой строке.
        skip = set() if group_id is not None else {"group_note", "group_need", "group_work_type", "group_added_at"}
        if not paged:
            skip.add("last_visit")
        skip = skip or None
        rows = [PointOut.model_validate(_strip_credentials(p, user)).model_dump(exclude=skip) for p in items]
        body = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        points_version.put_payload(key, tag, body, total)
    else:
        body, total = cached
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if total is not None:
        headers["X-Total-Count"] = str(total)
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("", response_model=PointOut, status_code=status.HTTP_201_CREATED)
async def create_point(payload: PointIn, user: UserOut = Depends(require_password_ok)):
    if await points_repository.get_point(payload.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Точка с таким номером уже есть")
    created = await points_repository.create_point(payload.model_dump())
    return _strip_credentials(created, user)


class ResolveIn(BaseModel):
    labels: list[str]


@router.post("/resolve")
async def resolve_labels(payload: ResolveIn):
    """Для импорта меток с карты: какой уже заведённый комплекс скрывается за каждой меткой (без учёта регистра, пробелов,
    русских/латинских двойников; по инвентарному номеру; хвост «+1», «+2» — повтор того же комплекса). Нет такого —
    `create_as`: под каким названием завести новый комплекс (без хвоста, одинаково для всех повторов в файле)."""
    index = PointIndex(await points_repository.list_points())
    created: dict[str, str] = {}
    items = []
    for label in payload.labels:
        point = index.find(label)
        if point:
            dup = strip_dup_suffix(label)[1] is not None and point["id"].strip().casefold() != str(label).strip().casefold()
            items.append({"label": label, "point_id": point["id"], "create_as": None, "dup": dup})
        else:
            base = index.base_name(label)
            items.append({"label": label, "point_id": None, "create_as": created.setdefault(compact_name(base) or base, base), "dup": False})
    return {"items": items}


@router.get("/duplicates")
async def list_duplicates(user: UserOut = Depends(require_password_ok)):
    """Дубли комплексов («НОМЕР +1», «НОМЕР +2» и номера, отличающиеся лишь регистром/пробелами) — для окна «Объединить дубли»."""
    if not P.has(user.permissions, "points.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: изменять комплексы")
    return {"groups": await point_merge.find_groups()}


class MergeGroupIn(BaseModel):
    base: str
    dups: list[str]


class MergeIn(BaseModel):
    groups: list[MergeGroupIn]


@router.post("/merge-duplicates")
async def merge_duplicates(payload: MergeIn, user: UserOut = Depends(require_password_ok)):
    """Объединить выбранные группы: всё, что привязано к дублям, переходит на основной комплекс, дубли удаляются."""
    if not P.has(user.permissions, "points.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: изменять комплексы")
    results = []
    for g in payload.groups:
        try:
            results.append(await point_merge.merge(g.base, g.dups))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"results": results, "merged": sum(len(r["merged"]) for r in results)}


@router.post("/assign")
async def assign_points(payload: PointsAssignIn):
    """Turns several points picked straight from the directory (the "Все
    АПК" page, or the calendar's "+ Точка" multi-pick) into tasks for one
    бригада/day at once, each with its own type of work. A point that
    is already added — a task that day for any бригада, or one in the same
    командировка (see state_repository.point_ids_on) — is skipped rather than
    duplicated; the names come back in `skipped_points` so the UI can say
    "такая точка уже добавлена". Nothing is consumed here (unlike
    /api/point-plans/assign), so an accidental second add is an easy mistake;
    KML import skips repeats the same way. The work text starts as the
    point's own description."""
    if not payload.items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одной точки")
    for item in payload.items:
        check_work_type(item.type)
    await check_assignment_target(payload.date, payload.team)

    already = await state_repository.point_ids_on(payload.date, payload.team)
    new_tasks, skipped = [], []
    for item in payload.items:
        point = await points_repository.get_point(item.point_id)
        if not point:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Точка «{item.point_id}» не найдена")
        key = point["id"].strip().lower()
        if key in already:
            skipped.append(point["id"])
            continue
        already.add(key)
        new_tasks.append({
            "id": "t" + uuid.uuid4().hex,
            "date": payload.date,
            "team": payload.team,
            "point": point["id"],
            "work": point.get("description") or "",
            "address": "",
            "hours": payload.hours,
            "travel": payload.travel,
            "type": item.type,
            "note": "",
        })
    await state_repository.add_tasks(new_tasks)
    return {"created": len(new_tasks), "skipped": len(skipped), "skipped_points": skipped}


@router.put("/{point_id}", response_model=PointOut)
async def update_point(point_id: str, payload: PointIn, user: UserOut = Depends(require_password_ok)):
    current = await points_repository.get_point(point_id)
    if not current:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    if payload.id != point_id and await points_repository.get_point(payload.id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Точка с таким номером уже есть")
    data = payload.model_dump()
    # Сведения, которых роли видеть нельзя (логин, пароль, IP, порт), форма присылает пустыми: правка комплекса не должна их стирать — остаются прежние.
    if not P.secrets_ok(user.permissions):
        data["login"], data["password"] = current.get("login") or "", current.get("password") or ""
    if not P.network_ok(user.permissions):
        data["ip_address"], data["port"] = current.get("ip_address") or "", current.get("port") or ""
    await points_repository.update_point(point_id, data)
    updated = await points_repository.get_point(payload.id)
    return _strip_credentials(updated, user)


@router.put("/{point_id}/status", response_model=PointOut)
async def set_point_status(point_id: str, payload: StatusIn, user: UserOut = Depends(require_password_ok)):
    """Narrow status-only update — lets a caller (e.g. the warehouse page's
    complex card) change just the status without having to resend every
    other field of the point, unlike the full PUT /{point_id} above."""
    if not await points_repository.get_point(point_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    await points_repository.set_status(point_id, payload.status.strip(), source="manual")
    result = await points_repository.get_point(point_id)
    return _strip_credentials(result, user)


@router.delete("/{point_id}")
async def delete_point(point_id: str):
    if not await points_repository.get_point(point_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    await points_repository.delete_point(point_id)
    return {"ok": True}


@router.get("/{point_id}/sim-links")
async def point_sim_links(point_id: str):
    """С какими комплексами связан этот общей SIM (смена номера SIM здесь меняет её и у них) — для подсказки в карточке."""
    return {"linked": await point_links_repository.linked_with(point_id)}


@router.get("/{point_id}/ping")
async def ping_point(point_id: str):
    point = await points_repository.get_point(point_id)
    if not point:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    result = await network_check.ping_host(point.get("ip_address", ""))
    await point_health_service.recheck_and_apply(point_id)
    return result


@router.get("/{point_id}/check-port")
async def check_point_port(point_id: str):
    point = await points_repository.get_point(point_id)
    if not point:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    result = await network_check.check_tcp_port(point.get("ip_address", ""), point.get("port", ""))
    await point_health_service.recheck_and_apply(point_id)
    return result


@router.get("/{point_id:path}", response_model=PointOut)
async def get_point_by_id(point_id: str, user: UserOut = Depends(require_password_ok)):
    """Один комплекс по номеру (точному): карточка не должна зависеть от того, попал ли он в первые строки поиска. Объявлен последним — «/duplicates», «/…/ping» и др. разбираются раньше."""
    point = await points_repository.get_point(point_id)
    if not point:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    return _strip_credentials(point, user)
