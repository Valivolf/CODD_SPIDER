"""Журнал событий безопасности.

Что пишется (kind): login_ok / login_fail / login_locked (вход, неудачный вход, блокировка из-за перебора), logout, password_change / password_change_fail, user_create / user_delete /
user_role / password_reset (действия над учётными записями), role_create / role_update / role_delete (создание, правка прав и удаление ролей), backup_download / backup_restore (копия базы), key_view / key_change (ключ шифрования полей), admin_action
(изменение настроек и справочников: метод, адрес и код ответа — без тела запроса, чтобы в журнал не попали секреты), access_denied (отказ в доступе: у роли нет нужного права).
У каждого события: время (UTC), логин, роль, адрес компьютера, результат (ok), подробности (короткий JSON). Пароли, токены и содержимое запросов НЕ записываются.

Запись никогда не мешает основному действию: сбой записи только попадает в журнал приложения. Каждое событие дублируется строкой «SECEVENT {json}» в журнал контейнера (docker logs), откуда его можно
пересылать во внешнюю систему сбора событий (syslog/SIEM) средствами Docker. Срок хранения в базе — SECURITY_LOG_RETENTION_DAYS (по умолчанию 365)."""
from __future__ import annotations

import asyncio
import csv
import datetime as dt
import io
import json
import logging
import os
import time
from typing import Any, Optional

from app.db.database import get_connection
from app.repositories import roles_repository

logger = logging.getLogger(__name__)
_event_log = logging.getLogger("app.security")

KINDS = {
    "login_ok": "Вход в систему", "login_fail": "Неудачный вход", "login_locked": "Вход заблокирован (перебор паролей)", "logout": "Выход",
    "password_change": "Смена своего пароля", "password_change_fail": "Неудачная смена пароля", "user_create": "Создана учётная запись",
    "user_delete": "Удалена учётная запись", "user_role": "Изменена роль пользователя", "password_reset": "Администратор сбросил пароль",
    "person_erase": "Удалён сотрудник, сведения обезличены", "role_create": "Создана роль", "role_update": "Изменены права роли", "role_delete": "Удалена роль",
    "backup_download": "Скачана копия базы", "backup_restore": "Восстановление базы из копии", "key_view": "Показан ключ шифрования",
    "key_change": "Заменён ключ шифрования", "admin_action": "Изменение настроек / справочников", "access_denied": "Отказ в доступе",
}
DEFAULT_RETENTION_DAYS = 365


