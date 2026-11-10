"""Страница /notifications — история уведомлений диспетчера (проверки, решения по запросам монтёров, отчёты).
Те же роли, что и у /monitoring: диспетчер, админ и «только просмотр»."""
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


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user, redirect = await page_user(request, "notifications.view")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(
        request, "notifications.html", {"app_name": settings.app_name, "yandex_maps_api_key": settings.yandex_maps_api_key}
    )
