"""Допуск на страницы по правам роли: нет сеанса — на вход; нет права на страницу — на первую страницу, где у роли право есть (а если страниц нет совсем — «Нет доступа»)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from app.core import permissions as P
from app.core.deps import get_session_user
from app.models.user import UserOut


async def page_user(request: Request, *keys: str) -> tuple[UserOut | None, Response | None]:
    """(пользователь, None) — страницу показывать; (None/пользователь, ответ-перенаправление) — отдать ответ. Достаточно любого из указанных прав."""
    user = await get_session_user(request)
    if user is None:
        return None, RedirectResponse("/login")
    if keys and not P.has_any(user.permissions, keys):
        dest = P.landing(user.permissions)
        if dest == request.url.path:
            dest = "/no-access"
        return user, RedirectResponse(dest)
    return user, None
