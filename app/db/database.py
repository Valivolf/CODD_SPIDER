"""Async SQLite access — connection helper and schema bootstrap.

An aiosqlite connection per operation, taken from a small pool of idle
connections (opening one costs milliseconds — a thread, PRAGMAs, a SQL
function — while a query on an open connection takes ~0.2 ms; most requests
make several). WAL + busy_timeout keep the file safe if it is ever opened by
more than one process. A connection that raised, or was left inside an open
transaction, is never reused; the pool is emptied before the database file is
replaced (restore from a backup, see exclusive_access).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import json
import logging
import os
import shutil
import sys
import time
from typing import AsyncIterator

import aiosqlite

from app.config import get_settings
from app.core import field_crypto
from app.core.names import compact_name
from app.core.text import decode_entities, parse_names
from app.db.field_encryption import ensure_field_encryption, insert_person_raw
from app.db.equipment_schema import (
    create_equipment_tables,
    migrate_equipment_features,
    migrate_equipment_layout,
    migrate_people_roles,
    seed_equipment_statuses,
)

INITIAL_STATE = {
    "points": [],
    "tasks": [],
}


async def _ensure_column(conn: aiosqlite.Connection, table: str, column: str, ddl: str) -> None:
    """Idempotently adds a column to an existing table — `create table if
    not exists` only helps for a table that doesn't exist yet; a table
    that's already on disk from before needs this instead."""
    cur = await conn.execute(f"pragma table_xinfo({table})")      # xinfo: видит и вычисляемые столбцы (зашифрованные поля — см. field_encryption.py)
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        await conn.execute(f"alter table {table} add column {column} {ddl}")


async def _drop_column_if_exists(conn: aiosqlite.Connection, table: str, column: str) -> None:
    """DROP COLUMN needs SQLite >= 3.35 (bundled with every Python 3.12+
    build this app ships on); logs and leaves the column in place rather
    than failing startup if an older SQLite can't do it."""
    cur = await conn.execute(f"pragma table_xinfo({table})")
    existing = {row[1] for row in await cur.fetchall()}
    if column not in existing:
        return
    try:
        await conn.execute(f"alter table {table} drop column {column}")
    except aiosqlite.OperationalError:
        logging.getLogger(__name__).warning(
            "Could not drop column %s.%s (SQLite too old?) — left in place, unused.", table, column
        )


def _py_lower(s: str | None) -> str | None:
    """Registered as the `pylower` SQL function below — SQLite's own
    lower()/LIKE case-folding only covers ASCII, so a Cyrillic search term
    against Cyrillic text (addresses, descriptions — most of what's
    searched in this app) stayed case-sensitive even through `lower(col)
    like lower(?)`. Python's str.lower() handles Cyrillic correctly, so
    queries that need case-insensitive matching use `pylower(col) like
    pylower(?)` instead of the bare SQL LIKE/lower()."""
    return s.lower() if s is not None else None


def _py_compact(s: str | None) -> str | None:
    """`pycompact` — название «без разбора» для поиска: без регистра, пробелов, дефисов и знаков, ё = е, латинские буквы-двойники русских приведены к русским
    («ARENA-0901205» = «arena 0901205» = «АРЕНА0901205»). См. app/core/names.py::compact_name."""
    return compact_name(s) if s is not None else None


# Эксклюзивный доступ к файлу базы — для восстановления из резервной копии
# (app/api/routes_backup.py): файл базы подменяется, пока приложение работает, а
# не «перезапуском процесса» (без Docker, при запуске скриптом, его некому
# поднять обратно). На время подмены новые get_connection() ждут, а уже
# открытые — дорабатывают; пока открыто хоть одно, подмена не начинается.
class _Gate:
    def __init__(self) -> None:
        self.open = asyncio.Event()
        self.open.set()
        self.idle = asyncio.Event()
        self.idle.set()
        self.active = 0
        # Кто сейчас держит соединение (место вызова и момент открытия) — чтобы при
        # зависшем восстановлении было видно, что именно не отпускает базу.
        self.holders: dict[object, tuple[str, float]] = {}


_gate = _Gate()
logger = logging.getLogger(__name__)


def _caller_label() -> str:
    """«файл:функция:строка» того, кто открыл соединение (мимо служебных кадров
    contextlib и самого этого модуля)."""
    frame = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename.endswith(("contextlib.py", "database.py")):
        frame = frame.f_back
    if frame is None:
        return "?"
    return f"{os.path.basename(frame.f_code.co_filename)}:{frame.f_code.co_name}:{frame.f_lineno}"


def active_connections() -> int:
    return _gate.active


def describe_holders(limit: int = 5) -> str:
    now = time.monotonic()
    busy = sorted(_gate.holders.values(), key=lambda h: h[1])[:limit]
    return "; ".join(f"{label} ({now - since:.0f} с)" for label, since in busy)
# Задача, уже держащая соединение, может открыть вложенное (так устроены некоторые
# репозитории) — ей ждать закрытую «калитку» нельзя, иначе взаимная блокировка.
_conn_depth: contextvars.ContextVar[int] = contextvars.ContextVar("db_conn_depth", default=0)
_exclusive_owner: contextvars.ContextVar[bool] = contextvars.ContextVar("db_exclusive_owner", default=False)


# Кто-то записал в базу: после закрытия блока get_connection(), в котором менялись строки, вызываются эти функции (без аргументов).
# Так кэш справочника комплексов (app/core/points_version.py) узнаёт об изменении сразу, а не через секунду. Фоновые записи,
# которые ничего видимого не меняют (отметка «проверено»), помечаются quiet_writes() и кэш не сбрасывают.
_write_listeners: list = []
_quiet: contextvars.ContextVar[bool] = contextvars.ContextVar("db_quiet_writes", default=False)


def add_write_listener(fn) -> None:
    _write_listeners.append(fn)


@contextlib.contextmanager
def quiet_writes():
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


_POOL_MAX = 6
_idle: list[aiosqlite.Connection] = []      # открытые, свободные соединения (последним вернули — первым берём)


async def _open_connection(path: str) -> aiosqlite.Connection:
    pending = aiosqlite.connect(path, timeout=10)
    # Свободные соединения в пуле не должны держать процесс (скрипты, тесты) после завершения работы: поток — фоновый.
    worker = getattr(pending, "_thread", None)          # aiosqlite ≥ 0.22: поток лежит внутри соединения
    if worker is not None:
        worker.daemon = True
    elif hasattr(pending, "daemon"):                    # прежние версии: соединение само — поток
        pending.daemon = True
    conn = await pending
    conn._lc_path = path                    # type: ignore[attr-defined] — для проверки, что файл базы не подменили
    await conn.execute("pragma journal_mode=wal")
    await conn.execute("pragma busy_timeout=10000")
    await conn.execute("pragma foreign_keys=on")
    # Для режима WAL «NORMAL» безопасен: файл не портится, при внезапном отключении питания можно потерять лишь последние секунды
    # записей (ежедневная копия остаётся). Зато запись не ждёт сброса на диск при каждой операции.
    await conn.execute("pragma synchronous=normal")
    await conn.execute("pragma temp_store=memory")
    await conn.execute("pragma cache_size=-16384")           # ~16 МБ страниц на соединение
    await conn.execute("pragma mmap_size=134217728")         # чтение через отображение файла в память
    await conn.create_function("pylower", 1, _py_lower)
    await conn.create_function("pycompact", 1, _py_compact)
    await field_crypto.register_aiosqlite(conn)         # расшифровка зашифрованных полей (логины/пароли комплексов, ФИО и телефоны) для вычисляемых столбцов
    trace = os.environ.get("LC_SQL_TRACE")          # только для разбора запросов (индексы): путь к файлу, куда писать выполненные запросы
    if trace:
        def _log(statement: str, _path=trace) -> None:
            try:
                with open(_path, "a", encoding="utf-8") as f:
                    f.write(statement.replace("\n", " ")[:1500] + "\n")
            except OSError:
                pass
        await conn.set_trace_callback(_log)
    return conn


async def _close_quietly(conn: aiosqlite.Connection) -> None:
    try:
        await conn.close()
    except Exception:  # noqa: BLE001
        pass


async def close_pool() -> None:
    """Закрыть свободные соединения (перед подменой файла базы и при остановке приложения)."""
    while _idle:
        await _close_quietly(_idle.pop())


@contextlib.asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    if _conn_depth.get() == 0 and not _exclusive_owner.get():
        await _gate.open.wait()
    token = _conn_depth.set(_conn_depth.get() + 1)
    _gate.active += 1
    _gate.idle.clear()
    holder = object()
    _gate.holders[holder] = (_caller_label(), time.monotonic())
    path = str(get_settings().db_path)
    conn = None
    healthy = False
    try:
        while _idle and conn is None:
            candidate = _idle.pop()
            if getattr(candidate, "_lc_path", None) == path:
                conn = candidate
            else:
                await _close_quietly(candidate)
        if conn is None:
            conn = await _open_connection(path)
        changes_before = conn.total_changes
        yield conn
        healthy = True
        if _write_listeners and not _quiet.get() and conn.total_changes != changes_before:
            for listener in _write_listeners:
                try:
                    listener()
                except Exception:  # noqa: BLE001 — кэш не должен ломать запись
                    logger.exception("Обработчик записи в базу упал")
    finally:
        try:
            if conn is not None:
                reuse = healthy and len(_idle) < _POOL_MAX and _gate.open.is_set() and not _exclusive_owner.get()
                if reuse:
                    try:
                        if conn.in_transaction:                  # незавершённая транзакция — откат, как при закрытии соединения
                            await conn.rollback()
                        conn.row_factory = None
                        _idle.append(conn)
                    except Exception:  # noqa: BLE001
                        reuse = False
                if not reuse:
                    await _close_quietly(conn)
        finally:
            _conn_depth.reset(token)
            _gate.holders.pop(holder, None)
            _gate.active -= 1
            if _gate.active <= 0:
                _gate.active = 0
                _gate.idle.set()


@contextlib.asynccontextmanager
async def exclusive_access(timeout: float = 30.0) -> AsyncIterator[None]:
    """Пока внутри — к базе обращается только этот блок (его собственные
    вызовы get_connection проходят), остальные ждут. Ждёт завершения уже
    открытых соединений не дольше `timeout` секунд, иначе RuntimeError."""
    _gate.open.clear()
    try:
        try:
            await asyncio.wait_for(_gate.idle.wait(), timeout)
        except asyncio.TimeoutError:
            holders = describe_holders()
            logger.warning("Эксклюзивный доступ к базе: за %.0f с не дождались завершения операций: %s", timeout, holders)
            raise RuntimeError(
                "База сейчас занята — не удалось дождаться завершения текущих операций. "
                f"Повторите через минуту. (Держат базу: {holders or 'неизвестно'})"
            )
        await close_pool()      # свободные соединения держат файл базы — на время подмены их быть не должно
        token = _exclusive_owner.set(True)
        try:
            yield
        finally:
            _exclusive_owner.reset(token)
    finally:
        _gate.open.set()


