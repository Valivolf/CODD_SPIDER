"""Отчёты из онлайн-конструктора (страница /reports/builder): создать шаблон
(выбрать формат xlsx/docx), править, посмотреть, сформировать за период.
Шаблон и логика разворачивания — app/models/report_design.py,
app/services/report_designer.py; данные — report_design_data.collect()."""
from __future__ import annotations

import io
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.dates import format_date
from app.core.day_selection import parse_days_param
from app.core.deps import require_password_ok
from app.models.report_design import FORMATS, sanitize_design, starter_design
from app.models.user import UserOut
from app.repositories import report_designs_repository
from app.repositories import user_repository
from app.services import report_access, report_design_data, report_designer

router = APIRouter(prefix="/api/report-designs", tags=["report-designs"], dependencies=[Depends(require_password_ok)])

_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_MAX_NAME = 100
_FORBIDDEN_FILENAME_CHARS = '\\/:*?"<>|'


class DesignCreate(BaseModel):
    name: str
    format: str
    kind: str = "crews"          # «crews» — заготовка «по таблице на бригаду», «blank» — пустой
    design: dict | None = None   # готовый шаблон (например, копия существующего)
    visibility: str = "private"


class DesignUpdate(BaseModel):
    name: str
    design: dict


class AccessUpdate(BaseModel):
    visibility: str
    user_ids: list[int] = Field(default_factory=list)


class PreviewIn(BaseModel):
    design: dict
    date_from: str = ""
    date_to: str = ""
    days: list[str] | None = None     # отдельные выбранные дни (вместо периода)
    today: str = ""
    now: str = ""


def _clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите название отчёта")
    return name[:_MAX_NAME]


def _period(date_from: str, date_to: str, today: str, days: list[str] | str | None = None) -> tuple[str, str, str, list[str] | None]:
    """(с, по, сегодня, выбранные дни). Если указаны `days` — период берётся из
    них (крайние даты), и данные собираются только за эти дни."""
    try:
        today_iso = date.fromisoformat(today).isoformat() if today else date.today().isoformat()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    if days:
        try:
            day_list = parse_days_param(days)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
        return day_list[0], day_list[-1], today_iso, day_list
    try:
        d_from, d_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    if d_to < d_from:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Начало периода позже конца")
    if (d_to - d_from).days + 1 > report_design_data.MAX_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Период не больше {report_design_data.MAX_DAYS} дней")
    return d_from.isoformat(), d_to.isoformat(), today_iso, None


def _sanitized(design: dict, fmt: str) -> dict:
    try:
        return sanitize_design(design, fmt)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("")
async def list_designs(user: UserOut = Depends(require_password_ok)):
    return [
        {**d, "can_edit": report_access.can_edit(d, user), "can_manage_access": report_access.can_manage_access(d, user)}
        for d in await report_designs_repository.list_designs() if report_access.can_view(d, user)
    ]


@router.get("/users")
async def access_users():
    return [{"id": u["id"], "username": u["username"]} for u in await user_repository.list_users()]


@router.get("/variables")
async def variables():
    return {"variables": report_design_data.design_variable_catalog()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_design(payload: DesignCreate, user: UserOut = Depends(require_password_ok)):
    name = _clean_name(payload.name)
    if payload.format not in FORMATS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Формат файла — xlsx или docx")
    if payload.design is not None:
        design = _sanitized(payload.design, payload.format)
    else:
        design = starter_design(payload.format, "blank" if payload.kind == "blank" else "crews")
    try:
        visibility, _ = report_access.clean_access(payload.visibility, [])
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный уровень доступа")
    return await report_designs_repository.create_design(name, payload.format, design, user.username, visibility)


@router.put("/{design_id}/access")
async def set_design_access(design_id: int, payload: AccessUpdate, user: UserOut = Depends(require_password_ok)):
    item = await report_designs_repository.get_design(design_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    if not report_access.can_manage_access(item, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Менять доступ может владелец отчёта")
    try:
        visibility, ids = report_access.clean_access(payload.visibility, payload.user_ids)
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Проверьте уровень доступа и выбранные учётные записи")
    known = {u["id"] for u in await user_repository.list_users()}
    if not set(ids) <= known:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В списке есть неизвестная учётная запись")
    return await report_designs_repository.set_access(design_id, visibility, ids)


@router.post("/preview")
async def preview(payload: PreviewIn):
    """Развёрнутый шаблон (без файла) — то, что рисует предпросмотр в конструкторе."""
    fmt = payload.design.get("format") if isinstance(payload.design, dict) else None
    design = _sanitized(payload.design, fmt or "")
    date_from, date_to, today, days = _period(payload.date_from, payload.date_to, payload.today, payload.days)
    data = await report_design_data.collect(date_from, date_to, today, payload.now, days)
    return report_designer.expand(design, data)


@router.get("/{design_id}")
async def get_design(design_id: int, user: UserOut = Depends(require_password_ok)):
    found = await report_designs_repository.get_design(design_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    if not report_access.can_view(found, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    return {**found, "can_edit": report_access.can_edit(found, user), "can_manage_access": report_access.can_manage_access(found, user)}


@router.put("/{design_id}")
async def update_design(design_id: int, payload: DesignUpdate, user: UserOut = Depends(require_password_ok)):
    found = await report_designs_repository.get_design(design_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    if not report_access.can_edit(found, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Изменять отчёт может владелец")
    design = _sanitized(payload.design, found["format"])  # формат файла не меняется после создания
    return await report_designs_repository.update_design(design_id, _clean_name(payload.name), design)


@router.delete("/{design_id}")
async def delete_design(design_id: int, user: UserOut = Depends(require_password_ok)):
    found = await report_designs_repository.get_design(design_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    if not report_access.can_edit(found, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Удалить отчёт может владелец")
    await report_designs_repository.delete_design(design_id)
    return {"ok": True}


@router.get("/{design_id}/generate")
async def generate(design_id: int, date_from: str = "", date_to: str = "", days: str = "", today: str = "", now: str = "", user: UserOut = Depends(require_password_ok)):
    found = await report_designs_repository.get_design(design_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    if not report_access.can_view(found, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёт не найден")
    d_from, d_to, today_iso, day_list = _period(date_from, date_to, today, days)
    data = await report_design_data.collect(d_from, d_to, today_iso, now, day_list)
    expanded = report_designer.expand(found["design"], data)
    fmt = found["format"]
    content = report_designer.render_xlsx(expanded) if fmt == "xlsx" else report_designer.render_docx(expanded)

    period_part = format_date(d_from) if d_from == d_to else f"{format_date(d_from)}_{format_date(d_to)}"
    if day_list and len(day_list) > 1 and (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days + 1 != len(day_list):
        period_part += f"-{len(day_list)}-dn"
    safe_name = "".join(c for c in found["name"] if c not in _FORBIDDEN_FILENAME_CHARS).strip() or "otchet"
    filename = f"{safe_name}-{period_part}.{fmt}"
    ascii_filename = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    headers = {"Content-Disposition": f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename)}"}
    if expanded["warnings"]:
        headers["X-Report-Warnings"] = str(len(expanded["warnings"]))
    return StreamingResponse(io.BytesIO(content), media_type=_MEDIA_TYPES[fmt], headers=headers)
