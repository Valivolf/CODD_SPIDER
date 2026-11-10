"""Короткий кэш сводки страницы «Статистика».

Сводка считается по истории статусов и заданиям — на больших базах это сотни миллисекунд, а на «Весь период» секунды, а
страница перезапрашивает её по таймеру из каждой открытой вкладки. Кэш делает две вещи:
  • в пределах TTL повторные запросы (от любого числа вкладок и пользователей) получают готовый ответ;
  • одинаковые одновременные запросы ждут одного вычисления, а не запускают каждый своё.
Любая запись, которая меняет статистику (смена статуса комплекса, отчёт, задания, карточка комплекса), вызывает
`invalidate()` — следующий запрос посчитает заново, так что данные не «залипают». TTL — страховка на случай записи в обход
этих мест (импорт, прямой SQL). STATS_CACHE_SECONDS=0 отключает кэш."""
from __future__ import annotations

import asyncio
import functools
import os
import time
from typing import Any, Awaitable, Callable, Hashable

TTL_SECONDS = float(os.environ.get("STATS_CACHE_SECONDS", "15"))
_MAX_ENTRIES = 32

_version = 0
_entries: dict[Hashable, tuple[int, float, Any]] = {}      # key -> (версия данных, годен до, значение)
_locks: dict[Hashable, asyncio.Lock] = {}


def invalidate() -> None:
    """Данные, от которых зависит статистика, изменились — готовые ответы больше не годятся."""
    global _version
    _version += 1
    _entries.clear()


def _valid(entry: tuple[int, float, Any] | None) -> bool:
    return entry is not None and entry[0] == _version and entry[1] > time.monotonic()


async def get_or_compute(key: Hashable, compute: Callable[[], Awaitable[Any]]) -> Any:
    if TTL_SECONDS <= 0:
        return await compute()
    entry = _entries.get(key)
    if _valid(entry):
        return entry[2]  # type: ignore[index]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        entry = _entries.get(key)
        if _valid(entry):
            return entry[2]  # type: ignore[index]
        version = _version
        value = await compute()
        if len(_entries) >= _MAX_ENTRIES:
            for stale in [k for k, e in _entries.items() if not _valid(e)] or [next(iter(_entries))]:
                _entries.pop(stale, None)
        _entries[key] = (version, time.monotonic() + TTL_SECONDS, value)
        if len(_locks) > 4 * _MAX_ENTRIES:
            _locks.clear()
        return value


def invalidates(func):
    """Декоратор для асинхронных функций записи: после выполнения (в том числе неудачного) сбрасывает кэш сводки."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        finally:
            invalidate()

    return wrapper
