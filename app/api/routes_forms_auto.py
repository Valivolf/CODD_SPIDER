"""Яндекс Формы: автозагрузка отчётов и проверка скачивания фото — см. services/forms_auto.py. Только администратор (окно
«Настройки → Яндекс Формы: вход и автозагрузка»; вход в Яндекс делается в окне браузера робота, как для карт).

  GET  /api/forms-auto            настройки + состояние последнего запуска
  PUT  /api/forms-auto            сохранить настройки (включить, страница формы, периодичность, вход для Форм)
  POST /api/forms-auto/run        выгрузить и загрузить отчёты прямо сейчас (в фоне)
  POST /api/forms-auto/page-from-robot   запомнить страницу, открытую сейчас в окне браузера робота, как страницу «Ответы» формы
  POST /api/forms-auto/diag       «почему фото не скачивается»: что отвечает Яндекс и что видит браузер робота
  GET  /api/forms-auto/diag/shot  снимок страницы, которую открыл браузер робота при проверке
  GET  /api/forms-auto/export-shot  снимок страницы ответов, если выгрузка не удалась"""
from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.core.deps import require_settings
from app.services import forms_auto, yandex_robot

router = APIRouter(prefix="/api/forms-auto", tags=["forms-auto"], dependencies=[Depends(require_settings)])


async def _payload() -> dict[str, Any]:
    return {"config": await forms_auto.load(), "state": await forms_auto.state()}


@router.get("")
async def get_all() -> dict[str, Any]:
    return await _payload()


class SettingsIn(BaseModel):
    enabled: Optional[bool] = None
    url: Optional[str] = Field(default=None, max_length=1000)
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=100000)
    apply_status: Optional[bool] = None
    profile: Optional[str] = Field(default=None, max_length=10)


@router.put("")
async def put_settings(body: SettingsIn) -> dict[str, Any]:
    try:
        await forms_auto.save(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return await _payload()


@router.post("/run")
async def run_now() -> dict[str, Any]:
    cfg = await forms_auto.load()
    if not cfg["url"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сначала укажите страницу «Ответы» формы")
    if not forms_auto.start_now():
        raise HTTPException(status.HTTP_409_CONFLICT, "Выгрузка уже идёт — дождитесь окончания")
    return {"started": True}


@router.post("/page-from-robot")
async def page_from_robot() -> dict[str, Any]:
    """Запоминает адрес страницы, открытой в окне браузера робота (администратор открыл там форму → «Ответы»)."""
    cfg = await forms_auto.load()
    try:
        data = await yandex_robot.call("GET", "/view/url", params={"profile": cfg["profile"]}, timeout=30.0)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.detail)
    url = (data.get("url") or "").strip()
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host != "forms.yandex.ru" and not host.endswith(".forms.yandex.ru"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В окне браузера сейчас не страница Яндекс Форм. Откройте нужную форму, вкладку «Ответы», и повторите")
    try:
        await forms_auto.save({"url": url})
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"url": url}


class DiagIn(BaseModel):
    url: str = Field(default="", max_length=2000)


@router.post("/diag")
async def diag(body: DiagIn) -> dict[str, Any]:
    try:
        return await forms_auto.diag(body.url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.detail)


async def _shot(path: str) -> Response:
    try:
        resp = await yandex_robot.call("GET", path, raw=True)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.detail)
    return Response(content=resp.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/diag/shot")
async def diag_shot() -> Response:
    return await _shot("/download/diag/shot")


@router.get("/export-shot")
async def export_shot() -> Response:
    return await _shot("/forms/export/shot")
