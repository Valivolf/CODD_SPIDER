"""Server-rendered pages (login / main calendar shell)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, get_settings
from app.core import permissions as P
from app.core.deps import get_session_user
from app.web.page_guard import page_user
from app.web.asset_url import asset_url

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
templates.env.globals["asset_url"] = asset_url


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers request this exact root-level path on every page regardless
    # of any <link rel="icon"> in <head> — covers every route (including
    # ones that don't set the explicit tag) with one file, no auth needed.
    return FileResponse(ROOT_DIR / "static" / "favicon.ico")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Full validation (not just "is the JWT signature valid") — otherwise a
    # cookie left over from before a password change/reset renders the app
    # shell here while every API call it makes then 401s, stranding the
    # user on a page stuck at "Подключение…" instead of at the login form.
    if await get_session_user(request) is not None:
        return RedirectResponse("/")
    settings = get_settings()
    return templates.TemplateResponse(request, "login.html", {"app_name": settings.app_name})


@router.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    user, redirect = await page_user(request, "calendar.view")
    if redirect:
        return redirect      # нет права на календарь — на первую страницу, где право есть
    settings = get_settings()
    return templates.TemplateResponse(request, "index.html", {"app_name": settings.app_name})


@router.get("/no-access", response_class=HTMLResponse)
async def no_access_page(request: Request):
    """Роли не доступен ни один раздел (например, права убрали, пока человек был в системе)."""
    user, redirect = await page_user(request)
    if redirect:
        return redirect
    dest = P.landing(user.permissions)
    if dest != "/no-access":
        return RedirectResponse(dest)                   # доступ есть — сюда попадать незачем
    settings = get_settings()
    return templates.TemplateResponse(request, "no_access.html", {"app_name": settings.app_name, "username": user.username, "role_name": user.role_name})
