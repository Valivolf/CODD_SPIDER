"""«Состояние системы»: всё, что может незаметно сломаться, в одном месте — диск, база данных и её копии, фоновые циклы, бот, робот карт, ИИ, сетевой диск с фото.

Идея отказоустойчивости: не ждать, пока пользователь наткнётся на сбой. Каждая проверка отвечает «ок / внимание / плохо» и подсказывает, что делать; администратору в шапке
календаря показывается значок, если что-то не в порядке. Проверки лёгкие и не меняют данные; тяжёлая проверка целостности базы идёт отдельным фоновым циклом (integrity_loop) и
здесь только читается."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.core import field_crypto, runtime_state, tz
from app.core.point_ref import load_refs
from app.core.work_types import AUX_TYPE_NAME
from app.db import database

logger = logging.getLogger(__name__)

OK, WARN, BAD = "ok", "warn", "bad"
_ORDER = {OK: 0, WARN: 1, BAD: 2}
_cache: dict[str, Any] = {"at": 0.0, "value": None}
INTEGRITY_EVERY = 12 * 3600


def _item(key: str, title: str, level: str, value: str, hint: str = "") -> dict[str, Any]:
    return {"key": key, "title": title, "level": level, "value": value, "hint": hint}


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} ГБ"


def _mb(n: float) -> str:
    return f"{n / 1e6:.0f} МБ"


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} с назад"
    if seconds < 5400:
        return f"{seconds // 60} мин назад"
    if seconds < 172800:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн. назад"


# ---------------------------------------------------------------- проверка целостности (тяжёлая, в фоне)

def _integrity_sync(path: Path) -> tuple[bool, str]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        con.create_function("pylower", 1, database._py_lower)  # noqa: SLF001 — как у рабочих соединений
        field_crypto.register_sqlite(con)
        rows = [r[0] for r in con.execute("pragma quick_check(5)").fetchall()]
    finally:
        con.close()
    return (rows == ["ok"], "; ".join(str(r) for r in rows)[:300])


async def run_integrity_check() -> dict[str, Any]:
    started = time.monotonic()
    try:
        ok, detail = await asyncio.to_thread(_integrity_sync, get_settings().db_path)
    except Exception as exc:  # noqa: BLE001 — сама проверка не должна ронять приложение
        ok, detail = False, f"проверку выполнить не удалось: {type(exc).__name__}: {str(exc)[:160]}"
    result = {"ok": ok, "detail": detail, "at": time.time(), "seconds": round(time.monotonic() - started, 1)}
    runtime_state.set_fact("integrity", result)
    if not ok:
        logger.error("Проверка целостности базы данных: %s", detail)
    return result


async def integrity_loop() -> None:
    """Сразу после запуска (через минуту, чтобы не мешать старту) и затем раз в 12 часов."""
    await asyncio.sleep(60)
    while True:
        try:
            await run_integrity_check()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Цикл проверки целостности базы")
        await asyncio.sleep(INTEGRITY_EVERY)


# ---------------------------------------------------------------- отдельные проверки

def _check_time() -> dict[str, Any]:
    now = dt.datetime.now()
    zone = time.tzname[0] if time.tzname else ""
    level = OK if zone in ("MSK", "+03") or os.environ.get("TZ", "").endswith(("Moscow", "MSK-3")) or not hasattr(time, "tzset") else WARN
    return _item("time", "Время сервера", level, f"{now.strftime('%d.%m.%Y %H:%M')} ({zone or 'пояс не определён'})",
                 "" if level == OK else "Часовой пояс сервера не московский: «сегодня» может считаться неверно. Установщик приложения выставляет Europe/Moscow.")


def _check_disk() -> dict[str, Any]:
    s = get_settings()
    try:
        usage = shutil.disk_usage(s.data_dir)
    except OSError as exc:
        return _item("disk", "Место на диске", WARN, "не удалось узнать", str(exc))
    free_pct = usage.free / usage.total * 100 if usage.total else 100
    level = BAD if usage.free < 2e9 or free_pct < 3 else WARN if usage.free < 6e9 or free_pct < 10 else OK
    hint = "" if level == OK else "Диск сервера почти заполнен: приложению нужно место для базы, копий и фото. Освободите место (старые архивы в домашней папке, лишние копии) или расширьте диск."
    return _item("disk", "Место на диске", level, f"свободно {_gb(usage.free)} из {_gb(usage.total)} ({free_pct:.0f}%)", hint)


def _check_db() -> dict[str, Any]:
    s = get_settings()
    try:
        size = s.db_path.stat().st_size
    except OSError:
        return _item("db", "База данных", BAD, "файл базы не найден", "Файл базы не найден в папке данных — проверьте том /opt/lan_calendar/data.")
    wal = Path(str(s.db_path) + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    integ = runtime_state.fact("integrity")
    if integ is None:
        return _item("db", "База данных", OK, f"{_mb(size)}; проверка целостности ещё не выполнялась (первая — через минуту после запуска)")
    when = _ago(time.time() - integ["at"])
    if not integ["ok"]:
        return _item("db", "База данных", BAD, f"{_mb(size)}; ПРОВЕРКА ЦЕЛОСТНОСТИ НЕ ПРОЙДЕНА ({when})",
                     f"{integ['detail']}. Не вносите новых данных; сделайте копию файла базы и восстановите работу из последней хорошей копии (Настройки → Восстановить) или обратитесь к разработчику.")
    level = WARN if wal_size > 512e6 else OK
    return _item("db", "База данных", level, f"{_mb(size)}; целостность в порядке ({when}, проверка {integ['seconds']} с)",
                 "" if level == OK else "Журнал записи базы разросся — перезапуск приложения его сожмёт.")


def _backups() -> list[Path]:
    return sorted(get_settings().backup_dir.glob("calendar-*.db"))


def _check_backup() -> dict[str, Any]:
    files = _backups()
    recent = sorted(get_settings().backup_dir.glob("recent-*.db"), key=lambda p: p.stat().st_mtime)
    if not files and not recent:
        return _item("backup", "Резервные копии", BAD, "копий нет", "Плановая копия делается при запуске и раз в сутки. Сделайте копию вручную (Настройки → Копия) и проверьте место на диске.")
    newest = max([*files, *recent], key=lambda p: p.stat().st_mtime)
    age = time.time() - newest.stat().st_mtime
    verified = runtime_state.fact("backup_verified")
    level = OK if age < 30 * 3600 else WARN if age < 72 * 3600 else BAD
    bits = [f"последняя {_ago(age)} ({_mb(newest.stat().st_size)})", f"ежедневных: {len(files)}", f"промежуточных: {len(recent)}"]
    hint = ""
    if verified and not verified.get("ok"):
        level = BAD
        hint = f"Последняя копия не прошла проверку: {verified.get('detail')}. Сделайте новую копию вручную."
    elif level != OK:
        hint = "Копия давно не обновлялась: проверьте место на диске и журнал приложения (docker logs lan-calendar)."
    return _item("backup", "Резервные копии", level, "; ".join(bits), hint)


def _check_tasks() -> dict[str, Any]:
    state = runtime_state.tasks_state()
    dead = {k: v for k, v in state.items() if v != "ok"}
    if not state:
        return _item("tasks", "Фоновые задачи", OK, "не отслеживаются")
    if dead:
        return _item("tasks", "Фоновые задачи", BAD, "остановились: " + ", ".join(f"{k} ({v})" for k, v in dead.items()),
                     "Фоновая задача остановилась — перезапустите приложение (docker restart lan-calendar) и сообщите разработчику.")
    return _item("tasks", "Фоновые задачи", OK, f"работают все ({len(state)})")


def _check_points_health() -> dict[str, Any]:
    from app.services import point_health_service  # noqa: PLC0415

    last = getattr(point_health_service, "LAST_ROUND", None)
    if not last:
        return _item("ping", "Проверка связи с комплексами", OK, "первый круг ещё не закончился")
    age = time.time() - last["at"]
    level = OK if age < 1800 else WARN if age < 7200 else BAD
    return _item("ping", "Проверка связи с комплексами", level, f"последний круг {_ago(age)}: {last['points']} комплексов за {last['seconds']} с",
                 "" if level == OK else "Круг проверки связи давно не заканчивался — статусы комплексов могут быть устаревшими.")


def _check_bot() -> dict[str, Any]:
    ago = runtime_state.seen_ago("bot")
    if ago is None:
        return _item("bot", "Бот ВКонтакте", OK, "с момента запуска приложения обращений не было (бот обращается при первом сообщении монтёра)")
    return _item("bot", "Бот ВКонтакте", OK, f"последнее обращение бота {_ago(ago)}")


async def _check_robot() -> dict[str, Any]:
    from app.services import yandex_map_sync, yandex_robot  # noqa: PLC0415

    try:
        st = await asyncio.wait_for(yandex_robot.call("GET", "/status", timeout=6.0), timeout=8)
    except Exception:  # noqa: BLE001 — робот не установлен/остановлен: не ошибка приложения
        return _item("robot", "Робот карт Яндекса", OK, "не запущен (карты в Яндексе не ведутся)")
    err = yandex_map_sync.STATUS.get("last_error") or ""
    if not st.get("logged_in"):
        return _item("robot", "Робот карт Яндекса", WARN, "запущен, но не вошёл в Яндекс", "Настройки → «Яндекс-аккаунт для карт» → «Войти в Яндекс».")
    if err:
        return _item("robot", "Робот карт Яндекса", WARN, f"вошёл в Яндекс; последняя ошибка: {err[:140]}", "Окно «Яндекс-аккаунт для карт» покажет подробности и снимок экрана.")
    return _item("robot", "Робот карт Яндекса", OK, "работает, вход в Яндекс выполнен")


async def _check_ai() -> Optional[dict[str, Any]]:
    from app.services.ai import client  # noqa: PLC0415
    from app.services.ai import settings as ai_settings  # noqa: PLC0415

    cfg = await ai_settings.load()
    if not (cfg.get("assistant") or cfg.get("bot_voice")):
        return None
    try:
        st = await asyncio.wait_for(client.status(), timeout=10)
    except Exception:  # noqa: BLE001
        return _item("ai", "ИИ (модель и распознавание речи)", WARN, "не отвечает", "Контейнеры lan-calendar-llm и lan-calendar-asr: docker ps; docker logs lan-calendar-llm.")
    llm_ok, asr_ok = bool(st["llm"].get("ok")), bool(st["asr"].get("ok"))
    llm_off, asr_off = bool(st["llm"].get("disabled")), bool(st["asr"].get("disabled"))
    level = OK if llm_ok and (asr_ok or not cfg.get("bot_voice")) else WARN
    llm_txt = ("считают компьютеры сотрудников" if st["llm"].get("via_agents") else "готова") if llm_ok else ("выключена на сервере, свободных компьютеров нет" if llm_off else "не отвечает")
    asr_txt = "готово" if asr_ok else ("выключено на сервере" if asr_off else "не отвечает")
    if asr_off and not cfg.get("bot_voice"):
        level = OK if llm_ok else WARN               # речь не нужна: бот без голоса, выключатель — осознанный
    hint = "" if level == OK else ("Подключите компьютер с программой-агентом или включите ИИ на сервере: Настройки → ИИ." if (llm_off or asr_off)
                                   else "Подождите минуту после запуска контейнеров (модель загружается) или проверьте их: docker ps; docker logs lan-calendar-llm.")
    return _item("ai", "ИИ (модель и распознавание речи)", level, f"модель: {llm_txt}; распознавание речи: {asr_txt}", hint)


async def _check_photos() -> Optional[dict[str, Any]]:
    from app.services import photo_storage  # noqa: PLC0415

    try:
        st = await asyncio.wait_for(photo_storage.status(), timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if not st.get("configured"):
        return None
    buf = (st.get("server_files") or {}).get("buffer") or {}
    n_buf = buf.get("files", 0) if isinstance(buf, dict) else 0
    if not st.get("available"):
        return _item("photos", "Сетевой диск с фото", WARN, f"недоступен ({st.get('reason') or 'нет ответа'}); фото копятся на сервере: {n_buf}",
                     "Фото не потеряются — они ждут на сервере и перенесутся сами, когда диск вернётся. Проверьте сетевую папку и пароль (Настройки → Фото).")
    return _item("photos", "Сетевой диск с фото", OK, "доступен" + (f"; во временном буфере: {n_buf}" if n_buf else ""))


def _check_load() -> Optional[dict[str, Any]]:
    try:
        load1, load5, _ = os.getloadavg()
    except (AttributeError, OSError):
        return None
    cores = os.cpu_count() or 1
    level = OK if load5 < cores * 1.2 else WARN if load5 < cores * 2.5 else BAD
    return _item("load", "Нагрузка процессора", level, f"{load1:.1f} / {load5:.1f} (ядер: {cores})",
                 "" if level == OK else "Сервер перегружен: проверьте, не занимает ли процессор модель ИИ или робот (docker stats).")


# ---------------------------------------------------------------- сводка

async def _check_point_refs() -> dict[str, Any]:
    """Задания (с сегодняшнего дня) и фото, ссылающиеся на комплекс, которого нет в справочнике: у такого задания в боте нет данных комплекса, а фото не видны в карточке комплекса.
    Названия, которые отличаются от номера комплекса только регистром, пробелами и буквами-двойниками, система исправляет сама (app/services/point_refs_repair.py) — здесь остаётся то,
    что определить однозначно нельзя."""
    today = dt.date.today().isoformat()
    async with database.get_connection() as conn:
        cur = await conn.execute(
            "select point, count(*) from tasks where point != '' and type != ? and date >= ? and point not in (select id from points) group by point order by count(*) desc",
            (AUX_TYPE_NAME, today))
        tasks = await cur.fetchall()
        cur = await conn.execute(
            "select point_id, count(*) from visit_photos where point_id != '' and point_id not in (select id from points) group by point_id order by count(*) desc")
        photos = await cur.fetchall()
        refs = await load_refs(conn)
    # то, что однозначно определяется «без разбора» (регистр, пробел, двойники), система исправляет сама — в предупреждение оно не попадает
    tasks = [(p, n) for p, n in tasks if refs.resolve(p) is None]
    photos = [(p, n) for p, n in photos if refs.resolve(p) is None]
    if not tasks and not photos:
        return _item("point_refs", "Связи заданий и фото с комплексами", OK, "все задания и фото привязаны к комплексам из справочника")
    parts = []
    if tasks:
        parts.append(f"заданий с неизвестным комплексом: {sum(n for _, n in tasks)} ({', '.join(repr(p) for p, _ in tasks[:3])}{'…' if len(tasks) > 3 else ''})")
    if photos:
        parts.append(f"фото под неизвестным номером: {sum(n for _, n in photos)} ({', '.join(repr(p) for p, _ in photos[:3])}{'…' if len(photos) > 3 else ''})")
    item = _item("point_refs", "Связи заданий и фото с комплексами", WARN, "; ".join(parts),
                 "Такого комплекса нет в «Все АПК». Если комплекс переименовали — привяжите прежний номер к новому кнопкой ниже (фото, отчёты, история переедут вместе с ним); "
                 "иначе заведите комплекс или поправьте номер в задании — без этого в боте не будет адреса и SIM, а фото не появятся в карточке комплекса.")
    orphans: dict[str, dict[str, Any]] = {}
    for p, n in tasks:
        orphans.setdefault(p, {"id": p, "photos": 0, "tasks": 0})["tasks"] = n
    for p, n in photos:
        orphans.setdefault(p, {"id": p, "photos": 0, "tasks": 0})["photos"] = n
    item["orphans"] = [{**o, "suggest": refs.suggest(o["id"]) or ""} for o in list(orphans.values())[:30]]
    return item


async def _check_security() -> dict[str, Any]:
    """Журнал безопасности: перебор паролей и отказы в доступе за сутки; пароль администратора по умолчанию."""
    from app.services import security_log  # noqa: PLC0415

    info = await security_log.summary(24)
    fails, locked, denied = info["login_fail"], info["login_locked"], info["access_denied"]
    value = f"за сутки: неудачных входов {fails}, блокировок {locked}, отказов в доступе {denied}"
    if locked or fails >= 50:
        return _item("security", "Безопасность (журнал событий)", WARN, value,
                     "Много неудачных входов или блокировок: возможен подбор паролей. Откройте «Настройки → Журнал безопасности», посмотрите логины и адреса компьютеров.")
    return _item("security", "Безопасность (журнал событий)", OK, value)


async def collect(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < 20:
        return _cache["value"]
    items = [_check_time(), _check_disk(), _check_db(), _check_backup(), _check_tasks(), _check_points_health(), _check_bot(), _check_load()]
    extra = await asyncio.gather(_check_robot(), _check_ai(), _check_photos(), _check_security(), _check_point_refs(), return_exceptions=True)
    for it in extra:
        if isinstance(it, dict):
            items.append(it)
    items = [i for i in items if i]
    worst = max((i["level"] for i in items), key=lambda lv: _ORDER[lv], default=OK)
    value = {"level": worst, "items": items, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "uptime_seconds": int(time.time() - runtime_state.STARTED_AT), "timezone": tz.apply() if not hasattr(time, "tzset") else os.environ.get("TZ", "")}
    _cache.update(at=now, value=value)
    return value


async def summary() -> dict[str, Any]:
    full = await collect()
    problems = [{"title": i["title"], "level": i["level"], "value": i["value"]} for i in full["items"] if i["level"] != OK]
    return {"level": full["level"], "problems": problems}
