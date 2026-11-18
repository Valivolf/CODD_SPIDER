from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import get_settings
from app.core import rate_limit
from app.core.clientip import client_ip
from app.core import permissions as P
from app.core.deps import build_user, get_current_user, get_session_user
from app.core.security import create_access_token
from app.models.user import PasswordChange, UserLogin, UserOut
from app.repositories import user_repository
from app.services import auth_service, security_log

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _cookie_secure(request: Request) -> bool:
    """Флаг Secure у cookie сессии — только если запрос реально пришёл по
    HTTPS (напрямую или через прокси: caddy передаёт X-Forwarded-Proto).
    Раньше он зависел только от COOKIE_SECURE в .env: при COOKIE_SECURE=true
    (вариант для Docker с HTTPS) и запуске без Docker по обычному http://
    (scripts/Запустить.bat) браузер молча не сохранял cookie — вход «успешно»
    проходил, а следующий же запрос возвращал на экран входа."""
    if not get_settings().cookie_secure:
        return False
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip().lower()
    return proto == "https"


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )


@router.post("/login")
async def login(payload: UserLogin, request: Request, response: Response):
    throttle_key = payload.username.strip().lower()
    ip = client_ip(request)
    if rate_limit.ip_locked(ip):
        await security_log.record("login_locked", username=throttle_key, ip=ip, ok=False, details={"reason": "много неудачных входов с этого адреса"})
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток входа с этого компьютера. Повторите через несколько минут.",
        )
    # Reserve the attempt slot synchronously, before the slow (bcrypt)
    # authenticate() call — otherwise a burst of concurrent requests for
    # the same username would all pass an is-locked check before any of
    # them registers a failure, defeating the limit for that burst.
    if not rate_limit.check_and_reserve(throttle_key):
        await security_log.record("login_locked", username=throttle_key, ip=ip, ok=False, details={"reason": "много неудачных входов под этим логином"})
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток входа. Повторите через несколько минут.",
        )
    try:
        user, token = await auth_service.authenticate(payload.username, payload.password)
    except auth_service.AuthError as exc:
        rate_limit.ip_register_failure(ip)
        await security_log.record("login_fail", username=throttle_key, ip=ip, ok=False)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    rate_limit.clear(throttle_key)
    _set_session_cookie(request, response, token)
    await security_log.record("login_ok", username=user["username"], role=user["role"], ip=ip)
    full = await build_user(user)
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "role_name": full.role_name,
        "permissions": full.permissions,
        "landing": "/apk" if full.role == "accounting" and P.has(full.permissions, "accounting.view") else P.landing(full.permissions),
        "must_change_password": bool(user["must_change_password"]),
    }


@router.post("/logout")
async def logout(request: Request, response: Response, idle: bool = False):
    settings = get_settings()
    who = await get_session_user(request)
    if who is not None:
        await security_log.record("logout", username=who.username, role=who.role, ip=client_ip(request), details={"reason": "бездействие"} if idle else None)
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: UserOut = Depends(get_current_user)):
    return user


@router.post("/password")
async def change_own_password(
    payload: PasswordChange, request: Request, response: Response, user: UserOut = Depends(get_current_user)
):
    ip = client_ip(request)
    if user.username.strip().lower() in payload.new_password.lower() and len(user.username.strip()) >= 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ненадёжный пароль: пароль не должен содержать логин")
    try:
        await auth_service.change_password(user.id, payload.current_password, payload.new_password)
    except auth_service.AuthError as exc:
        await security_log.record("password_change_fail", username=user.username, role=user.role, ip=ip, ok=False)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await security_log.record("password_change", username=user.username, role=user.role, ip=ip)
    # Changing the password bumps token_version, which would otherwise log
    # this very session out too — mint a fresh token so the user stays in.
    refreshed = await user_repository.get_user_by_id(user.id)
    token = create_access_token(
        user_id=refreshed["id"], username=refreshed["username"], role=refreshed["role"],
        token_version=refreshed["token_version"],
    )
    _set_session_cookie(request, response, token)
    return {"ok": True}
