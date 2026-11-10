"""Server-to-server API for the монтёр (fitter) VK bot — no lan_calendar
user session, authenticated by a single shared header instead (see
app/core/deps.py::require_bot_key). The bot has no database of its own:
every piece of state it needs (who's assigned what, what to ask, where to
save a photo) is fetched or written here.

Endpoints mirror one bot conversation turn each — see the "Этап C" plan
for the exact scripted flow this API is meant to drive.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.core.deps import require_bot_key
from app.core.point_ref import load_refs
from app.core.power_source import normalize_power_source
from app.core.work_types import AUX_TYPE_NAME, combined_visit_type, has_bot_tools, is_repair_type
from app.db.database import get_connection
from app.models.people import PersonOut
from app.repositories import (
    access_issue_requests_repository,
    bot_join_requests_repository,
    bot_settings_repository,
    bot_work_sessions_repository,
    demontage_requests_repository,
    people_repository,
    points_repository,
    work_types_repository,
    sim_ip_registry_repository,
    state_repository,
    task_visit_reports_repository,
    verification_requests_repository,
    visit_photos_repository,
    bot_task_progress_repository,
)
from app.core import runtime_state
from app.services import equipment_service, point_health_service

router = APIRouter(prefix="/api/bot", tags=["bot"], dependencies=[Depends(require_bot_key)])


@router.get("/enabled")
async def get_bot_enabled():
    """Checked by the bot on every incoming VK event (see AuthMiddleware) —
    lets the web panel silence whichever bot process shouldn't be live."""
    runtime_state.touch("bot")           # «Состояние системы»: когда бот в последний раз отвечал
    return {"enabled": await bot_settings_repository.get_enabled()}


async def _require_person(vk_id: int) -> dict:
    person = await people_repository.get_by_vk_id(vk_id)
    if not person or not person["active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Монтёр не найден или неактивен")
    return person


async def _canonical_point(conn, text: str | None) -> str | None:
    """Номер комплекса, на который ссылается текст из задания (в нём мог быть другой регистр, лишний пробел, латинская буква вместо русской — см. app/core/point_ref.py).
    Точный номер возвращается как есть (дёшево, одним запросом); иначе — однозначное совпадение «без разбора»; не нашлось — None."""
    text = text or ""
    if not text.strip():
        return None
    cur = await conn.execute("select 1 from points where id = ?", (text,))
    if await cur.fetchone():
        return text
    return (await load_refs(conn)).resolve(text)


