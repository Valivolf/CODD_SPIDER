"""Session-authed side of the "no access after SIM update, fitter called the
dispatcher" queue — the bot-key side that creates these lives in
routes_bot.py. See access_issue_requests_repository.py for the shape.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.core.point_credentials import strip_credentials
from app.models.user import UserOut
from app.repositories import access_issue_requests_repository

router = APIRouter(prefix="/api/access-issues", tags=["access-issues"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def list_access_issues(
    status_filter: str | None = Query(default=None, alias="status"), limit: int = 20, user: UserOut = Depends(require_password_ok)
):
    if status_filter == "pending":
        return strip_credentials(await access_issue_requests_repository.list_pending(), user)
    return await access_issue_requests_repository.list_recent(limit)


class ResolveIn(BaseModel):
    decision: str  # 'released' | 'demontage'
    demontage_note: str | None = None


@router.post("/{request_id}/resolve")
async def resolve_access_issue(request_id: int, payload: ResolveIn, user: UserOut = Depends(require_password_ok)):
    if payload.decision not in ("released", "demontage"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестное решение")
    existing = await access_issue_requests_repository.get(request_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    if existing["status"] != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Запрос уже обработан")
    if payload.decision == "demontage" and not (payload.demontage_note or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Укажите, что демонтировать")
    return await access_issue_requests_repository.resolve(
        request_id, decision=payload.decision, demontage_note=payload.demontage_note, resolved_by=user.username
    )
