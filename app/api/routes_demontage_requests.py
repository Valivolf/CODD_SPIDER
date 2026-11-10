"""Session-authed side of monter-initiated demontage requests — the
bot-key side that creates these lives in routes_bot.py. See
demontage_requests_repository.py for the shape.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.core.point_credentials import strip_credentials
from app.models.user import UserOut
from app.repositories import demontage_requests_repository

router = APIRouter(prefix="/api/demontage-requests", tags=["demontage-requests"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def list_demontage_requests(
    status_filter: str | None = Query(default=None, alias="status"), limit: int = 20, user: UserOut = Depends(require_password_ok)
):
    if status_filter == "pending":
        return strip_credentials(await demontage_requests_repository.list_pending(), user)
    return await demontage_requests_repository.list_recent(limit)


class ResolveIn(BaseModel):
    approve: bool
    decline_comment: str | None = None


@router.post("/{request_id}/resolve")
async def resolve_demontage_request(request_id: int, payload: ResolveIn, user: UserOut = Depends(require_password_ok)):
    existing = await demontage_requests_repository.get(request_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    if existing["status"] != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Запрос уже обработан")
    if not payload.approve and not (payload.decline_comment or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите причину отказа")
    return await demontage_requests_repository.resolve(
        request_id, approve=payload.approve, decline_comment=payload.decline_comment, resolved_by=user.username
    )
