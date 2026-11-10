"""Данные учёта оборудования по серийным номерам: единицы, справочники (категории, модели,
состояния), журнал. Операции (приёмка, выдача, установка…) — в app/services/equipment_service.py;
здесь только чтение/запись таблиц. Все функции с суффиксом _conn работают на уже открытом
соединении, чтобы сервис мог собрать из них одну транзакцию.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Optional

from app.core import equipment as eq
from app.db.database import get_connection

_ITEM_COLUMNS = [
    ("id", "e.id"), ("serial_number", "e.serial_number"), ("model_id", "e.model_id"),
    ("model_name", "m.name"), ("category_id", "c.id"), ("category_name", "c.name"),
    ("place", "e.place"), ("status_id", "e.status_id"), ("status", "s.name"), ("status_color", "s.color"),
    ("status_usable", "s.usable"), ("status_role", "s.role"),
    ("point_id", "e.point_id"), ("point_address", "pt.address"), ("point_inventory", "pt.inventory_number"),
    ("person_id", "e.person_id"), ("person_name", "pe.full_name"), ("held_reason", "e.held_reason"),
    ("received_date", "e.received_date"), ("issued_date", "e.issued_date"),
    ("installed_date", "e.installed_date"), ("installed_by", "e.installed_by"),
    ("removed_date", "e.removed_date"), ("removed_by", "e.removed_by"), ("removed_point_id", "e.removed_point_id"),
    ("accepted_date", "e.accepted_date"), ("accepted_by", "e.accepted_by"),
    ("notes", "e.notes"), ("created_at", "e.created_at"), ("updated_at", "e.updated_at"),
    ("category_kind", "c.kind"), ("removal_reason", "e.removal_reason"),
]
ITEM_KEYS = [k for k, _ in _ITEM_COLUMNS]
ITEM_SELECT = (
    "select " + ", ".join(f"{expr} as {key}" for key, expr in _ITEM_COLUMNS) + """
    from equipment e
    join equipment_models m on m.id = e.model_id
    join equipment_categories c on c.id = m.category_id
    left join equipment_statuses s on s.id = e.status_id
    left join points pt on pt.id = e.point_id
    left join people pe on pe.id = e.person_id
