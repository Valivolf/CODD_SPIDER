"""Session-authed side of the operator/admin verification-request queue —
the bot-key side that creates these (and polls for a verdict) lives in
routes_bot.py. See verification_requests_repository.py for the shape.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.core.point_credentials import strip_credentials
from app.models.user import UserOut
from app.repositories import verification_requests_repository

router = APIRouter(prefix="/api/verification-requests", tags=["verification-requests"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def list_verification_requests(
    status_filter: str | None = Query(default=None, alias="status"), limit: int = 20, user: UserOut = Depends(require_password_ok)
):
    if status_filter == "pending":
        # с логином/паролем комплекса (только admin/user) — кнопка «Веб-интерфейс» копирует пароль для входа
        return strip_credentials(await verification_requests_repository.list_pending(), user)
    return await verification_requests_repository.list_recent(limit)


class ResolveIn(BaseModel):
    ok: bool
    issue_comment: str | None = None
    # «Работает, но с замечанием — отпустить»: ok=true + remark=true, замечание обязательно.
    remark: bool = False


@router.post("/{request_id}/resolve")
async def resolve_verification_request(request_id: int, payload: ResolveIn, user: UserOut = Depends(require_password_ok)):
    existing = await verification_requests_repository.get(request_id)
    if not existing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден")
    if existing["status"] != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Запрос уже обработан")
    if payload.remark and not payload.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Замечание бывает только у работающего комплекса")
    if payload.remark and not (payload.issue_comment or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Напишите замечание")
    return await verification_requests_repository.resolve(
        request_id, ok=payload.ok, issue_comment=payload.issue_comment, resolved_by=user.username, remark=payload.remark
    )


@router.delete("/{request_id}")
async def delete_verification_request(request_id: int):
    await verification_requests_repository.delete(request_id)
    return {"ok": True}
