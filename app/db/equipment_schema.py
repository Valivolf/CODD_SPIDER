"""Схема учёта оборудования и миграции со старых раскладок таблиц.

Вызывается из database.init_db(): `create_equipment_tables` — создать таблицы в новой раскладке
(на свежей базе), `migrate_*` — привести к ней базу, где эти таблицы уже есть в старом виде.
Все миграции идемпотентны и срабатывают один раз.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import aiosqlite

from app.core import equipment as eq

log = logging.getLogger(__name__)

_EQUIPMENT_DDL = """create table if not exists {name} (
    id integer primary key autoincrement,
    model_id integer not null references equipment_models(id),
    serial_number text not null unique,
    place text not null default 'stock',
    status_id integer references equipment_statuses(id) on delete set null,
    point_id text,
    person_id integer references people(id) on delete set null,
    held_reason text,
    received_date text,
    issued_date text,
    installed_date text,
    installed_by text,
    removed_date text,
    removed_by text,
    removed_point_id text,
    accepted_date text,
    accepted_by text,
    notes text,
    removal_reason text,
    created_at text not null,
    updated_at text not null
)"""

_INDEXES = (
    "create index if not exists idx_equipment_place on equipment(place)",
    "create index if not exists idx_equipment_point on equipment(point_id)",
    "create index if not exists idx_equipment_person on equipment(person_id)",
    "create index if not exists idx_equipment_model on equipment(model_id)",
    "create index if not exists idx_equipment_status on equipment(status_id)",
    "create index if not exists idx_eqh_equipment on equipment_history(equipment_id)",
    "create index if not exists idx_eqh_point on equipment_history(point_id)",
    "create index if not exists idx_eqh_event on equipment_history(event_date)",
    "create unique index if not exists idx_solar_point on solar_panels(point_id) where point_id is not null",
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"pragma table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def create_equipment_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """create table if not exists equipment_statuses (
            id integer primary key autoincrement,
            name text not null unique,
            color text not null default '#64748b',
            usable integer not null default 0,
            role text not null default '',
            protected integer not null default 0,
            sort_order integer not null default 0,
            created_at text not null
        )"""
    )
    await conn.execute(
        """create table if not exists equipment_categories (
            id integer primary key autoincrement,
            name text not null unique,
            kind text not null default ''
        )"""
    )
    await conn.execute(
        """create table if not exists equipment_models (
            id integer primary key autoincrement,
            category_id integer not null references equipment_categories(id) on delete cascade,
            name text not null,
            unique(category_id, name)
        )"""
    )
    await conn.execute(_EQUIPMENT_DDL.format(name="equipment"))
    await conn.execute(
        """create table if not exists equipment_history (
            id integer primary key autoincrement,
            ts text not null,
            equipment_id integer references equipment(id) on delete set null,
            serial_number text,
            model_name text,
            action_type text not null,
            person_name text,
            point_id text,
            performed_by text,
            comment text
        )"""
    )
    # Дополнительные поля оборудования (IMEI, MAC, ICCID, гарантия, № коробки… — что угодно): справочник полей
    # (при желании — для одной категории) и значения по единицам. Заполняются вручную и импортом из Excel.
    await conn.execute(
        """create table if not exists equipment_fields (
            id integer primary key autoincrement,
            name text not null unique,
            category_id integer references equipment_categories(id) on delete set null,
            sort_order integer not null default 0,
            created_at text not null
        )"""
    )
    await conn.execute(
        """create table if not exists equipment_field_values (
            equipment_id integer not null references equipment(id) on delete cascade,
            field_id integer not null references equipment_fields(id) on delete cascade,
            value text not null,
            primary key (equipment_id, field_id)
        )"""
    )
    # Оборудование, которое монтёр поставил на комплекс, указав только последние символы серийного номера (6 и больше), а в учёте такого номера ещё нет
    # (не выдавали, не добавили). Как только такое оборудование появится в системе (приёмка, импорт), оно само встанет на этот комплекс.
    await conn.execute(
        """create table if not exists equipment_pending (
            id integer primary key autoincrement,
            point_id text not null,
            category_id integer references equipment_categories(id) on delete set null,
            suffix text not null,
            person_id integer references people(id) on delete set null,
            person_name text,
            install_date text,
            note text,
            created_at text not null,
            resolved_at text,
            resolved_equipment_id integer references equipment(id) on delete set null
        )"""
    )
    await conn.execute("create index if not exists idx_eqpend_open on equipment_pending(resolved_at, suffix)")
    await conn.execute(
        """create table if not exists solar_panels (
            id integer primary key autoincrement,
            panel_number text not null unique,
            point_id text,
            location text,
            notes text,
            at_warehouse integer not null default 0,
            created_at text not null,
            updated_at text not null
        )"""
    )


async def seed_equipment_statuses(conn: aiosqlite.Connection) -> None:
    """Справочник состояний: служебные «Исправно»/«Дефектное» нужны всегда, остальные — только на пустой базе."""
    cur = await conn.execute("select count(*) from equipment_statuses")
    (count,) = await cur.fetchone()
    now = _now()
    if count == 0:
        for order, (name, color, usable, role, protected) in enumerate(eq.SEED_STATUSES, start=1):
            await conn.execute(
                "insert into equipment_statuses (name, color, usable, role, protected, sort_order, created_at) "
                "values (?, ?, ?, ?, ?, ?, ?)",
                (name, color, usable, role, protected, order, now),
            )
    for role in (eq.ROLE_OK, eq.ROLE_DEFECT):
        cur = await conn.execute("select 1 from equipment_statuses where role = ?", (role,))
        if await cur.fetchone():
            continue
        name, color, usable, _, protected = next(s for s in eq.SEED_STATUSES if s[3] == role)
        # Имя может быть занято обычным статусом — тогда служебным делаем его.
        cur = await conn.execute("select id from equipment_statuses where name = ?", (name,))
        row = await cur.fetchone()
        if row:
            await conn.execute("update equipment_statuses set role = ?, protected = 1 where id = ?", (role, row[0]))
        else:
            await conn.execute(
                "insert into equipment_statuses (name, color, usable, role, protected, sort_order, created_at) "
                "values (?, ?, ?, ?, ?, 0, ?)",
                (name, color, usable, role, protected, now),
            )
    cur = await conn.execute("select count(*) from equipment_categories")
    (cats,) = await cur.fetchone()
    if cats == 0:
        await conn.executemany(
            "insert into equipment_categories (name) values (?)", [(n,) for n in eq.DEFAULT_CATEGORIES]
        )


async def _status_id(conn: aiosqlite.Connection, name: str, color: str, usable: int) -> int:
    cur = await conn.execute("select id from equipment_statuses where name = ?", (name,))
    row = await cur.fetchone()
    if row:
        return row[0]
    cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from equipment_statuses")
    (order,) = await cur.fetchone()
    cur = await conn.execute(
        "insert into equipment_statuses (name, color, usable, role, protected, sort_order, created_at) "
        "values (?, ?, ?, '', 0, ?, ?)",
        (name, color, usable, order, _now()),
    )
    return cur.lastrowid


async def migrate_equipment_layout(conn: aiosqlite.Connection) -> None:
    """Старая раскладка equipment (статус строкой 'На складе'/'Выдано'/…, без дат и происхождения)
    → новая (место + настраиваемый статус). Строки переносятся с теми же id, журнал не теряется."""
    cols = await _columns(conn, "equipment")
    if "place" not in cols:
        log.warning("Учёт оборудования: перевожу таблицу equipment на новую раскладку")
        await conn.commit()
        ok_id = (await (await conn.execute("select id from equipment_statuses where role = ?", (eq.ROLE_OK,))).fetchone())[0]
        defect_id = (await (await conn.execute("select id from equipment_statuses where role = ?", (eq.ROLE_DEFECT,))).fetchone())[0]
        legacy_ids = {
            "В ремонте": await _status_id(conn, "В ремонте", "#f59e0b", 0),
            "Списано": await _status_id(conn, "Списано", "#64748b", 0),
            "Снято": await _status_id(conn, "Снято", "#fb923c", 0),
        }
        await conn.commit()
        cur = await conn.execute(
            "select id, model_id, serial_number, status, point_id, person_id, notes, created_at, updated_at from equipment"
        )
        legacy = await cur.fetchall()
        await conn.execute("pragma foreign_keys=off")
        try:
            await conn.execute("begin")
            await conn.execute("drop table if exists equipment_new")
            await conn.execute(_EQUIPMENT_DDL.format(name="equipment_new"))
            for (rid, model_id, serial, status, point_id, person_id, notes, created_at, updated_at) in legacy:
                place, held, sid = eq.PLACE_STOCK, None, ok_id
                if status == "Выдано" and person_id:
                    place, held = eq.PLACE_PERSON, eq.HELD_ISSUED
                elif status == "Установлено" and point_id:
                    place = eq.PLACE_POINT
                elif status in legacy_ids:
                    sid = legacy_ids[status]
                elif status == "Брак":
                    sid = defect_id
                await conn.execute(
                    "insert into equipment_new (id, model_id, serial_number, place, status_id, point_id, person_id, "
                    "held_reason, notes, created_at, updated_at, received_date) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid, model_id, serial, place, sid,
                        point_id if place == eq.PLACE_POINT else None,
                        person_id if place == eq.PLACE_PERSON else None,
                        held, notes, created_at, updated_at, (created_at or "")[:10] or None,
                    ),
                )
            await conn.execute("drop table equipment")
            await conn.execute("alter table equipment_new rename to equipment")
            await conn.commit()
        except Exception:
            await conn.rollback()
            log.exception("Учёт оборудования: не удалось перевести таблицу equipment — оставлена как была")
            raise
        finally:
            await conn.execute("pragma foreign_keys=on")

    hist = await _columns(conn, "equipment_history")
    if "event_date" not in hist:
        await conn.execute("alter table equipment_history add column event_date text")
    if "person_id" not in hist:
        await conn.execute("alter table equipment_history add column person_id integer")
    await conn.execute("update equipment_history set event_date = substr(ts, 1, 10) where event_date is null")
    for ddl in _INDEXES:
        await conn.execute(ddl)
    await conn.commit()


async def migrate_equipment_features(conn: aiosqlite.Connection) -> None:
    """Колонки, добавленные позже: вид категории (служебная SIM), причина снятия; служебная категория SIM-карт
    и её модель по умолчанию. Идемпотентно."""
    if "kind" not in await _columns(conn, "equipment_categories"):
        await conn.execute("alter table equipment_categories add column kind text not null default ''")
    if "removal_reason" not in await _columns(conn, "equipment"):
        await conn.execute("alter table equipment add column removal_reason text")
    await conn.execute("create index if not exists idx_efv_field on equipment_field_values(field_id)")
    await ensure_sim_category(conn)
    await conn.commit()


async def ensure_sim_category(conn: aiosqlite.Connection) -> int:
    """Служебная категория SIM-карт: одна, с моделью по умолчанию. Уже заведённую вручную категорию с таким названием
    («SIM-карта», «SIM», «Сим-карта») делаем служебной, а не создаём вторую."""
    cur = await conn.execute("select id from equipment_categories where kind = ? order by id limit 1", (eq.CATEGORY_KIND_SIM,))
    row = await cur.fetchone()
    if row:
        cid = row[0]
    else:
        cur = await conn.execute("select id, name from equipment_categories")
        wanted = {eq.SIM_CATEGORY_NAME.casefold(), "sim", "сим-карта", "сим карта", "sim-карты", "sim карта"}
        cid = next((r[0] for r in await cur.fetchall() if (r[1] or "").strip().casefold() in wanted), None)
        if cid:
            await conn.execute("update equipment_categories set kind = ? where id = ?", (eq.CATEGORY_KIND_SIM, cid))
        else:
            cur = await conn.execute(
                "insert into equipment_categories (name, kind) values (?, ?)", (eq.SIM_CATEGORY_NAME, eq.CATEGORY_KIND_SIM)
            )
            cid = cur.lastrowid
    cur = await conn.execute("select count(*) from equipment_models where category_id = ?", (cid,))
    (models,) = await cur.fetchone()
    if models == 0:
        await conn.execute("insert into equipment_models (category_id, name) values (?, ?)", (cid, eq.SIM_DEFAULT_MODEL))
    return cid


_PEOPLE_DDL = """create table people_new (
    id integer primary key autoincrement,
    full_name text not null,
    phone text,
    role_type text not null check (role_type in ('fitter','driver','operator','warehouse')),
    vk_id integer unique,
    active integer not null default 1,
    notes text,
    created_at text not null
)"""


async def migrate_people_roles(conn: aiosqlite.Connection) -> None:
    """В персонале появляется роль «склад» ('warehouse') — ограничение CHECK на role_type в SQLite
    нельзя изменить на месте, поэтому таблица пересоздаётся (те же id, внешние ключи не трогаются)."""
    cur = await conn.execute("select sql from sqlite_master where type = 'table' and name = 'people'")
    row = await cur.fetchone()
    if not row or "'warehouse'" in (row[0] or ""):
        return
    log.warning("Персонал: добавляю роль «склад» (пересоздаю таблицу people)")
    # Новое описание берём из сохранённого старого — меняется только список ролей, остальные колонки и
    # ограничения (в том числе добавленные когда-либо вручную) переезжают как есть.
    old_sql = row[0] or ""
    new_sql, n_roles = re.subn(r"(check\s*\(\s*role_type\s+in\s*\([^)]*?)\s*\)", r"\1,'warehouse')", old_sql, count=1, flags=re.I)
    new_sql, n_name = re.subn(r"^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`\[]?people[\"`\]]?", "create table people_new", new_sql, count=1, flags=re.I)
    if not (n_roles and n_name):
        new_sql = _PEOPLE_DDL
    await conn.commit()
    await conn.execute("pragma foreign_keys=off")
    try:
        await conn.execute("begin")
        await conn.execute("drop table if exists people_new")
        await conn.execute(new_sql)
        old_cols = [r[1] for r in await (await conn.execute("pragma table_info(people)")).fetchall()]
        new_cols = [r[1] for r in await (await conn.execute("pragma table_info(people_new)")).fetchall()]
        shared = ", ".join(c for c in old_cols if c in new_cols)
        await conn.execute(f"insert into people_new ({shared}) select {shared} from people")
        await conn.execute("drop table people")
        await conn.execute("alter table people_new rename to people")
        await conn.commit()
    except Exception:
        await conn.rollback()
        log.exception("Персонал: не удалось добавить роль «склад» — таблица оставлена как была")
        raise
    finally:
        await conn.execute("pragma foreign_keys=on")