"""
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def today() -> str:
    return dt.date.today().isoformat()


def _item(row) -> dict[str, Any]:
    d = dict(zip(ITEM_KEYS, row))
    d["status_usable"] = bool(d["status_usable"])
    d["place_label"] = eq.PLACE_LABEL.get(d["place"], d["place"])
    # Снято монтёром с комплекса и ждёт приёмки на склад.
    d["awaiting_accept"] = d["place"] == eq.PLACE_PERSON and d["held_reason"] == eq.HELD_REMOVED
    # Снятая SIM-карта осталась у монтёра: приёмка на склад не обязательна, он может поставить её снова.
    d["kept"] = d["place"] == eq.PLACE_PERSON and d["held_reason"] == eq.HELD_KEPT
    return d


def _filters(
    *, search: str = "", category_id: Optional[int] = None, model_id: Optional[int] = None,
    place: str = "", status_id: Optional[int] = None, point_id: str = "", person_id: Optional[int] = None,
    held_reason: str = "", usable: Optional[bool] = None, date_from: str = "", date_to: str = "",
    ids: Optional[Iterable[int]] = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if search:
        s = f"%{search.strip()}%"
        where.append(
            "(pylower(e.serial_number) like pylower(?) or pylower(m.name) like pylower(?) "
            "or pylower(c.name) like pylower(?) or pylower(coalesce(e.point_id, '')) like pylower(?) "
            "or pylower(coalesce(pe.full_name, '')) like pylower(?) or pylower(coalesce(e.notes, '')) like pylower(?) "
            "or pylower(coalesce(e.removal_reason, '')) like pylower(?) "
            "or e.id in (select equipment_id from equipment_field_values where pylower(value) like pylower(?)) "
            "or (c.kind = 'sim' and e.serial_number = ?))"
        )
        params.extend([s] * 8)
        # Полный номер SIM (11–20 цифр) находит карту по последним 7 цифрам.
        params.append(eq.sim_key(search) if len("".join(ch for ch in search if ch.isdigit())) > 7 else "")
    if category_id:
        where.append("c.id = ?")
        params.append(category_id)
    if model_id:
        where.append("m.id = ?")
        params.append(model_id)
    if place:
        where.append("e.place = ?")
        params.append(place)
    if status_id:
        where.append("e.status_id = ?")
        params.append(status_id)
    if point_id:
        where.append("e.point_id = ?")
        params.append(point_id)
    if person_id:
        where.append("e.person_id = ?")
        params.append(person_id)
    if held_reason:
        where.append("e.held_reason = ?")
        params.append(held_reason)
    if usable is not None:
        where.append("e.place = 'stock' and coalesce(s.usable, 1) = ?")
        params.append(1 if usable else 0)
    if date_from or date_to:
        parts = []
        for col in ("received_date", "installed_date", "issued_date", "removed_date", "accepted_date"):
            cond = f"(e.{col} is not null and e.{col} != ''"
            if date_from:
                cond += f" and e.{col} >= ?"
                params.append(date_from)
            if date_to:
                cond += f" and e.{col} <= ?"
                params.append(date_to)
            parts.append(cond + ")")
        where.append("(" + " or ".join(parts) + ")")
    if ids is not None:
        ids = list(ids)
        if not ids:
            where.append("0")
        else:
            where.append(f"e.id in ({', '.join('?' for _ in ids)})")
            params.extend(ids)
    return (" where " + " and ".join(where)) if where else "", params


async def list_items(
    limit: Optional[int] = None, offset: int = 0, *, with_extra: bool = False, **filters: Any
) -> tuple[list[dict[str, Any]], int]:
    where, params = _filters(**filters)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select count(*) from equipment e join equipment_models m on m.id = e.model_id "
            "join equipment_categories c on c.id = m.category_id left join equipment_statuses s on s.id = e.status_id "
            "left join people pe on pe.id = e.person_id" + where,
            params,
        )
        (total,) = await cur.fetchone()
        sql = ITEM_SELECT + where + " order by c.name collate nocase, m.name collate nocase, e.serial_number"
        q_params = list(params)
        if limit is not None:
            sql += " limit ? offset ?"
            q_params += [limit, offset]
        cur = await conn.execute(sql, q_params)
        items = [_item(r) for r in await cur.fetchall()]
        if with_extra:
            await attach_extra_conn(conn, items)
        return items, total


async def get_item(item_id: int, *, with_extra: bool = False) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        item = await get_item_conn(conn, item_id)
        if item and with_extra:
            await attach_extra_conn(conn, [item])
        return item


async def get_item_conn(conn, item_id: int) -> Optional[dict[str, Any]]:
    cur = await conn.execute(ITEM_SELECT + " where e.id = ?", (item_id,))
    row = await cur.fetchone()
    return _item(row) if row else None


async def find_by_serial_conn(conn, serial: str) -> Optional[dict[str, Any]]:
    cur = await conn.execute(ITEM_SELECT + " where pylower(e.serial_number) = pylower(?)", (serial.strip(),))
    row = await cur.fetchone()
    if row:
        return _item(row)
    # Полный номер SIM-карты (ICCID, номер с карты) — в системе она хранится по последним 7 цифрам.
    key = eq.sim_key(serial)
    if key and key != serial.strip() and len("".join(c for c in serial if c.isdigit())) > 7:
        cur = await conn.execute(ITEM_SELECT + " where c.kind = ? and e.serial_number = ?", (eq.CATEGORY_KIND_SIM, key))
        row = await cur.fetchone()
        return _item(row) if row else None
    return None


def suffix_key(text: Optional[str]) -> str:
    """Последние символы серийного номера, как их вводит человек: без пробелов, дефисов и знаков, без регистра («ab-12 34» → «ab1234»)."""
    return "".join(ch for ch in (text or "") if ch.isalnum()).casefold()


async def find_by_suffix_conn(conn, suffix: str, category_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Всё оборудование (кроме SIM-карт), серийный номер которого заканчивается на `suffix` (нормализованный suffix_key)."""
    suffix = suffix_key(suffix)
    if not suffix:
        return []
    sql = ITEM_SELECT + " where pylower(replace(replace(e.serial_number, '-', ''), ' ', '')) like ? and coalesce(c.kind, '') != ?"
    params: list[Any] = ["%" + suffix, eq.CATEGORY_KIND_SIM]
    if category_id:
        sql += " and c.id = ?"
        params.append(category_id)
    cur = await conn.execute(sql + " order by e.serial_number", params)
    return [_item(r) for r in await cur.fetchall()]


