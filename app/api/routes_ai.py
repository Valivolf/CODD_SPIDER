"""Настройки и состояние ИИ — «Настройки → ИИ…», только администратор: включить/выключить голос в боте и помощника в веб-панели,
увидеть, запущены ли распознавание речи и языковая модель, сколько занимает последний запрос."""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import require_settings
from app.services.ai import client, plan_cache, report_parser, warmup
from app.services.ai import settings as ai_settings

router = APIRouter(prefix="/api/ai", tags=["ai"], dependencies=[Depends(require_settings)])


class AiSettingsIn(BaseModel):
    bot_voice: Optional[bool] = None
    assistant: Optional[bool] = None
    forms_fault: Optional[bool] = None
    server_llm: Optional[bool] = None
    server_asr: Optional[bool] = None


@router.get("/settings")
async def get_settings():
    return {"config": await ai_settings.load(force=True), "status": await client.status(force=True), "plans": await plan_cache.stats()}


@router.put("/settings")
async def put_settings(payload: AiSettingsIn):
    before = await ai_settings.load(force=True)
    cfg = await ai_settings.save(payload.model_dump())
    if any(cfg.get(k) and not before.get(k) for k in ("bot_voice", "assistant")):
        warmup.schedule()                      # режим только что включили — заранее «прочитать» подсказки, чтобы первый ответ не ждал минуты
    return {"config": cfg, "status": await client.status(force=True)}


@router.get("/status")
async def get_status():
    return await client.status(force=True)


@router.post("/selftest")
async def selftest():
    """Проверка языковой модели на коротком примере: сколько времени занял разбор и что получилось (ничего не сохраняет)."""
    started = time.monotonic()
    try:
        parsed = await report_parser.parse(
            "приехали нет питания сгорел блок питания заменили перезагрузили роутер пинг пошёл", visit_type="Ремонт", priority=client.PRIORITY_USER,
        )
    except (client.AiUnavailable, client.AiError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return {"seconds": round(time.monotonic() - started, 1), "result": parsed}
