"""Клиент робота Яндекс.Конструктора карт (сервис yandex-robot, см. robot/).

Робот живёт в отдельном контейнере (профиль compose «robot») и может быть не запущен —
приложение от этого не ломается: все вызовы превращают недоступность в понятную ошибку.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class RobotUnavailable(Exception):
    """Робот не запущен / не отвечает."""


class RobotRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_UNAVAILABLE_HINT = (
    "Робот карт не запущен. На сервере запустите его командой "
    "«docker compose --profile robot up -d --build yandex-robot» (папка docker)."
)

# Одно постоянное соединение с роботом (keep-alive) вместо нового на каждый запрос — окно входа
# опрашивает робота часто, а фоновый цикл ходит к нему постоянно.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=get_settings().yandex_robot_url, limits=httpx.Limits(max_keepalive_connections=4))
    return _client


async def call(
    method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None, timeout: float = 25.0, raw: bool = False
) -> Any:
    settings = get_settings()
    try:
        for attempt in (1, 2):
            try:
                resp = await _get_client().request(
                    method, path, json=json, params=params, timeout=timeout, headers={"X-Robot-Key": settings.yandex_robot_api_key}
                )
                break
            except (httpx.RemoteProtocolError, httpx.ReadError):
                # Старое keep-alive соединение закрыто (робот перезапускался) — один повтор на новом.
                if attempt == 2:
                    raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol):
        raise RobotUnavailable(_UNAVAILABLE_HINT)
    except httpx.TimeoutException:
        raise RobotUnavailable("Робот не ответил вовремя (браузер ещё запускается или завис) — повторите через минуту.")
    except httpx.HTTPError as exc:
        raise RobotUnavailable(f"Нет связи с роботом карт: {type(exc).__name__}")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", "")
        except ValueError:
            detail = resp.text[:200]
        raise RobotRejected(resp.status_code, str(detail) or f"Робот ответил {resp.status_code}")
    if raw:
        return resp
    return resp.json()