async def find_by_serial(serial: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        return await find_by_serial_conn(conn, serial)


async def point_items_conn(conn, point_id: str) -> list[dict[str, Any]]:
    cur = await conn.execute(
        ITEM_SELECT + " where e.point_id = ? and e.place = 'point' "
        "order by c.name collate nocase, e.installed_date desc, e.id desc",
        (point_id,),
    )
    return [_item(r) for r in await cur.fetchall()]


async def person_items_conn(conn, person_id: int, category_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = ITEM_SELECT + " where e.person_id = ? and e.place = 'person'"
    params: list[Any] = [person_id]
    if category_id:
        sql += " and c.id = ?"
        params.append(category_id)
    cur = await conn.execute(sql + " order by e.held_reason desc, c.name collate nocase, e.serial_number", params)
    return [_item(r) for r in await cur.fetchall()]


async def person_items(person_id: int, category_id: Optional[int] = None) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        return await person_items_conn(conn, person_id, category_id)


# ------------------------------------------------------------------ справочники

async def list_categories() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """select c.id, c.name, count(distinct m.id), count(e.id), c.kind
               from equipment_categories c
               left join equipment_models m on m.category_id = c.id
               left join equipment e on e.model_id = m.id
               group by c.id order by c.name collate nocase"""
        )
        return [
            {"id": r[0], "name": r[1], "model_count": r[2], "equipment_count": r[3], "kind": r[4] or "", "system": bool(r[4])}
            for r in await cur.fetchall()
        ]


async def list_models(category_id: Optional[int] = None) -> list[dict[str, Any]]:
    sql = (
        "select m.id, m.name, m.category_id, c.name, count(e.id) from equipment_models m "
        "join equipment_categories c on c.id = m.category_id left join equipment e on e.model_id = m.id"
    )
    params: list[Any] = []
    if category_id:
        sql += " where m.category_id = ?"
        params.append(category_id)
    sql += " group by m.id order by c.name collate nocase, m.name collate nocase"
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        return [
            {"id": r[0], "name": r[1], "category_id": r[2], "category_name": r[3], "equipment_count": r[4]}
            for r in await cur.fetchall()
        ]


async def list_statuses() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """select s.id, s.name, s.color, s.usable, s.role, s.protected, s.sort_order, count(e.id)
               from equipment_statuses s left join equipment e on e.status_id = s.id
               group by s.id order by s.sort_order, s.id"""
        )
        return [
            {
                "id": r[0], "name": r[1], "color": r[2], "usable": bool(r[3]), "role": r[4],
                "protected": bool(r[5]), "sort_order": r[6], "equipment_count": r[7],
            }
            for r in await cur.fetchall()
        ]


async def get_status_conn(conn, status_id: int) -> Optional[dict[str, Any]]:
    cur = await conn.execute("select id, name, color, usable, role, protected from equipment_statuses where id = ?", (status_id,))
    r = await cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "color": r[2], "usable": bool(r[3]), "role": r[4], "protected": bool(r[5])}


async def role_status_id_conn(conn, role: str) -> int:
    cur = await conn.execute("select id from equipment_statuses where role = ? order by id limit 1", (role,))
    row = await cur.fetchone()
    if row:
        return row[0]
    # Служебный статус удалили руками в базе — создаём заново (защита от пустой ссылки).
    name, color, usable, _, _ = next(s for s in eq.SEED_STATUSES if s[3] == role)
    cur = await conn.execute("select id from equipment_statuses where name = ?", (name,))
    row = await cur.fetchone()
    if row:
        await conn.execute("update equipment_statuses set role = ? where id = ?", (role, row[0]))
        return row[0]
    cur = await conn.execute(
        "insert into equipment_statuses (name, color, usable, role, protected, sort_order, created_at) "
        "values (?, ?, ?, ?, 1, 0, ?)",
        (name, color, usable, role, now_iso()),
    )
    return cur.lastrowid


async def status_by_name_conn(conn, name: str, *, color: str = eq.DEFAULT_STATUS_COLOR, usable: int = 0, create: bool = True) -> Optional[int]:
    name = " ".join((name or "").split())
    if not name:
        return None
    cur = await conn.execute("select id from equipment_statuses where pylower(name) = pylower(?)", (name,))
    row = await cur.fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from equipment_statuses")
    (order,) = await cur.fetchone()
    cur = await conn.execute(
        "insert into equipment_statuses (name, color, usable, role, protected, sort_order, created_at) "
        "values (?, ?, ?, '', 0, ?, ?)",
        (name, (color or "").strip() or eq.DEFAULT_STATUS_COLOR, usable, order, now_iso()),
    )
    return cur.lastrowid


