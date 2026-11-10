"""Подгруппы АПК («Все АПК» → подгруппы): именованные подборки комплексов. Комплекс можно добавить вручную или из файла Яндекс.Карт; у каждого комплекса
в подгруппе свои описание (note), «что нужно на выезде» (need) и тип работы — они НЕ связаны с описанием в карточке комплекса и с другими подгруппами.
Таблицы: apk_groups (подгруппа) и apk_group_points (состав), см. app/db/database.py. Номер комплекса — текстовая ссылка (как в point_plans):
переименование комплекса и слияние дублей обновляют её, удаление комплекса убирает его из подгрупп."""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Optional

from app.core.text import decode_entities
from app.db.database import get_connection

MEMBER_FIELDS = ("note", "need", "work_type")
NAME_MAX = 60
TEXT_MAX = 2000


def clean_name(name: Any) -> str:
    """Название: лишние пробелы убраны, не длиннее 60 символов."""
    return " ".join(decode_entities(str(name or "")).split())[:NAME_MAX]


def clean_text(text: Any) -> str:
    return decode_entities(str(text or "")).strip()[:TEXT_MAX]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _group_row(row) -> dict[str, Any]:
    # kind: '' — обычная подгруппа; 'unrepaired' — автоматическая «Требуют повторного выезда» (app/services/apk_group_auto.py), вручную не меняется
    return {"id": row[0], "name": row[1], "description": row[2] or "", "created_at": row[3], "created_by": row[4] or "", "kind": row[6] if len(row) > 6 else "",
            "auto_remove": bool(row[7]) if len(row) > 7 else False}


