"""Session-authed data for the /monitoring page — active bot work
sessions, points needing a human recheck, and the day-by-day photo
archive (browse/download-zip/delete). See app/web/routes_monitoring_page.py
for the page itself.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.db.database import get_connection
from app.models.user import UserOut
from app.core.work_types import combined_visit_type
from app.repositories import (
    bot_task_progress_repository,
    bot_work_sessions_repository,
    demontage_requests_repository,
    monitoring_repository,
    people_repository,
    task_visit_reports_repository,
    verification_requests_repository,
    visit_photos_repository,
)
from app.services import excel_service

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"], dependencies=[Depends(require_password_ok)])


def _validate_date(value: str) -> None:
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")


@router.get("/active-sessions")
async def active_sessions():
    return await bot_work_sessions_repository.list_active()


async def _task_type_and_point(task_id: str) -> tuple[str, str]:
    async with get_connection() as conn:
        cur = await conn.execute("select type, point from tasks where id = ?", (task_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено")
    return row[0] or "", row[1] or ""


@router.post("/active-sessions/{session_id}/release")
async def release_active_session(session_id: int, user: UserOut = Depends(require_password_ok)):
    """Диспетчер решает не чинить прямо из «Сейчас в работе», не дожидаясь,
    пока бот сам заведёт access_issue_request — закрывает задание сразу,
    той же логикой, что и обычное «отпустить без ремонта»."""
    session = await bot_work_sessions_repository.get(session_id)
    if not session or session.get("ended_at"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Активная сессия не найдена")
    visit_type, point_id = await _task_type_and_point(session["task_id"])
    # В отчёт идёт то, что написал монтёр (что сделано и что нужно для следующего выезда), а не отметка о том, что
    # диспетчер отпустил бригаду: заглушка — только если монтёр отчёта ещё не писал.
    progress = await bot_task_progress_repository.get(session["task_id"])
    state = (progress or {}).get("state") or {}
    comment, removed, installed = bot_task_progress_repository.report_parts(state)
    sim_removed, sim_installed, sim_state = bot_task_progress_repository.sim_parts(state)
    fault, needs = bot_task_progress_repository.fault_parts(state)
    if state.get("switched_to_repair"):
        visit_type = combined_visit_type(visit_type)
    author = None
    if comment and progress and progress.get("updated_by"):
        same = [p for p in await people_repository.list_people() if p["full_name"] == progress["updated_by"]]
        author = same[0] if len(same) == 1 else None
    await task_visit_reports_repository.upsert(
        session["task_id"], point_id or session["point_id"], visit_type,
        {
            "result_status": "Не работает",
            "comment": comment or "Диспетчер отпустил бригаду без ремонта (отчёт монтёра не был написан).",
            "equipment_removed": removed, "equipment_installed": installed, "fault": fault, "needs": needs,
            "sim_removed": sim_removed, "sim_installed": sim_installed, "sim_removed_state": sim_state,
        },
        performed_by=progress["updated_by"] if comment and progress and progress.get("updated_by") else user.username,
        author_person_id=author["id"] if author else None, edited_by=user.username,
    )
    await bot_task_progress_repository.delete(session["task_id"])
    # Бригада из двух монтёров: задание закрыто — закрываем сессии обоих.
    await bot_work_sessions_repository.end_for_task(session["task_id"])
    await bot_work_sessions_repository.end(session_id)
    return {"ok": True}


class ActiveSessionDemontageIn(BaseModel):
    reason: str


@router.post("/active-sessions/{session_id}/demontage")
async def demontage_active_session(session_id: int, payload: ActiveSessionDemontageIn, user: UserOut = Depends(require_password_ok)):
    """Диспетчер инициирует демонтаж сам, не дожидаясь запроса монтёра —
    заявка сразу одобрена (это уже решение диспетчера), бот подхватит её
    через фоновый watcher задания и попросит монтёра прислать фото."""
    if not payload.reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите причину демонтажа")
    session = await bot_work_sessions_repository.get(session_id)
    if not session or session.get("ended_at"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Активная сессия не найдена")
    return await demontage_requests_repository.create_resolved(
        session["task_id"], session["point_id"], payload.reason.strip(), resolved_by=user.username
    )


@router.delete("/active-sessions/{session_id}")
async def delete_active_session(session_id: int):
    """Просто убирает запись из «Сейчас в работе» (например, бот упал и
    не закрыл сессию сам) — в отличие от /release, отчёт НЕ отправляет."""
    session = await bot_work_sessions_repository.get(session_id)
    if not session:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сессия не найдена")
    await bot_work_sessions_repository.end(session_id)
    return {"ok": True}


@router.get("/needs-check")
async def needs_check():
    return await monitoring_repository.needs_check()


@router.delete("/point-summaries/{task_id}")
async def delete_point_summary(task_id: str):
    await task_visit_reports_repository.delete_for_task(task_id)
    return {"ok": True}


@router.get("/point-summaries")
async def point_summaries(date_from: str = Query(...), date_to: str = Query(...), tz_offset: int = Query(default=0, ge=-900, le=900)):
    """Список на странице «Мониторинг»: период и дата — по ФАКТИЧЕСКОМУ дню отправки отчёта (по часам браузера,
    `tz_offset` = getTimezoneOffset). Выгрузка ниже, как и раньше, — по дате задания."""
    _validate_date(date_from)
    _validate_date(date_to)
    return await monitoring_repository.point_summaries(date_from, date_to, by_report_date=True, tz_offset_minutes=tz_offset)


@router.get("/point-summaries/export")
async def export_point_summaries(
    date_from: str = Query(...), date_to: str = Query(...), fields: list[str] = Query(default=[])
):
    """«Итоги по комплексам» as .xlsx, with the same period/field-picker
    pattern as /api/task-visit-reports/export — see monitoring_repository.
    EXPORT_FIELDS for the available columns."""
    _validate_date(date_from)
    _validate_date(date_to)
    known_keys = {k for k, _ in monitoring_repository.EXPORT_FIELDS}
    selected = set(fields) & known_keys
    chosen_fields = [(k, label) for k, label in monitoring_repository.EXPORT_FIELDS if not fields or k in selected]
    if not chosen_fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите хотя бы одно поле")
    rows = await monitoring_repository.point_summaries(date_from, date_to)
    for row in rows:       # в таблице — слова, а не внутренние коды
        row["sim_removed_state"] = task_visit_reports_repository.SIM_STATE_LABELS.get(row.get("sim_removed_state") or "", "")
    out = excel_service.build_point_summaries_export(rows, chosen_fields)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="point-summaries-{format_date(date_from)}_{format_date(date_to)}.xlsx"'},
    )


@router.get("/photos")
async def list_photos(date: str = Query(...)):
    _validate_date(date)
    return await visit_photos_repository.counts_for_date(date)


# Что браузер может показывать картинкой; всё остальное отдаём как файл для скачивания.
_IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "jpe": "image/jpeg", "jfif": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}


@router.get("/photos/days")
async def list_photo_days():
    """Дни с фото — новые сверху (окно «Фото по дням»)."""
    return await visit_photos_repository.list_days()


@router.get("/photos/day-items")
async def list_photo_day_items(date: str = Query(...)):
    """Фото одного дня по комплексам."""
    _validate_date(date)
    return await visit_photos_repository.list_for_date(date)


@router.get("/photos/by-point")
async def list_photo_points():
    """Комплексы, по которым есть фото (вкладка «По комплексам»): сколько, за какие даты, сколько из прежнего архива."""
    return await visit_photos_repository.list_points_with_photos()


@router.get("/photos/point-items")
async def list_photo_point_items(point_id: str = Query(...)):
    """Все фото одного комплекса по дням, новые сверху (кнопка «Фото» на карточке комплекса)."""
    return await visit_photos_repository.list_for_point(point_id)


MAX_UPLOAD_PHOTO_BYTES = 15 * 1024 * 1024
_EARLIEST_PHOTO_DATE = date_cls(2000, 1, 1)


def _sniff_image(content: bytes) -> str | None:
    """Расширение по самому содержимому (имени файла верить нельзя): только то, что браузер умеет показывать картинкой."""
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[:2] == b"BM":
        return "bmp"
    return None


@router.post("/photos/upload")
async def upload_point_photo(
    point_id: str = Form(...), date: str = Form(...), file: UploadFile = File(...), user: UserOut = Depends(require_password_ok),
):
    """Фото к комплексу, добавленное вручную из веба, с выбором даты, к которой оно относится. Лежит там же и показывается так же, как фото из бота
    (в «Фото комплекса» и «Фото по дням»); удалять такое фото можно (в отличие от фото из прежнего архива). Один файл за запрос — страница шлёт по одному,
    чтобы большая пачка не упиралась в предел размера запроса и сбой одного снимка не отменял остальные."""
    _validate_date(date)
    taken = date_cls.fromisoformat(date)
    if taken > date_cls.today() + timedelta(days=1):          # сутки запаса: часовой пояс браузера может быть впереди сервера
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Дата в будущем — выберите день, когда фото снято (не позже сегодняшнего)")
    if taken < _EARLIEST_PHOTO_DATE:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Слишком старая дата — проверьте год")
    point_id = (point_id or "").strip()
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from points where id = ?", (point_id,))
        if not await cur.fetchone():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Комплекс не найден")
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    if len(content) > MAX_UPLOAD_PHOTO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Файл слишком большой ({len(content) // 1_000_000} МБ): можно не больше {MAX_UPLOAD_PHOTO_BYTES // 1_000_000} МБ")
    ext = _sniff_image(content)
    if ext is None:
        if content[4:8] == b"ftyp" and content[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heim", b"heis"):
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Формат HEIC браузер показывать не умеет — пришлите снимок в JPEG (на iPhone: «Настройки → Камера → Форматы → Наиболее совместимый»)")
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Это не фото в поддерживаемом формате (JPEG, PNG, WebP, GIF, BMP)")
    name = "".join(c for c in (file.filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if c.isprintable()).strip()[-80:] or None
    try:
        photo = await visit_photos_repository.create("", point_id, taken.isoformat(), ext, content, user.username, source="web", file_name=name)
    except OSError:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, "Не удалось сохранить фото: на сервере нет места или диск недоступен")
    return {"id": photo["id"], "date": photo["taken_date"], "ext": ext}


@router.get("/photos/{photo_id}/file")
async def photo_file(photo_id: int, download: bool = False):
    photo = await visit_photos_repository.get(photo_id)
    if not photo:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото не найдено")
    try:
        content = await visit_photos_repository.read_file(photo)
    except TimeoutError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Сетевой диск с фото сейчас недоступен — попробуйте позже")
    except OSError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл фото не найден на диске")
    media_type = _IMAGE_TYPES.get((photo["file_ext"] or "").lower())
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
    if download:                      # кнопка «Скачать» у фото: сохранить файлом с понятным именем «<комплекс>_<дата>_<номер>.jpg»
        headers["Content-Disposition"] = f"attachment; filename=\"photo-{photo_id}.{photo['file_ext']}\"; filename*=UTF-8''{quote(visit_photos_repository.download_name(photo))}"
        headers["Cache-Control"] = "private, no-store"
        media_type = media_type or "application/octet-stream"
    elif media_type is None:
        name = photo.get("file_name") or f"photo-{photo_id}.{photo['file_ext']}"
        headers["Content-Disposition"] = f"attachment; filename=\"photo-{photo_id}.{photo['file_ext']}\"; filename*=UTF-8''{quote(name)}"
        media_type = "application/octet-stream"
    return Response(content=content, media_type=media_type, headers=headers)


@router.delete("/photos/{photo_id}")
async def delete_one_photo(photo_id: int):
    photo = await visit_photos_repository.get(photo_id)
    if photo and visit_photos_repository.is_imported(photo):
        raise HTTPException(status.HTTP_409_CONFLICT, "Это фото из прежнего архива на сетевом диске — программа его не удаляет. "
                                                      "Удалите файл на самом диске: при следующем поиске он пропадёт из списка.")
    if not await visit_photos_repository.delete_one(photo_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото не найдено")
    return {"removed": 1}


@router.get("/photos/export-all")
async def export_all_photos():
    """Все фото за все дни одним архивом: наверху — названия комплексов, внутри них папки по дням с датой."""
    photos = await visit_photos_repository.list_all_files()
    if not photos:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото пока нет")
    return StreamingResponse(
        visit_photos_repository.stream_zip_all(photos),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="photos-all-days.zip"'},
    )


@router.get("/photos/export")
async def export_photos(date: str = Query(...)):
    _validate_date(date)
    photos = await visit_photos_repository.list_files_for_date(date)
    return StreamingResponse(
        visit_photos_repository.stream_zip(photos),         # по частям, фото читаются параллельно и без сжатия — скачивание начинается сразу
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="photos-{format_date(date)}.zip"'},
    )


@router.delete("/photos")
async def delete_photos(point_id: str, date: str = Query(...)):
    _validate_date(date)
    removed = await visit_photos_repository.delete_for_point_date(point_id, date)
    return {"removed": removed}
