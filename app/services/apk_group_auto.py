"""Автоматическая подгруппа «Требуют повторного выезда» («Все АПК» → подгруппы).

В неё сами попадают комплексы, которые сейчас не работают и по которым выезд ничего не исправил:
  • «не удалось починить» — в отчёте ПОСЛЕДНЕГО выезда по комплексу итог «Не работает» (причина и «что нужно» берутся из отчёта и из полей «Неисправность» карточки);
  • «выезда не было» — задание на комплекс прошло (день выезда уже позади), а отчёта по нему нет; смотрим последние 60 дней, чтобы старые забытые задания не забивали список.
Как только комплекс исправлен — его статус стал рабочим («Работает» по отчёту, вручную или проверкой связи, «Установлен», «Демонтирован», «На складе»), он сам уходит из подгруппы;
то же, если задание без отчёта удалили или отчёт по нему сдали. Состав пересчитывается сам: сразу после любой записи в базу (не чаще раза в 10 секунд), при открытии «Все АПК»
и раз в 10 минут (прошедший день сам делает невыполненное задание «несостоявшимся»). Вручную подгруппа не меняется (ни состав, ни название, ни описания) — менять нужно сам комплекс:
статус, отчёт, поля «Неисправность / описание» в карточке.

Подгруппа — обычная запись apk_groups с признаком kind='unrepaired' (app/db/database.py): у неё те же описание, «что нужно» и тип работы у каждого комплекса, список и карта «Все АПК»,
выделение рамкой → бригада / план. Тип работы у «не починили» — «Ремонт» (если такой тип есть), у «выезда не было» — тип пропущенного задания."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any, Optional

from app.core import work_types as wt
from app.core.work_types import AUX_TYPE_NAME, REPAIR_TYPE_NAME
from app.db.database import add_write_listener, get_connection

log = logging.getLogger(__name__)

KIND = "unrepaired"
NAME = "Требуют повторного выезда"
DESCRIPTION = ("Ведётся автоматически. Сюда попадают комплексы, которые не работают и по которым выезд не помог (в отчёте последнего выезда «Не работает») "
               "или выезд на комплекс не состоялся (задание прошло без отчёта). Причина и «что нужно» берутся из отчёта. "
               "Как только комплекс исправлен (статус «Работает»), он сам уходит из подгруппы. Вручную подгруппа не меняется.")
# Статусы, при которых комплексу всё ещё нужен выезд (не работает, только пинг, нет питания…). Любой другой — исправен, снят или лежит на складе.
BROKEN = ("Не работает", "Есть только пинг", "Расстрелян\\ Мех. повреждения", "Нет питания", "Не подведено питание")
MISSED_WINDOW_DAYS = 60
TEXT_MAX = 2000
_MIN_GAP = 10.0            # не чаще раза в 10 с после записей в базу
_FULL_EVERY = 600.0        # пересчёт раз в 10 мин в любом случае

_lock = asyncio.Lock()
_state: dict[str, Any] = {"last": 0.0, "dirty": True, "syncing": False, "result": {}}


def _mark_dirty() -> None:
    if not _state["syncing"]:
        _state["dirty"] = True


add_write_listener(_mark_dirty)


def _fmt_date(value: str) -> str:
    v = (value or "")[:10]
    try:
        return dt.date.fromisoformat(v).strftime("%d.%m.%Y")
    except ValueError:
        return v


def _cut(text: str) -> str:
    return (text or "").strip()[:TEXT_MAX]


async def _group_id(conn) -> int:
    cur = await conn.execute("select id from apk_groups where kind = ?", (KIND,))
    row = await cur.fetchone()
    if row:
        return row[0]
    # подгруппа с таким названием, заведённая вручную, становится автоматической (состав пересчитается)
    cur = await conn.execute("select id from apk_groups where pylower(name) = pylower(?) and kind = ''", (NAME,))
    row = await cur.fetchone()
    if row:
        await conn.execute("update apk_groups set kind = ?, description = ? where id = ?", (KIND, DESCRIPTION, row[0]))
        return row[0]
    cur = await conn.execute(
        "insert into apk_groups (name, description, created_at, created_by, kind) values (?, ?, ?, 'система', ?)",
        (NAME, DESCRIPTION, dt.datetime.now(dt.timezone.utc).isoformat(), KIND))
    return cur.lastrowid


async def compute(conn, today: Optional[dt.date] = None) -> dict[str, dict[str, str]]:
    """{номер комплекса: {note, need, work_type}} — кто должен быть в подгруппе прямо сейчас."""
    today = today or dt.date.today()
    known_types = set(wt.work_type_names())
    marks = ",".join("?" for _ in BROKEN)
    wanted: dict[str, dict[str, str]] = {}

    # 1) не удалось починить: последний отчёт по комплексу — «Не работает»
    cur = await conn.execute(
        f"""select p.id, p.fault_reason, p.fault_need, r.visit_type, r.created_at, r.comment, r.fault, r.needs
            from points p
            join task_visit_reports r on r.id = (select r2.id from task_visit_reports r2 where r2.point_id = p.id order by r2.created_at desc, r2.id desc limit 1)
            where p.status in ({marks}) and r.result_status = 'Не работает'""", BROKEN)
    for pid, f_reason, f_need, vtype, at, comment, fault, needs in await cur.fetchall():
        reason = (fault or f_reason or "").strip()
        note = f"Не удалось починить: выезд {_fmt_date(at)}" + (f" ({vtype})" if vtype else "")
        if reason:
            note += f". Причина: {reason}"
        elif (comment or "").strip():
            note += f". {comment.strip()}"
        wanted[pid] = {"note": _cut(note), "need": _cut(needs or f_need or ""), "work_type": REPAIR_TYPE_NAME if REPAIR_TYPE_NAME in known_types else ""}

    # 2) выезда не было: задание прошло, а отчёта нет
    since = (today - dt.timedelta(days=MISSED_WINDOW_DAYS)).isoformat()
    cur = await conn.execute(
        f"""select p.id, p.fault_need, t.date, t.type, t.work, c.name
            from tasks t
            join points p on p.id = t.point
            left join task_visit_reports r on r.task_id = t.id
            left join crews c on c.id = t.team
            where t.type != ? and r.task_id is null and p.status in ({marks})
              and coalesce(nullif(t.date_to, ''), t.date) < ? and coalesce(nullif(t.date_to, ''), t.date) >= ?
            order by t.date desc, t.rowid desc""", (AUX_TYPE_NAME, *BROKEN, today.isoformat(), since))
    seen: set[str] = set()
    for pid, f_need, day, ttype, work, crew in await cur.fetchall():
        if pid in seen:                         # у комплекса несколько прошедших заданий без отчёта — берём самое свежее
            continue
        seen.add(pid)
        note = f"Выезда не было: задание на {_fmt_date(day)} не выполнено" + (f" (бригада «{crew}»)" if crew else "") + (f" — {work.strip()}" if (work or "").strip() else "")
        need = (f_need or work or "").strip()
        if pid in wanted:
            wanted[pid]["note"] = _cut(wanted[pid]["note"] + ". " + note)
            if not wanted[pid]["need"]:
                wanted[pid]["need"] = _cut(need)
        else:
            wanted[pid] = {"note": _cut(note), "need": _cut(need), "work_type": ttype if ttype in known_types else ""}
    return wanted


async def _prune(conn) -> int:
    """Тумблер подгруппы «убирать комплексы после выезда» (apk_groups.auto_remove): комплекс, по которому ПОСЛЕ его попадания в подгруппу сдан отчёт о выезде (отчёт всегда относится к заданию),
    уходит из этой подгруппы. Отчёты, которые были до попадания (старая история), не в счёт; у автоматической подгруппы «Требуют повторного выезда» своё правило — состав ведёт статус."""
    cur = await conn.execute(
        """select m.group_id, m.point_id from apk_group_points m join apk_groups g on g.id = m.group_id
           where g.auto_remove = 1 and g.kind = '' and m.added_at != ''
             and exists (select 1 from task_visit_reports r where r.point_id = m.point_id and r.created_at >= m.added_at)""")
    rows = await cur.fetchall()
    for gid, pid in rows:
        await conn.execute("delete from apk_group_points where group_id = ? and point_id = ?", (gid, pid))
    if rows:
        log.info("Подгруппы с тумблером «убирать после выезда»: убрано комплексов — %d", len(rows))
    return len(rows)


async def prune_visited() -> int:
    """Убрать из подгрупп с включённым тумблером комплексы, по которым уже был выезд (для вызова сразу после включения тумблера)."""
    async with _lock:
        _state["syncing"] = True
        try:
            async with get_connection() as conn:
                n = await _prune(conn)
                await conn.commit()
            return n
        finally:
            _state["syncing"] = False


async def sync() -> dict[str, int]:
    """Привести состав подгруппы в соответствие с данными: добавить, обновить тексты, убрать исправленные. Заодно убирает «посещённые» комплексы из подгрупп с тумблером «убирать после выезда». Возвращает {added, updated, removed, total, pruned}."""
    async with _lock:
        _state["syncing"] = True
        try:
            async with get_connection() as conn:
                gid = await _group_id(conn)
                wanted = await compute(conn)
                cur = await conn.execute("select point_id, note, need, work_type from apk_group_points where group_id = ?", (gid,))
                have = {r[0]: (r[1], r[2], r[3]) for r in await cur.fetchall()}
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                added = updated = removed = 0
                for pid in [p for p in have if p not in wanted]:
                    await conn.execute("delete from apk_group_points where group_id = ? and point_id = ?", (gid, pid))
                    removed += 1
                for pid, v in wanted.items():
                    cur_v = (v["note"], v["need"], v["work_type"])
                    if pid not in have:
                        await conn.execute("insert into apk_group_points (group_id, point_id, note, need, work_type, added_at) values (?, ?, ?, ?, ?, ?)", (gid, pid, *cur_v, now))
                        added += 1
                    elif have[pid] != cur_v:
                        await conn.execute("update apk_group_points set note = ?, need = ?, work_type = ? where group_id = ? and point_id = ?", (*cur_v, gid, pid))
                        updated += 1
                pruned = await _prune(conn)     # подгруппы с тумблером «убирать после выезда»
                await conn.commit()             # и когда менять нечего: подгруппа могла быть только что создана
                if added or updated or removed:
                    log.info("Подгруппа «%s»: добавлено %d, обновлено %d, убрано %d, всего %d", NAME, added, updated, removed, len(wanted))
            _state["last"] = time.monotonic()
            _state["dirty"] = False
            _state["result"] = {"added": added, "updated": updated, "removed": removed, "total": len(wanted), "pruned": pruned}
            return _state["result"]
        finally:
            _state["syncing"] = False


async def sync_if_stale(max_age: float = 20.0) -> None:
    """Для чтения «Все АПК»: пересчитать, если прошлый пересчёт был давно. Ошибка пересчёта читать не мешает."""
    idle = time.monotonic() - _state["last"]
    if idle < 3.0 or (idle < max_age and not _state["dirty"]):      # свежий пересчёт — не повторяем; после записей в базу обновляем уже через 3 секунды
        return
    try:
        await sync()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обновить подгруппу «%s»", NAME)


async def group_id() -> Optional[int]:
    async with get_connection() as conn:
        cur = await conn.execute("select id from apk_groups where kind = ?", (KIND,))
        row = await cur.fetchone()
    return row[0] if row else None


async def loop() -> None:
    """Фон: пересчёт после записей в базу и раз в 10 минут."""
    await asyncio.sleep(5)
    while True:
        try:
            idle = time.monotonic() - _state["last"]
            if (_state["dirty"] and idle >= _MIN_GAP) or idle >= _FULL_EVERY:
                await sync()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Пересчёт подгруппы «%s» не удался", NAME)
            _state["last"] = time.monotonic()
        await asyncio.sleep(5)
