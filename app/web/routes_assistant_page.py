"""Страница /assistant — «🤖 Помощник»: вопросы по данным и заполнение шаблонов Excel/Word. Открывают все роли; данные отдаются по правам роли
(app/services/ai/catalog.py), включён ли помощник — решает администратор («Настройки → ИИ»)."""
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


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request):
    user, redirect = await page_user(request, "assistant.use")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "assistant.html", {"app_name": settings.app_name})
