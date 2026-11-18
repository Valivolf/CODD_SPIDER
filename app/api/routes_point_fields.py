"""Значения дополнительных полей в карточках комплексов."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core import permissions as P
from app.core import accounting
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import point_category_fields_repository as fields_repo

router = APIRouter(
    prefix="/api/point-category-fields",
    tags=["point-category-fields"],
    dependencies=[Depends(require_password_ok)],
)


class PointFieldsIn(BaseModel):
    values: dict[str, str | None] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=0)


@router.get("/point/{point_id:path}")
async def point_fields(point_id: str, user: UserOut = Depends(require_password_ok)):
    if accounting.is_accountant(user):
        await accounting.require_point_access(user, point_id)
    fields = await fields_repo.fields_for_point(point_id)
    if not fields and not await fields_repo.point_exists(point_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комплекс не найден")
    # Existing readers of an APK may see its accounting values, not edit them.
    return [f for f in fields if f["accounting_only"] or not accounting.is_accountant(user)]


@router.put("/point/{point_id:path}")
async def save_point_fields(
    point_id: str, payload: PointFieldsIn, user: UserOut = Depends(require_password_ok)
):
    fields = await fields_repo.fields_for_point(point_id)
    if not fields and not await fields_repo.point_exists(point_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комплекс не найден")
    by_id = {str(f["id"]): f for f in fields}
    can_general = not accounting.is_accountant(user) and P.has(user.permissions, "points.edit")
    can_accounting = accounting.can_edit(user)
    values: dict[int, str] = {}
    for raw_id, raw_value in payload.values.items():
        field = by_id.get(raw_id)
        if field is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Поле не относится к категории этого комплекса")
        if field["accounting_only"] and not can_accounting:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права менять бухгалтерские поля")
        if not field["accounting_only"] and not can_general:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права менять дополнительные поля АПК")
        value = (raw_value or "").strip()
        if len(value) > 4000:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Значение поля длиннее 4000 знаков")
        if field["field_type"] == "currency" and value:
            try:
                number = Decimal(value.replace(",", "."))
                if not number.is_finite() or abs(number.adjusted()) > 4000:
                    raise InvalidOperation
                value = format(number, "f")
                if len(value) > 4000:
                    raise InvalidOperation
            except InvalidOperation as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Поле «{field['name']}» должно быть конечной десятичной суммой до 4000 знаков") from exc
        if field["field_type"] == "number" and value:
            try:
                number = float(value.replace(",", "."))
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Поле «{field['name']}» должно быть числом") from exc
            if not (number == number and abs(number) != float("inf")):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Поле «{field['name']}» должно быть конечным числом")
            value = str(number)
        if field["field_type"] == "date" and value:
            try:
                import datetime as dt

                dt.date.fromisoformat(value)
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Поле «{field['name']}» должно содержать дату") from exc
        values[field["id"]] = value
    try:
        await fields_repo.save_values(point_id, values, expected_revision=payload.expected_revision,
                                     allow_accounting=can_accounting, allow_general=can_general)
    except fields_repo.AccountingConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return await point_fields(point_id, user)
