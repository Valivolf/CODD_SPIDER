from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.repositories.task_visit_reports_repository import RESULT_STATUSES, SIM_STATES


class DemontageScopeIn(BaseModel):
    mode: str = "whole"                 # whole — весь комплекс, parts — отдельные элементы
    ids: list[int] = []                 # единицы оборудования (при parts)
    other: str = ""                     # что ещё снято, чего нет в учёте


class TaskVisitReportIn(BaseModel):
    # visit_type is NOT part of this payload — it's fixed on the task
    # itself (tasks.type, set when the task was scheduled) and resolved
    # server-side in routes_task_visit_reports.py, not chosen per report.
    # Пуст только у отчёта со «статусом выезда» (outcome): у не связанного с
    # ремонтом типа итог — одна из кнопок-статусов выезда этого типа
    # («Найден» / «Не найден»…), а не статус комплекса.
    result_status: str = ""
    comment: str = ""
    equipment_removed: Optional[str] = None
    equipment_installed: Optional[str] = None
    # None — не трогать (старый клиент); "" — сбросить; текст — статус выезда.
    outcome: Optional[str] = None
    # «Не работает»: что неисправно и что нужно для исправления (отдельно от отчёта). None — не трогать (старый
    # клиент); при другом итоге обе записи очищаются.
    fault: Optional[str] = None
    needs: Optional[str] = None
    # Итоговый тип выезда в отчёте: None — как раньше (тип задания; «… + Ремонт», если монтёр включал режим ремонта);
    # название типа — записать именно его, `with_repair` добавляет «+ Ремонт» (для типов, не связанных с ремонтом).
    visit_type: Optional[str] = None
    with_repair: Optional[bool] = None
    # Кто проводил работы на комплексе (человек из «Персонала»). Обязателен, когда отчёт пишет диспетчер с нуля;
    # у уже существующего отчёта пусто — автор не меняется.
    author_person_id: Optional[int] = None
    # Демонтаж из панели: весь комплекс или отдельные элементы — снятое оборудование записывается на исполнителя.
    demontage: Optional["DemontageScopeIn"] = None
    # SIM-карта — отдельно от остального оборудования: какая снята, какая поставлена и рабочая ли снятая
    # (None — не трогать, "" — очистить; состояние — working / defective).
    sim_removed: Optional[str] = None
    sim_installed: Optional[str] = None
    sim_removed_state: Optional[str] = None
    # Время отчёта (ISO, UTC) — диспетчер может поправить; None — не менять.
    created_at: Optional[str] = None
    # Чем запитан комплекс («СЭС» / «Электросеть» / ""): правится прямо из отчёта; None — не менять.
    power_source: Optional[str] = None

    @field_validator("sim_removed_state")
    @classmethod
    def sim_state_known(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in SIM_STATES:
            raise ValueError("Неизвестное состояние SIM")
        return v

    @field_validator("result_status")
    @classmethod
    def result_status_known(cls, v: str) -> str:
        if v and v not in RESULT_STATUSES:
            raise ValueError("Неизвестный статус результата")
        return v


class TaskVisitReportOut(BaseModel):
    task_id: str
    point_id: str
    visit_type: str
    result_status: str
    comment: str
    equipment_removed: Optional[str] = None
    equipment_installed: Optional[str] = None
    # «Статус выезда» из «Типов выездов» («Найден» / «Не найден»…) — только текст
    # для отчётов и статистики; у такого отчёта result_status пуст.
    outcome: str = ""
    fault: Optional[str] = None
    needs: Optional[str] = None
    sim_removed: Optional[str] = None
    sim_installed: Optional[str] = None
    sim_removed_state: str = ""
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    author_person_id: Optional[int] = None
    updated_by: Optional[str] = None
    source: str = ""
    # Запись загружена из Excel («История выездов», см. services/visit_history_import.py): у неё нет задания
    # в календаре, а время суток не известно — показывается одна дата.
    imported: bool = False
