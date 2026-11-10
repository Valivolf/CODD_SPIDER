"""Погода для фона страниц. GET /api/weather — без входа (нужна и странице входа; ничего личного в ответе нет).
Настройки источника и периодичности — только администратору («Настройки → Погода и фон…»)."""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.deps import require_settings
from app.services import weather

router = APIRouter(prefix="/api/weather", tags=["weather"])


class WeatherSettingsIn(BaseModel):
    source: str = "auto"
    refresh_minutes: int = weather.DEFAULT_REFRESH
    city: str = weather.CITY["name"]
    lat: float = weather.CITY["lat"]
    lon: float = weather.CITY["lon"]
    yandex_key: Optional[str] = None          # None — оставить прежний, "" — удалить


def _validated(payload: WeatherSettingsIn) -> dict:
    if payload.source not in weather.SOURCE_IDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный источник погоды")
    if not (weather.MIN_REFRESH <= payload.refresh_minutes <= weather.MAX_REFRESH):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Периодичность — от {weather.MIN_REFRESH} до {weather.MAX_REFRESH} минут")
    if not (-90 <= payload.lat <= 90 and -180 <= payload.lon <= 180):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Широта — от −90 до 90, долгота — от −180 до 180")
    return payload.model_dump()


@router.get("")
async def get_weather():
    data = await weather.current()
    cfg = await weather.load_config()
    # браузер может подержать ответ недолго; свежесть задаёт сервер (период запроса из настроек)
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={min(600, cfg['refresh_minutes'] * 30)}"})


@router.get("/settings")
async def get_settings(_: object = Depends(require_settings)):
    return weather.public_config(await weather.load_config())


@router.put("/settings")
async def put_settings(payload: WeatherSettingsIn, _: object = Depends(require_settings)):
    cfg = await weather.save_config(_validated(payload))
    return weather.public_config(cfg)


@router.post("/test")
async def test_settings(payload: WeatherSettingsIn, _: object = Depends(require_settings)):
    """Проверка источников с теми значениями, что сейчас в форме (ничего не сохраняет)."""
    saved = await weather.load_config()
    data = _validated(payload)
    key = data.pop("yandex_key")
    cfg = weather.clean_config({**data, "yandex_key": saved["yandex_key"] if key is None else key})
    results = await asyncio.to_thread(weather.test_sync, cfg)
    return {"results": results, "ok": any(r["ok"] for r in results)}
