"""FastAPI dependencies for authentication / authorization."""
from __future__ import annotations

import hmac

import jwt
from fastapi import Depends, Header, HTTPException, Request, status

from app.config import get_settings
from app.core import permissions as P
from app.core.clientip import client_ip
from app.core.security import decode_access_token
from app.models.user import UserOut
from app.repositories import roles_repository, user_repository
from app.services import security_log


async def get_session_user(request: Request) -> UserOut | None:
    """Full session validation (signature + user exists + token_version
    still current) without raising — used where a plain yes/no is enough,
    e.g. deciding whether to serve a page or redirect to /login. API routes
    should use get_current_user below instead, which raises with a proper
    status code."""
    cached = getattr(request.state, "session_user", _UNSET)
    if cached is not _UNSET:
        return cached
    request.state.session_user = result = await _resolve_session_user(request)
    return result


_UNSET = object()


async def _resolve_session_user(request: Request) -> UserOut | None:
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return None
    user = await user_repository.get_user_by_id(int(payload["sub"]))
    if user is None:
        return None
    if payload.get("tv") != user["token_version"]:
        # Password was changed/reset since this token was issued — a stolen
        # or logged-out cookie must not keep working past that point.
        return None
    return await build_user(user)


async def build_user(user: dict) -> UserOut:
    """Пользователь вместе с правами его роли (права читаются из памяти; роль удалена или неизвестна — прав нет, доступа нет)."""
    await roles_repository.ensure_loaded()
    return UserOut(
        id=user["id"],
        username=user["username"],
        role=user["role"],
        role_name=roles_repository.role_name(user["role"]),
        must_change_password=user["must_change_password"],
        created_at=user["created_at"],
        permissions=sorted(roles_repository.perms_for_role(user["role"])),
    )


async def get_current_user(request: Request) -> UserOut:
    user = await get_session_user(request)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия недействительна, войдите снова")
    return user


async def require_password_ok(user: UserOut = Depends(get_current_user)) -> UserOut:
    """Blocks everything except the auth endpoints themselves until a
    forced (default-admin) password change has actually happened —
    must_change_password was previously enforced client-side only.
    Какие адреса доступны роли, решают её права: app/core/perm_guard.py (на каждый запрос) и require_perm ниже."""
    if user.must_change_password:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Сначала необходимо сменить пароль")
    return user


async def _denied(request: Request, user: UserOut, reason: str) -> None:
    """Отказ в доступе — в журнал безопасности (не бросает исключений)."""
    try:
        await security_log.record_denied(username=user.username, role=user.role, ip=client_ip(request), method=request.method, path=request.url.path, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def require_perm(*keys: str):
    """Зависимость «нужно любое из указанных прав» (у администратора есть всё). Основная проверка — на каждый запрос в app/core/perm_guard.py по таблице адресов; эта — вторая линия
    и единственная для служебных вызовов, где от права зависит состав ответа."""
    need = tuple(keys)

    async def dependency(request: Request, user: UserOut = Depends(require_password_ok)) -> UserOut:
        if not P.has_any(user.permissions, need):
            await _denied(request, user, "нет права: " + P.need_text(need))
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: " + P.need_text(need))
        return user

    dependency.__name__ = "require_perm_" + "_or_".join(k.replace(".", "_") for k in need)
    return dependency


# Готовые зависимости для разделов (имя — для матрицы доступа и читаемости маршрутов)
require_admin = require_perm("admin.users")             # управление пользователями и ролями
require_warehouse = require_perm("warehouse.view")      # склад: чтение (операции — warehouse.edit ниже и в таблице адресов)
require_warehouse_edit = require_perm("warehouse.edit")


async def require_agent(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """ИИ-агент на компьютере сотрудника (app/services/ai/agents.py) — такой же служебный вызывающий, как бот: сеанса и пользователя нет, вместо них токен агента
    (Authorization: Bearer …), который сервер выдаёт при подключении по ключу. Неизвестный токен (агент удалён из списка) — 401: агент понимает, что нужно подключаться заново."""
    from app.services.ai import agents  # noqa: PLC0415

    token = (authorization or "")[7:].strip() if (authorization or "").lower().startswith("bearer ") else ""
    agent = await agents.authenticate(token, client_ip(request))
    if not agent:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Компьютер неизвестен серверу: отвязан от списка или неверный токен")
    # токен привязан к железу компьютера: скопированный на другой компьютер файл настроек не работает (сравниваются отпечатки оборудования, адрес сети не важен)
    hw = (request.headers.get("x-agent-hw") or "").strip().lower()
    if agent["hw_id"]:
        if not hmac.compare_digest(hw, agent["hw_id"]):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Этот токен выдан другому компьютеру (не совпадает оборудование). Подключите этот компьютер новым ключом")
    elif hw:
        await agents.bind_hardware(agent["id"], hw)
    return agent


async def require_bot_key(x_bot_key: str | None = Header(default=None)) -> None:
    """The монтёр bot has no lan_calendar user/session — it's a single
    trusted server-to-server caller authenticated by one shared header
    instead (see app/config.py's bot_api_key)."""
    settings = get_settings()
    if not x_bot_key or not hmac.compare_digest(x_bot_key, settings.bot_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный или отсутствующий X-Bot-Key")
require_settings = require_perm("admin.settings")      # ИИ, погода, Яндекс-аккаунт и Формы, сетевой диск с фото
require_backup = require_perm("admin.backup")
require_keys = require_perm("admin.keys")
require_security_log = require_perm("admin.security")
require_system = require_perm("admin.system")
