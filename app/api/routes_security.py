"""Журнал событий безопасности (только администратор) и политика безопасности для страницы (любой вошедший)."""
from __future__ import annotations

import datetime as dt
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.core import password_policy
from app.core.clientip import client_ip
from app.core.deps import get_current_user, require_security_log
from app.models.user import UserOut
from app.services import security_log

router = APIRouter(prefix="/api/security", tags=["security"])


def _date(value: str) -> str:
    if not value:
        return ""
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата (нужна ГГГГ-ММ-ДД)")


@router.get("/policy")
async def policy(user: UserOut = Depends(get_current_user)):
    """Что странице нужно знать о политике: минимальная длина пароля (подсказка при смене) и время бездействия до автоматического выхода (0 — выключено)."""
    try:
        idle = max(0, int(os.environ.get("SESSION_IDLE_MINUTES", "0")))
    except ValueError:
        idle = 0
    return {"password_min_length": password_policy.min_length(), "idle_minutes": idle}


@router.get("/events", dependencies=[Depends(require_security_log)])
async def events(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), kind: str = "", username: str = "", date_from: str = "", date_to: str = "", only_failed: bool = False):
    out = await security_log.list_events(limit=limit, offset=offset, kind=kind, username=username, date_from=_date(date_from), date_to=_date(date_to), only_failed=only_failed)
    out["kinds"] = security_log.KINDS
    out["retention_days"] = security_log.retention_days()
    return out


@router.get("/events/export")
async def export(request: Request, admin: UserOut = Depends(require_security_log), kind: str = "", username: str = "", date_from: str = "", date_to: str = "", only_failed: bool = False):
    body = await security_log.export_csv(kind=kind, username=username, date_from=_date(date_from), date_to=_date(date_to), only_failed=only_failed)
    await security_log.record("admin_action", username=admin.username, role=admin.role, ip=client_ip(request), details={"method": "GET", "path": "/api/security/events/export"})
    return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="security-events-{dt.date.today().isoformat()}.csv"'})
