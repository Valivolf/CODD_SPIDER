"""Состояние работающего приложения в памяти — для страницы «Состояние системы» (app/services/system_health.py): когда запущено, живы ли фоновые задачи, когда в последний раз
отвечал бот, чем кончилась проверка целостности базы. Ничего не хранится на диске: после перезапуска всё собирается заново."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

STARTED_AT = time.time()
_tasks: dict[str, asyncio.Task] = {}
_seen: dict[str, float] = {}
_facts: dict[str, Any] = {}


_oneshot: set[str] = set()


def register_task(name: str, task: asyncio.Task, *, oneshot: bool = False) -> None:
    """oneshot — разовая задача (например, прогрев модели после запуска): штатное завершение — это нормально, тревога только если она упала с ошибкой."""
    _tasks[name] = task
    (_oneshot.add if oneshot else _oneshot.discard)(name)


def tasks_state() -> dict[str, str]:
    """{название задачи: 'ok' | 'остановлена: причина'} — фоновый цикл не должен умирать, но если умер, это видно."""
    out: dict[str, str] = {}
    for name, task in _tasks.items():
        if not task.done():
            out[name] = "ok"
        elif task.cancelled():
            out[name] = "остановлена"
        else:
            exc = task.exception()
            out[name] = f"остановлена: {type(exc).__name__}: {str(exc)[:100]}" if exc else ("ok" if name in _oneshot else "завершилась")
    return out


def touch(name: str) -> None:
    """Отметить, что кто-то снаружи (бот, робот) только что обращался к приложению."""
    _seen[name] = time.time()


def seen_ago(name: str) -> Optional[float]:
    at = _seen.get(name)
    return None if at is None else max(0.0, time.time() - at)


def set_fact(key: str, value: Any) -> None:
    _facts[key] = value


def fact(key: str, default: Any = None) -> Any:
    return _facts.get(key, default)
