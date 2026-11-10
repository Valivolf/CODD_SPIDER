"""«Живые карты» — именованные карты в Яндекс.Конструкторе, которые приложение само обновляет по таймеру
(раз в 5 минут и т. п.): цвет и описание точек меняются вслед за статусами комплексов.

Что на карте, определяет условие (kind + params): «есть доступ», «доступ и действующая поверка», свой
список комплексов, «отвалились за период» или фильтр со страницы «Все АПК». Состав либо фиксируется при
создании (меняются только цвета и статусы — видно, кто из выбранных потерял доступ), либо пересчитывается
по условию на каждом обновлении. Что именно лежит на карте сейчас (rows_json) и её ссылка хранятся здесь;
саму работу в Конструкторе делает робот (см. app/services/live_maps.py).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.db.database import get_connection

_COLUMNS = (
    "id, name, kind, params_json, membership, members_json, interval_minutes, enabled, map_id, url, "
    "rows_json, signature, synced_name, points_count, skipped, last_check_ts, last_sync, last_error, "
    "created_by, created_at, updated_at"
)
_KEYS = [c.strip() for c in _COLUMNS.split(",")]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json(value: str, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _row(r) -> dict[str, Any]:
    d = dict(zip(_KEYS, r))
    d["params"] = _json(d.pop("params_json"), {})
    d["members"] = _json(d.pop("members_json"), [])
    rows_raw = d.pop("rows_json")
    d["rows"] = _json(rows_raw, None) if d["map_id"] else None
    d["enabled"] = bool(d["enabled"])
    return d


async def list_all() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from live_maps order by id")
        return [_row(r) for r in await cur.fetchall()]


async def get(map_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from live_maps where id = ?", (map_id,))
        row = await cur.fetchone()
    return _row(row) if row else None


async def count() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from live_maps")
        (n,) = await cur.fetchone()
    return n


async def create(
    *, name: str, kind: str, params: dict, membership: str, members: list[str], interval_minutes: int, created_by: str | None
) -> dict[str, Any]:
    now = _now()
    async with get_connection() as conn:
        cur = await conn.execute(
            """insert into live_maps (name, kind, params_json, membership, members_json, interval_minutes, enabled,
                   map_id, url, rows_json, signature, synced_name, points_count, skipped, last_check_ts, last_sync,
                   last_error, created_by, created_at, updated_at)
               values (?, ?, ?, ?, ?, ?, 1, '', '', '', '', '', 0, 0, 0, '', '', ?, ?, ?)""",
            (
                name, kind, json.dumps(params, ensure_ascii=False), membership,
                json.dumps(members, ensure_ascii=False), interval_minutes, created_by, now, now,
            ),
        )
        await conn.commit()
        new_id = cur.lastrowid
    return await get(new_id)  # type: ignore[arg-type,return-value]


async def update_fields(row_id: int, /, **fields: Any) -> None:
    """Точечная правка колонок (названия — как в таблице; params/members/rows — Python-объектами)."""
    if not fields:
        return
    mapping = {"params": "params_json", "members": "members_json", "rows": "rows_json"}
    sets, values = [], []
    for key, value in fields.items():
        column = mapping.get(key, key)
        if key in mapping:
            value = "" if value is None else json.dumps(value, ensure_ascii=False)
        elif key == "enabled":
            value = int(bool(value))
        sets.append(f"{column} = ?")
        values.append(value)
    sets.append("updated_at = ?")
    values.append(_now())
    values.append(row_id)
    async with get_connection() as conn:
        await conn.execute(f"update live_maps set {', '.join(sets)} where id = ?", values)
        await conn.commit()


async def delete(map_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from live_maps where id = ?", (map_id,))
        await conn.commit()
