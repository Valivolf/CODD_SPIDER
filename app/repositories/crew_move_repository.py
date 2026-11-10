"""Перенос бригады с её точками на другой день (кнопка «📅 Перенести» в карточке бригады + клик по дню в календаре).

Что происходит:
* бригада без выполненных точек переезжает целиком — та же запись (состав, название, место, ссылка на карту),
  у неё и у всех её заданий меняется дата;
* если часть точек уже выполнена (сдан отчёт / вспомогательная точка принята), на новый день уезжает только
  невыполненное — в НОВОЙ бригаде с тем же названием, составом, транспортом и местом (карта у неё будет своя),
  а выполненные остаются в старой бригаде на прежнем дне — история выездов не искажается;
* бригада в командировке не переносится (у неё свои дни — сначала «Завершить командировку»);
* если какая-то точка на новом дне уже есть (в любой бригаде), перенос требует отдельного согласия
  (allow_duplicates) — в остальном правило «одна точка в день один раз» не нарушается молча.

Всё делается одной транзакцией под тем же замком и с тем же счётчиком ревизии, что и сохранение календаря
(state_repository), поэтому открытый у кого-то календарь не перезапишет перенос устаревшей копией.
"""
from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from app.core import map_events
from app.core.work_types import AUX_TYPE_NAME
from app.db.database import get_connection
from app.repositories import state_repository


class MoveError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _norm(point: str | None) -> str:
    return (point or "").strip().lower()


def _check_date(day: str) -> None:
    try:
        date_cls.fromisoformat(day)
    except (TypeError, ValueError):
        raise MoveError("Неверная дата")


def _marks(ids: list[int]) -> str:
    return ",".join("?" for _ in ids)


async def _analyse(conn, crew_id: int, new_date: str) -> dict[str, Any]:
    _check_date(new_date)
    cur = await conn.execute("select id, date, name, equip, map_link, place, trip_id, contractor, contractor_note from crews where id = ?", (crew_id,))
    row = await cur.fetchone()
    if not row:
        raise MoveError("Бригада не найдена", 404)
    crew = dict(zip(("id", "date", "name", "equip", "map_link", "place", "trip_id", "contractor", "contractor_note"), row))

    cur = await conn.execute(
        """select t.id, t.point, t.type,
                  case when r.task_id is not null then 1 when t.type = ? and d.task_id is not null then 1 else 0 end
           from tasks t
           left join task_visit_reports r on r.task_id = t.id
           left join aux_task_done d on d.task_id = t.id
           where t.team = ? order by t.rowid""",
        (AUX_TYPE_NAME, crew_id),
    )
    tasks = [{"id": r[0], "point": r[1], "type": r[2], "done": bool(r[3])} for r in await cur.fetchall()]
    movable = [t for t in tasks if not t["done"]]
    done = [t for t in tasks if t["done"]]

    cur = await conn.execute("select point from tasks where date = ? and type != ?", (new_date, AUX_TYPE_NAME))
    on_target = {_norm(r[0]) for r in await cur.fetchall()}
    duplicates = sorted({t["point"] for t in movable if t["type"] != AUX_TYPE_NAME and _norm(t["point"]) in on_target})

    cur = await conn.execute(
        "select p.id, p.full_name from crew_members cm join people p on p.id = cm.person_id where cm.crew_id = ?", (crew_id,)
    )
    members = [(r[0], r[1]) for r in await cur.fetchall()]
    busy: list[dict] = []
    absent: list[dict] = []
    if members:
        ids = [m[0] for m in members]
        cur = await conn.execute(
            f"""select p.id, p.full_name, c.name from crew_members cm
                join crews c on c.id = cm.crew_id join people p on p.id = cm.person_id
                where c.date = ? and c.id != ? and cm.person_id in ({_marks(ids)})""",
            (new_date, crew_id, *ids),
        )
        busy = [{"person_id": r[0], "full_name": r[1], "crew_name": r[2]} for r in await cur.fetchall()]
        cur = await conn.execute(
            f"""select p.id, p.full_name, a.reason from people_absences a join people p on p.id = a.person_id
                where a.start_date <= ? and a.end_date >= ? and a.person_id in ({_marks(ids)})""",
            (new_date, new_date, *ids),
        )
        absent = [{"person_id": r[0], "full_name": r[1], "reason": r[2] or ""} for r in await cur.fetchall()]

    blocked = None
    if crew["trip_id"]:
        blocked = "Бригада в командировке — перенести её нельзя. Сначала нажмите «Завершить командировку»."
    elif new_date == crew["date"]:
        blocked = "Бригада уже стоит на этот день — выберите другой."
    elif tasks and not movable:
        blocked = "Все точки бригады уже выполнены — переносить нечего."
    return {
        "crew": {"id": crew["id"], "name": crew["name"], "date": crew["date"], "equip": crew["equip"], "place": crew["place"],
                 "contractor": crew["contractor"], "contractor_note": crew["contractor_note"]},
        "new_date": new_date, "tasks_total": len(tasks), "movable": len(movable), "done": len(done),
        "mode": "split" if (done and movable) else "move",
        "duplicates": duplicates, "busy_members": busy, "absent_members": absent, "blocked": blocked,
        "_movable_ids": [t["id"] for t in movable],
    }


