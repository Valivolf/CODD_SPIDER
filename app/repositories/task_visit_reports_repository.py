"""One visit report per task ("что было сделано на комплексе после
выезда") — see database.py's task_visit_reports table docstring.

Saving a report also drives the point's actual status: a "Демонтирован"
result moves the whole complex to "На складе" (all its components are
marked as on the shelf — see points_repository.set_status; the Склад
page's card refines them); "Установлен" sets the point status to the
dedicated "Установлен" dictionary entry; "Работает"/"Не работает" set
that literal status. All of this reuses points_repository.set_status
(source="visit_report"), so it also feeds point_status_history exactly
like any other status change.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Awaitable, Callable, Optional

import aiosqlite
from app.core.work_types import is_repair_type, work_type_names
from app.core import stats_cache
from app.db.database import get_connection
from app.repositories import points_repository, state_repository, warehouse_items_repository

# visit_type is no longer chosen in this form — it's the task's own `type`
# (see routes_task_visit_reports.py), sourced from the same vocabulary a
# dispatcher picks when creating the task (a managed dictionary now — see
# app/core/work_types.py), except a visit the fitter switched into repair
# mode in the bot, which is stored as "<type> + Ремонт".
def visit_types() -> list[str]:
    """The current work types, in dictionary order."""
    return work_type_names()


RESULT_STATUSES = ["Работает", "Не работает", "Демонтирован", "Установлен"]

# Отчёт со «статусом выезда» (итог работ из «Настройки → Типы выездов»:
# «Найден» / «Не найден»…) хранит его в поле outcome, а result_status у такого
# отчёта пуст — состояние комплекса он не меняет (см. upsert).
_RESULT_TO_POINT_STATUS = {
    "Работает": "Работает",
    "Не работает": "Не работает",
    "Демонтирован": "На складе",
    "Установлен": "Установлен",
}

_FIELDS = ["visit_type", "result_status", "comment", "equipment_removed", "equipment_installed", "outcome", "fault", "needs",
           "sim_removed", "sim_installed", "sim_removed_state"]
# Состояние снятой SIM-карты (при её замене): рабочая — её можно использовать снова, нерабочая — на списание.
SIM_STATES = ("working", "defective")
SIM_STATE_LABELS = {"working": "рабочая", "defective": "нерабочая"}

# Автор отчёта: ФИО человека из «Персонала», к которому привязан отчёт (author_person_id), иначе — как записано при сдаче.
_SELECT = (
    f"select task_id, point_id, {', '.join(_FIELDS)}, created_at, updated_at, created_by, imported, author_person_id, updated_by, source, "
    "(select p.full_name from people p where p.id = task_visit_reports.author_person_id) "
    "from task_visit_reports"
)


def strip_vk(name: Optional[str]) -> str:
    """Прежние отчёты из бота были подписаны «vk:Иванов И.» — приставка «vk:» людям не нужна."""
    value = (name or "").strip()
    return value[3:].strip() if value.lower().startswith("vk:") else value


def _row_to_dict(row) -> dict[str, Any]:
    keys = ["task_id", "point_id", *_FIELDS, "created_at", "updated_at", "created_by", "imported", "author_person_id", "updated_by", "source", "author_name"]
    d = dict(zip(keys, row))
    d["sim_removed_state"] = d.get("sim_removed_state") or ""
    d["imported"] = bool(d["imported"])   # загружено из Excel (история выездов), а не сдано монтёром/диспетчером
    d["created_by"] = d.pop("author_name") or strip_vk(d["created_by"]) or None
    return d


async def get_for_task(task_id: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " where task_id = ?", (task_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_for_point(point_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            _SELECT + " where point_id = ? order by created_at desc, id desc limit ?", (point_id, limit)
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def search_archive(*, q: str = "", date_from: str = "", date_to: str = "", source: str = "", status: str = "",
                         visit_type: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Постраничный архив всех отчётов о выездах без ограничения по давности."""
    day = "case when r.imported = 1 then substr(r.created_at, 1, 10) else date(r.created_at, '+3 hours') end"
    where, args = [], []
    if date_from:
        where.append(f"{day} >= ?")
        args.append(date_from)
    if date_to:
        where.append(f"{day} <= ?")
        args.append(date_to)
    if source == "imported":
        where.append("r.imported = 1")
    elif source == "forms":
        where.append("r.imported = 0 and r.source = 'forms'")
    elif source == "bot":
        where.append("r.imported = 0 and r.source = 'bot'")
    elif source == "system":
        where.append("r.imported = 0 and coalesce(r.source, '') not in ('forms', 'bot')")
    if status:
        if status == "Без статуса":
            where.append("coalesce(nullif(r.result_status, ''), r.outcome, '') = ''")
        else:
            where.append("coalesce(nullif(r.result_status, ''), r.outcome, '') like ?")
            args.append(f"%{status.strip()}%")
    if visit_type:
        where.append("r.visit_type like ?")
        args.append(f"%{visit_type.strip()}%")
    if q.strip():
        needle = f"%{q.strip()}%"
        where.append("(r.point_id like ? or p.address like ? or p.inventory_number like ? or r.task_id like ? "
                     "or r.visit_type like ? or r.result_status like ? or r.outcome like ? or r.comment like ? "
                     "or r.fault like ? or r.needs like ? or coalesce(pe.full_name, r.created_by, '') like ?)")
        args.extend([needle] * 11)
    clause = " where " + " and ".join(where) if where else ""
    from_sql = (" from task_visit_reports r left join points p on p.id = r.point_id "
                "left join people pe on pe.id = r.author_person_id")
    async with get_connection() as conn:
        cur = await conn.execute("select count(*)" + from_sql + clause, args)
        total = (await cur.fetchone())[0]
        cur = await conn.execute(
            "select r.task_id, r.point_id, " + day + ", r.visit_type, r.result_status, r.outcome, r.comment, r.fault, r.needs, "
            "coalesce(pe.full_name, r.created_by, '') as author, r.imported, r.source, p.address, p.inventory_number "
            + from_sql + clause + f" order by {day} desc, r.id desc limit ? offset ?",
            [*args, limit, offset],
        )
        rows = await cur.fetchall()
    keys = ("task_id", "point_id", "visit_date", "visit_type", "result_status", "outcome", "comment", "fault", "needs",
            "author", "imported", "source", "address", "inventory_number")
    items = [dict(zip(keys, row)) for row in rows]
    for row in items:
        row["imported"] = bool(row["imported"])
        row["author"] = strip_vk(row["author"])
        row["source_label"] = ("Импорт из Excel" if row["imported"] else
                                "Яндекс Формы" if row["source"] == "forms" else
                                "Бот" if row["source"] == "bot" else "Система")
        row["status_label"] = row["result_status"] or row["outcome"] or "Без статуса"
    return {"items": items, "total": total}


