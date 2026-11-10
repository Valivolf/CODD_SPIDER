"""Dedicated /stats page — charts/counters over a configurable date range
(visit types, results, current status distribution, went-down/recovered).
Same audience as /apk: dispatchers ("user") and admins, not the
'warehouse' role.
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


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    user, redirect = await page_user(request, "stats.view")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "stats.html", {"app_name": settings.app_name})
