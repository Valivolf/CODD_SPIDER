"""«Уведомления» — история того, о чём узнаёт диспетчер: проверки комплексов, решения по запросам монтёров, отчёты
(и среди них — не заработавшие комплексы: что неисправно и что нужно для исправления). См. notifications_repository.py."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import data_issues_repository, notifications_repository, point_links_repository
from app.services import equipment_service

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_password_ok)])


def _day(value: str) -> str:
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, "Неверная дата")
    return value


@router.get("/pending-requests")
async def pending_requests(user: UserOut = Depends(require_password_ok)):
    """Всё, что ждёт решения диспетчера, одним запросом: проверки комплексов, «нет доступа после смены SIM», запросы на демонтаж.
    Колокольчик на каждой странице спрашивает это раз в несколько секунд — один запрос вместо трёх (права — те же, что у трёх списков)."""
    from app.core.point_credentials import strip_credentials  # noqa: PLC0415
    from app.repositories import (  # noqa: PLC0415
        access_issue_requests_repository, demontage_requests_repository, verification_requests_repository,
    )

    verification = await verification_requests_repository.list_pending()
    access = await access_issue_requests_repository.list_pending()
    demontage = await demontage_requests_repository.list_pending()
    return {"verification": strip_credentials(verification, user), "access": strip_credentials(access, user), "demontage": strip_credentials(demontage, user)}


@router.get("/data-issues")
async def list_data_issues(
    kind: str = "sim", date_from: str = "", date_to: str = "", q: str = "", hint: str = "", acked: bool = False,
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), user: UserOut = Depends(require_password_ok),
):
    """Вкладка «Ошибки данных»: kind=day — одна точка в один день несколько раз; kind=sim — одинаковый номер SIM у нескольких
    комплексов (показываются все совпадения, без отсева по портам). Логин/пароль комплекса — по праву points.secrets, IP — по праву points.network."""
    if kind not in data_issues_repository.KINDS:
        raise HTTPException(400, "Неизвестный вид ошибок данных")
    if hint not in data_issues_repository.HINT_FILTERS:
        raise HTTPException(400, "Неизвестный фильтр")
    return await data_issues_repository.list_issues(
        kind, date_from=_day(date_from), date_to=_day(date_to), q=q.strip(), hint=hint, acked=acked, limit=limit, offset=offset,
        include_credentials=P.secrets_ok(user.permissions), include_ip=P.network_ok(user.permissions),
    )


@router.get("/data-issues/summary")
async def data_issues_summary(date_from: str = ""):
    """Сколько открытых ошибок данных — для бейджа на вкладке (дубли считаются с date_from и позже)."""
    return await data_issues_repository.summary(_day(date_from))


class AckIn(BaseModel):
    kind: str
    key: str
    value: bool = True


@router.post("/data-issues/ack")
async def ack_data_issue(payload: AckIn, user: UserOut = Depends(require_password_ok)):
    """«Считать нормой» / вернуть в список. Ничего не удаляет; при изменении состава группы отметка перестаёт действовать."""
    if not await data_issues_repository.set_ack(payload.kind, payload.key, payload.value, user.username):
        raise HTTPException(404, "Такой записи уже нет — возможно, данные исправили")
    return {"ok": True}


class LinkIn(BaseModel):
    point_ids: list[str]


@router.post("/data-issues/link")
async def link_data_issue_points(payload: LinkIn):
    """Связать комплексы с одним номером SIM: дальше смена SIM на любом из них меняет её у остальных."""
    try:
        return await point_links_repository.link_points(payload.point_ids)
    except point_links_repository.LinkError as exc:
        raise HTTPException(exc.status_code, exc.detail)


class UnlinkIn(BaseModel):
    point_id: str = ""
    group_id: int | None = None


@router.post("/data-issues/unlink")
async def unlink_data_issue_points(payload: UnlinkIn):
    """Убрать комплекс из связки (point_id) либо разорвать связку целиком (group_id). Номера SIM при этом не меняются."""
    if payload.group_id is not None:
        done = await point_links_repository.unlink_group(payload.group_id)
    elif payload.point_id:
        done = await point_links_repository.unlink_point(payload.point_id)
    else:
        raise HTTPException(400, "Укажите комплекс или связку")
    if not done:
        raise HTTPException(404, "Такой связки уже нет")
    return {"ok": True}


class KeepSimIn(BaseModel):
    point_id: str


@router.post("/data-issues/keep-sim")
async def keep_sim_on_point(payload: KeepSimIn, user: UserOut = Depends(require_password_ok)):
    """«Оставить здесь»: SIM и IP остаются у выбранного комплекса, у остальных комплексов с тем же номером SIM они убираются."""
    try:
        return await equipment_service.keep_sim_on_point(payload.point_id, performed_by=user.username)
    except equipment_service.EquipmentError as exc:
        raise HTTPException(exc.status_code, exc.detail)


@router.get("")
async def list_notifications(
    kind: str = "", date_from: str = "", date_to: str = "", q: str = "", limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0), tz_offset: int = Query(default=0, ge=-900, le=900),
):
    if kind not in ("", *notifications_repository.KINDS):
        raise HTTPException(400, "Неизвестный вид уведомлений")
    return await notifications_repository.list_notifications(
        kind=kind, date_from=_day(date_from), date_to=_day(date_to), q=q.strip(), limit=limit, offset=offset, tz_offset_minutes=tz_offset,
    )