# Ordered (key, header label) pairs — the field picker on /stats offers
# exactly these, in this order, for the "история ремонта" Excel export.
EXPORT_FIELDS = [
    ("created_at", "Дата/время"),
    ("point_id", "Комплекс"),
    ("point_address", "Адрес"),
    ("category_name", "Категория"),
    ("inventory_number", "Инвентарный номер"),
    ("visit_type", "Тип выезда"),
    ("result_status", "Статус"),
    ("outcome", "Статус выезда"),
    ("comment", "Комментарий"),
    ("equipment_removed", "Снято оборудование"),
    ("equipment_installed", "Установлено оборудование"),
    ("sim_removed", "SIM снята"),
    ("sim_removed_state", "Состояние снятой SIM"),
    ("sim_installed", "SIM установлена"),
    ("fault", "Что неисправно"),
    ("needs", "Что нужно для исправления"),
    ("created_by", "Исполнитель"),
]


async def list_for_export(date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Full report rows (not aggregated, unlike stats_repository's counts),
    joined with the point's address/category/inventory number, for the
    Excel export on /stats — see EXPORT_FIELDS for the available columns."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select case when r.imported = 1 then substr(r.created_at, 1, 10) else r.created_at end,
                   r.point_id, p.address, pc.name, p.inventory_number,
                   r.visit_type, r.result_status, r.outcome, r.comment, r.equipment_removed,
                   r.equipment_installed, r.sim_removed, r.sim_removed_state, r.sim_installed, r.fault, r.needs, coalesce(pe.full_name, r.created_by)
            from task_visit_reports r
            left join points p on p.id = r.point_id
            left join point_categories pc on pc.id = p.category_id
            left join people pe on pe.id = r.author_person_id
            where r.created_at >= ? and r.created_at <= ?
            order by r.created_at desc
            """,
            (date_from, date_to + "T23:59:59.999999"),
        )
        keys = [k for k, _ in EXPORT_FIELDS]
        rows = [dict(zip(keys, r)) for r in await cur.fetchall()]
    for row in rows:
        row["created_by"] = strip_vk(row["created_by"])
        row["sim_removed_state"] = SIM_STATE_LABELS.get(row.get("sim_removed_state") or "", "")
    return rows


@stats_cache.invalidates
async def upsert(
    task_id: str, point_id: str, visit_type: str, data: dict[str, Any], performed_by: Optional[str],
    author_person_id: Optional[int] = None, edited_by: Optional[str] = None,
    created_at: Optional[str] = None, source: Optional[str] = None, apply_status: bool = True,
    imported: bool = False, import_batch: Optional[str] = None,
    before_commit: Optional[Callable[[aiosqlite.Connection], Awaitable[None]]] = None,
) -> dict[str, Any]:
    """One report per task (unique task_id) — resubmitting the same task
    edits its existing report instead of creating a duplicate. `visit_type`
    comes from the task's own `type` (resolved by the caller — see
    routes_task_visit_reports.py), never from client-supplied `data`: the
    work type is fixed when the task is scheduled, not chosen here.

    Автор отчёта: `performed_by` (ФИО) и `author_person_id` (человек из «Персонала») записываются, когда отчёт
    создаётся. При правке существующего отчёта автор НЕ меняется — только если явно передан `author_person_id`
    (диспетчер выбрал другого исполнителя); кто правил, остаётся в `edited_by`.

    SIM-карта — отдельные поля (`sim_removed`, `sim_installed`, `sim_removed_state`), а не строка среди оборудования:
    не переданное поле (None) остаётся прежним, пустая строка — очищает.
    `created_at` — время отчёта (UTC, ISO): задаётся при загрузке отчётов из таблицы Яндекс Форм и при правке диспетчером;
    `source` — откуда отчёт («forms»); `apply_status=False` — записать отчёт, не трогая нынешний статус комплекса
    (запоздалый отчёт из форм не должен перебивать более свежую проверку). `imported` — запись истории без задания (отчёт из таблицы форм,
    для которого задания в календаре не нашлось): в «Мониторинге» и уведомлениях не показывается, у неё условное время дня; `import_batch` —
    метка загрузки."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    # «Что сняли / что поставили» — только у связанных с ремонтом типов
    # (в том числе «Инвентаризация + Ремонт»).
    equipment_removed = data.get("equipment_removed") if is_repair_type(visit_type) else None
    equipment_installed = data.get("equipment_installed") if is_repair_type(visit_type) else None

    async with get_connection() as conn:
        cur = await conn.execute(
            "select created_at, outcome, fault, needs, created_by, author_person_id, sim_removed, sim_installed, sim_removed_state, source "
            "from task_visit_reports where task_id = ?", (task_id,)
        )
        existing = await cur.fetchone()
        report_time = (created_at or "").strip() or (existing[0] if existing else now)
        if existing and not author_person_id:
            performed_by, author_id, updated_by = existing[4], existing[5], edited_by or performed_by
        else:
            author_id, updated_by = author_person_id, (edited_by if existing else None)
        # «Что неисправно» (причина) записывается и у выезда, после которого комплекс заработал (в чём была причина), а «что нужно для
        # восстановления» имеет смысл только у итога «Не работает» — при другом итоге очищается. Не прислали (старый клиент) — остаётся прежнее.
        fault = (data["fault"].strip() or None) if data.get("fault") is not None else (existing[2] if existing else None)
        if data["result_status"] != "Не работает":
            needs = None
        else:
            needs = (data["needs"].strip() or None) if data.get("needs") is not None else (existing[3] if existing else None)
        # Правка отчёта без поля outcome (форма в календаре) статус выезда не
        # затирает; передали явно (бот) — перезаписываем.
        outcome = data["outcome"] if data.get("outcome") is not None else (existing[1] if existing else "")

        def keep(key: str, old):
            value = data.get(key)
            if value is None:
                return old
            value = str(value).strip()
            return value or None
        sim_removed = keep("sim_removed", existing[6] if existing else None)
        sim_installed = keep("sim_installed", existing[7] if existing else None)
        sim_state = data.get("sim_removed_state")
        sim_state = (existing[8] if existing else "") if sim_state is None else (sim_state if sim_state in SIM_STATES else "")
        if not sim_removed:
            sim_state = ""            # состояние бывает только у снятой SIM
        source_value = source if source is not None else (existing[9] if existing else "")
        await conn.execute(
            "insert into task_visit_reports "
            "(task_id, point_id, visit_type, result_status, comment, equipment_removed, equipment_installed, "
            "outcome, fault, needs, sim_removed, sim_installed, sim_removed_state, source, "
            "created_at, updated_at, created_by, author_person_id, updated_by, imported, import_batch) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(task_id) do update set "
            "point_id = excluded.point_id, visit_type = excluded.visit_type, "
            "result_status = excluded.result_status, comment = excluded.comment, "
            "equipment_removed = excluded.equipment_removed, equipment_installed = excluded.equipment_installed, "
            "outcome = excluded.outcome, fault = excluded.fault, needs = excluded.needs, "
            "sim_removed = excluded.sim_removed, sim_installed = excluded.sim_installed, sim_removed_state = excluded.sim_removed_state, "
            "source = excluded.source, created_at = excluded.created_at, "
            "updated_at = excluded.updated_at, created_by = excluded.created_by, "
            "author_person_id = excluded.author_person_id, updated_by = excluded.updated_by",
            (
                task_id, point_id, visit_type, data["result_status"], data.get("comment") or "",
                equipment_removed, equipment_installed, outcome, fault, needs, sim_removed, sim_installed, sim_state, source_value,
                report_time, now, performed_by, author_id, updated_by, int(imported), import_batch or "",
            ),
        )
        if before_commit is not None:
            await before_commit(conn)
        await conn.commit()

    # Пустой result_status (отчёт со статусом выезда) состояние комплекса не меняет.
    target_status = _RESULT_TO_POINT_STATUS.get(data["result_status"]) if apply_status else None
    if target_status:
        await points_repository.set_status(point_id, target_status, source="visit_report", task_id=task_id)

    if data["result_status"] == "Демонтирован" and apply_status:
        # Комплекс уходит на склад целиком (статус «На складе» выставлен выше —
        # все его компоненты при этом становятся «на складе»); состав и
        # описание компонентов уточняются в карточке на странице «Склад».
        await warehouse_items_repository.bulk_upsert_from_import(
            point_id, {"received_at": dt.date.today().isoformat()}
        )

    await points_repository.apply_report_fault(point_id, {
        "result_status": data["result_status"], "fault": fault, "needs": needs, "created_at": report_time,
        "by": strip_vk(performed_by),
    })       # карточка комплекса: «почему не работает» / «что нужно» — из этого отчёта (если он свежее записанного)
    await state_repository.bump_rev()
    return await get_for_task(task_id)  # type: ignore[return-value]


@stats_cache.invalidates
async def set_visit_type(task_id: str, visit_type: str) -> Optional[dict[str, Any]]:
    """Меняет только «итоговый тип выезда» отчёта (например, монтёр случайно включил режим ремонта, а были
    просто замеры, — в отчёте было «Замер зон фиксации + Ремонт»). Статус, комментарий и оборудование не трогаются,
    состояние комплекса — тоже; задание в календаре остаётся с тем типом, на который его назначили."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "update task_visit_reports set visit_type = ?, updated_at = ? where task_id = ?",
            (visit_type, dt.datetime.now(dt.timezone.utc).isoformat(), task_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
    await state_repository.bump_rev()
    return await get_for_task(task_id)


@stats_cache.invalidates
async def set_ai_fault(task_id: str, cause: str, needs: str, by: str = "") -> bool:
    """ИИ разобрал текст итога отчёта из формы (app/services/ai/forms_fault.py): «что неисправно» и «что нужно» записываются в отчёт (только у итога «Не работает»), а в карточку
    комплекса — по общему правилу app/core/fault.py (отчёт, который старше уже записанной неисправности, карточку не перебивает). Остальные поля отчёта не меняются."""
    async with get_connection() as conn:
        cur = await conn.execute("select point_id, result_status, created_at from task_visit_reports where task_id = ?", (task_id,))
        row = await cur.fetchone()
        if not row or row[1] != "Не работает":
            return False
        point_id, _status, created_at = row
        await conn.execute("update task_visit_reports set fault = ?, needs = ?, updated_at = ? where task_id = ?",
                           (cause.strip() or None, needs.strip() or None, dt.datetime.now(dt.timezone.utc).isoformat(), task_id))
        await conn.commit()
    await points_repository.apply_report_fault(point_id, {"result_status": "Не работает", "fault": cause, "needs": needs, "created_at": created_at, "by": by})
    await state_repository.bump_rev()
    return True


@stats_cache.invalidates
async def delete_for_task(task_id: str) -> None:
    """Used by /monitoring's «Итоги по комплексам» — removes the report
    record AND undoes its footprint on point_status_history/points.status
    (and therefore on the /stats "отвалилось"/"восстановлено" tiles), so a
    mistakenly-created report doesn't leave a permanent, un-removable trace
    in the statistics after it's deleted.

    Only reverts the point's *current* status if this report is still the
    reason for it — i.e. nothing else (another report, a ping check, a
    manual edit) has changed the point's status since this report's own
    last edit. If something else already moved the point on, its status is
    left alone; only the now-orphaned history rows are removed."""
    removed_report = None
    async with get_connection() as conn:
        cur = await conn.execute("select point_id, created_at from task_visit_reports where task_id = ?", (task_id,))
        removed_report = await cur.fetchone()
        cur = await conn.execute(
            "select point_id, old_status, new_status, changed_at from point_status_history "
            "where task_id = ? order by changed_at",
            (task_id,),
        )
        rows = await cur.fetchall()
        if rows:
            point_id = rows[0][0]
            earliest_old_status = rows[0][1]
            latest_new_status, latest_changed_at = rows[-1][2], rows[-1][3]
            cur2 = await conn.execute("select status from points where id = ?", (point_id,))
            point_row = await cur2.fetchone()
            current_status = point_row[0] if point_row else None
            cur3 = await conn.execute(
                "select count(*) from point_status_history where point_id = ? and changed_at > ?",
                (point_id, latest_changed_at),
            )
            (later_count,) = await cur3.fetchone()
            if point_row is not None and current_status == latest_new_status and later_count == 0:
                await conn.execute("update points set status = ? where id = ?", (earliest_old_status, point_id))
        await conn.execute("delete from point_status_history where task_id = ?", (task_id,))
        await conn.execute("delete from report_ai_fault where task_id = ?", (task_id,))
        await conn.execute("delete from task_visit_reports where task_id = ?", (task_id,))
        await conn.commit()
    if removed_report:
        await points_repository.recompute_fault_if_source(removed_report[0], removed_report[1])
    await state_repository.bump_rev()


async def list_for_tasks(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Отчёты сразу по набору заданий (task_id → отчёт) — для выгрузки
    «выезды бригад по дням»."""
    result: dict[str, dict[str, Any]] = {}
    ids = [i for i in task_ids if i]
    async with get_connection() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(_SELECT + f" where task_id in ({marks})", chunk)
            for row in await cur.fetchall():
                report = _row_to_dict(row)
                result[report["task_id"]] = report
    return result
