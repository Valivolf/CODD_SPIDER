"""Постраничные экраны бота: у каждого есть имя и функция отрисовки страницы.

Экран запоминает своё имя и страницу в сессии; кнопки ◀ ▶ (action "pg") просто перерисовывают
текущий экран на другой странице — без отдельного обработчика на каждый список."""
from __future__ import annotations

from typing import Awaitable, Callable

Renderer = Callable[[int, dict, dict, int], Awaitable[None]]  # (peer_id, user, session, page)

_VIEWS: dict[str, Renderer] = {}


def register(name: str):
    def decorator(func: Renderer) -> Renderer:
        _VIEWS[name] = func
        return func

    return decorator


async def show(name: str, peer_id: int, user: dict, session: dict, page: int = 0) -> None:
    session["view"] = name
    session["view_page"] = page
    await _VIEWS[name](peer_id, user, session, page)


async def turn_page(peer_id: int, user: dict, session: dict, page: int) -> None:
    name = session.get("view")
    if name in _VIEWS:
        await show(name, peer_id, user, session, page)
