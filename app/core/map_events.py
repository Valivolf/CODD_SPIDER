"""Сигнал фоновому циклу карт: «что-то изменилось — проверь карты сейчас».

Карты бригад (app/services/yandex_map_sync.py) раньше узнавали об изменениях только по таймеру
(раз в полминуты). Теперь любое действие, которое может изменить карту (сохранили календарь,
сдан отчёт, добавили задания), будит цикл сразу: изменение «устаивается» несколько секунд — и карта
правится, а не через минуту-две.

Модуль без зависимостей от остального приложения, чтобы его мог вызвать любой репозиторий.
"""
from __future__ import annotations

import asyncio

_event: asyncio.Event | None = None


def _get() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def kick() -> None:
    """Разбудить цикл карт (безопасно вызывать откуда угодно; без запущенного цикла ничего не делает)."""
    try:
        _get().set()
    except RuntimeError:  # нет работающего цикла событий (скрипты, тесты)
        pass


async def wait(timeout: float) -> bool:
    """Ждать сигнала не дольше `timeout` секунд. True — разбудили сигналом, False — вышло время."""
    event = _get()
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except asyncio.TimeoutError:
        return False
    event.clear()
    return True
