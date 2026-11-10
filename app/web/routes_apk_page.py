"""Dedicated /apk page — a flat, filterable directory of every point
("АПК"): search, category filter, health-status icon, ping/web-interface
buttons. Available to dispatchers ("user"/«Оператор»), admins and the
read-only 'viewer' role — not the 'warehouse' role, which has its own
dedicated /warehouse page with the full equipment-management workflow
instead.
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


@router.get("/apk", response_class=HTMLResponse)
async def apk_page(request: Request):
    user, redirect = await page_user(request, "points.view", "check.use")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(
        request, "apk.html",
        {"app_name": settings.app_name, "yandex_maps_api_key": settings.yandex_maps_api_key},
    )