async def init_db() -> None:
    """Create tables if missing, seed the initial calendar state, and
    migrate legacy free-text бригада composition into `people` once."""
    async with get_connection() as conn:
        await conn.execute(
            """create table if not exists state (
                id integer primary key,
                rev integer not null,
                body text not null
            )"""
        )
        await conn.execute(
            """create table if not exists users (
                id integer primary key autoincrement,
                username text not null unique,
                password_hash text not null,
                role text not null default 'user',
                must_change_password integer not null default 0,
                token_version integer not null default 0,
                created_at text not null
            )"""
        )
        # Личные сообщения между учётными записями. Сами сообщения не имеют срока хранения,
        # вложения очищаются отдельным фоновым циклом через 14 дней.
        await conn.execute("""create table if not exists account_messages (
            id integer primary key autoincrement,
            sender_id integer not null references users(id) on delete cascade,
            recipient_id integer not null references users(id) on delete cascade,
            text text not null default '',
            reply_to integer references account_messages(id) on delete set null,
            created_at text not null,
            edited_at text,
            deleted_at text
        )""")
        await conn.execute("""create table if not exists account_message_files (
            id integer primary key autoincrement,
            message_id integer not null references account_messages(id) on delete cascade,
            file_name text not null,
            file_ext text not null,
            size integer not null,
            created_at text not null,
            deleted_at text
        )""")
        await conn.execute("create index if not exists idx_account_msg_pair on account_messages(sender_id, recipient_id, id)")
        await conn.execute("create index if not exists idx_account_file_expiry on account_message_files(created_at, deleted_at)")
        # Игры (страница /games, app/services/games.py): кому из пользователей они включены (администратор — для каждого отдельно; games — JSON-список разрешённых игр или NULL = все)
        # и лучшие результаты (по одной строке на пользователя и игру: лучший результат, число партий).
        await conn.execute(
            "create table if not exists user_games (user_id integer primary key references users(id) on delete cascade, enabled integer not null default 0, games text)"
        )
        await conn.execute(
            """create table if not exists game_scores (
                user_id integer not null references users(id) on delete cascade,
                game text not null,
                best real not null,
                plays integer not null default 0,
                best_at text not null,
                last_at text not null,
                primary key (user_id, game)
            )"""
        )
        # Журнал событий безопасности (app/services/security_log.py): вход и отказы во входе, смена паролей, действия администратора, скачивание копий базы,
        # отказы в доступе. Только дописывается; срок хранения — SECURITY_LOG_RETENTION_DAYS (по умолчанию год).
        await conn.execute(
            """create table if not exists security_events (
                id integer primary key autoincrement,
                at text not null,
                kind text not null,
                username text not null default '',
                role text not null default '',
                ip text not null default '',
                ok integer not null default 1,
                details text not null default ''
            )"""
        )
        await conn.execute("create index if not exists idx_secev_at on security_events(at)")
        await conn.execute("create index if not exists idx_secev_kind on security_events(kind, at)")
        await conn.execute(
            """create table if not exists people (
                id integer primary key autoincrement,
                full_name text not null,
                phone text,
                role_type text not null check (role_type in ('fitter','driver','operator','warehouse')),
                vk_id integer unique,
                active integer not null default 1,
                notes text,
                created_at text not null
            )"""
        )
        await conn.execute(
            """create table if not exists team_assignments (
                id integer primary key autoincrement,
                team_id integer not null,
                person_id integer not null references people(id) on delete cascade,
                date text not null,
                unique(team_id, person_id, date)
            )"""
        )
        await conn.execute(
            """create table if not exists people_absences (
                id integer primary key autoincrement,
                person_id integer not null references people(id) on delete cascade,
                start_date text not null,
                end_date text not null,
                reason text,
                created_at text not null
            )"""
        )
        # Кто отметил отсутствие (логин) — страница «Отсутствия» показывает, чья это отметка.
        await _ensure_column(conn, "people_absences", "created_by", "text")
        await conn.execute("create index if not exists idx_absences_person on people_absences(person_id, start_date)")
        # Учёт оборудования по серийным номерам (бывшее приложение «Склад»): категории, модели,
        # единицы, статусы-состояния, журнал, солнечные панели — см. app/db/equipment_schema.py.
        # Старая раскладка этих таблиц (если база её помнит) переводится на новую ниже, после commit.
        await create_equipment_tables(conn)
        await conn.execute(
            """create table if not exists teams (
                id integer primary key,
                name text not null,
                archived integer not null default 0
            )"""
        )
        await conn.execute(
            """create table if not exists points (
                id text primary key,
                address text not null default '',
                description text not null default '',
                lat text not null default '',
                lon text not null default ''
            )"""
        )
        # Warehouse-relevant fields, added on top of the original point
        # directory columns — _ensure_column so this also backfills them
        # onto a `points` table that already existed before this turn.
        for col, ddl in (
            ("inventory_number", "text not null default ''"),
            # Optimistic locking of accounting values; existing values are not rewritten.
            ("accounting_revision", "integer not null default 0"),
            ("sim_number", "text not null default ''"),
            ("ip_address", "text not null default ''"),
            ("port", "text not null default ''"),
            ("notes", "text not null default ''"),
            ("status", "text not null default ''"),
            ("category_id", "integer references point_categories(id)"),
            # Комплекс's own web-interface credentials — заполняются и видны
            # только оператору/админу (см. app/api/routes_points.py — роль
            # 'warehouse' и бот получают их вычищенными из ответа).
            ("login", "text not null default ''"),
            ("password", "text not null default ''"),
            # Дата, до которой действует сертификат поверки (метрологической
            # проверки) оборудования — свободная строка формата YYYY-MM-DD,
            # тот же формат, что и у остальных дат в проекте. Пусто — нет
            # данных (не то же самое, что "истёк").
            ("verification_valid_until", "text not null default ''"),
            # Чем запитан комплекс: «СЭС» (солнечная электростанция) или
            # «Электросеть»; пусто — не указано (бот спросит у монтёра при
            # взятии комплекса в работу).
            ("power_source", "text not null default ''"),
            # When the point's configured port (or, if it has none, plain
            # ping) last STARTED failing — null while it's currently up.
            # Maintained by app/services/point_health_service.py, driving
            # the "Сначала дольше не работает" sort/дауntime badge — kept
            # separate from point_status_history's status_since since that
            # one reflects the literal status string (which also covers
            # non-network states like "Демонтирован"), not specifically
            # "is the port reachable".
            ("port_down_since", "text"),
            # Когда фоновая/ручная проверка в последний раз ПРОВЕРИЛА эту точку — в карточке «проверено N мин назад»:
            # по ней видно, что проверка идёт, и что она не отстаёт (круг по тысячам точек занимает минуты).
            ("health_checked_at", "text"),
        ):
            await _ensure_column(conn, "points", col, ddl)

        # Free-form, colour-tagged categories for points ("АПК") — same
        # get-or-create-by-name pattern as the Sklad warehouse app's
        # complex_categories: типed freely on the point form, a new name
        # gets its own colour, an existing one reuses its stored colour.
        await conn.execute(
            """create table if not exists point_categories (
                id integer primary key autoincrement,
                name text not null unique,
                color text not null default '#6366f1'
            )"""
        )
        # match_pattern: an optional substring (e.g. "ARENA", "POST",
        # "Метеор") — a point whose id/description contains it (case-
        # insensitively, Cyrillic-safe — matched in Python, not SQL, see
        # point_categories_repository.detect_category_id) gets this
        # category assigned automatically on create/import, or in bulk via
        # POST /api/point-categories/auto-assign.
        await _ensure_column(conn, "point_categories", "match_pattern", "text")
        # Старый/новый комплекс — decides how "Зайти на веб-интерфейс"
        # connects (see ui.js::openPointWebInterface): a legacy complex gets
        # an .rdp file (Windows Remote Desktop), a new one opens straight in
        # the browser with credentials embedded in the URL.
        await _ensure_column(conn, "point_categories", "is_legacy", "integer not null default 0")
        await conn.execute(
            """create table if not exists point_category_fields (
                id integer primary key autoincrement,
                category_id integer references point_categories(id) on delete cascade,
                name text not null,
                field_type text not null default 'text',
                accounting_only integer not null default 0,
                sort_order integer not null default 0,
                created_at text not null,
                unique(category_id, name)
            )"""
        )
        await conn.execute("create index if not exists idx_point_category_fields_category on point_category_fields(category_id, sort_order)")
        await conn.execute("update point_category_fields set accounting_only = 0 where category_id is not null and accounting_only != 0")
        await conn.execute(
            """create table if not exists point_category_field_values (
                point_id text not null references points(id) on delete cascade on update cascade,
                field_id integer not null references point_category_fields(id) on delete cascade,
                value text not null,
                updated_at text not null,
                primary key (point_id, field_id)
            )"""
        )
        await conn.execute("create index if not exists idx_point_category_field_values_field on point_category_field_values(field_id)")
        await conn.execute(
            """create table if not exists consumables (
                id integer primary key autoincrement,
                name text not null,
                unit text not null default 'шт.',
                active integer not null default 1,
                created_at text not null,
                updated_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_consumables_name on consumables(name collate nocase)")
        await conn.execute(
            """create table if not exists consumable_movements (
                id integer primary key autoincrement,
                consumable_id integer not null references consumables(id),
                action text not null check(action in ('receipt', 'issue', 'install', 'return')),
                quantity integer not null check(quantity > 0),
                person_id integer references people(id) on delete set null,
                point_id text references points(id) on delete set null on update cascade,
                task_id text,
                actor text not null default '',
                note text not null default '',
                created_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_consumable_moves_item on consumable_movements(consumable_id, created_at)")
        await conn.execute("create index if not exists idx_consumable_moves_person on consumable_movements(person_id, consumable_id)")
        await conn.execute("create index if not exists idx_consumable_moves_point on consumable_movements(point_id, created_at)")
        await conn.execute(
            "create unique index if not exists idx_consumable_task_install "
            "on consumable_movements(task_id, consumable_id) where action = 'install' and task_id is not null"
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for order, (name, field_type) in enumerate((
            ("Заводской номер", "text"),
            ("Инвентарный номер", "text"),
            ("Наименование в бухгалтерском учете", "text"),
            ("Наименование в паспорте", "text"),
            ("Балансовая стоимость", "currency"),
            ("Местонахождение", "text"),
            ("Год выпуска", "number"),
            ("Дата ввода в эксплуатацию", "date"),
            ("Контракт", "text"),
            ("Ремонтные работы", "textarea"),
        ), start=1):
            cur = await conn.execute(
                "select id from point_category_fields where category_id is null and pylower(name) = pylower(?)",
                (name,),
            )
            if not await cur.fetchone():
                await conn.execute(
                    "insert into point_category_fields (category_id, name, field_type, accounting_only, sort_order, created_at) "
                    "values (null, ?, ?, 1, ?, ?)",
                    (name, field_type, order, now),
                )
        cur = await conn.execute("select count(*) from point_categories")
        (category_count,) = await cur.fetchone()
        if category_count == 0:
            for name, color in (
                ("Камера", "#6366f1"),
                ("Табло", "#0ea5e9"),
                ("Светофор", "#f59e0b"),
                ("Регистратор", "#14b8a6"),
            ):
                await conn.execute(
                    "insert into point_categories (name, color) values (?, ?)", (name, color)
                )

        # Справочник типов выездов («Настройки → Типы выездов»): название и
        # признак «связан с ремонтом» (меню ремонта в боте, «что сняли/поставили»
        # в отчёте). Пустая таблица засевается типами по умолчанию (см.
        # app/core/work_types.py); читает её work_types_repository.load().
        await conn.execute(
            """create table if not exists work_types (
                name text primary key,
                is_repair integer not null default 0,
                sort_order integer not null default 0
            )"""
        )
        # «Проверка комплекса и SIM в боте» — второй переключатель типа (для не связанных с ремонтом): в простом меню
        # бота появляются «Проверка доступности» (без запроса проверки диспетчером) и «Замена SIM-карты».
        await _ensure_column(conn, "work_types", "bot_tools", "integer not null default 0")
        # Статусы выезда по типам (кнопки итога в боте при завершении у не
        # связанных с ремонтом типов) — см. work_types_repository.set_outcomes.
        await conn.execute(
            """create table if not exists work_type_outcomes (
                id integer primary key autoincrement,
                type_name text not null,
                label text not null,
                sort_order integer not null default 0
            )"""
        )
        await conn.execute("create index if not exists idx_wto_type on work_type_outcomes(type_name)")
        cur = await conn.execute("select count(*) from work_types")
        (work_type_rows,) = await cur.fetchone()
        if work_type_rows == 0:
            from app.core.work_types import DEFAULT_WORK_TYPES

            await conn.executemany(
                "insert into work_types (name, is_repair, sort_order) values (?, ?, ?)",
                [(name, int(is_repair), i) for i, (name, is_repair) in enumerate(DEFAULT_WORK_TYPES, start=1)],
            )
        # Общие однократные отметки («это уже делали»), чтобы сделанное один раз при обновлении
        # не повторялось на каждом запуске (например, не возвращало тип, который диспетчер удалил).
        await conn.execute("create table if not exists app_flags (key text primary key, value text not null)")
        cur = await conn.execute("select 1 from app_flags where key = 'aux_work_type'")
        if not await cur.fetchone():
            # Тип «Вспомогательная точка» появился позже остальных — добавляем его в уже существующий справочник.
            from app.core.work_types import AUX_TYPE_NAME

            cur = await conn.execute("select coalesce(max(sort_order), 0) + 1 from work_types")
            (next_order,) = await cur.fetchone()
            await conn.execute(
                "insert or ignore into work_types (name, is_repair, sort_order) values (?, 0, ?)", (AUX_TYPE_NAME, next_order)
            )
            await conn.execute("insert or ignore into app_flags (key, value) values ('aux_work_type', '1')")
        # Managed dictionary of point statuses (replaces the old hardcoded
        # 5-string list duplicated across app.js/warehouse.js/point_health_service).
        # `is_auto` marks statuses the background ping/port checker is allowed
        # to overwrite; `protected` marks statuses that can't be deleted (and,
        # for "На складе" specifically, can't be renamed either — checked in
        # the repository, not here).
        await conn.execute(
            """create table if not exists point_statuses (
                id integer primary key autoincrement,
                name text not null unique,
                color text not null default '#64748b',
                is_auto integer not null default 0,
                protected integer not null default 0,
                created_at text not null
            )"""
        )
        cur = await conn.execute("select count(*) from point_statuses")
        (status_count,) = await cur.fetchone()
        if status_count == 0:
            seed_created_at = dt.datetime.now(dt.timezone.utc).isoformat()
            for name, color, is_auto, protected in (
                ("Работает", "#22c55e", 1, 1),
                ("Есть только пинг", "#f59e0b", 1, 1),
                ("Не работает", "#f43f5e", 1, 1),
                # «На складе» — статус один; «Частично на складе» — только подпись
                # в интерфейсе, когда не все компоненты на складе (core/warehouse.py).
                ("На складе", "#64748b", 0, 1),
                ("Установлен", "#38bdf8", 0, 0),
                # Не is_auto: не входят в тройку, которую фоновая проверка
                # выставляет сама по пингу/порту (Работает/Есть только
                # пинг/Не работает) — их ставит только монтёр через бота.
                # «Нет питания» тем не менее продолжает пинговаться (см.
                # point_health_service.py — отдельный частный случай, не
                # завязанный на is_auto) и сама уходит в обычный статус,
                # как только пинг снова отвечает. «Не подведено питание»
                # пропускается фоновой проверкой полностью, как «На складе».
                ("Нет питания", "#fb923c", 0, 1),
                ("Не подведено питание", "#a855f7", 0, 1),
            ):
                await conn.execute(
                    "insert into point_statuses (name, color, is_auto, protected, created_at) values (?, ?, ?, ?, ?)",
                    (name, color, is_auto, protected, seed_created_at),
                )
        else:
            # An install that already had point_statuses rows (i.e. every
            # install before this turn) never runs the seed loop above, so
            # "Установлен" needs its own one-time backfill — insert only if
            # missing, so this stays a no-op on every later boot.
            await conn.execute(
                "insert into point_statuses (name, color, is_auto, protected, created_at) "
                "select 'Установлен', '#38bdf8', 0, 0, ? where not exists "
                "(select 1 from point_statuses where name = 'Установлен')",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            )
            backfill_at = dt.datetime.now(dt.timezone.utc).isoformat()
            for name, color, protected in (
                ("Нет питания", "#fb923c", 1), ("Не подведено питание", "#a855f7", 1),
            ):
                await conn.execute(
                    "insert into point_statuses (name, color, is_auto, protected, created_at) "
                    "select ?, ?, 0, ?, ? where not exists (select 1 from point_statuses where name = ?)",
                    (name, color, protected, backfill_at, name),
                )
        # Комплекс загрузили со статусом «Работает», а SIM или IP не указаны — проверить связь нечем, «Работает» из таблицы ничем не подтверждено. Такому комплексу нужен
        # существующий статус «Не известно» (пустой статус). Уже загруженные переводятся один раз (флаг unknown_status_v1): «Работает» без IP, либо без SIM, если фоновая
        # проверка ещё ни разу не подтверждала связь. Пустой статус фоновая проверка сама заменяет настоящим, как только есть IP. Дальше правило применяют загрузки (routes_excel, sklad_import).
        cur = await conn.execute("select 1 from app_flags where key = 'unknown_status_v1'")
        if not await cur.fetchone():
            await conn.execute(
                "update points set status = '' where status = 'Работает' and ("
                "trim(coalesce(ip_address, '')) = '' or (trim(coalesce(sim_number, '')) = '' and coalesce(health_checked_at, '') = ''))"
            )
            await conn.execute("insert or ignore into app_flags (key, value) values ('unknown_status_v1', '1')")
        # «Частично на складе» была отдельным статусом (недолго) — теперь это
        # подпись по компонентам, статус один «На складе». Точки со старым
        # статусом переводим в «На складе» (их компоненты уже отмечены, так что
        # подпись «Частично» им выставится сама), строку словаря убираем.
        cur = await conn.execute("select 1 from point_statuses where name = 'Частично на складе' and protected = 1")
        if await cur.fetchone():
            await conn.execute("update points set status = 'На складе' where status = 'Частично на складе'")
            await conn.execute("delete from point_statuses where name = 'Частично на складе' and protected = 1")
        # "Демонтирована" is retired — the визит-report flow (result "Демонтирован")
        # now moves a point straight to "На складе" and logs the fact in
        # task_visit_reports instead of leaving a standing status. Confirmed
        # zero points used this status in production before removing it; not
        # protected, so a plain delete (not a data migration) is enough.
        await conn.execute("delete from point_statuses where name = 'Демонтирована' and protected = 0")

        # Warehouse-only per-point data: which sub-components (box/camera/
        # board) are physically on the shelf and their condition, plus the
        # "паутина" registry status and receipt date. One row per point,
        # only meaningful while that point's status is "На складе" — deleted
        # by points_repository as soon as the point leaves that status.
        await conn.execute(
            """create table if not exists warehouse_items (
                point_id text primary key references points(id) on delete cascade on update cascade,
                box_state text,
                camera_state text,
                board_state text,
                web_status text,
                received_at text,
                info text,
                updated_at text not null
            )"""
        )

        # Компоненты комплекса (камера, ящик, плата… — сколько угодно, каждый со
        # своей отметкой «на складе» и описанием): заменяют прежние готовые поля
        # warehouse_items.box_state/camera_state/board_state. Статус точки
        # («На складе» — если хоть что-то на складе) считается по ним — см.
        # point_components_repository.recompute_status.
        await conn.execute(
            """create table if not exists point_components (
                id integer primary key autoincrement,
                point_id text not null references points(id) on delete cascade on update cascade,
                name text not null,
                in_stock integer not null default 0,
                description text not null default '',
                sort_order integer not null default 0
            )"""
        )
        await conn.execute("create index if not exists idx_pc_point on point_components(point_id)")
        # Один раз: старые поля «Ящик / Камера / Плата» → компоненты. Пустое поле
        # у точки, где другое заполнено, — компонент, которого на складе нет
        # (точка тогда показывается «Частично на складе»). Только для точек без компонентов,
        # поэтому повторный запуск ничего не дублирует.
        cur = await conn.execute(
            "select point_id, box_state, camera_state, board_state from warehouse_items w "
            "where (coalesce(box_state, '') != '' or coalesce(camera_state, '') != '' or coalesce(board_state, '') != '') "
            "and not exists (select 1 from point_components c where c.point_id = w.point_id)"
        )
        for point_id, box, camera, board in await cur.fetchall():
            parts = (("Ящик", box), ("Камера", camera), ("Плата", board))
            for order, (comp_name, state) in enumerate(parts, start=1):
                await conn.execute(
                    "insert into point_components (point_id, name, in_stock, description, sort_order) values (?, ?, ?, ?, ?)",
                    (point_id, comp_name, 1 if state else 0, state or "", order),
                )

        # Every automatic (background health check), manual (edit form /
        # warehouse status change) and visit-report-driven change to a
        # point's status, for the statistics page's "N complexes went
        # down/recovered over period" metrics and as a general audit trail.
        # point_id is a loose text reference (like tasks.point) — points can
        # be renamed/deleted independently, old history rows just become
        # historical facts about a point id that may no longer exist.
        await conn.execute(
            """create table if not exists point_status_history (
                id integer primary key autoincrement,
                point_id text not null,
                old_status text not null default '',
                new_status text not null default '',
                source text not null,
                changed_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_psh_point on point_status_history(point_id)")
        await conn.execute("create index if not exists idx_psh_changed_at on point_status_history(changed_at)")
        # Ties a source='visit_report' row back to the task whose report
        # caused it — lets deleting that report (task_visit_reports_repository
        # .delete_for_task) also undo its footprint on the stats page instead
        # of leaving a "recovered"/"went down" transition behind forever.
        await _ensure_column(conn, "point_status_history", "task_id", "text")
        await conn.execute("create index if not exists idx_psh_task on point_status_history(task_id)")
        # Страница «Статистика» (app/repositories/stats_repository.py): «с какого момента комплекс в текущем статусе» и подсчёт
        # восстановлений — без этих индексов каждый запрос просматривал всю историю статусов (секунды на десятках тысяч записей).
        await conn.execute(
            "create index if not exists idx_psh_point_status_at on point_status_history(point_id, new_status, changed_at)"
        )
        await conn.execute(
            "create index if not exists idx_psh_recovery on point_status_history(new_status, old_status, changed_at)"
        )

        # One visit report per task — what was actually done on the point
        # that task was for (see task_visit_reports_repository.py). A
        # "Демонтирован" result writes box/camera/board condition straight
        # into warehouse_items as the point moves to "На складе" (see
        # upsert() there), so this table itself only stores the report text.
        await conn.execute(
            """create table if not exists task_visit_reports (
                id integer primary key autoincrement,
                task_id text not null unique,
                point_id text not null,
                visit_type text not null,
                result_status text not null,
                comment text not null default '',
                equipment_removed text,
                equipment_installed text,
                created_at text not null,
                updated_at text not null,
                created_by text
            )"""
        )
        await conn.execute("create index if not exists idx_tvr_point on task_visit_reports(point_id)")

        # Статус выезда (итог работ из «Настройки → Типы выездов»: «Найден» /
        # «Не найден»…) — только текст для отчётов и статистики, с состоянием
        # комплекса не связан (result_status у такого отчёта пуст).
        await _ensure_column(conn, "task_visit_reports", "outcome", "text not null default ''")
        # Разбивки «Статистики» по периоду читают только эти столбцы — индекс покрывает запрос целиком (создаётся после
        # столбца outcome: на пустой базе он появляется строкой выше).
        await conn.execute(
            "create index if not exists idx_tvr_created on task_visit_reports(created_at, visit_type, result_status, outcome)"
        )
        # Комплекс не заработал: что именно неисправно и что нужно для исправления (отдельно от текста отчёта) —
        # уходит в итоги, выгрузки и «Уведомления» диспетчера (см. app/api/routes_notifications.py).
        await _ensure_column(conn, "task_visit_reports", "fault", "text")
        await _ensure_column(conn, "task_visit_reports", "needs", "text")
        # Автор отчёта — привязка к человеку из «Персонала» (а не к логину/VK id): в системе показывается его ФИО.
        # Правка отчёта автора не меняет; кто правил последним — updated_by.
        await _ensure_column(conn, "task_visit_reports", "author_person_id", "integer")
        await _ensure_column(conn, "task_visit_reports", "updated_by", "text")
        await conn.execute(
            """update task_visit_reports set author_person_id = (
                   select p.id from people p
                   where p.full_name = case when substr(task_visit_reports.created_by, 1, 3) = 'vk:' then substr(task_visit_reports.created_by, 4)
                                            else task_visit_reports.created_by end
                     and (select count(*) from people q where q.full_name = p.full_name) = 1)
               where author_person_id is null and coalesce(created_by, '') != ''"""
        )
        # История выездов, загруженная из Excel (app/services/visit_history_import.py): запись без
        # задания в календаре (task_id вида «imp-…»), состояние комплекса и статистику падений она
        # не меняет — это справка о том, что было раньше. import_batch — номер загрузки, чтобы её
        # можно было отменить целиком.
        await _ensure_column(conn, "task_visit_reports", "imported", "integer not null default 0")
        await _ensure_column(conn, "task_visit_reports", "import_batch", "text not null default ''")
        # SIM-карта в отчёте — отдельные поля, а не строка среди «снятого/установленного оборудования»: какая SIM снята,
        # какая поставлена и в каком состоянии снятая («working» — рабочая, «defective» — нерабочая, пусто — не указано).
        await _ensure_column(conn, "task_visit_reports", "sim_removed", "text")
        await _ensure_column(conn, "task_visit_reports", "sim_installed", "text")
        await _ensure_column(conn, "task_visit_reports", "sim_removed_state", "text not null default ''")
        # Откуда отчёт: пусто — бот/диспетчер, «forms» — загружен из таблицы Яндекс Форм (app/services/forms_import.py).
        await _ensure_column(conn, "task_visit_reports", "source", "text not null default ''")

        # Строки таблицы Яндекс Форм (отчёты, которые монтёры всё ещё сдают через форму): что в них было, к какому комплексу
        # и заданию строка привязана и как идёт скачивание приложенных фото (оно идёт через браузер робота карт — файлы форм
        # открываются только создателю формы). Один и тот же файл можно загружать повторно: строка узнаётся по row_hash.
        await conn.execute(
            """create table if not exists forms_import_rows (
                id integer primary key autoincrement,
                row_hash text not null unique,
                batch text not null default '',
                form_time text not null default '',
                raw_point text not null default '',
                point_id text,
                task_id text,
                task_date text,
                crew_text text not null default '',
                status_text text not null default '',
                result_text text not null default '',
                power_text text not null default '',
                sim_text text not null default '',
                location_text text not null default '',
                inventory_text text not null default '',
                equipment_text text not null default '',
                photo_urls text not null default '[]',
                photos_total integer not null default 0,
                photos_done integer not null default 0,
                photos_state text not null default '',
                photos_error text not null default '',
                outcome text not null default '',
                outcome_note text not null default '',
                created_at text not null,
                updated_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_fir_task on forms_import_rows(task_id)")
        await conn.execute("create index if not exists idx_fir_batch on forms_import_rows(batch)")

        # Manually-entered fields for report/document templates the app
        # can't fully compute on its own (staff headcount, vehicles, the
        # план-наряд letterhead) — see report_values_repository.py.
        # `period` is a date for a per-day report (e.g. the "ежедневный
        # отчёт") or '' for a global, non-dated template (план-наряд).
        await conn.execute(
            """create table if not exists report_field_values (
                report_type text not null,
                period text not null,
                field_key text not null,
                value text not null default '',
                updated_at text not null,
                updated_by text,
                primary key (report_type, period, field_key)
            )"""
        )
        await conn.execute(
            "create index if not exists idx_rfv_type_key on report_field_values(report_type, field_key, period)"
        )

        # Operator-uploaded .docx/.xlsx report templates — the file itself
        # (with {{variable}} placeholders) lives on disk under
        # data_dir/report_templates/{id}.{ext}; see
        # report_templates_repository.py and app/services/template_engine.py
        # for how it's filled in and rendered on demand.
        await conn.execute(
            """create table if not exists report_templates (
                id integer primary key autoincrement,
                name text not null,
                file_type text not null,
                mode text not null,
                variables text not null default '[]',
                created_at text not null,
                created_by text
            )"""
        )
        await _ensure_column(conn, "report_templates", "visibility", "text not null default 'public'")
        await _ensure_column(conn, "report_templates", "shared_users", "text not null default '[]'")

        # Отчёты, собранные в онлайн-конструкторе (/reports/builder): сам шаблон —
        # JSON с секциями/абзацами/таблицами и переменными {{…}} (см.
        # app/models/report_design.py), формат файла (xlsx/docx) выбирается при
        # создании. Лежит в БД, поэтому попадает в ежедневные бэкапы.
        await conn.execute(
            """create table if not exists report_designs (
                id integer primary key autoincrement,
                name text not null,
                format text not null,
                design text not null,
                created_at text not null,
                updated_at text not null,
                created_by text
            )"""
        )
        await _ensure_column(conn, "report_designs", "visibility", "text not null default 'public'")
        await _ensure_column(conn, "report_designs", "shared_users", "text not null default '[]'")

        # "Монтёр is actively on this task right now" — started by the VK
        # bot when a fitter opens a task, ended when they submit/confirm.
        # See app/repositories/bot_work_sessions_repository.py and the
        # /monitoring page's "Сейчас в работе" section.
        await conn.execute(
            """create table if not exists bot_work_sessions (
                id integer primary key autoincrement,
                task_id text not null,
                point_id text not null,
                person_id integer not null,
                started_at text not null,
                ended_at text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_bws_task on bot_work_sessions(task_id)"
        )
        await conn.execute(
            "create index if not exists idx_bws_active on bot_work_sessions(ended_at)"
        )
        # Что уже сделано по комплексу, пока на нём монтёры (текст отчёта, замена оборудования, итоги проверок) —
        # общее для бригады и переживает выход монтёра с комплекса и перезапуск бота. Удаляется, когда отчёт сдан.
        # См. app/repositories/bot_task_progress_repository.py.
        # Чат монтёра с диспетчером (VK-бот ↔ окно «Чаты» в веб-панели): история живёт только здесь,
        # см. app/repositories/chat_repository.py.
        await conn.execute(
            """create table if not exists chat_messages (
                id integer primary key autoincrement,
                person_id integer not null references people(id) on delete cascade,
                direction text not null,
                text text not null,
                author text,
                point_id text,
                task_id text,
                created_at text not null,
                read_at text,
                claimed_at text,
                delivered_at text,
                attempts integer not null default 0,
                delivery_error text,
                failed_at text
            )"""
        )
        # Отметки диспетчера «это не ошибка» на вкладке «Ошибки данных» (уведомления): ничего не удаляют и не скрывают
        # навсегда — signature хранит состав группы на момент отметки, при изменении состава запись снова всплывает.
        # См. app/repositories/data_issues_repository.py.
        await conn.execute(
            """create table if not exists data_issue_ack (
                kind text not null,
                issue_key text not null,
                signature text not null default '',
                acked_by text not null default '',
                acked_at text not null,
                primary key (kind, issue_key)
            )"""
        )
        # Связки комплексов с общей SIM (см. app/repositories/point_links_repository.py): комплексы одной связки (group_id)
        # получают новый номер SIM, когда он меняется у любого из них. point_id — текстовая ссылка, как у остальных таблиц.
        await conn.execute(
            """create table if not exists point_sim_links (
                point_id text primary key,
                group_id integer not null
            )"""
        )
        await conn.execute("create index if not exists idx_point_sim_links_group on point_sim_links(group_id)")
        # Фото в сообщениях чата (до 10 на сообщение): файлы — на диске сервера (app/services/chat_files.py); переписка и файлы
        # живут неделю (chat_repository.purge_old).
        await conn.execute(
            """create table if not exists chat_attachments (
                id integer primary key autoincrement,
                message_id integer not null references chat_messages(id) on delete cascade,
                file_ext text not null,
                file_name text,
                size integer not null default 0,
                created_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_chat_att_msg on chat_attachments(message_id)")
        # Массовая рассылка монтёрам в бота (chat_repository.add_broadcast): заголовок рассылки здесь, а каждому получателю — своё
        # исходящее сообщение чата (chat_messages.broadcast_id), так что доставка, повторы и статусы те же, что у ответов диспетчера.
        # Фото рассылки лежит на диске одним файлом: у вложений получателей src_id указывает на вложение-владельца файла.
        await conn.execute(
            """create table if not exists chat_broadcasts (
                id integer primary key autoincrement,
                created_at text not null,
                author text not null default '',
                text text not null default '',
                mode text not null default 'pick',
                filter_desc text not null default '',
                total integer not null default 0,
                token text unique
            )"""
        )
        await _ensure_column(conn, "chat_messages", "broadcast_id", "integer")
        await _ensure_column(conn, "chat_attachments", "src_id", "integer")
        await conn.execute("create index if not exists idx_chat_msg_broadcast on chat_messages(broadcast_id)")
        await conn.execute("create index if not exists idx_chat_att_src on chat_attachments(src_id)")
        # Задачи ИИ (расшифровка голоса, вопрос помощнику, разбор и заполнение шаблона): идут в фоне — модель на процессоре отвечает десятки
        # секунд — и опрашиваются по номеру. Файлы шаблонов и результатов — на диске (data_dir/ai_files), живут несколько дней.
        # См. app/services/ai/jobs.py.
        await conn.execute(
            """create table if not exists ai_jobs (
                id text primary key,
                kind text not null,
                owner text not null,
                status text not null,
                created_at text not null,
                started_at text,
                finished_at text,
                request text not null default '{}',
                result text,
                error text
            )"""
        )
        await conn.execute("create index if not exists idx_ai_jobs_owner on ai_jobs(owner, created_at)")
        await conn.execute(
            """create table if not exists ai_files (
                id text primary key,
                owner text not null,
                kind text not null,
                name text not null,
                ext text not null,
                size integer not null default 0,
                created_at text not null
            )"""
        )
        # Сколько раз задачу начинали (после перезапуска сервера недоделанный вопрос выполняется заново — не больше трёх раз подряд).
        await _ensure_column(conn, "ai_jobs", "attempts", "integer not null default 0")
        # Переписка с помощником (страница «🤖 Помощник») лежит на сервере, а не в браузере: ответ дожидается человека, даже если связь оборвалась
        # или страницу закрыли; открыть можно с любого компьютера. Текст вопроса и ответ (в нём бывают пароли комплексов) — зашифрованы.
        await conn.execute(
            """create table if not exists ai_chat (
                id integer primary key autoincrement,
                owner text not null,
                role text not null,
                text text not null default '',
                job_id text,
                status text not null default 'done',
                answer text,
                error text,
                client_id text,
                created_at text not null,
                finished_at text
            )"""
        )
        await conn.execute("create index if not exists idx_ai_chat_owner on ai_chat(owner, id)")
        await conn.execute("create index if not exists idx_ai_chat_job on ai_chat(job_id)")
        await conn.execute("create unique index if not exists idx_ai_chat_client on ai_chat(owner, client_id) where client_id is not null")
        # Разобранные вопросы: повторный вопрос (то же слово в слово) не отправляется в модель, а берёт готовый разбор — ответ мгновенный
        # (данные при этом считаются заново). См. app/services/ai/plan_cache.py.
        await conn.execute(
            """create table if not exists ai_plan_cache (
                key text primary key,
                spec text not null,
                created_at text not null,
                last_used text not null,
                hits integer not null default 0
            )"""
        )
        await conn.execute("create index if not exists idx_chat_person on chat_messages(person_id, id)")
        await conn.execute("create index if not exists idx_chat_pending on chat_messages(direction, delivered_at)")
        await conn.execute(
            """create table if not exists bot_task_progress (
                task_id text primary key,
                state text not null default '{}',
                updated_at text not null,
                updated_by text
            )"""
        )

        # Photos a fitter sends through the bot for a visit — one row per
        # file, the file itself on disk under
        # data_dir/visit_photos/<point_id>/<taken_date>/{id}.{ext}. See
        # app/repositories/visit_photos_repository.py.
        await conn.execute(
            """create table if not exists visit_photos (
                id integer primary key autoincrement,
                task_id text not null,
                point_id text not null,
                taken_date text not null,
                file_ext text not null,
                uploaded_at text not null,
                uploaded_by text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_vp_point_date on visit_photos(point_id, taken_date)"
        )
        # Фото, которые уже лежали на сетевом диске до программы («прежний архив»): их находит поиск по папкам комплексов
        # (app/services/photo_index.py). rel_path — путь файла внутри папки хранилища как он есть на диске; у фото из
        # бота пусто (путь выводится из номера комплекса, даты и номера фото).
        for col, ddl in (("rel_path", "text"), ("source", "text"), ("file_name", "text"), ("file_size", "integer")):
            await _ensure_column(conn, "visit_photos", col, ddl)
        await conn.execute(
            "create unique index if not exists idx_vp_rel_path on visit_photos(rel_path) where rel_path is not null"
        )

        # A ping/port recheck coming back "reachable" after a Ремонт/
        # Установка report isn't proof the complex actually works end to
        # end — this queues that for a human (operator/admin) to confirm
        # or flag, and lets the bot's conversation with the fitter wait on
        # the answer. See verification_requests_repository.py, routes_bot.py
        # and routes_verification_requests.py.
        await conn.execute(
            """create table if not exists verification_requests (
                id integer primary key autoincrement,
                task_id text not null,
                point_id text not null,
                created_at text not null,
                status text not null default 'pending',
                resolved_at text,
                resolved_by text,
                issue_comment text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_vr_status on verification_requests(status)"
        )

        # A stranger messaging the bot can ask to be linked instead of the
        # dispatcher having to fish their VK ID out of them by hand — one
        # row per vk_id (re-requesting just bumps requested_at, subject to
        # a cooldown in bot_join_requests_repository.py), 'blocked' is how
        # the web panel silences a bogus/unwanted vk_id for good. Resolved
        # (linked into `people`) rows are deleted, not kept, so this table
        # only ever holds what still needs a decision.
        await conn.execute(
            """create table if not exists bot_join_requests (
                id integer primary key autoincrement,
                vk_id integer not null unique,
                vk_name text,
                status text not null default 'pending',
                requested_at text not null,
                resolved_at text,
                resolved_by text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_bjr_status on bot_join_requests(status)"
        )

        # Single-row on/off switch for the VK bot — the org runs it from two
        # places (the org server's Docker container, and sometimes a
        # developer's laptop for testing) and VK's group Long Poll happily
        # hands the same updates to both pollers, so two live instances
        # double-reply and diverge in their in-memory navigation state. The
        # bot checks this on every incoming event (AuthMiddleware) and goes
        # fully silent — no replies, no lan_calendar writes — when disabled,
        # so only one instance is ever actually "live" regardless of how
        # many processes are polling. See bot_settings_repository.py.
        await conn.execute(
            """create table if not exists bot_settings (
                id integer primary key check (id = 1),
                enabled integer not null default 1
            )"""
        )
        await conn.execute(
            "insert or ignore into bot_settings (id, enabled) values (1, 1)"
        )

        # Ремонт/Установка: если после ввода SIM всё ещё нет доступа к
        # комплексу, монтёр звонит диспетчеру напрямую (вне бота), и уже
        # диспетчер решает с веб-панели — отпустить бригаду без ремонта, или
        # оформить демонтаж (с указанием, что именно демонтировать). Бот
        # ждёт этого решения так же, как ждёт verification_requests. Разные
        # таблицы, а не общая с verification_requests — это принципиально
        # другое решение (не "починили или нет", а "чинить сейчас или нет").
        await conn.execute(
            """create table if not exists access_issue_requests (
                id integer primary key autoincrement,
                task_id text not null,
                point_id text not null,
                created_at text not null,
                status text not null default 'pending',
                resolved_at text,
                resolved_by text,
                demontage_note text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_air_status on access_issue_requests(status)"
        )

        # Монтёр сам просит демонтаж (кнопка в меню Ремонт/Установка) с
        # указанием причины — в отличие от access_issue_requests (которая
        # реагирует на "нет доступа" и решает диспетчер), эту заявку
        # СОЗДАЁТ монтёр, и диспетчер явно одобряет/отклоняет (не просто
        # выбирает между двумя исходами реактивно). См. routes_bot.py
        # (bot-key: create/get) и routes_demontage_requests.py
        # (session-authed: list/resolve, плюс кнопка "Демонтаж" из
        # /monitoring "Сейчас в работе" создаёт такую же заявку сразу
        # одобренной, initiated_by='dispatcher').
        await conn.execute(
            """create table if not exists demontage_requests (
                id integer primary key autoincrement,
                task_id text not null,
                point_id text not null,
                reason text not null default '',
                initiated_by text not null default 'fitter',
                created_at text not null,
                status text not null default 'pending',
                resolved_at text,
                resolved_by text,
                decline_comment text
            )"""
        )
        await conn.execute(
            "create index if not exists idx_dr_status on demontage_requests(status)"
        )
        # 'demontage' (default, backward-compatible) or 'release' — a fitter
        # stuck with no access at «Завершить» can ask the dispatcher to
        # either approve a demontage or approve releasing the crew without
        # repair, both through this same pending→approved/declined request
        # instead of two parallel tables (see bot/service/mechanic.py's
        # repair_finish/_poll_demontage_request).
        await _ensure_column(conn, "demontage_requests", "kind", "text not null default 'demontage'")

        # Реестр «какая SIM сейчас на каком IP» — независимо от того, к
        # какому комплексу она сейчас подключена. Загружается/обновляется
        # отдельным Excel-импортом (app/services/excel_service.py,
        # SIM_IP_REGISTRY_HEADERS), а IP из него автоматически
        # проставляется точкам с такой же SIM — и при самом импорте, и
        # когда монтёр через бота указывает/меняет SIM на точке (см.
        # routes_bot.py::set_point_sim). sim_number хранится уже
        # нормализованным (последние 7 цифр) — тем же способом, что и
        # points.sim_number.
        await conn.execute(
            """create table if not exists sim_ip_registry (
                sim_number text primary key,
                ip_address text not null,
                updated_at text not null
            )"""
        )

        await conn.execute(
            """create table if not exists tasks (
                id text primary key,
                date text not null,
                team integer not null,
                point text not null default '',
                work text not null default '',
                address text not null default '',
                hours real not null default 0,
                travel real not null default 0,
                note text not null default '',
                trip_id text,
                date_to text
            )"""
        )
        for col, ddl in (
            ("trip_id", "text"), ("date_to", "text"), ("type", "text not null default ''"),
            ("priority", "integer not null default 0"),
            # Координаты «вспомогательной точки» (см. app/core/work_types.py::AUX_TYPE_NAME) — у
            # обычного задания координаты берутся из справочника точек, у вспомогательной своих нет.
            ("aux_lat", "text not null default ''"), ("aux_lon", "text not null default ''"),
        ):
            await _ensure_column(conn, "tasks", col, ddl)
        # `status` (Запланировано/В работе/...) is retired in favour of
        # `type` (Ремонт/Установка/... — the same work-type vocabulary a
        # visit report used to ask for separately, see work_types.py) so a
        # dispatcher fixes the work type up front instead of the installer
        # picking it after the fact. Only present on a pre-existing install.
        await _drop_column_if_exists(conn, "tasks", "status")
        await conn.execute("create index if not exists idx_tasks_date on tasks(date)")
        # «Был ли на комплексе ремонт, пока он не работал» (статистика восстановлений) — по комплексу и типу работ.
        await conn.execute("create index if not exists idx_tasks_point_type_date on tasks(point, type, date)")
        # Вспомогательные точки, которые монтёр отметил в боте кнопкой «Принято» (см. work_types.AUX_TYPE_NAME):
        # отчёта о выезде у них нет, поэтому «выполнено» хранится отдельно — и не затирается, когда календарь
        # целиком пересохраняет список заданий (write_state). На карте бригады такая точка зелёная.
        await conn.execute(
            """create table if not exists aux_task_done (
                task_id text primary key,
                done_at text not null,
                done_by text
            )"""
        )
        await conn.execute(
            """create table if not exists day_team_status (
                date text not null,
                team integer not null,
                active integer not null default 0,
                equip text not null default 'Лестница',
                primary key (date, team)
            )"""
        )
        await conn.execute(
            """create table if not exists state_rev (
                id integer primary key check (id = 1),
                rev integer not null
            )"""
        )
        await conn.execute(
            """create table if not exists crews (
                id integer primary key autoincrement,
                date text not null,
                name text not null default '',
                equip text not null default 'Лестница'
            )"""
        )
        await conn.execute("create index if not exists idx_crews_date on crews(date)")
        # Свободная ссылка/заметка для бригады на этот день — например,
        # маршрут Яндекс.Карт по сегодняшним точкам; показывается монтёру
        # в боте в «Мои задания на сегодня» (см. routes_bot.py::list_my_tasks).
        await _ensure_column(conn, "crews", "map_link", "text not null default ''")
        # Многодневная командировка: N per-day crew rows (one per date in
        # range, same composition) share a trip_id — see app/core/trips.py
        # and crew_repository.start_trip/cancel_trip. Replaces the old
        # tasks.trip_id/date_to scheme (kept below only so pre-existing
        # historical trip tasks still round-trip correctly).
        await _ensure_column(conn, "crews", "trip_id", "text")
        await conn.execute("create index if not exists idx_crews_trip on crews(trip_id)")
        # Место работ бригады («г. Тихорецк», «ст. Северская», «по дороге
        # А-147») — идёт в строку «…на местах дислокации …» план-задания и в
        # текст для копирования по бригаде; пусто → для бланка берётся общая
        # подпись маршрута из настроек.
        await _ensure_column(conn, "crews", "place", "text not null default ''")
        await conn.execute(
            """create table if not exists crew_members (
                crew_id integer not null references crews(id) on delete cascade,
                person_id integer not null references people(id),
                primary key (crew_id, person_id)
            )"""
        )

        # Карты бригад в Яндекс.Конструкторе, созданные роботом (см. app/services/yandex_map_sync.py):
        # владелец карты — бригада (или вся командировка), по записи видно, что именно лежит на
        # карте (rows_json) и когда её удалять. Настройки робота — отдельная таблица ключ/значение.
        await conn.execute(
            """create table if not exists yandex_maps (
                id integer primary key autoincrement,
                owner_key text not null unique,
                crew_id integer not null,
                map_id text not null,
                url text not null,
                name text not null default '',
                rows_json text not null default '',
                signature text not null default '',
                work_date text not null default '',
                created_at text not null,
                updated_at text not null
            )"""
        )
        await conn.execute("create index if not exists idx_yandex_maps_map on yandex_maps(map_id)")
        # Карты, которые надо удалить из Конструктора (заменённые, у удалённых бригад; удаление не вышло — повтор).
        await conn.execute(
            """create table if not exists yandex_trash (
                map_id text primary key,
                reason text not null default '',
                created_at text not null
            )"""
        )
        # «Живые карты» (см. app/repositories/live_maps_repository.py): именованные карты в Конструкторе, которые
        # приложение само обновляет по таймеру — цвет точек следует за статусами комплексов.
        await conn.execute(
            """create table if not exists live_maps (
                id integer primary key autoincrement,
                name text not null,
                kind text not null,
                params_json text not null default '',
                membership text not null default 'fixed',
                members_json text not null default '',
                interval_minutes integer not null default 5,
                enabled integer not null default 1,
                map_id text not null default '',
                url text not null default '',
                rows_json text not null default '',
                signature text not null default '',
                synced_name text not null default '',
                points_count integer not null default 0,
                skipped integer not null default 0,
                last_check_ts real not null default 0,
                last_sync text not null default '',
                last_error text not null default '',
                created_by text,
                created_at text not null,
                updated_at text not null
            )"""
        )
        await conn.execute(
            """create table if not exists yandex_robot_settings (
                key text primary key,
                value text not null
            )"""
        )

        # "Планы" — точки, замеченные оператором как нуждающиеся в выезде,
        # но ещё не привязанные ни к какой бригаде/дате. Живут отдельно от
        # tasks; при добавлении задания оператор выбирает точку из плана
        # (routes_point_plans.py), и запись удаляется — план "расходуется"
        # в задание, а не копируется.
        await conn.execute(
            """create table if not exists point_plans (
                id integer primary key autoincrement,
                point_id text not null,
                description text not null default '',
                created_at text not null,
                created_by text not null default ''
            )"""
        )
        # Приоритет — отмечается при добавлении точки в план или прямо в
        # задании; не меняет само описание/work нигде в приложении, только
        # добавляет пометку и жёлтый маркер в Excel-экспорте для
        # Яндекс.Конструктора карт (см. excel_service.py::build_yandex_export).
        await _ensure_column(conn, "point_plans", "priority", "integer not null default 0")
        # Группа плана (подпись, под которой точки собраны вместе) и заранее выбранный тип работы: оба не зависят друг от друга —
        # тип можно поставить и точке в группе, и точке без группы; потом всю группу записывают на бригаду одним действием.
        await _ensure_column(conn, "point_plans", "group_name", "text not null default ''")
        await _ensure_column(conn, "point_plans", "work_type", "text not null default ''")

        # «Подгруппы АПК» («Все АПК» → подгруппы): именованные подборки комплексов — вручную или из файла Яндекс.Карт. У каждого комплекса в подгруппе
        # СВОИ описание (note) и «что нужно на выезде» (need) и тип работы: они не связаны с описанием в карточке комплекса. Комплекс может быть в нескольких
        # подгруппах (у каждой свои тексты). Номер комплекса — текстовая ссылка (как в point_plans), переименование и слияние дублей её обновляют.
        await conn.execute(
            """create table if not exists apk_groups (
                id integer primary key autoincrement,
                name text not null,
                description text not null default '',
                created_at text not null,
                created_by text not null default ''
            )"""
        )
        await conn.execute(
            """create table if not exists apk_group_points (
                group_id integer not null references apk_groups(id) on delete cascade,
                point_id text not null,
                note text not null default '',
                need text not null default '',
                work_type text not null default '',
                added_at text not null default '',
                primary key (group_id, point_id)
            )"""
        )
        await conn.execute("create index if not exists idx_agp_point on apk_group_points(point_id)")
        # kind: '' — обычная подгруппа; 'unrepaired' — автоматическая «Требуют повторного выезда» (app/services/apk_group_auto.py): состав ведёт программа.
        await _ensure_column(conn, "apk_groups", "kind", "text not null default ''")
        # auto_remove: тумблер подгруппы «убирать комплексы после выезда» — как только по комплексу сдан отчёт о выезде (позже, чем он попал в подгруппу), он сам уходит из неё.
        await _ensure_column(conn, "apk_groups", "auto_remove", "integer not null default 0")
        # Файлы при комплексе (карточка комплекса): документы, фото, схемы. Сам файл — на диске сервера (data/point_files/<id>.dat), здесь только сведения (app/services/point_files.py).
        await conn.execute(
            """create table if not exists point_files (
                id integer primary key autoincrement,
                point_id text not null,
                name text not null,
                ext text not null default '',
                kind text not null default 'other',
                mime text not null default 'application/octet-stream',
                size integer not null default 0,
                comment text not null default '',
                uploaded_at text not null,
                uploaded_by text not null default ''
            )"""
        )
        await conn.execute("create index if not exists idx_pf_point on point_files(point_id)")
        # «Статистика» → вкладка «Счётчики»: именованные счётчики с набором фильтров (JSON: что считаем, период, категории, статусы, поверка, выезды, смены статуса…).
        # Считаются при каждом открытии вкладки (app/services/stat_counters.py), числа нигде не хранятся.
        await conn.execute(
            """create table if not exists stat_counters (
                id integer primary key autoincrement,
                name text not null,
                filters text not null default '{}',
                sort_order integer not null default 0,
                created_at text not null,
                created_by text not null default ''
            )"""
        )
        # ИИ разобрал текст итога из отчёта Яндекс Формы на «что неисправно» и «что нужно» (app/services/ai/forms_fault.py): что получилось и откуда; повторно отчёт не разбирается.
        # status: ok (записано), empty (в тексте нечего достать), rejected (ответ не опирается на текст — отброшен), skipped (поля правили руками), error (сбой модели).
        await conn.execute(
            """create table if not exists report_ai_fault (
                task_id text primary key,
                point_id text not null default '',
                analyzed_at text not null,
                status text not null,
                cause text not null default '',
                needs text not null default '',
                source_text text not null default '',
                error text not null default '',
                attempts integer not null default 0
            )"""
        )
        # ИИ-агенты на компьютерах сотрудников (app/services/ai/agents.py): список подключённых компьютеров и ключи подключения (хранятся только хеши; токен агента и ключ показываются один раз).
        await conn.execute(
            """create table if not exists ai_agents (
                id text primary key,
                name text not null,
                token_hash text not null unique,
                hardware text not null default '{}',
                model text not null default '',
                params_b real not null default 0,
                version text not null default '',
                enabled integer not null default 1,
                tps real not null default 0,
                prompt_tps real not null default 0,
                slots integer not null default 1,
                gpu_used integer not null default 0,
                created_at text not null,
                last_seen text not null default '',
                last_ip text not null default '',
                jobs_done integer not null default 0,
                jobs_failed integer not null default 0,
                note text not null default ''
            )"""
        )
        await conn.execute(
            """create table if not exists ai_agent_keys (
                id integer primary key autoincrement,
                key_hash text not null unique,
                label text not null default '',
                created_at text not null,
                created_by text not null default '',
                expires_at text not null default '',
                used integer not null default 0
            )"""
        )
        # Привязка компьютера к железу и одноразовые ключи: hw_id — отпечаток оборудования (его считает сама программа-агент; адрес сети в привязке не участвует),
        # agent_id у ключа — компьютер, который им подключился (после этого ключ недействителен), used_at — когда.
        await _ensure_column(conn, "ai_agents", "hw_id", "text not null default ''")
        await _ensure_column(conn, "ai_agent_keys", "agent_id", "text not null default ''")
        await _ensure_column(conn, "ai_agent_keys", "used_at", "text not null default ''")
        await conn.execute("create index if not exists idx_ai_agents_hw on ai_agents(hw_id)")
        # Неисправность из отчётов: «почему не работает» и «что нужно для восстановления» — берутся из последнего отчёта выезда (см. app/core/fault.py),
        # показываются в карточке комплекса в разделе «Неисправность / описание» и правятся вручную; fault_at — время отчёта-источника.
        await _ensure_column(conn, "points", "fault_reason", "text not null default ''")
        await _ensure_column(conn, "points", "fault_need", "text not null default ''")
        await _ensure_column(conn, "points", "fault_at", "text not null default ''")
        await _ensure_column(conn, "points", "fault_by", "text not null default ''")

        await conn.execute(
            "insert or ignore into state (id, rev, body) values (1, 0, ?)",
            (json.dumps(INITIAL_STATE, ensure_ascii=False),),
        )
        await conn.commit()
        await seed_equipment_statuses(conn)
        await conn.commit()
        await migrate_people_roles(conn)
        # Вид водителя: '' — обычный, 'tower' — водитель вышки, 'cover' — водитель прикрытия (роль остаётся «водитель»).
        await _ensure_column(conn, "people", "driver_kind", "text not null default ''")
        # «Подрядчик» в составе бригады: не человек из персонала (подрядчики часто разные), а отметка на самой бригаде — поэтому он
        # может стоять в нескольких бригадах сразу и в план-задание не печатается. contractor_note — кто именно (необязательно).
        await _ensure_column(conn, "crews", "contractor", "integer not null default 0")
        await _ensure_column(conn, "crews", "contractor_note", "text not null default ''")
        await migrate_equipment_layout(conn)
        await migrate_equipment_features(conn)
        await _migrate_people_from_state(conn)
        await _migrate_calendar_state_to_tables(conn)
        await _migrate_teams_to_crews(conn)
        await _cleanup_html_entities(conn)
        try:
            from app.services.point_refs_repair import repair as _repair_point_refs  # noqa: PLC0415
            await _repair_point_refs(conn)              # задания/фото/отчёты, ссылающиеся на комплекс «не его названием» (регистр, пробел, двойники)
        except Exception:  # noqa: BLE001 — исправление ссылок не должно мешать запуску
            logging.getLogger(__name__).exception("Не удалось выровнять ссылки на комплексы")
        await _create_performance_indexes(conn)
        await conn.commit()
        await _backfill_fault_fields(conn)
        await conn.commit()
        from app.repositories import roles_repository  # noqa: PLC0415
        await roles_repository.ensure(conn)             # таблица ролей и встроенные роли с правами по умолчанию (у «admin» — все права, восстанавливаются при каждом запуске)
        await ensure_field_encryption(conn)             # в самом конце: все остальные миграции работают с открытым текстом и не знают про шифрование
        await conn.execute("pragma optimize")           # статистика для планировщика запросов: после неё он чаще выбирает индекс
    await roles_repository.load()                       # права ролей — в память (после восстановления копии базы — заново)


# Индексы под частые запросы (найдены разбором журнала запросов: чтение по этим столбцам шло перебором всей таблицы). Все
# безопасны и повторяемы: «if not exists»; таблицы, которых нет, пропускаются.
_PERFORMANCE_INDEXES = [
    ("idx_vp_task", "visit_photos", "task_id"),                   # счётчик фото по заданиям (карточка комплекса в боте)
    ("idx_vp_date", "visit_photos", "taken_date"),                # «Фото по дням»: список фото за день
    ("idx_tasks_team", "tasks", "team"),                          # задания бригады, перенос бригады на другой день
    ("idx_tvr_batch", "task_visit_reports", "import_batch"),      # загрузки истории выездов из Excel: отмена загрузки
    ("idx_vr_point", "verification_requests", "point_id"),
    ("idx_vr_task", "verification_requests", "task_id"),
    ("idx_air_point", "access_issue_requests", "point_id"),
    ("idx_air_task", "access_issue_requests", "task_id"),
    ("idx_dr_point", "demontage_requests", "point_id"),
    ("idx_dr_task", "demontage_requests", "task_id"),
    ("idx_bws_point", "bot_work_sessions", "point_id"),
    ("idx_chat_point", "chat_messages", "point_id"),
    ("idx_eqh_serial", "equipment_history", "serial_number"),
    ("idx_points_status", "points", "status"),                    # фильтры по статусу в «Все АПК», карте и сводках
    ("idx_points_category", "points", "category_id"),
    ("idx_points_sim", "points", "sim_number"),                   # поиск комплекса по SIM, связки и дубли SIM
]


async def _backfill_fault_fields(conn: aiosqlite.Connection) -> None:
    """Один раз: «почему не работает / что нужно» в карточках комплексов заполняется из уже сданных отчётов (дальше — каждым новым отчётом, см.
    points_repository.apply_report_fault). Отчёты берутся от старых к новым, поэтому итог — состояние по самому свежему."""
    cur = await conn.execute("select 1 from app_flags where key = 'fault_backfill_v1'")
    if await cur.fetchone():
        return
    from app.core import fault  # noqa: PLC0415 — модуль без зависимостей от базы, импорт здесь, чтобы не тянуть его при старте раньше времени

    state: dict[str, dict] = {}
    cur = await conn.execute(
        "select r.point_id, r.result_status, r.fault, r.needs, r.created_at, coalesce(pe.full_name, r.created_by, '') "
        "from task_visit_reports r left join people pe on pe.id = r.author_person_id order by r.created_at asc, r.rowid asc"
    )
    for point_id, result_status, fault_text, needs, created_at, by in await cur.fetchall():
        cur_state = state.get(point_id) or {"reason": "", "need": "", "at": "", "by": ""}
        nxt = fault.next_state(cur_state, {"result_status": result_status, "fault": fault_text, "needs": needs, "created_at": created_at, "by": by})
        if nxt is not None:
            state[point_id] = nxt
    for point_id, st in state.items():
        if st["reason"] or st["need"]:
            await conn.execute("update points set fault_reason = ?, fault_need = ?, fault_at = ?, fault_by = ? where id = ?",
                               (st["reason"], st["need"], st["at"], st["by"], point_id))
    await conn.execute("insert or ignore into app_flags (key, value) values ('fault_backfill_v1', '1')")


async def _create_performance_indexes(conn: aiosqlite.Connection) -> None:
    for name, table, column in _PERFORMANCE_INDEXES:
        if not await _table_exists(conn, table):
            continue
        cur = await conn.execute(f"pragma table_info({table})")
        if column not in {row[1] for row in await cur.fetchall()}:
            continue
        await conn.execute(f"create index if not exists {name} on {table}({column})")


# (table, primary-key column, free-text columns) — rows whose text was saved
# entity-encoded ("&#1052;..." for "МЕ..."), see app/core/text.py.
_ENTITY_TEXT_COLUMNS = [
    ("points", "id", ("address", "description", "notes", "inventory_number")),
    ("tasks", "id", ("work", "address", "note")),
    ("point_plans", "id", ("description",)),
    ("task_visit_reports", "task_id", ("comment", "equipment_removed", "equipment_installed")),
]
# Every place a point's id is stored as a (mostly loose, text) reference.
_POINT_ID_REFERENCES = [
    ("tasks", "point"), ("point_plans", "point_id"), ("point_status_history", "point_id"),
    ("task_visit_reports", "point_id"), ("bot_work_sessions", "point_id"), ("visit_photos", "point_id"),
    ("verification_requests", "point_id"), ("access_issue_requests", "point_id"),
    ("demontage_requests", "point_id"), ("equipment", "point_id"), ("equipment", "removed_point_id"),
    ("equipment_history", "point_id"), ("solar_panels", "point_id"), ("warehouse_items", "point_id"),
    ("forms_import_rows", "point_id"), ("apk_group_points", "point_id"), ("point_files", "point_id"),
]


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cur = await conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,))
    return await cur.fetchone() is not None


async def _cleanup_html_entities(conn: aiosqlite.Connection) -> None:
    """One-time, idempotent repair of text that was saved entity-encoded
    ("&#1052;&#1045;..." for "МЕ..." — from a Yandex Constructor file or a
    pasted table, before imports learned to decode it; see app/core/text.py).
    Without it every such value keeps reappearing in the app and in every
    file exported from it, e.g. the Excel for Yandex Constructor.

    Free-text columns are simply rewritten. A point's own number is the
    trickier case: it's referenced by tasks, reports, photos, history, …
    (and its photos live in a folder named after it on disk), so it's
    renamed across every reference in one transaction, and its photo folder
    moved afterwards. A number whose decoded form is already taken by
    another point is left alone — silently merging two real points isn't
    something a startup migration should decide — the exports decode it on
    the way out anyway. Runs on every start but only touches rows that
    still contain an entity, so after the first pass it's a few cheap
    LIKE scans."""
    log = logging.getLogger(__name__)

    text_fixed = 0
    for table, pk, cols in _ENTITY_TEXT_COLUMNS:
        if not await _table_exists(conn, table):
            continue
        where = " or ".join(f"{c} like '%&%;%'" for c in cols)
        cur = await conn.execute(f"select {pk}, {', '.join(cols)} from {table} where {where}")
        for row in await cur.fetchall():
            old_vals = list(row[1:])
            new_vals = [decode_entities(v) if v is not None else None for v in old_vals]
            if new_vals != old_vals:
                sets = ", ".join(f"{c} = ?" for c in cols)
                await conn.execute(f"update {table} set {sets} where {pk} = ?", (*new_vals, row[0]))
                text_fixed += 1

    cur = await conn.execute("select id from points where id like '%&%;%'")
    renamed: list[tuple[str, str]] = []
    skipped: list[str] = []
    for (old_id,) in await cur.fetchall():
        new_id = decode_entities(old_id).strip()
        if not new_id or new_id == old_id:
            continue
        cur = await conn.execute(
            "select 1 from points where id = ? or pylower(trim(id)) = pylower(?)", (new_id, new_id)
        )
        if await cur.fetchone():
            skipped.append(old_id)
            continue
        await conn.execute("update points set id = ? where id = ?", (new_id, old_id))
        for table, col in _POINT_ID_REFERENCES:
            if await _table_exists(conn, table):
                await conn.execute(f"update {table} set {col} = ? where {col} = ?", (new_id, old_id))
        renamed.append((old_id, new_id))

    if text_fixed or renamed:
        await conn.commit()

    photos_root = get_settings().data_dir / "visit_photos"
    for old_id, new_id in renamed:
        src, dst = photos_root / old_id, photos_root / new_id
        if not src.is_dir():
            continue
        try:
            for f in [f for f in src.rglob("*") if f.is_file()]:
                target = dst / f.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.move(str(f), str(target))
            shutil.rmtree(src, ignore_errors=True)
        except OSError:
            log.exception("Не удалось перенести фото точки %s в папку %s", old_id, new_id)

    if text_fixed or renamed or skipped:
        log.warning(
            "Очистка HTML-кодов в текстах: исправлено записей — %d, точек переименовано — %d, "
            "пропущено из-за совпадения с существующим номером — %d",
            text_fixed, len(renamed), len(skipped),
        )
        if skipped:
            log.warning("Номера точек, оставленные как есть (занято): %s", skipped)


async def _migrate_people_from_state(conn: aiosqlite.Connection) -> None:
    """One-time: turn the free-text fitters/drivers/operator fields that
    used to live on each бригада (inside the JSON state blob) into real
    `people` rows, so existing production names aren't lost now that the
    print plan and the "assemble a team" UI read from `people` instead of
    parsing those strings. Assignments to a specific team/date are not
    backfilled — a dispatcher re-picks the roster going forward."""
    cur = await conn.execute("select count(*) from people")
    (count,) = await cur.fetchone()
    if count:
        return
    cur = await conn.execute("select body from state where id = 1")
    row = await cur.fetchone()
    if not row:
        return
    body = json.loads(row[0])
    role_by_name: dict[str, str] = {}
    phone_by_name: dict[str, str] = {}
    for team in body.get("teams", []):
        for name in parse_names(team.get("fitters", "")):
            role_by_name.setdefault(name, "fitter")
        for name in parse_names(team.get("drivers", "")):
            role_by_name.setdefault(name, "driver")
        operator = (team.get("operator") or "").strip()
        if operator:
            role_by_name.setdefault(operator, "operator")
            phone = (team.get("operatorPhone") or "").strip()
            if phone:
                phone_by_name[operator] = phone
    if not role_by_name:
        return
    created_at = dt.datetime.now(dt.timezone.utc).isoformat()
    for name, role_type in role_by_name.items():
        await insert_person_raw(conn, name, phone_by_name.get(name), role_type, created_at)
    await conn.commit()


async def _migrate_calendar_state_to_tables(conn: aiosqlite.Connection) -> None:
    """One-time: the calendar domain (teams/points/tasks/day-status) used to
    live in a single JSON blob (`state.body`) with whole-document
    compare-and-swap writes — fine for one browser tab, but not once
    warehouse/personnel data needs to join against points, or more than one
    actor writes concurrently. This moves it into real tables. The
    /api/state contract (GET/PUT the same {teams, points, tasks, days}
    shape — see state_repository.py) is unchanged, so the existing
    frontend needs no changes, only the storage underneath does.

    Idempotent/resumable: only renames the old `state` table away once the
    new tables are fully populated and committed, so a crash mid-migration
    just retries cleanly on the next startup instead of losing data.

    The completion guard checks `state_rev` having a row — the one thing
    only this function ever inserts into. Checking `teams` doesn't work:
    it's later renamed away by _migrate_teams_to_crews, then silently
    recreated empty by this file's own `create table if not exists teams`
    on the next boot, so it would look "not migrated" forever. Checking
    `state` doesn't work either: `init_db()` unconditionally recreates
    and reseeds it a few lines above, so it always exists by the time
    this function runs, migrated or not."""
    cur = await conn.execute("select count(*) from state_rev")
    (already_migrated,) = await cur.fetchone()
    if already_migrated:
        return

    cur = await conn.execute("select rev, body from state where id = 1")
    row = await cur.fetchone()
    rev, body = (row[0], json.loads(row[1])) if row else (0, INITIAL_STATE)

    for t in body.get("teams", []):
        await conn.execute(
            "insert or ignore into teams (id, name, archived) values (?, ?, ?)",
            (int(t["id"]), t.get("name", ""), int(bool(t.get("archived")))),
        )
    for p in body.get("points", []):
        if not p.get("id"):
            continue
        await conn.execute(
            "insert or ignore into points (id, address, description, lat, lon) values (?, ?, ?, ?, ?)",
            (
                str(p["id"]),
                p.get("address") or "",
                p.get("description") or "",
                p.get("lat") or "",
                p.get("lon") or "",
            ),
        )
    for t in body.get("tasks", []):
        await conn.execute(
            "insert or ignore into tasks (id, date, team, point, work, address, hours, travel, type, note) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(t["id"]),
                t["date"],
                int(t["team"]),
                t.get("point") or "",
                t.get("work") or "",
                t.get("address") or "",
                float(t.get("hours") or 0),
                float(t.get("travel") or 0),
                t.get("type") or "",
                t.get("note") or "",
            ),
        )
    for date_str, teams_for_day in body.get("days", {}).items():
        for team_id, cfg in teams_for_day.items():
            await conn.execute(
                "insert or ignore into day_team_status (date, team, active, equip) values (?, ?, ?, ?)",
                (date_str, int(team_id), int(bool(cfg.get("active"))), cfg.get("equip") or "Лестница"),
            )

    await conn.execute("insert or ignore into state_rev (id, rev) values (1, ?)", (rev,))
    cur = await conn.execute("select name from sqlite_master where type='table' and name='state'")
    if await cur.fetchone():
        await conn.execute("alter table state rename to _legacy_state_backup")
    await conn.commit()


async def _migrate_teams_to_crews(conn: aiosqlite.Connection) -> None:
    """One-time: бригады used to be 7 persistent named slots, with
    composition assigned separately per date (`team_assignments`) and an
    on/off toggle per date (`day_team_status`) — two extra steps before a
    slot was actually ready to use, and dead slots carried forward even on
    days nothing was assigned. A crew is now just a group created for one
    date (composed inline while importing that day's KML tasks), so it
    only exists when it's actually needed. Folds the three old tables into
    `crews`/`crew_members` and repoints every task's `team` column at the
    new crew id — the field name doesn't change, only what the integer
    means (crew id instead of legacy team-slot id).

    Completion guard: `_legacy_teams_backup` existing is the only safe
    signal — `crews` having rows is NOT (a data-sparse DB can legitimately
    finish this migration with zero crews, e.g. nothing was ever assigned
    to a team, which would make the guard re-run every boot), and `teams`
    existing is NOT either (init_db's own `create table if not exists
    teams` recreates it empty every boot regardless). Same bug class as
    the one fixed in _migrate_calendar_state_to_tables below — see its
    docstring."""
    cur = await conn.execute("select name from sqlite_master where type='table' and name='_legacy_teams_backup'")
    if await cur.fetchone():
        return  # already migrated on a previous boot
    cur = await conn.execute("select name from sqlite_master where type='table' and name='teams'")
    if not await cur.fetchone():
        return  # nothing to migrate

    cur = await conn.execute("select id, name from teams")
    team_names = {r[0]: r[1] for r in await cur.fetchall()}

    cur = await conn.execute("select team_id, person_id, date from team_assignments")
    members_by_pair: dict[tuple[str, int], list[int]] = {}
    for team_id, person_id, date_str in await cur.fetchall():
        members_by_pair.setdefault((date_str, team_id), []).append(person_id)

    cur = await conn.execute("select date, team, equip from day_team_status")
    equip_by_pair: dict[tuple[str, int], str] = {(date_str, team_id): equip for date_str, team_id, equip in await cur.fetchall()}

    cur = await conn.execute("select distinct date, team from tasks")
    pairs = {(r[0], r[1]) for r in await cur.fetchall()}
    pairs |= set(members_by_pair.keys())
    pairs |= set(equip_by_pair.keys())

    crew_id_by_pair: dict[tuple[str, int], int] = {}
    for date_str, team_id in pairs:
        name = team_names.get(team_id, f"Бригада {team_id}")
        equip = equip_by_pair.get((date_str, team_id), "Лестница")
        cur = await conn.execute("insert into crews (date, name, equip) values (?, ?, ?)", (date_str, name, equip))
        crew_id = cur.lastrowid
        crew_id_by_pair[(date_str, team_id)] = crew_id
        for person_id in members_by_pair.get((date_str, team_id), []):
            await conn.execute(
                "insert or ignore into crew_members (crew_id, person_id) values (?, ?)", (crew_id, person_id)
            )

    cur = await conn.execute("select id, date, team from tasks")
    for task_id, date_str, team_id in await cur.fetchall():
        new_crew_id = crew_id_by_pair.get((date_str, team_id))
        if new_crew_id is not None:
            await conn.execute("update tasks set team = ? where id = ?", (new_crew_id, task_id))

    for old_name, backup_name in (
        ("teams", "_legacy_teams_backup"),
        ("team_assignments", "_legacy_team_assignments_backup"),
        ("day_team_status", "_legacy_day_team_status_backup"),
    ):
        cur = await conn.execute("select name from sqlite_master where type='table' and name=?", (old_name,))
        if await cur.fetchone():
            await conn.execute(f"alter table {old_name} rename to {backup_name}")
    await conn.commit()
