"""Резервные копии SQLite: ежедневная (calendar-ГГГГ-ММ-ДД.db, хранится backup_retention штук) и промежуточные снимки раз в 6 часов (recent-N.db, четыре по кругу) — если с базой
что-то случится днём, теряется не больше шести часов работы, а не сутки. Копия пишется во временный файл и только проверенная (quick_check) становится настоящей: оборванная
записью при нехватке места или остановке сервера копия не маскируется под хорошую. Результат проверки виден в «Состоянии системы»."""
from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.core import field_crypto, runtime_state

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60
RECENT_EVERY = 6 * 3600
RECENT_SLOTS = 4


def _verify(path: Path) -> tuple[bool, str]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        field_crypto.register_sqlite(con)
        con.create_function("pylower", 1, lambda v: v.lower() if isinstance(v, str) else v)
        rows = [r[0] for r in con.execute("pragma quick_check(3)").fetchall()]
        con.execute("select count(*) from points").fetchone()
    finally:
        con.close()
    return rows == ["ok"], "; ".join(str(r) for r in rows)[:200]


def _write_backup(target: Path) -> None:
    """Копия через встроенный backup SQLite во временный файл → проверка → переименование. Не хватает места / сбой — временный файл удаляется, старые копии остаются."""
    settings = get_settings()
    tmp = target.with_name(target.name + ".part")
    tmp.unlink(missing_ok=True)
    try:
        need = settings.db_path.stat().st_size * 2
        free = shutil.disk_usage(settings.backup_dir).free
        if free < need:
            raise OSError(f"на диске мало места для копии: свободно {free // 1_000_000} МБ, нужно около {need // 1_000_000} МБ")
        src = sqlite3.connect(settings.db_path)
        try:
            dest = sqlite3.connect(tmp)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        ok, detail = _verify(tmp)
        runtime_state.set_fact("backup_verified", {"ok": ok, "detail": detail, "at": time.time(), "file": target.name})
        if not ok:
            raise sqlite3.DatabaseError(f"копия не прошла проверку: {detail}")
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _run_backup_sync() -> None:
    settings = get_settings()
    target = settings.backup_dir / f"calendar-{date.today().isoformat()}.db"
    if not target.exists():
        _write_backup(target)
        all_backups = sorted(settings.backup_dir.glob("calendar-*.db"))
        for old in all_backups[: max(0, len(all_backups) - settings.backup_retention)]:
            old.unlink(missing_ok=True)


def _run_recent_sync() -> None:
    """Промежуточный снимок: новый берётся, когда самому свежему больше шести часов (минус запас на цикл), и ложится в пустой или самый старый из четырёх слотов — так снимки
    покрывают сутки, а не набиваются подряд сразу после запуска."""
    settings = get_settings()
    slots = [settings.backup_dir / f"recent-{i}.db" for i in range(RECENT_SLOTS)]
    mtimes = [p.stat().st_mtime if p.exists() else 0 for p in slots]
    if time.time() - max(mtimes) < RECENT_EVERY - 600:
        return
    _write_backup(slots[mtimes.index(min(mtimes))])


async def run_backup() -> None:
    await asyncio.to_thread(_run_backup_sync)


async def daily_backup_loop() -> None:
    """При запуске — ежедневная копия (если сегодняшней ещё нет), дальше каждые полчаса: наступили новые сутки — ежедневная, прошло шесть часов — промежуточный снимок."""
    while True:
        try:
            await run_backup()
            await asyncio.to_thread(_run_recent_sync)
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("Плановое резервное копирование завершилось ошибкой")
        await asyncio.sleep(1800)
