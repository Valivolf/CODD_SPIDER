"""Tracks "a fitter is actively on this task right now" — started/ended by
the VK bot (see routes_bot.py), read by the /monitoring page's "Сейчас в
работе" section (see app/api/routes_monitoring.py).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection

_COLUMNS = "id, task_id, point_id, person_id, started_at, ended_at"
_KEYS = ["id", "task_id", "point_id", "person_id", "started_at", "ended_at"]


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


# Сессия, начатая давно и так и не закрытая (бот перезапустили, монтёр закрыл чат), в «кто сейчас на комплексе»
# не считается — иначе второй монтёр видел бы «в работе: Иванов» по вчерашней записи.
STALE_AFTER_HOURS = 16


def _fresh_cutoff() -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=STALE_AFTER_HOURS)).isoformat()


async def start(task_id: str, point_id: str, person_id: int) -> dict[str, Any]:
    """Монтёр взял комплекс в работу. Повторное «Начать/Продолжить» тем же человеком не плодит строки: возвращается
    его открытая сессия по этому заданию (`resumed` = True). Любые ДРУГИЕ его открытые сессии закрываются —
    человек работает на одном комплексе за раз (раньше такие записи висели в «Сейчас в работе» до ручной очистки)."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "update bot_work_sessions set ended_at = ? where person_id = ? and ended_at is null and task_id != ?",
            (now, person_id, task_id),
        )
        cur = await conn.execute(
            f"select {_COLUMNS} from bot_work_sessions where task_id = ? and person_id = ? and ended_at is null "
            "order by id desc limit 1",
            (task_id, person_id),
        )
        row = await cur.fetchone()
        if row:
            await conn.commit()
            return {**_row_to_dict(row), "resumed": True}
        cur = await conn.execute(
            "insert into bot_work_sessions (task_id, point_id, person_id, started_at) values (?, ?, ?, ?)",
            (task_id, point_id, person_id, now),
        )
        await conn.commit()
        session_id = cur.lastrowid
        cur = await conn.execute(f"select {_COLUMNS} from bot_work_sessions where id = ?", (session_id,))
        row = await cur.fetchone()
    return {**_row_to_dict(row), "resumed": False}  # type: ignore[arg-type]


async def end_for_task(task_id: str) -> int:
    """Закрывает все открытые сессии по заданию (отчёт сдан / диспетчер отпустил бригаду) — у обоих монтёров."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "update bot_work_sessions set ended_at = ? where task_id = ? and ended_at is null", (now, task_id)
        )
        await conn.commit()
        return cur.rowcount


async def active_for_tasks(task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """task_id → кто сейчас на комплексе (открытые свежие сессии): [{person_id, name, started_at}]."""
    result: dict[str, list[dict[str, Any]]] = {}
    ids = [i for i in task_ids if i]
    cutoff = _fresh_cutoff()
    async with get_connection() as conn:
        for start_at in range(0, len(ids), 500):
            chunk = ids[start_at:start_at + 500]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                "select s.task_id, s.person_id, pe.full_name, s.started_at from bot_work_sessions s "
                f"left join people pe on pe.id = s.person_id where s.ended_at is null and s.started_at >= ? "
                f"and s.task_id in ({marks}) order by s.started_at",
                (cutoff, *chunk),
            )
            for r in await cur.fetchall():
                result.setdefault(r[0], []).append({"person_id": r[1], "name": r[2] or "—", "started_at": r[3]})
    return result


async def latest_active_for_person(person_id: int) -> Optional[dict[str, Any]]:
    """Последняя открытая свежая сессия человека — по ней бот после перезапуска возвращает монтёра на его комплекс."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from bot_work_sessions where person_id = ? and ended_at is null and started_at >= ? "
            "order by id desc limit 1",
            (person_id, _fresh_cutoff()),
        )
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def end(session_id: int) -> Optional[dict[str, Any]]:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "update bot_work_sessions set ended_at = ? where id = ? and ended_at is null", (now, session_id)
        )
        await conn.commit()
        cur = await conn.execute(f"select {_COLUMNS} from bot_work_sessions where id = ?", (session_id,))
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def get(session_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from bot_work_sessions where id = ?", (session_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_active() -> list[dict[str, Any]]:
    """Active sessions joined with point address and the fitter's name —
    everything the "Сейчас в работе" list needs in one query."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select s.id, s.task_id, s.point_id, s.started_at,
                   p.address, pe.full_name
            from bot_work_sessions s
            left join points p on p.id = s.point_id
            left join people pe on pe.id = s.person_id
            where s.ended_at is null
            order by s.started_at
            """
        )
        rows = await cur.fetchall()
    keys = ["id", "task_id", "point_id", "started_at", "point_address", "person_name"]
    return [dict(zip(keys, r)) for r in rows]
