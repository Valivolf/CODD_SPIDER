"""Учёт карт, которые робот создал в Яндекс.Конструкторе, и настройки робота.

yandex_maps — по одной записи на «владельца» карты (бригаду или командировку): что лежит на карте
(rows_json — строки, по которым считается, что изменилось), её идентификатор в Конструкторе и дата
выезда (по ней работает автоудаление). yandex_trash — карты, которые нужно удалить из Конструктора.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.db.database import get_connection

DEFAULT_SETTINGS: dict[str, Any] = {
    "auto_sync": True,          # создавать и обновлять карты сами, когда меняются точки
    "sync_days_ahead": 2,       # на сколько дней вперёд (считая сегодня) вести карты
    "auto_delete": False,       # удалять старые карты
    "auto_delete_days": 7,      # …через столько дней после даты выезда
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _coerce(key: str, raw: str) -> Any:
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return raw == "1"
    try:
        return int(raw)
    except ValueError:
        return default


async def get_settings() -> dict[str, Any]:
    async with get_connection() as conn:
        cur = await conn.execute("select key, value from yandex_robot_settings")
        stored = {k: v for k, v in await cur.fetchall()}
    return {key: (_coerce(key, stored[key]) if key in stored else default) for key, default in DEFAULT_SETTINGS.items()}


async def set_settings(values: dict[str, Any]) -> dict[str, Any]:
    async with get_connection() as conn:
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                continue
            stored = ("1" if value else "0") if isinstance(DEFAULT_SETTINGS[key], bool) else str(int(value))
            await conn.execute(
                "insert into yandex_robot_settings (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                (key, stored),
            )
        await conn.commit()
    return await get_settings()


_COLUMNS = "owner_key, crew_id, map_id, url, name, rows_json, signature, work_date, created_at, updated_at"
_KEYS = _COLUMNS.split(", ")


def _row(r) -> dict[str, Any]:
    d = dict(zip(_KEYS, r))
    try:
        d["rows"] = json.loads(d["rows_json"]) if d["rows_json"] else None
    except ValueError:
        d["rows"] = None
    return d


async def get_map(owner_key: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from yandex_maps where owner_key = ?", (owner_key,))
        row = await cur.fetchone()
    return _row(row) if row else None


async def list_maps() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from yandex_maps order by work_date, owner_key")
        return [_row(r) for r in await cur.fetchall()]


async def upsert_map(
    owner_key: str, *, crew_id: int, map_id: str, url: str, name: str, rows: Optional[list], signature: str, work_date: str
) -> None:
    now = _now()
    async with get_connection() as conn:
        await conn.execute(
            """insert into yandex_maps (owner_key, crew_id, map_id, url, name, rows_json, signature, work_date, created_at, updated_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               on conflict(owner_key) do update set
                 crew_id = excluded.crew_id, map_id = excluded.map_id, url = excluded.url, name = excluded.name,
                 rows_json = excluded.rows_json, signature = excluded.signature, work_date = excluded.work_date,
                 updated_at = excluded.updated_at""",
            (owner_key, crew_id, map_id, url, name, json.dumps(rows, ensure_ascii=False) if rows is not None else "", signature, work_date, now, now),
        )
        await conn.commit()


async def delete_map_row(owner_key: str) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from yandex_maps where owner_key = ?", (owner_key,))
        await conn.commit()


async def trash_add(map_id: str, reason: str = "") -> None:
    async with get_connection() as conn:
        await conn.execute(
            "insert or ignore into yandex_trash (map_id, reason, created_at) values (?, ?, ?)", (map_id, reason, _now())
        )
        await conn.commit()


async def trash_list() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select map_id, reason, created_at from yandex_trash order by created_at")
        return [{"map_id": r[0], "reason": r[1], "created_at": r[2]} for r in await cur.fetchall()]


async def trash_remove(map_id: str) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from yandex_trash where map_id = ?", (map_id,))
        await conn.commit()
