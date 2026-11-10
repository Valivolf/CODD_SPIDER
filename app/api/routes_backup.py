"""Backup/restore — a full copy of the SQLite database file, not a
hand-rolled per-table JSON export.

The old "Копия"/"Восстановить" flow only round-tripped `tasks`+`points`
through JSON, built back when those were the only two real domains. Every
domain added since (people, crews, point categories/statuses, warehouse
items, visit reports, status history, users) quietly fell outside it —
"восстановление" silently lost data with every new feature. A raw SQLite
file copy (the same `sqlite3.Connection.backup()` API the daily
`backup_service.py` already uses) can't go stale like that: it captures
every table there is, present and future, with no per-table maintenance.

Восстановление не перезапускает процесс (раньше после него сервер просто
завершался в расчёте на Docker с `restart: unless-stopped` — при запуске
скриптом `Запустить.bat` его никто не поднимал обратно, и страница после
«восстановления» не открывалась). Теперь файл базы подменяется прямо в
работающем приложении под эксклюзивным доступом (database.exclusive_access),
после чего базу приводят к схеме этой версии (миграции init_db) и
обновляют кэш типов работ. Если файл от другой версии не мигрирует — текущая
база возвращается из страховочной копии, и приложение остаётся рабочим.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import sqlite3
import time
import zlib
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.core import field_crypto
from app.core.clientip import client_ip
from app.core.dates import format_date
from app.core.deps import get_session_user, require_backup
from app.db import database, field_encryption
from app.db.bootstrap import bootstrap_admin
from app.models.user import UserOut
from app.repositories import bot_settings_repository, state_repository, work_types_repository
from app.services import forms_auto, security_log
from app.services import weather as weather_service

router = APIRouter(prefix="/api/backup", tags=["backup"])
logger = logging.getLogger(__name__)

# Tables a genuine backup of this app must have — a sanity check against
# restoring an unrelated/corrupt .db file over the live one.
_EXPECTED_TABLES = {"points", "tasks", "state_rev", "users"}

# Файл базы на Windows могут на секунды занимать OneDrive / антивирус / индексатор
# («PermissionError: файл используется другим процессом») — подмену повторяем, а
# если файл так и не освободился — записываем содержимое в него средствами SQLite
# (_overwrite_via_backup_api), не заменяя сам файл.
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024  # потолок распаковки присланного .gz
_REPLACE_ATTEMPTS = 12
_WAL_REMOVE_ATTEMPTS = 40
_REPLACE_PAUSE = 0.25


def _backup_to_file_sync(target: str) -> None:
    settings = get_settings()
    src = sqlite3.connect(settings.db_path)
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


@router.get("")
async def download_backup(request: Request, admin: UserOut = Depends(require_backup)):
    """A fresh, consistent copy of the whole database (via SQLite's own
    backup API, so a concurrent write elsewhere can't produce a half-
    written file) — every table, not a hand-picked subset.

    Только администратор: в копии — все учётные записи (хэши паролей), коды VK, переписка, журнал безопасности; раньше скачать её мог любой вошедший, включая роль «просмотр»."""
    await security_log.record("backup_download", username=admin.username, role=admin.role, ip=client_ip(request))
    settings = get_settings()
    tmp_path = str(settings.backup_dir / f"_download-{os.getpid()}-{id(object())}.db")
    await asyncio.to_thread(_backup_to_file_sync, tmp_path)
    try:
        with open(tmp_path, "rb") as f:
            body = f.read()
    finally:
        os.remove(tmp_path)
    return StreamingResponse(
        io.BytesIO(body),
        media_type="application/vnd.sqlite3",
        headers={"Content-Disposition": f'attachment; filename="calendar-{format_date(date.today())}.db"'},
    )


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Не удалось удалить временный файл %s", path)


def _validate_sqlite_file_sync(path: str) -> str | None:
    """Returns an error message, or None if the file is a plausible backup
    of this app (valid SQLite file, passes integrity_check, has the tables
    a real backup must have)."""
    try:
        conn = sqlite3.connect(path)
        field_crypto.register_sqlite(conn)          # зашифрованные поля — вычисляемые столбцы, им нужны функции расшифровки
    except sqlite3.Error as exc:
        return f"Не удалось открыть файл как базу SQLite: {exc}"
    try:
        try:
            (result,) = conn.execute("pragma integrity_check(1)").fetchone()
        except sqlite3.DatabaseError:
            return "Файл повреждён или это не база SQLite"
        if result != "ok":
            return f"Проверка целостности базы не пройдена: {result}"
        cur = conn.execute("select name from sqlite_master where type = 'table'")
        tables = {r[0] for r in cur.fetchall()}
        missing = _EXPECTED_TABLES - tables
        if missing:
            return f"В файле не хватает таблиц этого приложения: {', '.join(sorted(missing))}"
        return None
    finally:
        conn.close()
        # База в режиме WAL при открытии оставляет рядом -wal/-shm — они не нужны.
        for suffix in ("-wal", "-shm"):
            _remove_quietly(path + suffix)


def _replace_with_retry(src: str, dst: str) -> None:
    last: OSError | None = None
    for _ in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:  # Windows: файл на мгновение занят
            last = exc
            time.sleep(_REPLACE_PAUSE)
    assert last is not None
    raise last


def _drop_stale_wal(live_path: str) -> None:
    # Хвосты -wal/-shm от *прежнего* файла нельзя оставлять: при следующем
    # открытии SQLite применил бы их к новой базе. Под эксклюзивным доступом
    # соединений нет, но антивирус может держать файл — повторяем недолго.
    for suffix in ("-wal", "-shm"):
        for _ in range(_WAL_REMOVE_ATTEMPTS):
            try:
                os.remove(live_path + suffix)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(_REPLACE_PAUSE)
        else:
            logger.warning("Не удалось удалить %s%s — оставлен", live_path, suffix)


def _overwrite_via_backup_api(src_path: str, live_path: str) -> None:
    """Запасной путь, когда файл базы занят и его нельзя заменить: содержимое
    `src_path` записывается в живую базу тем же механизмом, что и резервная копия
    (только в обратную сторону). Файл остаётся на месте, SQLite сам берёт нужные
    блокировки; при другом размере страницы базу в режиме WAL перезаписать нельзя,
    поэтому на время записи включается обычный журнал."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(live_path, timeout=30)
        try:
            try:
                dst.execute("pragma journal_mode=delete")
            except sqlite3.Error:
                logger.warning("Не удалось переключить журнал базы на DELETE перед восстановлением")
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _swap_in(src_path: str, live_path: str) -> None:
    """Ставит файл `src_path` на место живой базы: переименованием, а если файл
    базы занят (Windows) — записью содержимого через backup API."""
    try:
        _replace_with_retry(src_path, live_path)
    except PermissionError:
        logger.warning("Файл базы занят другим процессом — записываю содержимое через SQLite backup API")
        _overwrite_via_backup_api(src_path, live_path)
        _remove_quietly(src_path)
        return
    _drop_stale_wal(live_path)


