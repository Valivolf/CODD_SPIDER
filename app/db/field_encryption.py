"""Перевод полей в зашифрованный вид (см. app/core/field_crypto.py): people.full_name, people.phone, points.login, points.password.

Столбец `full_name` переименовывается в `full_name_enc` (в нём — шифртекст), а под прежним именем создаётся ВЫЧИСЛЯЕМЫЙ столбец
`full_name = lc_dec_pfn(full_name_enc)`: весь существующий SQL (выборки, сортировка, поиск, соединения) читает расшифрованные значения без
правок, а в файле базы открытого текста нет. Писать в вычисляемый столбец нельзя — записи идут в `*_enc` через `field_crypto.encrypt`.

Миграция идемпотентна и выполняется в конце init_db при каждом запуске: новая база, старая база с открытым текстом, копия, восстановленная
из старой версии, и уже переведённая база — все приводятся к одному виду (открытый текст, случайно попавший в *_enc, шифруется)."""
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app.core import field_crypto as fc

logger = logging.getLogger(__name__)

# таблица, столбец, поле шифра (в имени SQL-функции — см. field_crypto.FIELDS)
COLUMNS = [
    ("people", "full_name", "people.full_name"),
    ("people", "phone", "people.phone"),
    ("points", "login", "points.login"),
    ("points", "password", "points.password"),
]


async def _xinfo(conn: aiosqlite.Connection, table: str) -> dict[str, tuple]:
    cur = await conn.execute(f"pragma table_xinfo({table})")
    return {row[1]: row for row in await cur.fetchall()}


async def ensure_field_encryption(conn: aiosqlite.Connection) -> dict[str, int]:
    """Приводит таблицы к зашифрованному виду. Возвращает, сколько значений зашифровано сейчас (для журнала)."""
    done = 0
    logger.info("Шифрование полей: ключ — %s", fc.key_source())        # ключ создаётся при первом запуске, а не при первой записи: его сразу можно сохранить
    for table, col, field in COLUMNS:
        cur = await conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,))
        if not await cur.fetchone():
            continue
        info = await _xinfo(conn, table)
        enc = f"{col}_enc"
        if col not in info and enc not in info:
            continue                                              # столбца нет (очень старая схема) — нечего шифровать
        if enc not in info:
            if info[col][6] != 0:                                 # уже вычисляемый, а *_enc нет — так не бывает; не трогаем
                continue
            await conn.execute(f"alter table {table} rename column {col} to {enc}")
            await conn.execute(f"alter table {table} add column {col} text generated always as ({fc.FIELDS[field]}({enc})) virtual")
            await conn.commit()
            logger.info("Поле %s.%s переведено в зашифрованный столбец %s", table, col, enc)
        # открытый текст → шифртекст (первый запуск после обновления, восстановление старой копии, запись старым кодом)
        cur = await conn.execute(f"select rowid, {enc} from {table} where {enc} is not null and {enc} != '' and {enc} not like 'enc:v1:%'")
        rows = await cur.fetchall()
        for rowid, value in rows:
            await conn.execute(f"update {table} set {enc} = ? where rowid = ?", (fc.encrypt(field, value), rowid))
        if rows:
            await conn.commit()
            done += len(rows)
    if done:
        logger.info("Зашифровано значений в базе: %d", done)
    return {"encrypted_now": done}


async def table_uses_encryption(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    return f"{column}_enc" in await _xinfo(conn, table)


async def insert_person_raw(conn: aiosqlite.Connection, name: str, phone: Any, role_type: str, created_at: str) -> None:
    """Вставка человека из старого списка бригад (одноразовая миграция в init_db) — и до, и после перевода в зашифрованный вид."""
    if await table_uses_encryption(conn, "people", "full_name"):
        await conn.execute(
            "insert into people (full_name_enc, phone_enc, role_type, active, created_at) values (?, ?, ?, 1, ?)",
            (fc.encrypt("people.full_name", name), fc.encrypt("people.phone", phone), role_type, created_at),
        )
    else:
        await conn.execute("insert into people (full_name, phone, role_type, active, created_at) values (?, ?, ?, 1, ?)", (name, phone, role_type, created_at))


async def status(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Сколько значений зашифровано / осталось открытым / не расшифровывается — для окна «Ключ шифрования»."""
    out: dict[str, Any] = {"fields": [], "key_source": fc.key_source(), "failures": 0, "ok": True}
    for table, col, field in COLUMNS:
        info = await _xinfo(conn, table)
        enc = f"{col}_enc"
        if enc not in info:
            out["fields"].append({"field": f"{table}.{col}", "encrypted": 0, "plain": 0, "unreadable": 0, "converted": False})
            out["ok"] = False
            continue
        cur = await conn.execute(f"select count(*) from {table} where {enc} like 'enc:v1:%'")
        (n_enc,) = await cur.fetchone()
        cur = await conn.execute(f"select count(*) from {table} where {enc} is not null and {enc} != '' and {enc} not like 'enc:v1:%'")
        (n_plain,) = await cur.fetchone()
        bad = 0
        cur = await conn.execute(f"select {enc} from {table} where {enc} like 'enc:v1:%' limit 200")
        for (value,) in await cur.fetchall():
            if fc.decrypt(field, value) == "":
                bad += 1
        out["fields"].append({"field": f"{table}.{col}", "encrypted": n_enc, "plain": n_plain, "unreadable": bad, "converted": True})
        if n_plain or bad:
            out["ok"] = False
    out["failures"] = fc.failures()
    return out
