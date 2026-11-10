"""История уведомлений диспетчера — одна лента из того, о чём диспетчеру приходили (или могли прийти) уведомления:
проверки комплексов оператором, решения «отпустить без ремонта / демонтаж» и отчёты монтёров. Отдельной таблицы нет:
лента собирается из уже существующих данных (verification_requests, demontage_requests, access_issue_requests,
task_visit_reports), поэтому в неё попадает и всё, что было до появления этого меню.

Вид записи: kind (check | decision | report), ts (время события, UTC), point_id/point_address, title, status/tone,
text, person, fault/needs (только у отчётов «Не работает» — по ним нужен повторный выезд), resolved_by/resolved_at."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection

KINDS = {
    "check": "Проверки",
    "decision": "Решения по запросам монтёров",
    "report": "Отчёты",
    "repeat": "Нужен повторный выезд",
}

_CHECK_STATUS = {
    "pending": ("ждёт решения оператора", "warn"),
    "confirmed": ("полностью работает", "ok"),
    "remark": ("работает, но с замечанием — бригаду отпустили", "warn"),
    "issue": ("продолжить работу — оператор не принял", "bad"),
}
_DEMONTAGE_STATUS = {
    "pending": ("ждёт решения диспетчера", "warn"),
    "approved": ("одобрено", "ok"),
    "declined": ("отклонено", "bad"),
}


def _bounds(date_from: str, date_to: str, tz_offset_minutes: int) -> tuple[Optional[str], Optional[str]]:
    """Границы суток по часам браузера в UTC (время событий хранится в UTC); `tz_offset_minutes` — как у JS
    getTimezoneOffset()."""
    low = high = None
    if date_from:
        low = (dt.datetime.fromisoformat(date_from) + dt.timedelta(minutes=tz_offset_minutes)).replace(tzinfo=dt.timezone.utc).isoformat()
    if date_to:
        high = (dt.datetime.fromisoformat(date_to) + dt.timedelta(days=1, minutes=tz_offset_minutes)).replace(tzinfo=dt.timezone.utc).isoformat()
    return low, high


def _clean_who(name: Optional[str]) -> str:
    name = (name or "").strip()
    return name[3:] if name.startswith("vk:") else name


def _search_clause(cols: list[str], q: str) -> tuple[str, list[Any]]:
    if not q:
        return "", []
    s = f"%{q.strip()}%"
    return " and (" + " or ".join(f"pylower(coalesce({c}, '')) like pylower(?)" for c in cols) + ")", [s] * len(cols)


async def _reports(conn, low, high, q, limit, kind) -> tuple[list[dict[str, Any]], int]:
    where = "r.imported = 0"
    params: list[Any] = []
    if low:
        where += " and r.created_at >= ?"
        params.append(low)
    if high:
        where += " and r.created_at < ?"
        params.append(high)
    if kind == "repeat":
        where += " and r.result_status = 'Не работает'"
    clause, qp = _search_clause(["r.point_id", "p.address", "r.comment", "r.fault", "r.needs", "r.created_by", "pe.full_name", "r.visit_type"], q)
    where += clause
    params += qp
    base = ("from task_visit_reports r left join points p on p.id = r.point_id left join tasks t on t.id = r.task_id "
            "left join crews c on c.id = t.team left join people pe on pe.id = r.author_person_id where " + where)
    cur = await conn.execute(f"select count(*) {base}", params)
    (total,) = await cur.fetchone()
    cur = await conn.execute(
        "select r.created_at, r.task_id, r.point_id, p.address, r.visit_type, r.result_status, r.outcome, r.comment, "
        f"coalesce(pe.full_name, r.created_by), r.fault, r.needs, c.name {base} order by r.created_at desc limit ?",
        [*params, limit],
    )
    items = []
    for ts, task_id, point_id, addr, vtype, status, outcome, comment, by, fault, needs, crew in await cur.fetchall():
        repeat = status == "Не работает"
        result = outcome or status or "—"
        items.append({
            "id": f"report:{task_id}", "kind": "report", "ts": ts, "task_id": task_id, "point_id": point_id,
            "point_address": addr or "", "title": f"Отчёт · {vtype}", "status": result,
            "tone": "bad" if repeat else ("ok" if status in ("Работает", "Установлен") else "info"),
            "text": comment or "", "person": _clean_who(by), "crew": crew or "", "repeat_visit": repeat,
            "fault": fault or "", "needs": needs or "", "resolved_by": "", "resolved_at": None,
        })
    return items, total


async def _checks(conn, low, high, q, limit) -> tuple[list[dict[str, Any]], int]:
    where, params = "1 = 1", []
    if low:
        where += " and v.created_at >= ?"
        params.append(low)
    if high:
        where += " and v.created_at < ?"
        params.append(high)
    clause, qp = _search_clause(["v.point_id", "p.address", "v.issue_comment", "v.resolved_by"], q)
    where += clause
    params += qp
    base = ("from verification_requests v left join points p on p.id = v.point_id left join tasks t on t.id = v.task_id "
            "left join crews c on c.id = t.team where " + where)
    cur = await conn.execute(f"select count(*) {base}", params)
    (total,) = await cur.fetchone()
    cur = await conn.execute(
        "select v.id, v.created_at, v.task_id, v.point_id, p.address, v.status, v.resolved_at, v.resolved_by, v.issue_comment, c.name "
        f"{base} order by v.created_at desc limit ?",
        [*params, limit],
    )
    items = []
    for vid, ts, task_id, point_id, addr, status, resolved_at, resolved_by, comment, crew in await cur.fetchall():
        label, tone = _CHECK_STATUS.get(status, (status, "info"))
        items.append({
            "id": f"check:{vid}", "kind": "check", "ts": ts, "task_id": task_id, "point_id": point_id, "point_address": addr or "",
            "title": "Проверка комплекса оператором", "status": label, "tone": tone, "text": comment or "", "person": "",
            "crew": crew or "", "repeat_visit": False, "fault": "", "needs": "", "resolved_by": resolved_by or "", "resolved_at": resolved_at,
        })
    return items, total


async def _decisions(conn, low, high, q, limit) -> tuple[list[dict[str, Any]], int]:
    items: list[dict[str, Any]] = []
    total = 0
    # Запросы «отпустить без ремонта» / «демонтаж» (монтёр или диспетчер)
    where, params = "1 = 1", []
    if low:
        where += " and d.created_at >= ?"
        params.append(low)
    if high:
        where += " and d.created_at < ?"
        params.append(high)
    clause, qp = _search_clause(["d.point_id", "p.address", "d.reason", "d.decline_comment", "d.resolved_by"], q)
    where += clause
    params += qp
    base = ("from demontage_requests d left join points p on p.id = d.point_id left join tasks t on t.id = d.task_id "
            "left join crews c on c.id = t.team where " + where)
    cur = await conn.execute(f"select count(*) {base}", params)
    total += (await cur.fetchone())[0]
    cur = await conn.execute(
        "select d.id, d.created_at, d.task_id, d.point_id, p.address, d.kind, d.reason, d.initiated_by, d.status, d.resolved_at, "
        f"d.resolved_by, d.decline_comment, c.name {base} order by d.created_at desc limit ?",
        [*params, limit],
    )
    for did, ts, task_id, point_id, addr, kind, reason, by, status, resolved_at, resolved_by, decline, crew in await cur.fetchall():
        label, tone = _DEMONTAGE_STATUS.get(status, (status, "info"))
        what = "Отпустить бригаду без ремонта" if kind == "release" else "Демонтаж комплекса"
        who = "по решению диспетчера" if by == "dispatcher" else "по запросу монтёра"
        items.append({
            "id": f"decision:d{did}", "kind": "decision", "ts": ts, "task_id": task_id, "point_id": point_id, "point_address": addr or "",
            "title": f"{what} — {who}", "status": label, "tone": tone, "text": decline if status == "declined" and decline else (reason or ""),
            "person": "", "crew": crew or "", "repeat_visit": False, "fault": "", "needs": "",
            "resolved_by": resolved_by or "", "resolved_at": resolved_at,
        })
    # Прежние «нет доступа»: решение диспетчера отпустить или демонтировать
    where, params = "1 = 1", []
    if low:
        where += " and a.created_at >= ?"
        params.append(low)
    if high:
        where += " and a.created_at < ?"
        params.append(high)
    clause, qp = _search_clause(["a.point_id", "p.address", "a.demontage_note", "a.resolved_by"], q)
    where += clause
    params += qp
    base = ("from access_issue_requests a left join points p on p.id = a.point_id left join tasks t on t.id = a.task_id "
            "left join crews c on c.id = t.team where " + where)
    cur = await conn.execute(f"select count(*) {base}", params)
    total += (await cur.fetchone())[0]
    cur = await conn.execute(
        "select a.id, a.created_at, a.task_id, a.point_id, p.address, a.status, a.resolved_at, a.resolved_by, a.demontage_note, c.name "
        f"{base} order by a.created_at desc limit ?",
        [*params, limit],
    )
    for aid, ts, task_id, point_id, addr, status, resolved_at, resolved_by, note, crew in await cur.fetchall():
        label, tone = {
            "pending": ("ждёт решения диспетчера", "warn"), "released": ("бригаду отпустили без ремонта", "info"),
            "demontage": ("оформлен демонтаж", "info"),
        }.get(status, (status, "info"))
        items.append({
            "id": f"decision:a{aid}", "kind": "decision", "ts": ts, "task_id": task_id, "point_id": point_id, "point_address": addr or "",
            "title": "Нет доступа к комплексу — решение по бригаде", "status": label, "tone": tone, "text": note or "", "person": "",
            "crew": crew or "", "repeat_visit": False, "fault": "", "needs": "", "resolved_by": resolved_by or "", "resolved_at": resolved_at,
        })
    return items, total


async def list_notifications(
    kind: str = "", date_from: str = "", date_to: str = "", q: str = "", limit: int = 100, offset: int = 0,
    tz_offset_minutes: int = 0,
) -> dict[str, Any]:
    """Лента уведомлений, новые сверху. `kind` — check | decision | report | repeat (пусто — всё)."""
    low, high = _bounds(date_from, date_to, tz_offset_minutes)
    want = limit + offset
    items: list[dict[str, Any]] = []
    total = 0
    async with get_connection() as conn:
        if kind in ("", "report", "repeat"):
            got, n = await _reports(conn, low, high, q, want, kind)
            items += got
            total += n
        if kind in ("", "check"):
            got, n = await _checks(conn, low, high, q, want)
            items += got
            total += n
        if kind in ("", "decision"):
            got, n = await _decisions(conn, low, high, q, want)
            items += got
            total += n
        cur = await conn.execute("select count(*) from task_visit_reports where imported = 0 and result_status = 'Не работает'")
        (repeat_total,) = await cur.fetchone()
    items.sort(key=lambda i: i["ts"] or "", reverse=True)
    return {"items": items[offset:offset + limit], "total": total, "repeat_total": repeat_total, "kinds": KINDS}