async def category_by_name_conn(conn, name: str, *, create: bool = True) -> Optional[int]:
    name = " ".join((name or "").split())
    if not name:
        return None
    cur = await conn.execute("select id from equipment_categories where pylower(name) = pylower(?)", (name,))
    row = await cur.fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = await conn.execute("insert into equipment_categories (name) values (?)", (name,))
    return cur.lastrowid


async def model_by_name_conn(conn, category_id: int, name: str, *, create: bool = True) -> Optional[int]:
    name = " ".join((name or "").split())
    if not name:
        return None
    cur = await conn.execute(
        "select id from equipment_models where category_id = ? and pylower(name) = pylower(?)", (category_id, name)
    )
    row = await cur.fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = await conn.execute("insert into equipment_models (category_id, name) values (?, ?)", (category_id, name))
    return cur.lastrowid


async def sim_category_id_conn(conn) -> int:
    """Служебная категория SIM-карт (создаётся при старте; здесь — на случай, если её убрали руками в базе)."""
    cur = await conn.execute("select id from equipment_categories where kind = ? order by id limit 1", (eq.CATEGORY_KIND_SIM,))
    row = await cur.fetchone()
    if row:
        return row[0]
    from app.db.equipment_schema import ensure_sim_category
    return await ensure_sim_category(conn)


async def sim_model_id_conn(conn, name: Optional[str] = None) -> int:
    """Модель SIM-карты: указанная (оператор) или первая в категории; нет моделей — по умолчанию."""
    cid = await sim_category_id_conn(conn)
    name = " ".join((name or "").split())
    if name:
        return await model_by_name_conn(conn, cid, name)  # type: ignore[return-value]
    cur = await conn.execute("select id from equipment_models where category_id = ? order by id limit 1", (cid,))
    row = await cur.fetchone()
    if row:
        return row[0]
    return await model_by_name_conn(conn, cid, eq.SIM_DEFAULT_MODEL)  # type: ignore[return-value]


async def find_installed_sim_conn(conn, point_id: str) -> list[dict[str, Any]]:
    """SIM-карты, числящиеся установленными на комплексе (должна быть одна; больше — следствие ручных правок)."""
    cur = await conn.execute(
        ITEM_SELECT + " where e.point_id = ? and e.place = 'point' and c.kind = ? order by e.installed_date desc, e.id desc",
        (point_id, eq.CATEGORY_KIND_SIM),
    )
    return [_item(r) for r in await cur.fetchall()]


# ------------------------------------------------------------------ дополнительные поля

async def list_fields() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """select f.id, f.name, f.category_id, c.name, f.sort_order, count(v.equipment_id)
               from equipment_fields f left join equipment_categories c on c.id = f.category_id
               left join equipment_field_values v on v.field_id = f.id
               group by f.id order by f.sort_order, f.name collate nocase"""
        )
        return [
            {"id": r[0], "name": r[1], "category_id": r[2], "category_name": r[3] or "", "sort_order": r[4], "value_count": r[5]}
            for r in await cur.fetchall()
        ]


async def field_by_name_conn(conn, name: str, *, category_id: Optional[int] = None, create: bool = True) -> Optional[int]:
    name = " ".join((name or "").split())
    if not name:
        return None
    cur = await conn.execute("select id from equipment_fields where pylower(name) = pylower(?)", (name,))
    row = await cur.fetchone()
    if row:
        return row[0]
    if not create:
        return None
    cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from equipment_fields")
    (order,) = await cur.fetchone()
    cur = await conn.execute(
        "insert into equipment_fields (name, category_id, sort_order, created_at) values (?, ?, ?, ?)",
        (name, category_id, order, now_iso()),
    )
    return cur.lastrowid


