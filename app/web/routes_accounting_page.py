"""Отдельная страница комплексов для бухгалтерии."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import ROOT_DIR, get_settings
from app.web.asset_url import asset_url
from app.web.page_guard import page_user

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT_DIR / "templates"))
templates.env.globals["asset_url"] = asset_url


@router.get("/accounting", response_class=HTMLResponse)
async def accounting_page(request: Request) -> Response:
    _, redirect = await page_user(request, "accounting.view")
    if redirect:
        return redirect
    settings = get_settings()
    return templates.TemplateResponse(request, "accounting.html", {"app_name": settings.app_name})
