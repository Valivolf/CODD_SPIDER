"""Поступление, выдача, установка и возврат расходных материалов."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.deps import require_password_ok, require_warehouse, require_warehouse_edit
from app.models.user import UserOut
from app.services import consumables as svc

router = APIRouter(
    prefix="/api/consumables",
    tags=["consumables"],
    dependencies=[Depends(require_password_ok), Depends(require_warehouse)],
)


def _fail(exc: svc.ConsumablesError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


class CreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(default="шт.", max_length=24)
    quantity: int = Field(default=0, ge=0)


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=24)
    active: bool = True


class MovementIn(BaseModel):
    consumable_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    person_id: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


class ReceiveIn(BaseModel):
    quantity: int = Field(gt=0)
    note: str = Field(default="", max_length=500)


@router.get("")
async def list_items():
    return await svc.list_items()


@router.get("/history")
async def list_history(limit: int = Query(500, ge=1, le=2000)):
    return await svc.list_movements(limit)


@router.get("/people/{person_id}")
async def person_balances(person_id: int):
    return await svc.person_balances(person_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_item(payload: CreateIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.create_item(payload.name, payload.unit, payload.quantity, user.username)
    except svc.ConsumablesError as exc:
        raise _fail(exc) from exc


@router.put("/{item_id}")
async def update_item(item_id: int, payload: ItemIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.update_item(item_id, payload.name, payload.unit, payload.active)
    except svc.ConsumablesError as exc:
        raise _fail(exc) from exc


@router.post("/{item_id}/receive")
async def receive(item_id: int, payload: ReceiveIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.receive(item_id, payload.quantity, user.username, payload.note)
    except svc.ConsumablesError as exc:
        raise _fail(exc) from exc


@router.post("/issue")
async def issue(payload: MovementIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.issue(
            payload.consumable_id, payload.person_id, payload.quantity, user.username, payload.note
        )
    except svc.ConsumablesError as exc:
        raise _fail(exc) from exc


@router.post("/return")
async def return_to_stock(payload: MovementIn, user: UserOut = Depends(require_warehouse_edit)):
    try:
        return await svc.return_to_stock(
            payload.consumable_id, payload.person_id, payload.quantity, user.username, payload.note
        )
    except svc.ConsumablesError as exc:
        raise _fail(exc) from exc