async def attach_extra_conn(conn, items: list[dict[str, Any]]) -> None:
    """Кладёт в каждую единицу `extra` — [{field_id, name, value}] (только заполненные, в порядке полей)."""
    for it in items:
        it["extra"] = []
    ids = [it["id"] for it in items]
    by_id = {it["id"]: it for it in items}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        cur = await conn.execute(
            "select v.equipment_id, f.id, f.name, v.value from equipment_field_values v join equipment_fields f on f.id = v.field_id "
            f"where v.equipment_id in ({', '.join('?' for _ in chunk)}) order by f.sort_order, f.name collate nocase",
            chunk,
        )
        for eid, fid, fname, value in await cur.fetchall():
            by_id[eid]["extra"].append({"field_id": fid, "name": fname, "value": value})


async def set_extra_conn(conn, equipment_id: int, values: dict[int, Optional[str]], *, only_blank: bool = False) -> int:
    """Записывает значения дополнительных полей {field_id: значение}; пустое значение — очищает поле.
    `only_blank` — не перезаписывать уже заполненное (для режима «дополнить»). Возвращает число изменённых полей."""
    changed = 0
    for field_id, raw in values.items():
        value = (raw or "").strip() if isinstance(raw, str) else ("" if raw is None else str(raw).strip())
        cur = await conn.execute(
            "select value from equipment_field_values where equipment_id = ? and field_id = ?", (equipment_id, field_id)
        )
        row = await cur.fetchone()
        if not value:
            if row and not only_blank:
                await conn.execute("delete from equipment_field_values where equipment_id = ? and field_id = ?", (equipment_id, field_id))
                changed += 1
            continue
        if row:
            if only_blank or row[0] == value:
                continue
            await conn.execute(
                "update equipment_field_values set value = ? where equipment_id = ? and field_id = ?", (value, equipment_id, field_id)
            )
        else:
            await conn.execute(
                "insert into equipment_field_values (equipment_id, field_id, value) values (?, ?, ?)", (equipment_id, field_id, value)
            )
        changed += 1
    return changed


# ------------------------------------------------------------------ журнал

async def add_history_conn(
    conn, action_type: str, *, item: Optional[dict[str, Any]] = None, equipment_id: Optional[int] = None,
    serial_number: Optional[str] = None, model_name: Optional[str] = None, person_id: Optional[int] = None,
    person_name: Optional[str] = None, point_id: Optional[str] = None, performed_by: Optional[str] = None,
    comment: Optional[str] = None, event_date: Optional[str] = None,
) -> None:
    """event_date — дата события, указанная пользователем (например, дата установки); иначе — день записи."""
    if item is not None:
        equipment_id = equipment_id if equipment_id is not None else item["id"]
        serial_number = serial_number or item["serial_number"]
        model_name = model_name or item["model_name"]
    ts = now_iso()
    await conn.execute(
        "insert into equipment_history (ts, event_date, equipment_id, serial_number, model_name, action_type, "
        "person_id, person_name, point_id, performed_by, comment) values (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts, event_date or ts[:10], equipment_id, serial_number, model_name, action_type,
            person_id, person_name, point_id, performed_by, comment,
        ),
    )


_HISTORY_COLUMNS = (
    "id, ts, event_date, equipment_id, serial_number, model_name, action_type, person_id, person_name, "
    "point_id, performed_by, comment"
)
_HISTORY_KEYS = [c.strip() for c in _HISTORY_COLUMNS.split(",")]


