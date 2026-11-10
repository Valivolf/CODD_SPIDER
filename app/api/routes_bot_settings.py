"""Session-authed side of the bot's on/off switch — the bot itself polls
GET /api/bot/enabled (bot-key auth, routes_bot.py). See
bot_settings_repository.py for why this exists (two possible bot
processes — org server + a developer's laptop — only one should be live).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.repositories import bot_settings_repository

router = APIRouter(prefix="/api/bot-settings", tags=["bot-settings"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def get_bot_settings():
    return {"enabled": await bot_settings_repository.get_enabled()}


class BotSettingsIn(BaseModel):
    enabled: bool


@router.put("")
async def update_bot_settings(payload: BotSettingsIn):
    return {"enabled": await bot_settings_repository.set_enabled(payload.enabled)}
