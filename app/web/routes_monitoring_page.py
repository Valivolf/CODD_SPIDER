"""Dedicated /monitoring page — who's currently working (bot work
sessions), what needs a human recheck, and the day-by-day photo archive.
Same audience as /reports: dispatchers ("user"), admins and the read-only
'viewer' role, not the 'warehouse' role.
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


@router.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    user, redirect = await page_user(request, "monitoring.view")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "monitoring.html", {"app_name": settings.app_name})
