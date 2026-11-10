"""Объединение дублей комплексов: «ARENA-1», «ARENA-1 +1», «ARENA-1 +2» — один комплекс.

Дубль возникает, когда метка с карты или строка файла не распознана как уже заведённый комплекс и создаётся новая точка с тем же
названием (с хвостом «+1», «+2»). Объединение переносит ВСЁ, что привязано к дублю, на основной комплекс — задания, отчёты и
историю выездов, фото, историю статусов, запросы монтёров, сессии бота, оборудование, журнал оборудования, солнечные панели,
чат, связки SIM, «Живые карты» — а потом удаляет дубль. Пустые поля основного комплекса (адрес, координаты, IP, SIM, сертификат…)
заполняются значениями из дубля. Фото остаются на своих местах на диске (в записи запоминается путь), ничего не копируется."""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core import field_crypto, map_events, stats_cache
from app.core.names import compact_name, strip_dup_suffix
from app.core.work_types import AUX_TYPE_NAME
from app.db.database import _POINT_ID_REFERENCES, get_connection
from app.repositories import points_repository
from app.services import photo_storage

log = logging.getLogger(__name__)

# Поля точки, которые из дубля переходят в основной комплекс, если там пусто.
_FILL_FIELDS = ["address", "description", "lat", "lon", "inventory_number", "sim_number", "ip_address", "port", "notes",
                "login", "password", "verification_valid_until", "power_source", "category_id"]
# Таблицы, где у комплекса может быть только одна запись: при конфликте остаётся запись основного комплекса.
_ONE_PER_POINT = {"warehouse_items", "point_sim_links", "solar_panels"}
_EXTRA_REFERENCES = [("point_components", "point_id"), ("chat_messages", "point_id"), ("point_sim_links", "point_id")]


async def _table_exists(conn, table: str) -> bool:
    cur = await conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,))
    return await cur.fetchone() is not None


async def _counts() -> dict[str, dict[str, int]]:
    """Сколько заданий, отчётов и фото привязано к каждому комплексу (для списка дублей)."""
    out: dict[str, dict[str, int]] = {}
    async with get_connection() as conn:
        for key, table, col in (("tasks", "tasks", "point"), ("reports", "task_visit_reports", "point_id"), ("photos", "visit_photos", "point_id")):
            if not await _table_exists(conn, table):
                continue
            cur = await conn.execute(f"select {col}, count(*) from {table} group by {col}")
            for pid, n in await cur.fetchall():
                out.setdefault(pid, {})[key] = n
    return out


async def find_groups() -> list[dict[str, Any]]:
    """Группы дублей: основной комплекс и его дубли. kind: 'suffix' — «НОМЕР +1» при заведённом «НОМЕР»; 'similar' — номера,
    отличающиеся только регистром, пробелами, знаками или русскими/латинскими буквами-двойниками."""
    points = await points_repository.list_points()
    by_id = {p["id"]: p for p in points}
    counts = await _counts()
    weight = lambda pid: sum(counts.get(pid, {}).values())  # noqa: E731

    by_compact: dict[str, list[str]] = {}
    for p in points:
        by_compact.setdefault(compact_name(p["id"]), []).append(p["id"])

    groups: dict[str, dict[str, Any]] = {}
    claimed: set[str] = set()
    # 1) «НОМЕР +N» при заведённом «НОМЕР»
    for p in points:
        base_label, n = strip_dup_suffix(p["id"])
        if n is None:
            continue
        cands = [c for c in by_compact.get(compact_name(base_label), []) if c != p["id"] and strip_dup_suffix(c)[1] is None]
        if len(cands) != 1:
            continue
        g = groups.setdefault(cands[0], {"kind": "suffix", "base": cands[0], "dups": []})
        g["dups"].append(p["id"])
        claimed.add(p["id"])
        claimed.add(cands[0])
    # 2) номера, совпадающие «по сути»
    for ids in by_compact.values():
        left = [i for i in ids if i not in claimed]
        if len(left) < 2:
            continue
        left.sort(key=lambda i: (-weight(i), i))
        groups[left[0]] = {"kind": "similar", "base": left[0], "dups": left[1:]}

    result = []
    for g in groups.values():
        item = {"kind": g["kind"], "base": _describe(by_id[g["base"]], counts), "dups": [_describe(by_id[d], counts) for d in sorted(g["dups"])]}
        result.append(item)
    result.sort(key=lambda g: (g["kind"] != "suffix", g["base"]["id"].casefold()))
    return result


def _describe(p: dict[str, Any], counts: dict[str, dict[str, int]]) -> dict[str, Any]:
    c = counts.get(p["id"], {})
    return {"id": p["id"], "address": p.get("address") or "", "status": p.get("status") or "", "inventory_number": p.get("inventory_number") or "",
            "tasks": c.get("tasks", 0), "reports": c.get("reports", 0), "photos": c.get("photos", 0)}