async def list_groups() -> list[dict[str, Any]]:
    """Все подгруппы с числом комплексов и разбивкой по типам работы (для плашек над списком «Все АПК»)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select g.id, g.name, g.description, g.created_at, g.created_by, "
            "(select count(*) from apk_group_points m where m.group_id = g.id), g.kind, g.auto_remove from apk_groups g order by (g.kind = ''), pylower(g.name), g.id")
        groups = []
        for r in await cur.fetchall():
            item = _group_row(r)
            item["count"] = r[5]
            groups.append(item)
        cur = await conn.execute("select group_id, work_type, count(*) from apk_group_points group by group_id, work_type")
        types: dict[int, dict[str, int]] = {}
        for gid, wt, n in await cur.fetchall():
            types.setdefault(gid, {})[wt or ""] = n
    for item in groups:
        item["types"] = types.get(item["id"], {})
    return groups


async def get(group_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select g.id, g.name, g.description, g.created_at, g.created_by, (select count(*) from apk_group_points m where m.group_id = g.id), g.kind, g.auto_remove "
            "from apk_groups g where g.id = ?", (group_id,))
        row = await cur.fetchone()
    if not row:
        return None
    item = _group_row(row)
    item["count"] = row[5]
    return item


async def find_by_name(name: str) -> Optional[dict[str, Any]]:
    name = clean_name(name)
    if not name:
        return None
    async with get_connection() as conn:
        cur = await conn.execute("select id from apk_groups where pylower(name) = pylower(?)", (name,))
        row = await cur.fetchone()
    return await get(row[0]) if row else None


async def create(name: str, description: str, created_by: str, auto_remove: bool = False) -> dict[str, Any]:
    name = clean_name(name)
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into apk_groups (name, description, created_at, created_by, auto_remove) values (?, ?, ?, ?, ?)", (name, clean_text(description), _now(), created_by or "", int(bool(auto_remove))))
        group_id = cur.lastrowid
        await conn.commit()
    return await get(group_id)  # type: ignore[return-value]


async def update(group_id: int, *, name: Optional[str] = None, description: Optional[str] = None, auto_remove: Optional[bool] = None) -> None:
    sets, params = [], []
    if auto_remove is not None:
        sets.append("auto_remove = ?")
        params.append(int(bool(auto_remove)))
    if name is not None:
        sets.append("name = ?")
        params.append(clean_name(name))
    if description is not None:
        sets.append("description = ?")
        params.append(clean_text(description))
    if not sets:
        return
    async with get_connection() as conn:
        await conn.execute(f"update apk_groups set {', '.join(sets)} where id = ?", (*params, group_id))
        await conn.commit()


async def delete(group_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from apk_group_points where group_id = ?", (group_id,))     # внешний ключ без каскада в этом соединении — убираем состав сами
        await conn.execute("delete from apk_groups where id = ?", (group_id,))
        await conn.commit()


async def member_ids(group_id: int) -> list[str]:
    async with get_connection() as conn:
        cur = await conn.execute("select point_id from apk_group_points where group_id = ? order by point_id", (group_id,))
        return [r[0] for r in await cur.fetchall()]


def _fields(work_type: Optional[str], note: Optional[str], need: Optional[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    if work_type is not None:
        out["work_type"] = (work_type or "").strip()
    if note is not None:
        out["note"] = clean_text(note)
    if need is not None:
        out["need"] = clean_text(need)
    return out


async def add_points(group_id: int, point_ids: Iterable[str], *, work_type: Optional[str] = None, note: Optional[str] = None,
                     need: Optional[str] = None) -> tuple[int, int]:
    """Добавляет комплексы в подгруппу (номера должны быть уже проверены вызывающим). Те, что уже в подгруппе, не дублируются, но получают переданные
    поля (тип/описание/«что нужно»), если они указаны. Возвращает (добавлено, уже было)."""
    values = _fields(work_type, note, need)
    now = _now()
    added = already = 0
    seen: set[str] = set()
    async with get_connection() as conn:
        for pid in point_ids:
            pid = (pid or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            cur = await conn.execute("select 1 from apk_group_points where group_id = ? and point_id = ?", (group_id, pid))
            if await cur.fetchone():
                already += 1
                if values:
                    await conn.execute(
                        f"update apk_group_points set {', '.join(f'{k} = ?' for k in values)} where group_id = ? and point_id = ?", (*values.values(), group_id, pid))
                continue
            await conn.execute(
                "insert into apk_group_points (group_id, point_id, note, need, work_type, added_at) values (?, ?, ?, ?, ?, ?)",
                (group_id, pid, values.get("note", ""), values.get("need", ""), values.get("work_type", ""), now))
            added += 1
        await conn.commit()
    return added, already


async def update_members(group_id: int, point_ids: Iterable[str], *, work_type: Optional[str] = None, note: Optional[str] = None,
                         need: Optional[str] = None) -> int:
    """Меняет переданные поля у комплексов подгруппы (остальные поля не трогает). Возвращает, у скольких изменилось."""
    values = _fields(work_type, note, need)
    ids = [p for p in dict.fromkeys(point_ids) if p]
    if not values or not ids:
        return 0
    changed = 0
    async with get_connection() as conn:
        for pid in ids:
            cur = await conn.execute(
                f"update apk_group_points set {', '.join(f'{k} = ?' for k in values)} where group_id = ? and point_id = ?", (*values.values(), group_id, pid))
            changed += cur.rowcount
        await conn.commit()
    return changed


async def remove_points(group_id: int, point_ids: Iterable[str]) -> int:
    ids = [p for p in dict.fromkeys(point_ids) if p]
    removed = 0
    async with get_connection() as conn:
        for pid in ids:
            cur = await conn.execute("delete from apk_group_points where group_id = ? and point_id = ?", (group_id, pid))
            removed += cur.rowcount
        await conn.commit()
    return removed


async def groups_of_point(point_id: str) -> list[dict[str, Any]]:
    """В каких подгруппах состоит комплекс и что в них про него написано (для «Проверки» и карточки)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select g.id, g.name, m.work_type, m.note, m.need from apk_group_points m join apk_groups g on g.id = m.group_id "
            "where m.point_id = ? order by pylower(g.name)", (point_id,))
        return [{"id": r[0], "name": r[1], "work_type": r[2] or "", "note": r[3] or "", "need": r[4] or ""} for r in await cur.fetchall()]


async def groups_of_points(point_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    ids = [p for p in dict.fromkeys(point_ids) if p]
    async with get_connection() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                f"select m.point_id, g.id, g.name, m.work_type, m.note, m.need from apk_group_points m join apk_groups g on g.id = m.group_id "
                f"where m.point_id in ({marks}) order by pylower(g.name)", chunk)
            for pid, gid, name, wt, note, need in await cur.fetchall():
                out.setdefault(pid, []).append({"id": gid, "name": name, "work_type": wt or "", "note": note or "", "need": need or ""})
    return out
