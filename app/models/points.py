from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.core.power_source import normalize_power_source


class PointIn(BaseModel):
    id: str
    address: str = ""
    description: str = ""
    lat: str = ""
    lon: str = ""
    inventory_number: str = ""
    sim_number: str = ""
    ip_address: str = ""
    port: str = ""
    notes: str = ""
    status: str = ""
    # Free-form category ("АПК") — get-or-create by name, see
    # point_categories_repository.get_or_create. Blank name means "no
    # category"; an unrecognized existing name creates a new category
    # with category_color (or the default colour if blank).
    category_name: str = ""
    category_color: str = ""
    # Web-interface credentials — filled and visible only to admin/user
    # (see routes_points.py::_strip_credentials, which blanks these out of
    # every response for any other role, including the bot).
    login: str = ""
    password: str = ""
    # Metrological verification ("поверка") expiry — free "YYYY-MM-DD" text,
    # same convention as tasks.date; blank means "no data", not "expired".
    verification_valid_until: str = ""
    # «СЭС» / «Электросеть» / "" (не указано). None — поле не прислали, при
    # сохранении остаётся прежнее значение (а не затирается).
    power_source: Optional[str] = None
    # Неисправность из отчётов выездов (раздел карточки «Неисправность / описание»): почему не работает и что нужно для восстановления.
    # None — не прислали, при сохранении остаётся прежнее. fault_at/fault_by — время и автор отчёта-источника.
    fault_reason: Optional[str] = None
    fault_need: Optional[str] = None
    fault_at: Optional[str] = None
    fault_by: Optional[str] = None

    @field_validator("power_source")
    @classmethod
    def power_source_known(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = normalize_power_source(v)
        if normalized is None:
            raise ValueError("Питание: СЭС или Электросеть")
        return normalized

    @field_validator("id")
    @classmethod
    def id_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Укажите номер точки")
        return v


class PointOut(PointIn):
    power_source: str = ""
    fault_reason: str = ""
    fault_need: str = ""
    fault_at: str = ""
    fault_by: str = ""
    last_visit: Optional[dict] = None          # только в постраничном списке «Все АПК»: последний выезд по отчётам (день, вид, итог, что нужно)
    category_id: Optional[int] = None
    # Mirrors the point's category's own point_categories.is_legacy — lets
    # the client decide how "Зайти на веб-интерфейс" should connect (RDP
    # for an old complex, browser with embedded credentials for a new one)
    # without a separate category lookup.
    category_is_legacy: bool = False
    # «На складе», но не все компоненты комплекса на складе — интерфейс
    # подписывает такой комплекс «Частично на складе» (это подпись, не статус).
    stock_partial: bool = False
    # У «Частично на складе»: названия компонентов, что лежат на складе (stock_in) и что нет (stock_out) — для карточки комплекса.
    stock_in: list[str] = []
    stock_out: list[str] = []
    # Only populated by search_points (the "Все АПК" page) — when the
    # point's port (or plain ping, if it has none) last started failing,
    # for "down for N days" display/sort. None on the plain
    # list_points()/get_point() path, and while currently up.
    port_down_since: Optional[str] = None
    # Когда точку в последний раз проверяла автоматическая/ручная проверка доступности (только в search_points).
    health_checked_at: Optional[str] = None
    # Только при выборке по подгруппе АПК (GET /api/points?group_id=…): описание, «что нужно на выезде» и тип работы ЭТОГО комплекса в подгруппе —
    # свои у каждой подгруппы, не связаны с описанием в карточке комплекса.
    group_note: Optional[str] = None
    group_need: Optional[str] = None
    group_work_type: Optional[str] = None
    group_added_at: Optional[str] = None
