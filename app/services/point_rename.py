"""Смена номера комплекса и «привязка» ссылок к другому номеру.

Номер комплекса — это и есть его ключ: на него текстом ссылаются задания, отчёты о выездах, фото, история статусов, запросы монтёров, сессии бота, оборудование,
чат, «Живые карты», подгруппы (список — app/db/database.py::_POINT_ID_REFERENCES). Если переименовать комплекс, а ссылки оставить, то всё это «осиротеет»: в «Состоянии
системы» появятся «фото под неизвестным номером», у заданий пропадёт адрес и SIM.

Здесь одна общая процедура для двух случаев:
  • комплекс переименовали в карточке (points_repository.update_point) — ссылки переезжают на новый номер вместе с ним;
  • сироты остались с прошлых переименований — администратор привязывает старый номер к существующему комплексу (relink, кнопка в «Состоянии системы»).

Файлы фото не переносятся и не копируются: в записи сначала закрепляется путь файла на диске (папка прежнего названия остаётся как есть), потом меняется номер комплекса —
как при объединении дублей. Поиск фото на диске узнаёт такую папку по закреплённым путям (photo_index.adopt_known_folders)."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core import map_events, stats_cache
from app.core.work_types import AUX_TYPE_NAME
from app.db.database import _POINT_ID_REFERENCES, get_connection
from app.services import photo_storage

log = logging.getLogger(__name__)

# Куда ещё текстом записан номер комплекса (кроме списка в database.py). point_components и warehouse_items переезжают сами (внешний ключ с каскадом).
_EXTRA_REFERENCES = [("chat_messages", "point_id"), ("point_sim_links", "point_id")]


class RelinkError(Exception):
    """Понятная человеку причина, по которой привязать нельзя."""


async def _table_exists(conn, table: str) -> bool:
    cur = await conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,))
    return await cur.fetchone() is not None


def _swap_id(values: list[Any], old: str, new: str) -> list[Any]:
    out: list[Any] = []
    for v in values:
        v = new if v == old else v
        if v not in out:
            out.append(v)
    return out


async def rename_references_conn(conn, old_id: str, new_id: str) -> dict[str, int]:
    """Переписать все ссылки с номера old_id на new_id внутри уже открытой транзакции (коммит — у вызывающего). Возвращает {таблица: сколько записей}."""
    moved: dict[str, int] = {}
    # фото: путь файла на диске закрепляем (папка прежнего номера остаётся на месте), только потом меняем номер — иначе файл по новому номеру не найти
    cur = await conn.execute("select id, point_id, taken_date, file_ext from visit_photos where point_id = ? and rel_path is null", (old_id,))
    for photo_id, point_id, taken_date, ext in await cur.fetchall():
        rel = photo_storage._rel({"id": photo_id, "point_id": point_id, "taken_date": taken_date, "file_ext": ext})  # noqa: SLF001
        await conn.execute("update visit_photos set rel_path = ? where id = ?", (rel, photo_id))
    for table, col in [*_POINT_ID_REFERENCES, *_EXTRA_REFERENCES]:
        if not await _table_exists(conn, table):
            continue
        if table == "tasks":            # у вспомогательной точки в этом поле её собственная подпись, а не номер комплекса
            cur = await conn.execute("update tasks set point = ? where point = ? and type != ?", (new_id, old_id, AUX_TYPE_NAME))
        else:
            cur = await conn.execute(f"update or ignore {table} set {col} = ? where {col} = ?", (new_id, old_id))
        if cur.rowcount and cur.rowcount > 0:
            moved[table] = moved.get(table, 0) + cur.rowcount
    # «Живые карты»: списки комплексов лежат внутри JSON
    if await _table_exists(conn, "live_maps"):
        needle = f"%{json.dumps(old_id, ensure_ascii=False)}%"
        cur = await conn.execute("select id, params_json, members_json from live_maps where params_json like ? or members_json like ?", (needle, needle))
        for lm_id, params_json, members_json in await cur.fetchall():
            try:
                params = json.loads(params_json) if params_json else {}
                members = json.loads(members_json) if members_json else []
            except ValueError:
                continue
            if isinstance(params.get("point_ids"), list):
                params["point_ids"] = _swap_id(params["point_ids"], old_id, new_id)
            if isinstance(members, list):
                members = _swap_id(members, old_id, new_id)
            await conn.execute("update live_maps set params_json = ?, members_json = ? where id = ?",
                               (json.dumps(params, ensure_ascii=False), json.dumps(members, ensure_ascii=False) if members_json else "", lm_id))
            moved["live_maps"] = moved.get("live_maps", 0) + 1
    return moved


async def relink(old_id: str, new_id: str) -> dict[str, Any]:
    """Привязать «осиротевшие» ссылки с номера old_id (такого комплекса в справочнике нет) к существующему комплексу new_id."""
    old_id, new_id = (old_id or "").strip(), (new_id or "").strip()
    if not old_id or not new_id:
        raise RelinkError("Укажите прежний номер и комплекс, к которому его привязать")
    if old_id == new_id:
        raise RelinkError("Номера совпадают")
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from points where id = ?", (new_id,))
        if not await cur.fetchone():
            raise RelinkError(f"Комплекса «{new_id}» нет в справочнике")
        cur = await conn.execute("select 1 from points where id = ?", (old_id,))
        if await cur.fetchone():
            raise RelinkError(f"«{old_id}» — действующий комплекс, его ссылки не трогаю (для дублей есть «Объединение дублей»)")
        moved = await rename_references_conn(conn, old_id, new_id)
        await conn.commit()
    stats_cache.invalidate()
    map_events.kick()
    log.warning("Ссылки с номера %r привязаны к комплексу %r: %s", old_id, new_id, moved)
    return {"old": old_id, "new": new_id, "moved": moved, "total": sum(moved.values())}
