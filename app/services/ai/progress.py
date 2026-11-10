"""Ход выполнения вопроса помощнику. Режим «агент» (agent.py) читает данные пачками и дописывает ответ — это минуты, и человеку полезно видеть, что происходит:
какую пачку записей модель читает сейчас и что у неё уже написано. Состояние лежит в памяти, пока задача идёт (после перезапуска сервера задача стартует заново
и пишет его снова), страница видит его в переписке (chat.list_messages → job.progress). Номер задачи берётся из контекста (CURRENT_JOB выставляет jobs.spawn)."""
from __future__ import annotations

import contextvars
from typing import Any, Optional

CURRENT_JOB: contextvars.ContextVar[str] = contextvars.ContextVar("ai_current_job", default="")
_STATE: dict[str, dict[str, Any]] = {}
_MAX_KEPT = 200


def report(**fields: Any) -> None:
    """Обновить ход выполнения текущей задачи (вне задачи — ничего не делает)."""
    job_id = CURRENT_JOB.get()
    if not job_id:
        return
    if job_id not in _STATE and len(_STATE) >= _MAX_KEPT:
        _STATE.pop(next(iter(_STATE)), None)
    _STATE.setdefault(job_id, {}).update(fields)


def get(job_id: str) -> Optional[dict[str, Any]]:
    state = _STATE.get(job_id)
    return dict(state) if state else None


def clear(job_id: str) -> None:
    _STATE.pop(job_id, None)