def retention_days() -> int:
    try:
        return max(30, int(os.environ.get("SECURITY_LOG_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


async def record(kind: str, *, username: str = "", role: str = "", ip: str = "", ok: bool = True, details: Optional[dict[str, Any]] = None) -> None:
    """Дописать событие. Не бросает исключений."""
    at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    text = json.dumps(details, ensure_ascii=False)[:500] if details else ""
    try:
        _event_log.info("SECEVENT %s", json.dumps({"at": at, "kind": kind, "user": username, "role": role, "ip": ip, "ok": ok, "details": details or {}}, ensure_ascii=False))
        async with get_connection() as conn:
            await conn.execute("insert into security_events (at, kind, username, role, ip, ok, details) values (?, ?, ?, ?, ?, ?, ?)",
                               (at, kind, (username or "")[:80], role or "", ip or "", 1 if ok else 0, text))
            await conn.commit()
    except Exception:  # noqa: BLE001 — журнал не должен ломать действие пользователя
        logger.exception("Не удалось записать событие безопасности %s", kind)


_denied_seen: dict[tuple[str, str], float] = {}


async def record_denied(*, username: str, role: str, ip: str, method: str, path: str, reason: str) -> None:
    """Отказ в доступе. Одинаковые отказы одному человеку на один адрес чаще раза в минуту не пишутся (страница могла бы опрашивать закрытый адрес каждые 5 секунд и засорить журнал)."""
    now = time.monotonic()
    key = (username, f"{method} {path}")
    if now - _denied_seen.get(key, -1e9) < 60:
        return
    if len(_denied_seen) > 2000:
        _denied_seen.clear()
    _denied_seen[key] = now
    await record("access_denied", username=username, role=role, ip=ip, ok=False, details={"method": method, "path": path[:160], "reason": reason})


def _where(kind: str, username: str, date_from: str, date_to: str, only_failed: bool) -> tuple[str, list[Any]]:
    cond, args = [], []
    if kind:
        cond.append("kind = ?")
        args.append(kind)
    if username:
        cond.append("lower(username) like ?")
        args.append(f"%{username.strip().lower()}%")
    if date_from:
        cond.append("at >= ?")
        args.append(date_from)
    if date_to:
        cond.append("at < ?")
        args.append((dt.date.fromisoformat(date_to) + dt.timedelta(days=1)).isoformat())
    if only_failed:
        cond.append("ok = 0")
    return (" where " + " and ".join(cond)) if cond else "", args


async def list_events(*, limit: int = 100, offset: int = 0, kind: str = "", username: str = "", date_from: str = "", date_to: str = "", only_failed: bool = False) -> dict[str, Any]:
    where, args = _where(kind, username, date_from, date_to, only_failed)
    async with get_connection() as conn:
        cur = await conn.execute(f"select count(*) from security_events{where}", args)
        (total,) = await cur.fetchone()
        cur = await conn.execute(f"select id, at, kind, username, role, ip, ok, details from security_events{where} order by id desc limit ? offset ?", [*args, max(1, min(limit, 500)), max(0, offset)])
        rows = await cur.fetchall()
    await roles_repository.ensure_loaded()
    items = [{"id": r[0], "at": r[1], "kind": r[2], "kind_label": KINDS.get(r[2], r[2]), "username": r[3], "role": r[4], "role_name": roles_repository.role_name(r[4]) if r[4] else "",
              "ip": r[5], "ok": bool(r[6]), "details": r[7]} for r in rows]
    return {"total": total, "items": items}


async def export_csv(*, kind: str = "", username: str = "", date_from: str = "", date_to: str = "", only_failed: bool = False) -> bytes:
    where, args = _where(kind, username, date_from, date_to, only_failed)
    async with get_connection() as conn:
        cur = await conn.execute(f"select at, kind, username, role, ip, ok, details from security_events{where} order by id", args)
        rows = await cur.fetchall()
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Время (UTC)", "Событие", "Код", "Логин", "Роль", "Адрес", "Результат", "Подробности"])
    await roles_repository.ensure_loaded()
    for at, kind_, user, role, ip, ok, details in rows:
        w.writerow([at, KINDS.get(kind_, kind_), kind_, user, roles_repository.role_name(role) if role else "", ip, "успешно" if ok else "отказ/ошибка", details])
    return ("﻿" + out.getvalue()).encode("utf-8")        # BOM — чтобы Excel открыл русский текст правильно


async def summary(hours: int = 24) -> dict[str, Any]:
    """Для «Состояния системы»: сколько неудачных входов и блокировок за последние часы, когда было последнее событие."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    async with get_connection() as conn:
        cur = await conn.execute("select kind, count(*) from security_events where at >= ? and kind in ('login_fail', 'login_locked', 'access_denied') group by kind", (since,))
        counts = {k: n for k, n in await cur.fetchall()}
        cur = await conn.execute("select max(at), count(*) from security_events")
        last, total = await cur.fetchone()
    return {"login_fail": counts.get("login_fail", 0), "login_locked": counts.get("login_locked", 0), "access_denied": counts.get("access_denied", 0), "last": last, "total": total}


async def purge(days: Optional[int] = None) -> int:
    edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days or retention_days())).isoformat(timespec="seconds")
    async with get_connection() as conn:
        cur = await conn.execute("delete from security_events where at < ?", (edge,))
        await conn.commit()
        return cur.rowcount


async def retention_loop() -> None:
    """Раз в сутки убирает события старше срока хранения."""
    while True:
        try:
            removed = await purge()
            if removed:
                logger.info("Журнал безопасности: удалено событий старше срока хранения: %s", removed)
        except Exception:  # noqa: BLE001
            logger.exception("Очистка журнала безопасности не удалась")
        await asyncio.sleep(24 * 3600)
