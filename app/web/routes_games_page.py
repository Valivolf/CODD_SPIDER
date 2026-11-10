"""Страница /games — игры для перерывов. Открывают только те пользователи, кому администратор включил игры («Администрирование → Игры»);
остальных перекидывает на их стартовую страницу. Сами игры, доступ и рекорды — app/services/games.py."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, get_settings
from app.core import permissions as P
from app.services import games
from app.web.asset_url import asset_url
from app.web.page_guard import page_user

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
templates.env.globals["asset_url"] = asset_url


@router.get("/games", response_class=HTMLResponse)
async def games_page(request: Request):
    user, redirect = await page_user(request)
    if redirect:
        return redirect
    back = P.landing(user.permissions)
    if await games.allowed_keys(user.id) is None:
        return RedirectResponse(back if back != "/games" else "/no-access")
    settings = get_settings()
    return templates.TemplateResponse(request, "games.html", {"app_name": settings.app_name, "back_url": back if back != "/no-access" else "/"})