def _apply_restore_sync(uploaded_path: str, live_path: str, safety_path: str) -> None:
    # Safety copy of the live DB first — via the same backup() API, so a
    # bad restore is itself always recoverable.
    src = sqlite3.connect(live_path)
    try:
        dest = sqlite3.connect(safety_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    _swap_in(uploaded_path, live_path)


def _rollback_sync(safety_path: str, live_path: str) -> None:
    """Возвращает прежнюю базу из страховочной копии (файл от другой версии не
    подошёл)."""
    tmp = live_path + ".rollback"
    shutil.copyfile(safety_path, tmp)
    _swap_in(tmp, live_path)


async def _raise_rev(old_rev: int) -> None:
    """Номер ревизии календаря после восстановления — строго больше прежнего:
    открытые у других диспетчеров страницы опрашивают /api/state и обновляются,
    только если ревизия изменилась (у восстановленной базы она может совпасть
    или оказаться меньше)."""
    async with database.get_connection() as conn:
        await conn.execute(
            "update state_rev set rev = case when rev <= ? then ? + 1 else rev end where id = 1", (old_rev, old_rev)
        )
        await conn.commit()


async def _summary() -> dict[str, int]:
    async with database.get_connection() as conn:
        out = {}
        for key, table in (("points", "points"), ("tasks", "tasks"), ("users", "users")):
            cur = await conn.execute(f"select count(*) from {table}")
            out[key] = (await cur.fetchone())[0]
        return out


def _gunzip_capped(data: bytes, limit: int) -> bytes:
    """Распаковка gzip с потолком на размер результата — защита от «архивной
    бомбы» (маленький файл, который разворачивается в гигабайты)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    try:
        chunk = d.decompress(data, limit + 1)
        out += chunk
        while d.unconsumed_tail and len(out) <= limit:
            out += d.decompress(d.unconsumed_tail, limit + 1 - len(out))
        if len(out) <= limit:
            out += d.flush()
    except zlib.error as exc:
        raise ValueError(f"не удалось распаковать gzip: {exc}")
    if len(out) > limit:
        raise ValueError(f"после распаковки файл больше {limit // 1048576} МБ")
    if not d.eof:
        raise ValueError("архив обрезан (загрузка прервалась?)")
    return bytes(out)


@router.post("/restore")
async def restore_backup(file: UploadFile, request: Request, admin: UserOut = Depends(require_backup)):
    # Страница отправляет файл сжатым (.db.gz) — вдвое-впятеро меньше по сети, что
    # важно на медленном канале; обычный .db тоже принимается.
    name = (file.filename or "").lower()
    is_gz = name.endswith(".gz")
    if is_gz:
        name = name[:-3]
    if not name.endswith((".db", ".sqlite", ".sqlite3")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл базы (.db)")
    started = time.monotonic()
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    if is_gz or content[:2] == b"\x1f\x8b":
        packed = len(content)
        try:
            content = await asyncio.to_thread(_gunzip_capped, content, _MAX_UNPACKED_BYTES)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Файл сжат, но {exc}")
        if not content:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
        logger.info("Restore: распаковано %d → %d байт", packed, len(content))
    # Этапы пишутся в журнал с отметкой времени: если восстановление «зависло»,
    # по последней строке видно, на каком шаге.
    logger.info("Restore: получен файл «%s», %d байт", file.filename, len(content))

    settings = get_settings()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Must live on the same filesystem/mount as db_path (data_dir), not
    # backup_dir — docker-compose.yml mounts data/ and backups/ as separate
    # volumes, and os.replace() below cannot rename across that boundary
    # (fails with "Invalid cross-device link" / EXDEV even though both are
    # ultimately on the same host disk).
    uploaded_path = str(settings.data_dir / f"_uploaded-{stamp}.db")
    with open(uploaded_path, "wb") as f:
        f.write(content)

    error = await asyncio.to_thread(_validate_sqlite_file_sync, uploaded_path)
    if error:
        _remove_quietly(uploaded_path)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, error)

    logger.info("Restore: файл проверен за %.1f с, жду освобождения базы (открытых операций: %d)", time.monotonic() - started, database.active_connections())
    live_path = str(settings.db_path)
    safety_path = str(settings.backup_dir / f"before-restore-{stamp}.db")
    old_rev = await state_repository.read_rev()
    # Переключатель «бот VK включён» — свойство ЭТОГО сервера (бот должен работать только на одном из них), а не
    # данных: из копии с другого сервера он приехать не должен — иначе после восстановления на тестовой машине
    # включается бот, который отвечает монтёрам наравне с рабочим (и наоборот, рабочий бот может оказаться выключен).
    old_bot_enabled = await bot_settings_repository.get_enabled()
    old_forms_auto = await forms_auto.raw_config()          # так же и автозагрузка из Яндекс Форм: это настройка робота ЭТОГО сервера
    try:
        async with database.exclusive_access():
            logger.info("Restore: доступ к базе получен (%.1f с), подменяю файл", time.monotonic() - started)
            try:
                await asyncio.to_thread(_apply_restore_sync, uploaded_path, live_path, safety_path)
            except OSError as exc:
                logger.exception("Restore failed")
                _remove_quietly(uploaded_path)
                raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Не удалось применить восстановление: {exc}")
            logger.info("Restore: файл базы подменён (%.1f с), привожу к схеме этой версии", time.monotonic() - started)
            try:
                # База из другой версии приложения / с другого сервера приводится
                # к схеме этой версии, как при обычном запуске.
                await database.init_db()
                await work_types_repository.load()
                weather_service.reset_cache()
                await bootstrap_admin()
                await _raise_rev(old_rev)
                await bot_settings_repository.set_enabled(old_bot_enabled)
                await forms_auto.restore_raw_config(old_forms_auto)
            except Exception as exc:  # noqa: BLE001 — любой сбой миграции = откат
                logger.exception("Restore: файл не подошёл, возвращаю прежнюю базу")
                await asyncio.to_thread(_rollback_sync, safety_path, live_path)
                await database.init_db()
                await work_types_repository.load()
                weather_service.reset_cache()
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Файл базы не подошёл этой версии приложения ({type(exc).__name__}: {exc}). "
                    "Текущая база возвращена без изменений.",
                )
    except RuntimeError as exc:  # не дождались завершения текущих операций
        _remove_quietly(uploaded_path)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # Сессия, с которой нажали «Восстановить», может не существовать в
    # восстановленной базе (другие пользователи / другая версия токена) —
    # тогда страница пошлёт на вход, и это стоит объяснить заранее.
    still_logged_in = await get_session_user(request) is not None
    summary = await _summary()
    logger.info("Restore: готово за %.1f с — %s", time.monotonic() - started, summary)
    # Копия с другого сервера зашифрована ЕГО ключом: без него ФИО, телефоны, логины и пароли не прочитать — предупреждаем сразу.
    warning = ""
    try:
        async with database.get_connection() as conn:
            enc = await field_encryption.status(conn)
        if any(f["unreadable"] for f in enc["fields"]):
            warning = ("ВНИМАНИЕ: в этой копии ФИО, телефоны, логины и пароли зашифрованы другим ключом и не читаются. "
                       "Поставьте на этот сервер ключ того сервера, где копия сделана (Настройки → «Ключ шифрования»), и перезапустите приложение.")
    except Exception:  # noqa: BLE001 — предупреждение не должно ломать ответ о восстановлении
        logger.exception("Restore: не удалось проверить шифрование")
    # Пишем уже в восстановленную базу: запись до подмены файла пропала бы вместе со старой базой (прежние события возвращаются только из строк SECEVENT в журнале контейнера)
    await security_log.record("backup_restore", username=admin.username, role=admin.role, ip=client_ip(request), details={"file": (file.filename or "")[:80]})
    return {"ok": True, "restarting": False, "relogin": not still_logged_in, "summary": summary, "encryption_warning": warning}
