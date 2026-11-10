"""«Живые карты» — карты в Яндекс.Конструкторе, которые приложение само обновляет раз в несколько минут
(цвет точек следует за статусами комплексов). Окно управления — кнопка «Живые карты» на странице «Все АПК».
Логика — app/services/live_maps.py; работу в Конструкторе делает робот (как и для карт бригад)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import live_maps_repository as repo
from app.services import live_maps as svc

router = APIRouter(prefix="/api/live-maps", tags=["live-maps"], dependencies=[Depends(require_password_ok)])

Kind = Literal["access", "access_cert", "list", "dropped", "filter"]


async def _editor(user: UserOut = Depends(require_password_ok)) -> UserOut:
    """Создавать и менять живые карты — по праву «Карты бригад и живые карты» (maps.manage)."""
    if not P.has(user.permissions, "maps.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: карты бригад и «живые карты»")
    return user


class PreviewIn(BaseModel):
    kind: Kind
    params: dict[str, Any] = Field(default_factory=dict)


class LiveMapIn(BaseModel):
    name: str
    kind: Kind
    params: dict[str, Any] = Field(default_factory=dict)
    membership: Optional[Literal["fixed", "dynamic"]] = None
    interval_minutes: int = svc.DEFAULT_INTERVAL


class LiveMapPatchIn(BaseModel):
    name: Optional[str] = None
    interval_minutes: Optional[int] = None
    enabled: Optional[bool] = None
    certificate: Optional[str] = None                      # поверка: "" (не важно) | valid | expired | missing | invalid
    membership: Optional[Literal["fixed", "dynamic"]] = None


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Назовите карту")
    if len(name) > svc.MAX_NAME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Название длиннее {svc.MAX_NAME} символов")
    return name


def _check_interval(minutes: int) -> int:
    if minutes not in svc.INTERVALS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Обновлять можно раз в " + ", ".join(str(m) for m in svc.INTERVALS) + " минут")
    return minutes


async def _get_or_404(map_id: int) -> dict:
    lm = await repo.get(map_id)
    if lm is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Карта не найдена")
    return lm


def _normalized(kind: str, params: dict) -> dict:
    try:
        return svc.normalize_params(kind, params)
    except svc.LiveMapError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _condition_changes(lm: dict, body: LiveMapPatchIn) -> dict[str, Any]:
    """Поверка и состав у готовой карты: меняем условие, а у зафиксированной карты заново запоминаем подходящие комплексы."""
    if body.certificate is None and body.membership is None:
        return {}
    kind = lm["kind"]
    params = dict(lm["params"] or {})
    membership = lm["membership"]
    out: dict[str, Any] = {}
    changed = False
    if body.certificate is not None:
        cert = body.certificate.strip()
        if cert not in ("", *svc.CERT_FILTERS):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестное условие по поверке")
        if kind == "access_cert":                   # прежний вид «доступ и поверка» — теперь обычный «доступ» + отдельная поверка
            kind = "access"
            out["kind"] = kind
            params["certificate"] = cert
            changed = True
        elif (params.get("certificate") or "") != cert:
            params["certificate"] = cert
            changed = True
    if body.membership is not None and body.membership != membership:
        if kind == "list":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "У своего списка состав задаёте вы — измените список")
        if kind == "dropped":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Карта «Отвалились за период» всегда пересчитывается")
        membership = body.membership
        out["membership"] = membership
        changed = True
    if not changed:
        return {}
    out["params"] = params
    out["last_check_ts"] = 0                        # обновить при ближайшей проверке
    if kind == "list" or membership == "fixed":
        points, _ = await svc.select_points(kind, params)
        out["members"] = [p["id"] for p in points]
    return out


@router.get("")
async def list_live_maps() -> dict[str, Any]:
    return {
        "items": [svc.public(m) for m in await repo.list_all()],
        "intervals": list(svc.INTERVALS),
        "kinds": svc.KIND_LABELS,
    }


@router.post("/preview")
async def preview(body: PreviewIn, _: UserOut = Depends(_editor)) -> dict[str, Any]:
    """Сколько комплексов подходит под условие (и сколько из них можно поставить на карту)."""
    params = _normalized(body.kind, body.params)
    points, unknown = await svc.select_points(body.kind, params)
    return {
        "total": len(points), "with_coords": svc.count_with_coords(points), "unknown": unknown[:30],
        "sample": [p["id"] for p in points[:8]],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_live_map(body: LiveMapIn, user: UserOut = Depends(_editor)) -> dict[str, Any]:
    name = _clean_name(body.name)
    interval = _check_interval(body.interval_minutes)
    params = _normalized(body.kind, body.params)
    membership = "fixed" if body.kind == "list" else (body.membership or svc.default_membership(body.kind))
    if body.kind == "dropped":
        membership = "dynamic"      # «отвалились за период» — окно времени скользит, состав всегда пересчитывается
    points, unknown = await svc.select_points(body.kind, params)
    with_coords = svc.count_with_coords(points)
    if not with_coords:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Под условие не подошло ни одного комплекса с координатами"
            + (f" (не найдены в справочнике: {', '.join(unknown[:5])})" if unknown else ""),
        )
    lm = await repo.create(
        name=name, kind=body.kind, params=params, membership=membership,
        members=[p["id"] for p in points], interval_minutes=interval, created_by=user.username,
    )
    job, plan, _ = await svc.sync_now(lm["id"])
    result = svc.public(await repo.get(lm["id"]))  # type: ignore[arg-type]
    result["job_id"] = job.id if job else None
    result["unknown"] = unknown[:30]
    return result


@router.put("/{map_id}")
async def update_live_map(map_id: int, body: LiveMapPatchIn, _: UserOut = Depends(_editor)) -> dict[str, Any]:
    lm = await _get_or_404(map_id)
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = _clean_name(body.name)
    if body.interval_minutes is not None:
        fields["interval_minutes"] = _check_interval(body.interval_minutes)
    if body.enabled is not None:
        fields["enabled"] = body.enabled
        if body.enabled:
            fields["last_check_ts"] = 0       # включили — обновить при ближайшей проверке
    fields.update(await _condition_changes(lm, body))
    await repo.update_fields(map_id, **fields)
    if "name" in fields and fields["name"] != lm["name"] and lm["map_id"]:
        # Название карты в Конструкторе поменяется при следующем обновлении (оно сравнивает названия).
        await repo.update_fields(map_id, last_check_ts=0)
    return svc.public(await _get_or_404(map_id))


@router.post("/{map_id}/sync")
async def sync_live_map(map_id: int, _: UserOut = Depends(_editor)) -> dict[str, Any]:
    """«Обновить сейчас» — не дожидаясь таймера."""
    await _get_or_404(map_id)
    job, plan, points = await svc.sync_now(map_id, force=False)
    return {"job_id": job.id if job else None, "uptodate": job is None, "points": points, "kind": plan["kind"], "summary": plan.get("summary", "")}


@router.post("/{map_id}/rebuild")
async def rebuild_live_map(map_id: int, _: UserOut = Depends(_editor)) -> dict[str, Any]:
    """«Пересобрать состав» — заново применить условие и зафиксировать подходящие комплексы."""
    await _get_or_404(map_id)
    try:
        ids = await svc.rebuild_members(map_id)
    except svc.LiveMapError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    job, plan, points = await svc.sync_now(map_id)
    return {"members": len(ids), "job_id": job.id if job else None, "uptodate": job is None, "points": points}


@router.delete("/{map_id}")
async def delete_live_map(map_id: int, _: UserOut = Depends(_editor)) -> dict[str, Any]:
    lm = await _get_or_404(map_id)
    if svc.active_job(map_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Карта сейчас обновляется — подождите минуту и удалите её снова")
    await svc.forget_map(lm)
    await repo.delete(map_id)
    from app.core import map_events

    map_events.kick()   # корзина разберётся сразу, а не через полминуты
    return {"ok": True}
