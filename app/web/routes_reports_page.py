"""Dedicated /reports page — текстовая сводка и .xlsx-выгрузки за день/
неделю. Same audience as /monitoring: dispatchers ("user"), admins and the
read-only 'viewer' role, not the 'warehouse' role.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, get_settings
from app.core.deps import get_session_user
from app.web.page_guard import page_user
from app.web.asset_url import asset_url

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
templates.env.globals["asset_url"] = asset_url


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    user, redirect = await page_user(request, "reports.view")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "reports.html", {"app_name": settings.app_name})


@router.get("/reports/builder", response_class=HTMLResponse)
async def report_builder_page(request: Request):
    """Конструктор отчётов (создание и правка шаблонов) — только тем, кто может
    писать: админ и оператор; «только просмотр» видит готовые отчёты на /reports."""
    user, redirect = await page_user(request, "reports.builder")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "report_builder.html", {"app_name": settings.app_name})