def _swap_id(values: list[Any], old: str, new: str) -> list[Any]:
    out: list[Any] = []
    for v in values:
        v = new if v == old else v
        if v not in out:
            out.append(v)
    return out


async def merge(base_id: str, dup_ids: list[str]) -> dict[str, Any]:
    """Перенести всё с дублей на основной комплекс и удалить дубли. Возвращает, что и сколько перенесено."""
    base_id = (base_id or "").strip()
    dups = [d for d in dict.fromkeys(dup_ids or []) if d and d != base_id]
    if not base_id or not dups:
        raise ValueError("Не указан комплекс или его дубли")
    summary: dict[str, Any] = {"base": base_id, "merged": [], "moved": {}, "left": {}}
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from points where id = ?", (base_id,))
        if not await cur.fetchone():
            raise ValueError(f"Комплекса «{base_id}» нет")
        for dup in dups:
            cur = await conn.execute("select 1 from points where id = ?", (dup,))
            if not await cur.fetchone():
                continue
            # поля: заполняем пустые у основного
            cols = ", ".join(_FILL_FIELDS)
            cur = await conn.execute(f"select {cols} from points where id = ?", (dup,))
            d_row = await cur.fetchone()
            cur = await conn.execute(f"select {cols} from points where id = ?", (base_id,))
            b_row = await cur.fetchone()
            fill = {c: dv for c, dv, bv in zip(_FILL_FIELDS, d_row, b_row) if (bv in (None, "")) and dv not in (None, "")}
            if fill:
                # логин и пароль в базе лежат зашифрованными — пишем в столбцы *_enc (прочитанные выше значения уже расшифрованы)
                names = [f"{c}_enc" if c in ("login", "password") else c for c in fill]
                values = [field_crypto.encrypt(f"points.{c}", v) if c in ("login", "password") else v for c, v in fill.items()]
                await conn.execute(f"update points set {', '.join(f'{n} = ?' for n in names)} where id = ?", (*values, base_id))

            # фото: файлы остаются там, где лежат, — в записи запоминаем путь (иначе по новому номеру комплекса файл не найти)
            cur = await conn.execute("select id, point_id, taken_date, file_ext from visit_photos where point_id = ? and rel_path is null", (dup,))
            for pid_, point_id, taken_date, ext in await cur.fetchall():
                rel = photo_storage._rel({"id": pid_, "point_id": point_id, "taken_date": taken_date, "file_ext": ext})
                await conn.execute("update visit_photos set rel_path = ? where id = ?", (rel, pid_))

            for table, col in [*_POINT_ID_REFERENCES, *_EXTRA_REFERENCES]:
                if not await _table_exists(conn, table):
                    continue
                if table == "point_components":
                    cur = await conn.execute(f"update {table} set {col} = ? where {col} = ? and not exists "
                                             f"(select 1 from {table} where {col} = ?)", (base_id, dup, base_id))
                elif table == "tasks":      # у вспомогательной точки в этом поле её собственная подпись, а не номер комплекса
                    cur = await conn.execute("update or ignore tasks set point = ? where point = ? and type != ?", (base_id, dup, AUX_TYPE_NAME))
                elif table in _ONE_PER_POINT:
                    cur = await conn.execute(f"update or ignore {table} set {col} = ? where {col} = ?", (base_id, dup))
                else:
                    cur = await conn.execute(f"update or ignore {table} set {col} = ? where {col} = ?", (base_id, dup))
                if cur.rowcount:
                    summary["moved"][table] = summary["moved"].get(table, 0) + cur.rowcount
            # «Живые карты»: списки комплексов
            if await _table_exists(conn, "live_maps"):
                cur = await conn.execute("select id, params_json, members_json from live_maps where params_json like ? or members_json like ?",
                                         (f"%{json.dumps(dup, ensure_ascii=False)}%", f"%{json.dumps(dup, ensure_ascii=False)}%"))
                for lm_id, params_json, members_json in await cur.fetchall():
                    try:
                        params = json.loads(params_json) if params_json else {}
                        members = json.loads(members_json) if members_json else []
                    except ValueError:
                        continue
                    if isinstance(params.get("point_ids"), list):
                        params["point_ids"] = _swap_id(params["point_ids"], dup, base_id)
                    if isinstance(members, list):
                        members = _swap_id(members, dup, base_id)
                    await conn.execute("update live_maps set params_json = ?, members_json = ? where id = ?",
                                       (json.dumps(params, ensure_ascii=False), json.dumps(members, ensure_ascii=False) if members_json else "", lm_id))
            # остатки (одна запись на комплекс — у основного уже есть своя) удаляются вместе с дублем
            await conn.execute("delete from point_sim_links where point_id = ?", (dup,))
            await conn.execute("delete from apk_group_points where point_id = ?", (dup,))      # в подгруппе уже стоит основной комплекс (запись дубля, если она там была, перенесена выше)
            await conn.execute("delete from points where id = ?", (dup,))
            summary["merged"].append(dup)
        await conn.commit()
    stats_cache.invalidate()
    map_events.kick()
    return summary
