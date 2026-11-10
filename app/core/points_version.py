"""Версия справочника комплексов для HTTP-кэша (ETag) и готовых ответов.

Список «Все АПК», карта и справочник в календаре опрашиваются из каждой открытой вкладки каждые несколько секунд, а меняются
редко. Пока данные те же, ответ готов без чтения всех комплексов: браузер получает «304 Not Modified» (несколько байт), а
одинаковые запросы разных вкладок берут готовое тело из памяти.

«Те же» определяется по отпечатку (хэш) того, что реально видно в ответах: поля комплексов, категории, число компонентов на
складе. Отпечаток пересчитывается не чаще раза в секунду и только когда кто-то спрашивает — поэтому ловит любые изменения,
в том числе сделанные в обход обычных путей (импорт, объединение дублей), и не требует вызывать что-то при каждой записи.
Время «проверено N мин назад» в отпечаток не входит (его пишет фоновая проверка доступности постоянно) — вместо этого ETag
меняется раз в минуту. ETag включает «номер запуска»: после перезапуска сервера старые ETag из браузеров не совпадут."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from typing import Optional

from app.db.database import add_write_listener, get_connection

_BOOT = secrets.token_hex(3)
_MIN_INTERVAL = 1.0                    # как часто можно пересчитывать отпечаток, с
_POINT_COLUMNS = ["id", "status", "lat", "lon", "address", "description", "ip_address", "port", "sim_number", "inventory_number", "notes",
                  "login", "password", "verification_valid_until", "power_source", "category_id", "port_down_since",
                  "fault_reason", "fault_need", "fault_at", "fault_by"]
_POINTS_SQL = ("select count(*), group_concat(" + " || '|' || ".join(f"coalesce({c}, '')" for c in _POINT_COLUMNS) + ", char(10)) from points")

_fp = ""
_fp_at = 0.0
_lock: Optional[asyncio.Lock] = None
_payloads: dict[tuple, tuple[str, bytes, Optional[int]]] = {}      # ключ запроса -> (etag, тело, всего строк)
_MAX_PAYLOADS = 48


def bump() -> None:
    """Данные точно изменились — следующий запрос не ждёт секунду, а сразу пересчитает отпечаток."""
    global _fp_at
    _fp_at = 0.0


add_write_listener(bump)        # любая запись в базу (кроме тихих фоновых отметок) — отпечаток проверится заново при следующем запросе


async def fingerprint() -> str:
    global _fp, _fp_at, _lock
    if _fp and time.monotonic() - _fp_at < _MIN_INTERVAL:
        return _fp
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _fp and time.monotonic() - _fp_at < _MIN_INTERVAL:
            return _fp
        h = hashlib.blake2b(digest_size=10)
        async with get_connection() as conn:
            cur = await conn.execute(_POINTS_SQL)
            n, blob = await cur.fetchone()
            h.update(f"{n}|{blob or ''}".encode("utf-8"))
            cur = await conn.execute("select count(*), coalesce(max(rowid), 0), coalesce(max(created_at), '') from task_visit_reports")      # последний выезд виден в карточке списка
            h.update(repr(await cur.fetchone()).encode("utf-8"))
            cur = await conn.execute("select group_concat(coalesce(id, '') || coalesce(name, '') || coalesce(color, '') || coalesce(is_legacy, ''), char(10)) from point_categories")
            h.update(((await cur.fetchone())[0] or "").encode("utf-8"))
            cur = await conn.execute(          # состав подгрупп и их тексты видны в ответе списка «Все АПК» (group_id=…)
                "select group_concat(group_id || ':' || point_id || ':' || work_type || ':' || note || ':' || need, char(10)) from apk_group_points")
            h.update(((await cur.fetchone())[0] or "").encode("utf-8"))
            cur = await conn.execute("select group_concat(id || ':' || name, char(10)) from apk_groups")
            h.update(((await cur.fetchone())[0] or "").encode("utf-8"))
            cur = await conn.execute(
                "select group_concat(point_id || ':' || c || ':' || s || ':' || names, char(10)) from "
                "(select point_id, count(*) c, coalesce(sum(in_stock), 0) s, group_concat(name || '=' || in_stock, ',') names "
                "from (select point_id, name, in_stock from point_components order by point_id, sort_order, id) group by point_id)")
            h.update(((await cur.fetchone())[0] or "").encode("utf-8"))
        _fp, _fp_at = h.hexdigest(), time.monotonic()
        return _fp


async def etag(role: str, query: str) -> str:
    q = hashlib.md5(query.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f'W/"{_BOOT}.{await fingerprint()}.{int(time.time() // 60)}.{role}.{q}"'


def get_payload(key: tuple, tag: str) -> Optional[tuple[bytes, Optional[int]]]:
    hit = _payloads.get(key)
    if hit and hit[0] == tag:
        return hit[1], hit[2]
    return None


def put_payload(key: tuple, tag: str, body: bytes, total: Optional[int]) -> None:
    if len(_payloads) >= _MAX_PAYLOADS:
        _payloads.pop(next(iter(_payloads)), None)
    _payloads[key] = (tag, body, total)
