"""Session-authed side of the bot's self-service "link me" requests — the
bot-key side that creates these lives in routes_bot.py. See
bot_join_requests_repository.py for the shape.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import bot_join_requests_repository

router = APIRouter(prefix="/api/join-requests", tags=["join-requests"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def list_join_requests():
    return await bot_join_requests_repository.list_all()


@router.post("/{vk_id}/block")
async def block_join_request(vk_id: int, user: UserOut = Depends(require_password_ok)):
    await bot_join_requests_repository.block(vk_id, resolved_by=user.username)
    return {"ok": True}


@router.post("/{vk_id}/unblock")
async def unblock_join_request(vk_id: int):
    await bot_join_requests_repository.unblock(vk_id)
    return {"ok": True}
