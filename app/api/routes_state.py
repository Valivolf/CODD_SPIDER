from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.deps import require_password_ok
from app.models.calendar import StatePayload
from app.models.user import UserOut
from app.repositories import state_repository

router = APIRouter(prefix="/api/state", tags=["state"], dependencies=[Depends(require_password_ok)])


@router.get("")
async def get_state(since_rev: int | None = None):
    # The calendar polls this every 5s (app.js's `load()`) — if the caller
    # already has the current revision, skip the tasks query/serialization
    # entirely and say so cheaply, instead of re-sending the whole task
    # list every poll whether or not anything changed.
    if since_rev is not None:
        rev = await state_repository.read_rev()
        if rev == since_rev:
            return {"rev": rev, "unchanged": True}
    rev, data = await state_repository.read_state()
    return {"rev": rev, "data": data}


@router.put("")
async def put_state(payload: StatePayload, user: UserOut = Depends(require_password_ok)):
    try:
        new_rev = await state_repository.write_state(payload.rev, payload.data)
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Конфликт изменений")
    return {"rev": new_rev}
