"""Проверка прав на каждый запрос к адресам /api/* (и печати план-наряда): какое право нужно адресу, определяет таблица app/core/permissions.py::RULES; нет права — 403 до выполнения действия
(и запись «отказ в доступе» в журнал безопасности). Служебные вызовы бота (/api/bot/*, по ключу) и ИИ-агентов (/api/agent/*, по токену агента) сюда не относятся — у них нет сеанса.

Это граница безопасности: скрытие кнопок в интерфейсе — удобство, а проверка права на сервере — обязательна и не зависит от интерфейса. Встроенные роли (администратор, оператор, склад,
просмотр, отсутствия) подчиняются тем же правилам через свои наборы прав."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import permissions as P
from app.core import accounting
from app.core.clientip import client_ip
from app.core.deps import get_session_user
from app.services import security_log


def _guarded(path: str) -> bool:
    return (path.startswith("/api/") and not path.startswith(("/api/bot/", "/api/agent/"))) or path.startswith("/plan-print")


async def permission_guard(request: Request, call_next):
    path = request.url.path
    if _guarded(path):
        user = await get_session_user(request)
        if user is not None and not user.must_change_password:
            ok, need, _note = P.allowed(request.method, path, user.permissions)
            if accounting.is_accountant(user):
                if path == "/api/monitoring/photos/upload" and request.method == "POST":
                    ok, need = accounting.can_edit(user), ("accounting.edit",)
                if not accounting.apk_request_allowed(request.method, path):
                    ok, need = False, ("points.edit",)
            if not ok:
                if request.method not in ("GET", "HEAD", "OPTIONS") or P.CORE not in need:
                    try:
                        await security_log.record_denied(username=user.username, role=user.role, ip=client_ip(request), method=request.method, path=path,
                                                         reason="нет права: " + P.need_text(need))
                    except Exception:  # noqa: BLE001
                        pass
                return JSONResponse({"detail": "Нет права: " + P.need_text(need)}, status_code=403)
    return await call_next(request)
