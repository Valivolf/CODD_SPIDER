"""Checks shared by the two "add several points to a бригада at once"
endpoints — /api/point-plans/assign (from plan entries) and
/api/points/assign (straight from the directory)."""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import HTTPException, status

from app.core.work_types import is_aux_type, work_type_names
from app.repositories import crew_repository


async def check_assignment_target(day: str, team: int) -> None:
    try:
        date_cls.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    crew = await crew_repository.get_crew(team)
    if not crew:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Бригада не найдена")
    if crew["date"] != day:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Бригада не относится к этой дате")


def check_work_type(work_type: str) -> None:
    if work_type not in work_type_names():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Неизвестный тип работы: {work_type or '—'}")
    if is_aux_type(work_type):
        # Вспомогательная точка — метка на карте бригады, а не комплекс из справочника «Все АПК».
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "«Вспомогательная точка» добавляется в бригаду только вручную («+ Точка» в календаре) или при импорте KML",
        )
