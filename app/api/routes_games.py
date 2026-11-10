"""Игры (страница /games): свои игры и рекорды у пользователей, которым администратор их включил, и настройка доступа в «Администрировании → Игры».

Каталог, правила подсчёта и защита — app/services/games.py. Игры включаются каждому пользователю отдельно; без включения все обращения к играм — отказ."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.clientip import client_ip
from app.core.deps import require_admin, require_password_ok
from app.models.user import UserOut
from app.repositories import user_repository
from app.services import games, security_log

router = APIRouter(prefix="/api/games", tags=["games"])
admin_router = APIRouter(prefix="/api/admin/games", tags=["admin"], dependencies=[Depends(require_admin)])


async def _allowed(user: UserOut) -> list[str]:
    keys = await games.allowed_keys(user.id)
    if keys is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Игры вам не включены — их включает администратор (Администрирование → Игры)")
    return keys


@router.get("/me")
async def my_games(user: UserOut = Depends(require_password_ok)):
    """Включены ли игры этому пользователю и какие именно; здесь же его лучшие результаты. Не включены — enabled=false (без ошибки: по этому ответу страницы показывают пункт меню)."""
    keys = await games.allowed_keys(user.id)
    if keys is None:
        return {"enabled": False, "games": [], "bests": {}, "categories": []}
    return {"enabled": True, "games": games.public_catalog(keys), "bests": await games.my_bests(user.id), "categories": games.category_list()}


@router.get("/leaderboard")
async def leaderboard(game: str, user: UserOut = Depends(require_password_ok)):
    keys = await _allowed(user)
    if game not in keys:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Эта игра вам не включена")
    try:
        return await games.leaderboard(game, user.id)
    except games.ScoreError as exc:
        raise HTTPException(exc.status_code, exc.detail)


class ScoreIn(BaseModel):
    game: str = Field(max_length=32)
    score: float


@router.post("/score")
async def post_score(payload: ScoreIn, user: UserOut = Depends(require_password_ok)):
    try:
        return await games.submit_score(user.id, payload.game, payload.score)
    except games.ScoreError as exc:
        raise HTTPException(exc.status_code, exc.detail)


# ---------------------------------------------------------------- администрирование

class AccessIn(BaseModel):
    enabled: bool
    games: Optional[list[str]] = None        # None — все игры; список — только эти


class BulkIn(BaseModel):
    enabled: bool


class ResetIn(BaseModel):
    game: Optional[str] = None
    user_id: Optional[int] = None


@admin_router.get("")
async def admin_overview():
    return {"catalog": games.public_catalog(), "users": await games.users_access()}


@admin_router.put("/users/{user_id}")
async def admin_set_access(user_id: int, payload: AccessIn, request: Request, admin: UserOut = Depends(require_admin)):
    target = await user_repository.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    unknown = [k for k in (payload.games or []) if k not in games.BY_KEY]
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Неизвестные игры: {', '.join(unknown)}")
    await games.set_access(user_id, payload.enabled, payload.games)
    await security_log.record("games_access", username=admin.username, role=admin.role, ip=client_ip(request),
                              details={"user": target["username"], "enabled": payload.enabled, "games": "все" if payload.games is None else len(payload.games)})
    return {"ok": True}


@admin_router.post("/bulk")
async def admin_bulk(payload: BulkIn, request: Request, admin: UserOut = Depends(require_admin)):
    n = await games.set_access_bulk(payload.enabled)
    await security_log.record("games_access", username=admin.username, role=admin.role, ip=client_ip(request), details={"user": "все", "enabled": payload.enabled, "count": n})
    return {"changed": n}


@admin_router.post("/reset-scores")
async def admin_reset(payload: ResetIn, request: Request, admin: UserOut = Depends(require_admin)):
    if payload.game and payload.game not in games.BY_KEY:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Такой игры нет")
    n = await games.reset_scores(game=payload.game, user_id=payload.user_id)
    await security_log.record("games_reset", username=admin.username, role=admin.role, ip=client_ip(request),
                              details={"game": payload.game or "все", "user_id": payload.user_id if payload.user_id is not None else "все", "removed": n})
    return {"removed": n}