def _history_filter(
    search: str = "", action_type: str = "", date_from: str = "", date_to: str = "",
    equipment_id: Optional[int] = None, point_id: str = "", person_id: Optional[int] = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if search:
        s = f"%{search.strip()}%"
        where.append(
            "(pylower(coalesce(serial_number, '')) like pylower(?) or pylower(coalesce(model_name, '')) like pylower(?) "
            "or pylower(coalesce(point_id, '')) like pylower(?) or pylower(coalesce(person_name, '')) like pylower(?) "
            "or pylower(coalesce(performed_by, '')) like pylower(?) or pylower(coalesce(comment, '')) like pylower(?))"
        )
        params.extend([s] * 6)
    if action_type:
        where.append("action_type = ?")
        params.append(action_type)
    if date_from:
        where.append("event_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("event_date <= ?")
        params.append(date_to)
    if equipment_id:
        where.append("(equipment_id = ? or serial_number = (select serial_number from equipment where id = ?))")
        params.extend([equipment_id, equipment_id])
    if point_id:
        where.append("point_id = ?")
        params.append(point_id)
    if person_id:
        where.append("person_id = ?")
        params.append(person_id)
    return (" where " + " and ".join(where)) if where else "", params


async def list_history(
    limit: Optional[int] = 200, offset: int = 0, **filters: Any
) -> tuple[list[dict[str, Any]], int, list[str]]:
    where, params = _history_filter(**filters)
    async with get_connection() as conn:
        cur = await conn.execute(f"select count(*) from equipment_history{where}", params)
        (total,) = await cur.fetchone()
        sql = f"select {_HISTORY_COLUMNS} from equipment_history{where} order by event_date desc, ts desc, id desc"
        q_params = list(params)
        if limit is not None:
            sql += " limit ? offset ?"
            q_params += [limit, offset]
        cur = await conn.execute(sql, q_params)
        rows = [dict(zip(_HISTORY_KEYS, r)) for r in await cur.fetchall()]
        cur = await conn.execute("select distinct action_type from equipment_history order by action_type")
        actions = [r[0] for r in await cur.fetchall()]
    return rows, total, actions


async def edit_history_entry(entry_id: int, event_date: Optional[str], comment: Optional[str], performed_by: Optional[str]) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute(
            "update equipment_history set event_date = ?, comment = ?, performed_by = ? where id = ?",
            (event_date, comment, performed_by, entry_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def delete_history_entry(entry_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from equipment_history where id = ?", (entry_id,))
        await conn.commit()
        return cur.rowcount > 0


async def delete_history_bulk(**filters: Any) -> int:
    where, params = _history_filter(**filters)
    async with get_connection() as conn:
        cur = await conn.execute(f"delete from equipment_history{where}", params)
        await conn.commit()
        return cur.rowcount


# ------------------------------------------------------------------ комплексы

async def list_points_with_equipment(
    search: str = "", category_id: Optional[int] = None, has_equipment: Optional[bool] = None,
    has_panel: Optional[bool] = None, limit: Optional[int] = 200, offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where: list[str] = []
    params: list[Any] = []
    if search:
        s = f"%{search.strip()}%"
        where.append(
            "(pylower(p.id) like pylower(?) or pylower(p.address) like pylower(?) or pylower(p.inventory_number) like pylower(?) "
            "or pylower(p.description) like pylower(?) or p.id in (select point_id from equipment where point_id is not null "
            "and place = 'point' and serial_number like ?))"
        )
        params.extend([s, s, s, s, f"%{search.strip()}%"])
    if category_id:
        where.append("p.category_id = ?")
        params.append(category_id)
    if has_equipment is True:
        where.append("exists (select 1 from equipment e where e.point_id = p.id and e.place = 'point')")
    elif has_equipment is False:
        where.append("not exists (select 1 from equipment e where e.point_id = p.id and e.place = 'point')")
    if has_panel is True:
        where.append("exists (select 1 from solar_panels sp where sp.point_id = p.id)")
    elif has_panel is False:
        where.append("not exists (select 1 from solar_panels sp where sp.point_id = p.id)")
    where_sql = (" where " + " and ".join(where)) if where else ""
    async with get_connection() as conn:
        cur = await conn.execute(f"select count(*) from points p{where_sql}", params)
        (total,) = await cur.fetchone()
        sql = (
            "select p.id, p.address, p.inventory_number, p.status, p.category_id, pc.name, pc.color, "
            "(select count(*) from equipment e where e.point_id = p.id and e.place = 'point'), "
            "(select panel_number from solar_panels sp where sp.point_id = p.id) "
            "from points p left join point_categories pc on pc.id = p.category_id" + where_sql + " order by p.id"
        )
        q_params = list(params)
        if limit is not None:
            sql += " limit ? offset ?"
            q_params += [limit, offset]
        cur = await conn.execute(sql, q_params)
        items = [
            {
                "id": r[0], "address": r[1], "inventory_number": r[2], "status": r[3], "category_id": r[4],
                "category_name": r[5] or "", "category_color": r[6] or "", "equipment_count": r[7], "panel_number": r[8] or "",
            }
            for r in await cur.fetchall()
        ]
    return items, total


# ------------------------------------------------------------------ отчёты

async def summary_by_category(**filters: Any) -> dict[str, Any]:
    """Остатки по категориям: на складе — по состояниям, у монтёров, на комплексах."""
    where, params = _filters(**filters)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select c.name, e.place, e.status_id, count(*) from equipment e "
            "join equipment_models m on m.id = e.model_id join equipment_categories c on c.id = m.category_id "
            "left join equipment_statuses s on s.id = e.status_id left join people pe on pe.id = e.person_id"
            + where + " group by c.name, e.place, e.status_id",
            params,
        )
        rows = await cur.fetchall()
        cur = await conn.execute("select id, name, color from equipment_statuses order by sort_order, id")
        statuses = await cur.fetchall()
    columns = [{"key": f"stock:{sid}", "label": f"На складе · {name}", "color": color} for sid, name, color in statuses]
    columns.append({"key": "stock:none", "label": "На складе · без статуса", "color": "#64748b"})
    columns += [
        {"key": "person", "label": "У монтёров", "color": "#f59e0b"},
        {"key": "point", "label": "На комплексах", "color": "#38bdf8"},
        {"key": "unknown", "label": "Неизвестное местонахождение", "color": "#f97316"},
    ]
    matrix: dict[str, dict[str, int]] = {}
    for cat, place, sid, cnt in rows:
        key = f"stock:{sid}" if place == eq.PLACE_STOCK and sid else ("stock:none" if place == eq.PLACE_STOCK else place)
        matrix.setdefault(cat, {})
        matrix[cat][key] = matrix[cat].get(key, 0) + cnt
    used = {k for m in matrix.values() for k in m}
    columns = [c for c in columns if c["key"] in used]
    table = [
        {"category": cat, "counts": matrix[cat], "total": sum(matrix[cat].values())}
        for cat in sorted(matrix, key=str.casefold)
    ]
    totals = {c["key"]: sum(m.get(c["key"], 0) for m in matrix.values()) for c in columns}
    return {"columns": columns, "rows": table, "totals": totals, "total": sum(totals.values())}


async def people_summary(
    person_id: Optional[int] = None, category_id: Optional[int] = None, date_from: str = "", date_to: str = ""
) -> list[dict[str, Any]]:
    """Что числится на каждом человеке (выдано и снято с комплексов), сгруппировано по человеку."""
    items, _ = await list_items(
        place=eq.PLACE_PERSON, person_id=person_id, category_id=category_id, date_from=date_from, date_to=date_to
    )
    groups: dict[int, dict[str, Any]] = {}
    for it in items:
        g = groups.setdefault(it["person_id"], {"person_id": it["person_id"], "full_name": it["person_name"] or "—", "items": []})
        g["items"].append(it)
    out = sorted(groups.values(), key=lambda g: (g["full_name"] or "").casefold())
    for g in out:
        g["count"] = len(g["items"])
        g["awaiting"] = sum(1 for i in g["items"] if i["awaiting_accept"])
        g["kept"] = sum(1 for i in g["items"] if i["kept"])
    return out


async def awaiting_accept_people() -> list[dict[str, Any]]:
    """Люди, на которых что-то числится: [{person_id, full_name, removed (ждёт приёмки), issued, kept (снятые SIM у монтёра)}]."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select p.id, p.full_name, sum(case when e.held_reason = 'removed' then 1 else 0 end), "
            "sum(case when e.held_reason = 'issued' then 1 else 0 end), "
            "sum(case when e.held_reason = 'kept' then 1 else 0 end) "
            "from equipment e join people p on p.id = e.person_id where e.place = 'person' "
            "group by p.id order by sum(case when e.held_reason = 'removed' then 1 else 0 end) desc, p.full_name collate nocase"
        )
        return [{"person_id": r[0], "full_name": r[1], "removed": r[2], "issued": r[3], "kept": r[4]} for r in await cur.fetchall()]


async def unknown_count() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from equipment where place = 'unknown'")
        (n,) = await cur.fetchone()
        return n


async def stock_totals_by_category() -> list[dict[str, Any]]:
    """Короткая сводка для бота: сколько по категориям на складе (годного / негодного)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select c.name, sum(case when coalesce(s.usable, 1) = 1 then 1 else 0 end), "
            "sum(case when coalesce(s.usable, 1) = 0 then 1 else 0 end) "
            "from equipment e join equipment_models m on m.id = e.model_id join equipment_categories c on c.id = m.category_id "
            "left join equipment_statuses s on s.id = e.status_id where e.place = 'stock' "
            "group by c.name order by c.name collate nocase"
        )
        return [{"category": r[0], "usable": r[1], "unusable": r[2]} for r in await cur.fetchall()]
