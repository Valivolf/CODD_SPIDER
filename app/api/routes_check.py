"""«Проверка» (Все АПК → Проверка): список комплексов → всё о каждом в понятном виде и выгрузка в Excel с выбором столбцов. Логика — app/services/check_service.py.
Ничего не меняет в данных (поэтому доступна и роли «только просмотр»), данные читаются заново при каждом запросе."""
from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.services import check_service

router = APIRouter(prefix="/api/check", tags=["check"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _allowed(user: UserOut) -> None:
    if not P.has(user.permissions, "check.use"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: вкладка «Проверка»")


class CheckIn(BaseModel):
    text: str = ""


class ExportIn(BaseModel):
    text: str = ""
    columns: list[str] = Field(default_factory=list)


@router.get("/columns")
async def columns(user: UserOut = Depends(require_password_ok)):
    _allowed(user)
    return check_service.columns_for(user.permissions)


@router.post("")
async def run_check(payload: CheckIn, user: UserOut = Depends(require_password_ok)):
    _allowed(user)
    if not check_service.tokens_of(payload.text):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Впишите номера комплексов — через пробел или с новой строки")
    return await check_service.check(payload.text, user.permissions)


@router.post("/export")
async def export(payload: ExportIn, user: UserOut = Depends(require_password_ok)):
    _allowed(user)
    if not check_service.tokens_of(payload.text):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Список комплексов пуст")
    result = await check_service.check(payload.text, user.permissions)
    try:
        data = check_service.build_xlsx(result["items"], payload.columns, user.permissions)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    name = f"proverka-{dt.date.today().strftime('%d-%m-%Y')}.xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Rows": str(len(result["items"]))})
