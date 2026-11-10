"""Standalone «какая SIM сейчас на каком IP» lookup — independent of which
point (if any) currently has that SIM plugged in. Populated by the Excel
import in routes_excel.py; consulted whenever a point's sim_number is set
(bulk import, or the bot's set_point_sim) so its ip_address can be filled
in automatically. sim_number is always the normalized last-7-digits form,
same convention as points.sim_number.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection


async def get_ip_for_sim(sim_number: str) -> Optional[str]:
    if not sim_number:
        return None
    async with get_connection() as conn:
        cur = await conn.execute("select ip_address from sim_ip_registry where sim_number = ?", (sim_number,))
        row = await cur.fetchone()
    return row[0] if row else None


async def get_all_map() -> dict[str, str]:
    """Весь реестр SIM → IP одним запросом: фоновая проверка доступности (point_health_service) на круг по тысячам комплексов берёт его один раз, а не открывает соединение с базой на каждую точку."""
    async with get_connection() as conn:
        cur = await conn.execute("select sim_number, ip_address from sim_ip_registry")
        rows = await cur.fetchall()
    return {sim: ip for sim, ip in rows if sim and ip}


async def upsert_many(pairs: list[tuple[str, str]]) -> int:
    """pairs: [(sim_number, ip_address), ...], both already normalized.
    Returns how many rows were written (inserted or updated)."""
    if not pairs:
        return 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        for sim_number, ip_address in pairs:
            await conn.execute(
                "insert into sim_ip_registry (sim_number, ip_address, updated_at) values (?, ?, ?) "
                "on conflict(sim_number) do update set ip_address = excluded.ip_address, updated_at = excluded.updated_at",
                (sim_number, ip_address, now),
            )
        await conn.commit()
    return len(pairs)


async def list_all() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select sim_number, ip_address, updated_at from sim_ip_registry order by updated_at desc")
        rows = await cur.fetchall()
    return [{"sim_number": r[0], "ip_address": r[1], "updated_at": r[2]} for r in rows]
