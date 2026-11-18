"""Сквозные защитные меры для ответов API.

1. Запрет кэширования ответов /api/*: в них пароли комплексов (для администратора/оператора), ФИО, телефоны — они не должны оставаться в кэше браузера или промежуточного прокси.
   Ответы, у которых приложение само задало Cache-Control или ETag (список комплексов с проверкой «не изменилось»), не трогаются.
2. Журнал безопасности: изменения настроек и справочников, данных персонала и комплексов (метод, адрес, код ответа — без тела запроса) пишутся событием admin_action."""
from __future__ import annotations

from starlette.requests import Request

from app.core.clientip import client_ip
from app.core.deps import get_session_user
from app.services import security_log

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Адреса, изменения которых попадают в журнал безопасности. Вход/выход, пользователи, копия базы и ключ шифрования пишутся своими событиями в самих маршрутах.
AUDITED_PREFIXES = (
    "/api/point-category-fields", "/api/point-files", "/api/monitoring/photos/upload",
    "/api/ai/settings", "/api/ai-agents", "/api/photo-storage", "/api/forms-auto", "/api/weather/settings", "/api/yandex-robot", "/api/bot-settings",
    "/api/people", "/api/points", "/api/point-categories", "/api/point-statuses", "/api/work-types",
)


async def audit_and_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and "cache-control" not in response.headers and "etag" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    if request.method in _MUTATING and path.startswith(AUDITED_PREFIXES) and "/view/" not in path:
        try:
            user = await get_session_user(request)
            if user is not None:
                await security_log.record("admin_action", username=user.username, role=user.role, ip=client_ip(request), ok=response.status_code < 400,
                                          details={"method": request.method, "path": path[:160], "status": response.status_code})
        except Exception:  # noqa: BLE001 — журнал не должен ломать ответ
            pass
    return response
