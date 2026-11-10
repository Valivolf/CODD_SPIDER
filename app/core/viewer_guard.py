"""Защита от подделки запросов с чужих сайтов (CSRF, проверка Origin). Права ролей на адреса проверяет app/core/perm_guard.py (раньше здесь была проверка роли «просмотр»)."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.clientip import client_ip
from app.core.deps import get_session_user
from app.services import security_log

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_allowed(request: Request, origin: str) -> bool:
    """Запрос на изменение пришёл со страницы этого же сайта? Браузер сам ставит Origin; у запросов бота и скриптов его нет. Сравниваем с заголовком Host (Caddy передаёт его без изменений);
    за промежуточным прокси, подменяющим Host, действует X-Forwarded-Host (только от внутреннего прокси) и список TRUSTED_ORIGINS в .env."""
    if origin == "null":
        return False
    netloc = urlsplit(origin).netloc.lower()
    if not netloc:
        return False
    if netloc == (request.headers.get("host") or "").lower():
        return True
    peer = request.client.host if request.client else ""
    forwarded = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()
    if forwarded and forwarded == netloc and peer and (peer.startswith("127.") or peer == "::1" or peer.startswith(("10.", "192.168.", "172."))):
        return True
    extra = {urlsplit(x.strip()).netloc.lower() or x.strip().lower() for x in os.environ.get("TRUSTED_ORIGINS", "").split(",") if x.strip()}
    return netloc in extra


async def origin_guard(request: Request, call_next):
    """Защита от подделки запросов с чужих сайтов (CSRF): изменяющий запрос к /api/*, у которого браузер указал чужой Origin, отклоняется. Основная защита — cookie SameSite=Lax;
    это вторая линия. Выключается ORIGIN_CHECK=false в .env."""
    path = request.url.path
    if path.startswith("/api/") and request.method not in _SAFE_METHODS and os.environ.get("ORIGIN_CHECK", "true").lower() not in ("0", "false", "no", "off"):
        origin = request.headers.get("origin")
        if origin is not None and not _origin_allowed(request, origin):
            try:
                user = await get_session_user(request)
                await security_log.record_denied(username=user.username if user else "", role=user.role if user else "", ip=client_ip(request), method=request.method, path=path,
                                                 reason=f"чужой источник запроса (Origin {origin[:80]})")
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({"detail": "Запрос отклонён: он отправлен со страницы другого сайта. Откройте систему по её адресу; если заходите через прокси — администратору нужно указать адрес в TRUSTED_ORIGINS."}, status_code=403)
    return await call_next(request)
