from __future__ import annotations

from pydantic import BaseModel, Field


class PersonBrief(BaseModel):
    id: int
    full_name: str
    phone: str | None = None
    role_type: str
    driver_kind: str = ""


class CrewOut(BaseModel):
    id: int
    date: str
    name: str
    equip: str
    map_link: str = ""
    place: str = ""
    # «Подрядчик» в составе: отметка на бригаде (не человек из персонала); в план-задание не печатается.
    contractor: bool = False
    contractor_note: str = ""
    members: list[PersonBrief]
    trip_id: str | None = None
    trip_from: str | None = None
    trip_to: str | None = None
    trip_crew_ids: list[int] | None = None


class CrewCreate(BaseModel):
    date: str
    name: str = ""
    equip: str = "Лестница"
    map_link: str = ""
    place: str = ""
    person_ids: list[int] = []
    contractor: bool = False
    contractor_note: str = ""


class CrewUpdate(BaseModel):
    name: str | None = None
    equip: str | None = None
    map_link: str | None = None
    place: str | None = None
    person_ids: list[int] | None = None
    contractor: bool | None = None
    contractor_note: str | None = None


class CrewTripIn(BaseModel):
    date_to: str


class TasksMoveIn(BaseModel):
    """Перенос выбранных заданий (точек) в другую бригаду: либо в существующую бригаду (crew_id — день берётся у неё), либо в новую бригаду на день `date`."""
    task_ids: list[str] = Field(min_length=1, max_length=500)
    crew_id: int | None = None
    date: str | None = None
    new_crew_name: str = Field(default="", max_length=120)       # название новой бригады (пусто — как у прежней)
    copy_members: bool = False                                    # новая бригада — с тем же составом, что у прежней
    allow_duplicates: bool = False                                # согласие, если какие-то точки на этом дне уже есть
    # Вместо бригады — в «План» («Все АПК» → План): задание из бригады убирается, комплекс встаёт в план (тип работы и описание сохраняются), без дня и бригады.
    to_plan: bool = False
    plan_group: str = Field(default="", max_length=60)           # группа плана (необязательно)


class CrewMoveIn(BaseModel):
    date: str
    # Согласие перенести, даже если какие-то точки на выбранном дне уже есть (будут дубли).
    allow_duplicates: bool = False
