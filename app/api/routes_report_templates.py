"""Operator-uploaded .docx/.xlsx report templates — upload, list, delete,
and generate a filled copy for a chosen date/period. See
app/services/template_engine.py for how {{variable}} placeholders are
filled in and app/models/report_fields.py for the variable catalog.
"""
from __future__ import annotations

import io
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from fastapi.responses import StreamingResponse

from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.models.report_fields import template_variable_catalog
from app.models.user import UserOut
from app.repositories import report_templates_repository, reports_repository, user_repository
from app.services import report_access, template_engine

router = APIRouter(prefix="/api/report-templates", tags=["report-templates"], dependencies=[Depends(require_password_ok)])

_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class AccessUpdate(BaseModel):
    visibility: str
    user_ids: list[int] = Field(default_factory=list)


@router.get("/variables")
async def get_variable_catalog():
    return {"variables": template_variable_catalog()}


@router.get("")
async def list_templates(user: UserOut = Depends(require_password_ok)):
    return [
        {**t, "can_edit": report_access.can_edit(t, user), "can_manage_access": report_access.can_manage_access(t, user)}
        for t in await report_templates_repository.list_templates() if report_access.can_view(t, user)
    ]


@router.get("/users")
async def access_users():
    return [{"id": u["id"], "username": u["username"]} for u in await user_repository.list_users()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_template(
    file: UploadFile,
    name: str = Form(...),
    visibility: str = Form("private"),
    user: UserOut = Depends(require_password_ok),
):
    if not name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите название отчёта")
    filename = file.filename or ""
    file_type = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if file_type not in report_templates_repository.ALLOWED_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл .docx или .xlsx")
    content = await file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    try:
        visibility, _ = report_access.clean_access(visibility, [])
        template = await report_templates_repository.create_template(name, file_type, content, user.username, visibility)
    except Exception as exc:  # noqa: BLE001 — surfaced as a clear upload error
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не удалось прочитать файл: {exc}")
    if not template["variables"]:
        # Not fatal — a template can legitimately be static — but almost
        # certainly means the operator forgot {{...}} placeholders or used
        # different-looking brackets, so flag it in the response rather
        # than silently accepting an empty-looking template.
        template["warning"] = "В файле не найдено ни одной переменной вида {{ключ}}"
    return template


@router.put("/{template_id}/access")
async def set_template_access(template_id: int, payload: AccessUpdate, user: UserOut = Depends(require_password_ok)):
    item = await report_templates_repository.get_template(template_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    if not report_access.can_manage_access(item, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Менять доступ может владелец шаблона")
    try:
        visibility, ids = report_access.clean_access(payload.visibility, payload.user_ids)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Проверьте уровень доступа и выбранные учётные записи")
    known = {u["id"] for u in await user_repository.list_users()}
    if not set(ids) <= known:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В списке есть неизвестная учётная запись")
    return await report_templates_repository.set_access(template_id, visibility, ids)


@router.put("/{template_id}")
async def update_template(
    template_id: int,
    name: str = Form(...),
    file: UploadFile | None = File(None),
    user: UserOut = Depends(require_password_ok),
):
    item = await report_templates_repository.get_template(template_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    if not report_access.can_edit(item, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Изменять шаблон может владелец")
    if not name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите название отчёта")
    content = None
    if file and file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext != item["file_type"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Новый файл должен быть того же типа: .{item['file_type']}")
        content = await file.read()
        if not content:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустой файл")
    try:
        updated = await report_templates_repository.update_template(template_id, name, content)
    except Exception as exc:  # noqa: BLE001 — ошибка чтения файла показана оператору
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не удалось прочитать файл: {exc}")
    return updated


@router.delete("/{template_id}")
async def delete_template(template_id: int, user: UserOut = Depends(require_password_ok)):
    item = await report_templates_repository.get_template(template_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    if not report_access.can_edit(item, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Удалить шаблон может владелец")
    await report_templates_repository.delete_template(template_id)
    return {"ok": True}


@router.get("/{template_id}/generate")
async def generate_report(template_id: int, date_from: str, date_to: str, user: UserOut = Depends(require_password_ok)):
    template = await report_templates_repository.get_template(template_id)
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    if not report_access.can_view(template, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Шаблон не найден")
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный формат даты")
    if date_from > date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Начало периода позже конца")

    context, row_records = await reports_repository.build_template_context(date_from, date_to)
    content = report_templates_repository.read_file(template)
    result = template_engine.render(template["file_type"], content, context, row_records)

    period_part = format_date(date_from) if date_from == date_to else f"{format_date(date_from)}_{format_date(date_to)}"
    safe_name = "".join(c for c in template["name"] if c not in '\\/:*?"<>|').strip() or "otchet"
    filename = f"{safe_name}-{period_part}.{template['file_type']}"
    # filename may contain Cyrillic — plain `filename=` must stay Latin-1,
    # so give an ASCII fallback plus the real name via RFC 5987 filename*
    # (every modern browser prefers filename* when both are present).
    ascii_filename = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    headers = {
        "Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"
    }
    if result.missing_variables:
        # Surfaced as a header rather than failing the download outright —
        # the document is still usable, just with those placeholders left
        # blank; the frontend reads this to show a warning.
        headers["X-Missing-Variables"] = ",".join(result.missing_variables)
    return StreamingResponse(io.BytesIO(result.content), media_type=_MEDIA_TYPES[template["file_type"]], headers=headers)
