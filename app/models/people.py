from __future__ import annotations

from datetime import date as _date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# warehouse — «склад»: ответственный за склад; в боте принимает снятое оборудование, выдаёт и ставит статусы.
RoleType = Literal["fitter", "driver", "operator", "warehouse"]
# Вид водителя: водитель вышки / водитель прикрытия (у остальных ролей всегда пусто).
DriverKind = Literal["", "tower", "cover"]


class PersonOut(BaseModel):
    id: int
    full_name: str
    phone: str | None = None
    role_type: RoleType
    # VK user id — links this person to the монтёр bot's /api/bot/me lookup
    # (see routes_bot.py); unset means they can't use the bot yet.
    vk_id: int | None = None
    active: bool
    notes: str | None = None
    created_at: str
    driver_kind: str = ""


class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    role_type: RoleType
    vk_id: int | None = None
    notes: str | None = None
    driver_kind: DriverKind = ""

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Укажите ФИО")
        return v


class PersonUpdate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone: str | None = None
    role_type: RoleType
    vk_id: int | None = None
    active: bool = True
    notes: str | None = None
    driver_kind: DriverKind = ""

    @field_validator("full_name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Укажите ФИО")
        return v


class TeamAssignmentsPut(BaseModel):
    # team_id (as string, since JSON object keys are strings) -> person ids
    assignments: dict[str, list[int]]


class AbsenceOut(BaseModel):
    id: int
    person_id: int
    start_date: str
    end_date: str
    reason: str | None = None
    created_at: str


class AbsenceCreate(BaseModel):
    start_date: str
    end_date: str
    reason: str | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError("Неверная дата")
        return v

    @field_validator("end_date")
    @classmethod
    def end_not_before_start(cls, v: str, info) -> str:
        start = info.data.get("start_date")
        if start and v < start:
            raise ValueError("Дата окончания раньше даты начала")
        return v