async def _get_task(task_id: str) -> dict:
    async with get_connection() as conn:
        cur = await conn.execute("select id, point, type, date from tasks where id = ?", (task_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено")
        point = row[1]
        if point and row[2] != AUX_TYPE_NAME:           # у вспомогательной точки в этом поле её подпись, а не номер комплекса
            point = await _canonical_point(conn, point) or point
    return {"id": row[0], "point": point, "type": row[2], "date": row[3]}


async def _point_or_404(point_id: str) -> dict:
    """Комплекс по номеру, как его прислал бот: точно, а если не нашёлся — «без разбора» (регистр, пробелы, двойники)."""
    point = await points_repository.get_point(point_id)
    if point is None:
        async with get_connection() as conn:
            canon = await _canonical_point(conn, point_id)
        if canon and canon != point_id:
            point = await points_repository.get_point(canon)
    if point is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    return point


@router.get("/me", response_model=PersonOut)
async def get_me(vk_id: int):
    return PersonOut(**await _require_person(vk_id))


@router.get("/tasks")
async def list_my_tasks(vk_id: int, day: str):
    """A трип crew's not-yet-reported tasks are visible on EVERY day of the
    trip, not just whichever day they were originally created under — a
    point assigned on day 1 but only finished on day 3 must still show up
    on day 3, and one already reported must not reappear as "new" on a
    later day (the r.task_id is null check below already drops it as soon
    as it's reported, regardless of date). See app/core/trips.py."""
    person = await _require_person(vk_id)
    async with get_connection() as conn:
        cur = await conn.execute(
            "select c.id, c.trip_id from crews c join crew_members cm on cm.crew_id = c.id "
            "where cm.person_id = ? and c.date = ?",
            (person["id"], day),
        )
        my_crews = await cur.fetchall()
        if not my_crews:
            return []

        team_ids: set[int] = set()
        for crew_id, trip_id in my_crews:
            if trip_id:
                cur = await conn.execute("select id from crews where trip_id = ?", (trip_id,))
                team_ids.update(r[0] for r in await cur.fetchall())
            else:
                team_ids.add(crew_id)

        placeholders = ",".join("?" for _ in team_ids)
        cur = await conn.execute(
            f"""
            select t.id, t.point, t.work, t.address, t.type, p.address as point_address,
                   c.name as crew_name, c.map_link, p.sim_number, p.power_source, t.aux_lat, t.aux_lon
            from tasks t
            join crews c on c.id = t.team
            left join points p on p.id = t.point
            left join task_visit_reports r on r.task_id = t.id
            where t.team in ({placeholders}) and r.task_id is null
              and (t.type != ? or t.id not in (select task_id from aux_task_done))
            order by t.date, t.id
            """,
            tuple(team_ids) + (AUX_TYPE_NAME,),
        )
        rows = await cur.fetchall()
    keys = ["id", "point", "work", "address", "type", "point_address", "crew_name", "map_link", "point_sim", "point_power_source", "aux_lat", "aux_lon"]
    tasks = [dict(zip(keys, r)) for r in rows]
    # Задание ссылается на комплекс текстом; если он набран иначе, чем номер комплекса (регистр, пробел, латинская буква вместо русской), соединение выше его не нашло —
    # ищем «без разбора» и подставляем настоящий номер, адрес, SIM и питание: иначе у монтёра нет данных комплекса, а фото и отчёт уходят «не на тот» номер.
    unlinked = [t for t in tasks if t["point"] and t["type"] != AUX_TYPE_NAME and t["point_address"] is None]
    if unlinked:
        async with get_connection() as conn:
            refs = await load_refs(conn)
            for t in unlinked:
                canon = refs.resolve(t["point"])
                if canon and canon != t["point"]:
                    cur = await conn.execute("select address, sim_number, power_source from points where id = ?", (canon,))
                    found = await cur.fetchone()
                    if found:
                        t["point"], t["point_address"], t["point_sim"], t["point_power_source"] = canon, found[0], found[1], found[2]
    outcomes = {t["type"]: await work_types_repository.outcomes_for(t["type"]) for t in tasks}
    # Что уже сделано по каждому комплексу и кто на нём сейчас (общее для бригады): фото, отчёт, коллеги.
    ids = [t["id"] for t in tasks]
    photo_counts = await visit_photos_repository.counts_for_tasks(ids)
    progress = await bot_task_progress_repository.get_many(ids)
    workers = await bot_work_sessions_repository.active_for_tasks(ids)
    for t in tasks:
        state = (progress.get(t["id"]) or {}).get("state") or {}
        on_point = workers.get(t["id"], [])
        t["photo_count"] = photo_counts.get(t["id"], 0)
        t["has_report"] = bool((state.get("report_text") or "").strip())
        t["in_work_by"] = [w["name"] for w in on_point if w["person_id"] != person["id"]]
        t["i_am_working"] = any(w["person_id"] == person["id"] for w in on_point)
        # «Взят» — по комплексу уже что-то делали (прогресс, фото) или на нём кто-то сейчас работает.
        t["started"] = bool(progress.get(t["id"]) or on_point or t["photo_count"])
    for t in tasks:
        # Вспомогательная точка: не работа, а метка с информацией («Принято» / «Выйти»).
        t["is_aux"] = t["type"] == AUX_TYPE_NAME
        t["point_sim"] = t["point_sim"] or ""
        t["point_power_source"] = t["point_power_source"] or ""
        # «Связан с ремонтом» (Настройки → Типы выездов): такой тип получает в
        # боте меню ремонта, остальные — меню с кнопкой «Перейти в режим ремонта».
        t["type_is_repair"] = is_repair_type(t["type"])
        # «Статусы выезда» типа — кнопки итога при завершении (только у не
        # связанных с ремонтом; пусто — стандартные итоги).
        t["type_outcomes"] = [] if t["type_is_repair"] else outcomes.get(t["type"], [])
        # «Проверка комплекса и SIM в боте» (второй переключатель типа): в простом меню — «Проверка доступности» и «Замена SIM-карты».
        t["type_tools"] = has_bot_tools(t["type"])
    return tasks


class AuxAcceptIn(BaseModel):
    task_id: str
    vk_id: int


@router.post("/aux-accept")
async def accept_aux_task(payload: AuxAcceptIn):
    """«Принято» у вспомогательной точки: она помечается выполненной (зелёная на карте бригады), в статистику
    не попадает. Рабочей сессии и отчёта у такой точки нет."""
    person = await _require_person(payload.vk_id)
    task = await _get_task(payload.task_id)
    if task["type"] != AUX_TYPE_NAME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Это не вспомогательная точка")
    await state_repository.mark_aux_done(task["id"], person["full_name"])
    return {"ok": True}


class WorkSessionIn(BaseModel):
    task_id: str
    vk_id: int


@router.post("/work-sessions")
async def start_work_session(payload: WorkSessionIn):
    person = await _require_person(payload.vk_id)
    task = await _get_task(payload.task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    return await bot_work_sessions_repository.start(task["id"], task["point"], person["id"])


@router.get("/active-session")
async def get_active_session(vk_id: int):
    """Открытая рабочая сессия человека (если есть): после перезапуска бота по ней он возвращает монтёра на его
    комплекс, когда тот нажимает кнопку в старом сообщении."""
    person = await _require_person(vk_id)
    session = await bot_work_sessions_repository.latest_active_for_person(person["id"])
    if not session:
        return {"session": None}
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from task_visit_reports where task_id = ?", (session["task_id"],))
        reported = await cur.fetchone()
        cur = await conn.execute("select 1 from tasks where id = ?", (session["task_id"],))
        exists = await cur.fetchone()
    if reported or not exists:
        await bot_work_sessions_repository.end(session["id"])
        return {"session": None}
    return {"session": session}


async def _progress_payload(task_id: str, person: dict) -> dict:
    """Общий вид прогресса по комплексу: сохранённое состояние, фото, кто на комплексе, сдан ли отчёт."""
    saved = await bot_task_progress_repository.get(task_id) or {"state": {}, "updated_at": None, "updated_by": None}
    photo_counts = await visit_photos_repository.counts_for_tasks([task_id])
    workers = (await bot_work_sessions_repository.active_for_tasks([task_id])).get(task_id, [])
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from task_visit_reports where task_id = ?", (task_id,))
        finished = await cur.fetchone() is not None
    state = saved["state"]
    return {
        "task_id": task_id,
        "state": state,
        "photo_count": photo_counts.get(task_id, 0),
        "has_report": bool((state.get("report_text") or "").strip()),
        "workers": [{**w, "me": w["person_id"] == person["id"]} for w in workers],
        "updated_at": saved["updated_at"],
        "updated_by": saved["updated_by"],
        "finished": finished,
    }


@router.get("/tasks/{task_id}/progress")
async def get_task_progress(task_id: str, vk_id: int):
    person = await _require_person(vk_id)
    await _get_task(task_id)
    return await _progress_payload(task_id, person)


class ProgressPatchIn(BaseModel):
    vk_id: int
    patch: dict


@router.put("/tasks/{task_id}/progress")
async def patch_task_progress(task_id: str, payload: ProgressPatchIn):
    """Сохраняет изменения прогресса (только перечисленные ключи; остальные остаются как были)."""
    person = await _require_person(payload.vk_id)
    await _get_task(task_id)
    await bot_task_progress_repository.patch(task_id, payload.patch, person["full_name"])
    return await _progress_payload(task_id, person)


@router.put("/work-sessions/{session_id}/end")
async def end_work_session(session_id: int):
    session = await bot_work_sessions_repository.end(session_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    return session


@router.get("/points/{point_id:path}")          # :path — в номере комплекса может быть «/» (например, «АПК 12/3»)
async def get_bot_point(point_id: str):
    point = await _point_or_404(point_id)
    return {
        "id": point["id"],
        "sim_number": point.get("sim_number") or "",
        "ip_address": point.get("ip_address") or "",
        "status": point.get("status") or "",
        "power_source": point.get("power_source") or "",
    }


class PowerSourceIn(BaseModel):
    power_source: str


@router.post("/points/{point_id:path}/power-source")
async def set_point_power_source(point_id: str, payload: PowerSourceIn):
    """Монтёр ответил боту «СЭС или Электросеть?» при взятии комплекса в работу."""
    value = normalize_power_source(payload.power_source)
    if not value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Питание: СЭС или Электросеть")
    point = await _point_or_404(point_id)
    if not await points_repository.set_power_source(point["id"], value):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    return {"ok": True, "power_source": value}


class SimIn(BaseModel):
    sim_number: str


@router.post("/points/{point_id:path}/sim")
async def set_point_sim(point_id: str, payload: SimIn):
    point = await _point_or_404(point_id)
    point_id = point["id"]
    merged = dict(point)
    # Та же нормализация (последние 7 цифр), что и у обычного Excel-импорта
    # SIM/IP — иначе номер с точки не совпадёт с ключом в sim_ip_registry.
    sim_normalized = "".join(c for c in payload.sim_number if c.isdigit())[-7:] or payload.sim_number
    # Номер SIM комплекса — это и SIM-карта в учёте оборудования: исправляем её запись (а не только поле). Если номер
    # уже числится за другим оборудованием/комплексом, поле комплекса всё равно обновляем — работа на месте не встаёт.
    try:
        await equipment_service.sim_fix(point_id, sim_normalized, performed_by="бот: номер SIM")
    except equipment_service.EquipmentError:
        pass
    merged["sim_number"] = sim_normalized
    known_ip = await sim_ip_registry_repository.get_ip_for_sim(sim_normalized)
    if known_ip:
        merged["ip_address"] = known_ip
    await points_repository.update_point(point_id, merged)
    # ip_known=False — новой SIM ещё нет в реестре SIM↔IP, IP остался прежним
    # (его обновит загрузка реестра); бот говорит об этом монтёру.
    return {
        "ok": True,
        "sim_number": sim_normalized,
        "ip_address": merged.get("ip_address") or "",
        "ip_known": bool(known_ip),
    }


_POWER_STATUSES = {"Нет питания", "Не подведено питание"}


class PowerStatusIn(BaseModel):
    status: str


@router.post("/points/{point_id:path}/power-status")
async def set_point_power_status(point_id: str, payload: PowerStatusIn):
    if payload.status not in _POWER_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный статус питания")
    point_id = (await _point_or_404(point_id))["id"]
    await points_repository.set_status(point_id, payload.status, source="visit_report")
    return {"ok": True}


@router.post("/points/{point_id:path}/recheck")
async def recheck_point(point_id: str):
    point_id = (await _point_or_404(point_id))["id"]
    await point_health_service.recheck_and_apply(point_id)
    point = await points_repository.get_point(point_id)
    return {"status": point.get("status") or ""}  # type: ignore[union-attr]


class VisitReportIn(BaseModel):
    task_id: str
    vk_id: int
    result_status: str
    comment: str = ""
    equipment_removed: str | None = None
    equipment_installed: str | None = None
    # Монтёр на не-ремонтном типе (напр. «Инвентаризация») нажал «Перейти в
    # режим ремонта» — в отчётах и выгрузках визит пишется «Инвентаризация +
    # Ремонт».
    repair_mode: bool = False
    # «Статус выезда» — выбранная монтёром кнопка итога у не связанного с
    # ремонтом типа («Найден» / «Не найден»…). Только запись в отчёте и
    # статистике: result_status такого отчёта пуст, статус комплекса не меняется.
    outcome: str | None = None
    # Итог «Не работает»: что неисправно и что нужно для исправления — два отдельных текста помимо отчёта.
    fault: str | None = None
    needs: str | None = None
    # SIM-карта при её замене — отдельно от остального оборудования: какая снята, какая поставлена, рабочая ли снятая.
    sim_removed: str | None = None
    sim_installed: str | None = None
    sim_removed_state: str | None = None


@router.post("/visit-reports")
async def submit_visit_report(payload: VisitReportIn):
    person = await _require_person(payload.vk_id)
    task = await _get_task(payload.task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    if not (task["type"] or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указан тип работы")
    visit_type = combined_visit_type(task["type"]) if payload.repair_mode else task["type"]
    outcome = (payload.outcome or "").strip()
    if outcome:
        allowed = await work_types_repository.outcomes_for(task["type"])
        if outcome not in allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Такого статуса выезда нет у этого типа работ")
    data = {
        "result_status": "" if outcome else payload.result_status,
        "outcome": outcome,
        "comment": payload.comment,
        "equipment_removed": payload.equipment_removed if is_repair_type(visit_type) else None,
        "equipment_installed": payload.equipment_installed if is_repair_type(visit_type) else None,
        "fault": payload.fault, "needs": payload.needs,
        "sim_removed": payload.sim_removed, "sim_installed": payload.sim_installed,
        "sim_removed_state": payload.sim_removed_state if payload.sim_removed_state in ("working", "defective") else "",
    }
    report = await task_visit_reports_repository.upsert(
        task["id"], task["point"], visit_type, data, performed_by=person["full_name"], author_person_id=person["id"]
    )
    # Задание закрыто: сохранённый прогресс больше не нужен, а у второго монтёра бригады рабочая сессия не должна
    # висеть в «Сейчас в работе» — бот увидит это сам (см. /tasks/{id}/watch).
    await bot_task_progress_repository.delete(task["id"])
    await bot_work_sessions_repository.end_for_task(task["id"])
    return report


@router.post("/photos")
async def upload_photo(vk_id: int = Form(...), task_id: str = Form(...), file: UploadFile = File(...)):
    person = await _require_person(vk_id)
    task = await _get_task(task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else "jpg"
    taken_date = date.today().isoformat()
    name = "".join(c for c in (file.filename or "") if c.isprintable()).strip()[-80:]
    # Бот при сбое связи (ответ не дошёл вовремя) отправляет то же фото ещё раз. Если оно уже сохранено по этому заданию — отдаём его номер, а не плодим копии.
    if name:
        dup = await visit_photos_repository.find_by_name(task["id"], name)
        if dup:
            return {"id": dup["id"], "duplicate": True}
    try:
        photo = await visit_photos_repository.create(
            task["id"], task["point"], taken_date, ext, content, uploaded_by=person["full_name"], file_name=name or None
        )
    except OSError:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, "Не удалось сохранить фото: на сервере нет места или диск недоступен")
    return {"id": photo["id"]}


@router.get("/photos")
async def list_my_photos(vk_id: int, task_id: str):
    """Фото этого монтёра по заданию (старые первыми) — чтобы он мог убрать присланное по ошибке. Фото коллеги по бригаде и
    прежний архив сюда не попадают: чужое бот не удаляет."""
    person = await _require_person(vk_id)
    photos = await visit_photos_repository.list_for_task(task_id)
    return [
        {"id": p["id"], "uploaded_at": p["uploaded_at"]}
        for p in photos if p.get("uploaded_by") == person["full_name"] and not visit_photos_repository.is_imported(p)
    ]


@router.delete("/photos/{photo_id}")
async def delete_bot_photo(photo_id: int, vk_id: int, task_id: str):
    """«Отмена» в режиме фото: убирает фото, отправленные в эту пачку. Только по тому же заданию."""
    await _require_person(vk_id)
    photo = await visit_photos_repository.get(photo_id)
    if not photo or photo["task_id"] != task_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото не найдено")
    await visit_photos_repository.delete_one(photo_id)
    return {"ok": True}


class VerificationRequestIn(BaseModel):
    task_id: str


@router.post("/verification-requests")
async def create_verification_request(payload: VerificationRequestIn):
    task = await _get_task(payload.task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    return await verification_requests_repository.create(task["id"], task["point"])


@router.get("/verification-requests/{request_id}")
async def get_verification_request(request_id: int):
    req = await verification_requests_repository.get(request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    return req


class JoinRequestIn(BaseModel):
    vk_id: int
    vk_name: str | None = None


@router.post("/join-requests")
async def submit_join_request(payload: JoinRequestIn):
    return await bot_join_requests_repository.submit(payload.vk_id, payload.vk_name)


@router.get("/dispatcher-phones")
async def get_dispatcher_phones():
    """Номера диспетчеров/операторов для кнопки «Получить номер диспетчера»
    в боте — источник тот же, что и «Персонал» в веб-панели (role_type
    'operator', с заполненным телефоном)."""
    people = await people_repository.list_people(role_type="operator", active_only=True)
    return [{"full_name": p["full_name"], "phone": p["phone"]} for p in people if (p.get("phone") or "").strip()]


class AccessIssueRequestIn(BaseModel):
    task_id: str


@router.post("/access-issues")
async def create_access_issue_request(payload: AccessIssueRequestIn):
    task = await _get_task(payload.task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    return await access_issue_requests_repository.create(task["id"], task["point"])


@router.get("/access-issues/{request_id}")
async def get_access_issue_request(request_id: int):
    req = await access_issue_requests_repository.get(request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    return req


class DemontageRequestIn(BaseModel):
    task_id: str
    reason: str
    kind: str = "demontage"  # 'demontage' | 'release' — see demontage_requests_repository.py


@router.post("/demontage-requests")
async def create_demontage_request(payload: DemontageRequestIn):
    task = await _get_task(payload.task_id)
    if not task["point"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    if not payload.reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите причину демонтажа")
    if payload.kind not in ("demontage", "release"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный тип запроса")
    return await demontage_requests_repository.create(task["id"], task["point"], payload.reason.strip(), kind=payload.kind)


@router.get("/demontage-requests/{request_id}")
async def get_demontage_request(request_id: int):
    req = await demontage_requests_repository.get(request_id)
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    return req


@router.get("/tasks/{task_id}/watch")
async def watch_task(task_id: str, work_session_id: int, vk_id: int | None = None):
    """Polled continuously (low frequency) while a Ремонт/Установка menu
    session is active — the only way the bot finds out about a decision
    the DISPATCHER made from /monitoring's «Сейчас в работе» (release or
    demontage) rather than one the fitter triggered themselves, since
    lan_calendar has no way to push to the bot."""
    session = await bot_work_sessions_repository.get(work_session_id)
    closed_externally = bool(session and session.get("ended_at"))
    demontage = await demontage_requests_repository.get_latest_dispatcher_initiated(task_id)
    result = {
        "closed_externally": closed_externally,
        "demontage": demontage if demontage and demontage["status"] == "approved" else None,
    }
    # Второй монтёр бригады мог завершить комплекс или поменять отчёт/оборудование — бот подтянет это сам.
    person = await people_repository.get_by_vk_id(vk_id) if vk_id else None
    if person:
        progress = await _progress_payload(task_id, person)
        result["progress"] = progress
        result["finished"] = progress["finished"]
    return result