async def check(crew_id: int, new_date: str) -> dict[str, Any]:
    """Что будет, если перенести бригаду на `new_date` (для окна подтверждения; ничего не меняет)."""
    async with get_connection() as conn:
        info = await _analyse(conn, crew_id, new_date)
    info.pop("_movable_ids", None)
    return info


async def move_tasks(task_ids: list[str], *, crew_id: int | None, date: str | None, new_crew_name: str = "", copy_members: bool = False,
                     allow_duplicates: bool = False) -> dict[str, Any]:
    """Переносит выбранные задания в другую бригаду: в существующую (crew_id; задания получают день этой бригады) или в новую на день `date`.
    Выполненные (сдан отчёт / вспомогательная точка принята) не переносятся — история выездов не искажается; в бригаду в командировке переносить нельзя;
    точка, которая на этом дне уже есть, требует отдельного согласия (allow_duplicates). Одна транзакция под общим замком календаря, ревизия +1."""
    ids = list(dict.fromkeys(str(t) for t in task_ids if str(t).strip()))
    if not ids:
        raise MoveError("Не выбрано ни одной точки")
    async with state_repository._write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            try:
                cur = await conn.execute(f"select id, team, date, point, type from tasks where id in ({_marks(ids)})", ids)
                rows = {r[0]: {"id": r[0], "team": r[1], "date": r[2], "point": r[3], "type": r[4]} for r in await cur.fetchall()}
                missing = [i for i in ids if i not in rows]
                if missing:
                    raise MoveError("Часть выбранных заданий уже не найдена — обновите страницу и выберите заново", 404)
                cur = await conn.execute(
                    f"""select t.id from tasks t
                        left join task_visit_reports r on r.task_id = t.id
                        left join aux_task_done d on d.task_id = t.id
                        where t.id in ({_marks(ids)}) and (r.task_id is not null or (t.type = ? and d.task_id is not null))""",
                    (*ids, AUX_TYPE_NAME),
                )
                done = [r[0] for r in await cur.fetchall()]
                if done:
                    names = ", ".join(sorted({rows[i]["point"] or i for i in done}))
                    raise MoveError(f"Уже выполнены и не переносятся: {names}. Снимите с них отметку и повторите.")

                source_team = next((r["team"] for r in rows.values() if r["team"] is not None), None)
                if crew_id is not None:
                    cur = await conn.execute("select id, date, trip_id from crews where id = ?", (crew_id,))
                    crew = await cur.fetchone()
                    if not crew:
                        raise MoveError("Бригада не найдена — обновите страницу", 404)
                    if crew[2]:
                        raise MoveError("Бригада в командировке — переносить в неё нельзя: у неё свои дни.")
                    target_team, target_date = crew[0], crew[1]
                else:
                    _check_date(date or "")
                    target_date = date
                    target_team = None
                    cur = await conn.execute("select name, equip, place, contractor, contractor_note from crews where id = ?", (source_team,))
                    src = await cur.fetchone() if source_team is not None else None
                    name = (new_crew_name or "").strip() or (src[0] if src else "") or "Бригада"
                    cur = await conn.execute(
                        "insert into crews (date, name, equip, map_link, place, contractor, contractor_note) values (?, ?, ?, '', ?, ?, ?)",
                        (target_date, name, src[1] if src else "Лестница", src[2] if src else "", (src[3] if src else 0) or 0, (src[4] if src else "") or ""),
                    )
                    target_team = cur.lastrowid
                    if copy_members and source_team is not None:
                        await conn.execute(
                            "insert or ignore into crew_members (crew_id, person_id) select ?, person_id from crew_members where crew_id = ?", (target_team, source_team))

                moving = [r for r in rows.values() if not (r["team"] == target_team and r["date"] == target_date)]
                if not moving:
                    raise MoveError("Выбранные задания уже стоят в этой бригаде на этот день")
                move_ids = [r["id"] for r in moving]
                cur = await conn.execute(
                    f"select point from tasks where date = ? and type != ? and id not in ({_marks(move_ids)})", (target_date, AUX_TYPE_NAME, *move_ids))
                on_target = {_norm(r[0]) for r in await cur.fetchall()}
                duplicates = sorted({r["point"] for r in moving if r["type"] != AUX_TYPE_NAME and _norm(r["point"]) in on_target})
                if duplicates and not allow_duplicates:
                    raise MoveError("Такие точки на выбранном дне уже есть: " + ", ".join(duplicates), 409)
                await conn.execute(f"update tasks set date = ?, team = ? where id in ({_marks(move_ids)})", (target_date, target_team, *move_ids))
                cur = await conn.execute("select rev from state_rev where id = 1")
                (rev,) = await cur.fetchone()
                await conn.execute("update state_rev set rev = ? where id = 1", (rev + 1,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    map_events.kick()   # карты бригад (прежних и новой) пересчитать сразу
    return {"moved": len(move_ids), "crew_id": target_team, "date": target_date, "created_crew": crew_id is None, "duplicates": duplicates if allow_duplicates else []}


async def move_tasks_to_plan(task_ids: list[str], *, created_by: str, group_name: str = "") -> dict[str, Any]:
    """Выбранные невыполненные задания уходят из бригады В ПЛАН («Все АПК» → План): задание удаляется, комплекс встаёт в план с тем же описанием работ, приоритетом и
    типом работы (без дня и бригады). Комплекс, который уже стоит в плане, второй раз не добавляется (задание всё равно снимается с бригады; пустой тип у записи плана
    заполняется типом задания). Выполненные (сдан отчёт / вспомогательная точка принята) не переносятся; вспомогательные точки — метки карты, не комплексы — в план не идут;
    точка, которой нет в справочнике, в план не попадает — тогда ничего не меняется. Одна транзакция под общим замком календаря, ревизия +1."""
    from app.core import work_types as wt  # noqa: PLC0415
    from app.core.point_ref import load_refs  # noqa: PLC0415
    from app.core.text import decode_entities  # noqa: PLC0415
    import datetime as dt  # noqa: PLC0415

    ids = list(dict.fromkeys(str(t) for t in task_ids if str(t).strip()))
    if not ids:
        raise MoveError("Не выбрано ни одной точки")
    group = " ".join(decode_entities(group_name or "").split())[:60]
    known_types = set(wt.work_type_names())
    async with state_repository._write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            try:
                cur = await conn.execute(f"select id, point, type, work, note, priority from tasks where id in ({_marks(ids)})", ids)
                rows = {r[0]: {"id": r[0], "point": r[1], "type": r[2], "work": r[3] or "", "note": r[4] or "", "priority": bool(r[5])} for r in await cur.fetchall()}
                if [i for i in ids if i not in rows]:
                    raise MoveError("Часть выбранных заданий уже не найдена — обновите страницу и выберите заново", 404)
                cur = await conn.execute(
                    f"""select t.id from tasks t
                        left join task_visit_reports r on r.task_id = t.id
                        left join aux_task_done d on d.task_id = t.id
                        where t.id in ({_marks(ids)}) and (r.task_id is not null or (t.type = ? and d.task_id is not null))""",
                    (*ids, AUX_TYPE_NAME),
                )
                done = [r[0] for r in await cur.fetchall()]
                if done:
                    names = ", ".join(sorted({rows[i]["point"] or i for i in done}))
                    raise MoveError(f"Уже выполнены и в план не переносятся: {names}. Снимите с них отметку и повторите.")
                aux = [r["point"] or r["id"] for r in rows.values() if r["type"] == AUX_TYPE_NAME]
                if aux:
                    raise MoveError("Вспомогательные точки — метки на карте бригады, не комплексы: в план они не переносятся (" + ", ".join(sorted(set(aux))) + "). Снимите с них отметку.")
                refs = await load_refs(conn)
                resolved: dict[str, str] = {}
                unknown: list[str] = []
                for r in rows.values():
                    real = refs.resolve(r["point"])
                    if real:
                        resolved[r["id"]] = real
                    else:
                        unknown.append(r["point"] or r["id"])
                if unknown:
                    raise MoveError("Этих точек нет в «Все АПК», в план их перенести нельзя: " + ", ".join(sorted(set(unknown))))

                if group:   # «рейд 1» и «Рейд 1» — одна группа: берём написание, которое уже есть в плане
                    cur = await conn.execute("select distinct group_name from point_plans where group_name != ''")
                    for (existing,) in await cur.fetchall():
                        if existing.lower() == group.lower():
                            group = existing
                            break
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                created = already = 0
                seen: set[str] = set()
                for tid in ids:
                    r = rows[tid]
                    pid = resolved[tid]
                    key = pid.strip().lower()
                    work_type = r["type"] if r["type"] in known_types else ""
                    if key in seen:             # одна и та же точка выбрана дважды (две бригады / два дня) — в плане она одна
                        already += 1
                        continue
                    seen.add(key)
                    cur = await conn.execute("select id, work_type from point_plans where pylower(trim(point_id)) = ? limit 1", (key,))
                    plan = await cur.fetchone()
                    if plan:
                        already += 1
                        if work_type and not (plan[1] or ""):
                            await conn.execute("update point_plans set work_type = ? where id = ?", (work_type, plan[0]))
                        if group:
                            await conn.execute("update point_plans set group_name = ? where id = ?", (group, plan[0]))
                        continue
                    await conn.execute(
                        "insert into point_plans (point_id, description, created_at, created_by, priority, group_name, work_type) values (?, ?, ?, ?, ?, ?, ?)",
                        (pid, decode_entities(r["work"] or r["note"]).strip(), now, created_by or "", int(r["priority"]), group, work_type),
                    )
                    created += 1
                await conn.execute(f"delete from tasks where id in ({_marks(ids)})", ids)
                await conn.execute(f"delete from aux_task_done where task_id in ({_marks(ids)})", ids)
                await conn.execute(f"delete from bot_task_progress where task_id in ({_marks(ids)})", ids)
                cur = await conn.execute("select rev from state_rev where id = 1")
                (rev,) = await cur.fetchone()
                await conn.execute("update state_rev set rev = ? where id = 1", (rev + 1,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    map_events.kick()   # метки убранных точек с карты бригады — сразу
    return {"to_plan": True, "moved": len(ids), "created": created, "already": already, "plan_group": group}


async def move(crew_id: int, new_date: str, *, allow_duplicates: bool = False) -> dict[str, Any]:
    async with state_repository._write_lock:
        async with get_connection() as conn:
            await conn.execute("begin immediate")
            try:
                info = await _analyse(conn, crew_id, new_date)
                if info["blocked"]:
                    raise MoveError(info["blocked"])
                if info["duplicates"] and not allow_duplicates:
                    raise MoveError("Такие точки на выбранном дне уже есть: " + ", ".join(info["duplicates"]), 409)
                movable_ids: list[str] = info["_movable_ids"]
                result_crew_id = crew_id
                if info["mode"] == "split":
                    crew = info["crew"]
                    cur = await conn.execute(
                        "insert into crews (date, name, equip, map_link, place, contractor, contractor_note) values (?, ?, ?, '', ?, ?, ?)",
                        (new_date, crew["name"], crew["equip"], crew["place"], crew["contractor"] or 0, crew["contractor_note"] or ""),
                    )
                    result_crew_id = cur.lastrowid
                    await conn.execute(
                        "insert or ignore into crew_members (crew_id, person_id) select ?, person_id from crew_members where crew_id = ?",
                        (result_crew_id, crew_id),
                    )
                    if movable_ids:
                        await conn.execute(
                            f"update tasks set date = ?, team = ? where id in ({_marks(movable_ids)})",
                            (new_date, result_crew_id, *movable_ids),
                        )
                else:
                    await conn.execute("update crews set date = ? where id = ?", (new_date, crew_id))
                    await conn.execute("update tasks set date = ? where team = ?", (new_date, crew_id))
                cur = await conn.execute("select rev from state_rev where id = 1")
                (rev,) = await cur.fetchone()
                await conn.execute("update state_rev set rev = ? where id = 1", (rev + 1,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    map_events.kick()   # карты бригад (старая и новая) пересчитать сразу
    return {
        "mode": info["mode"], "crew_id": result_crew_id, "date": new_date,
        "moved_tasks": len(movable_ids), "kept_done": info["done"] if info["mode"] == "split" else 0,
    }
