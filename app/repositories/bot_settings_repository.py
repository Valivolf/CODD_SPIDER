"""Single on/off switch for the VK bot — see the comment above the
`bot_settings` table in app/db/database.py for why this exists (two
possible bot processes, only one should ever be live). Single row, id=1.
"""
from __future__ import annotations

from app.db.database import get_connection


async def get_enabled() -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("select enabled from bot_settings where id = 1")
        row = await cur.fetchone()
    return bool(row[0]) if row else True


async def set_enabled(enabled: bool) -> bool:
    async with get_connection() as conn:
        await conn.execute(
            "insert into bot_settings (id, enabled) values (1, ?) "
            "on conflict(id) do update set enabled = excluded.enabled",
            (int(enabled),),
        )
        await conn.commit()
    return enabled
