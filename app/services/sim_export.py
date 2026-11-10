"""Выгрузка SIM-карт с IP в Excel по фильтрам.

Три режима:
  • «points»   — SIM и IP комплексов; фильтры — категории комплексов («Без категории» тоже) и статусы;
  • «free»     — SIM из реестра SIM↔IP, которые нигде не числятся: ни на одном комплексе, ни в учёте оборудования
                 (свободные номера и IP — например, чтобы поставить новый комплекс);
  • «registry» — весь реестр SIM↔IP с пометкой, где каждая SIM числится (комплекс / оборудование / нигде)."""
from __future__ import annotations

import datetime as dt
import io
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core import equipment as eq
from app.core.dates import format_datetime
from app.db.database import get_connection
from app.repositories import points_repository, sim_ip_registry_repository

SCOPES = {"points": "SIM комплексов", "free": "Свободные SIM (нигде не числятся)", "registry": "Весь реестр SIM↔IP"}
_HEAD_FILL = PatternFill("solid", fgColor="1E293B")
_HEAD_FONT = Font(color="F8FAFC", bold=True)


async def _equipment_sims() -> dict[str, list[str]]:
    """SIM из учёта оборудования: ключ SIM → где числится (описания мест)."""
    out: dict[str, list[str]] = {}
    async with get_connection() as conn:
        cur = await conn.execute(
            "select e.serial_number, e.place, e.point_id, pe.full_name "
            "from equipment e join equipment_models m on m.id = e.model_id join equipment_categories c on c.id = m.category_id "
            "left join people pe on pe.id = e.person_id where c.kind = ?",
            (eq.CATEGORY_KIND_SIM,),
        )
        for serial, place, point_id, person in await cur.fetchall():
            key = eq.sim_key(serial)
            label = eq.PLACE_LABEL.get(place, place or "")
            if place == eq.PLACE_POINT and point_id:
                label = f"Оборудование: на комплексе {point_id}"
            elif place == eq.PLACE_PERSON and person:
                label = f"Оборудование: у монтёра {person}"
            else:
                label = f"Оборудование: {label.lower()}"
            out.setdefault(key, []).append(label)
    return out


async def collect(scope: str, category_ids: Optional[list[int]] = None, uncategorized: bool = False,
                  statuses: Optional[list[str]] = None) -> tuple[list[str], list[list[Any]], list[str]]:
    """(заголовки, строки, описание фильтров для листа «Условия»)."""
    if scope not in SCOPES:
        raise ValueError("Неизвестный вид выгрузки")
    # Везде сравниваем по одному ключу (последние 7 цифр): на комплексе номер может стоять полным, в реестре — коротким
    registry = {eq.sim_key(r["sim_number"]): r for r in await sim_ip_registry_repository.list_all()}
    conditions = [f"Выгрузка: {SCOPES[scope]}", f"Когда: {dt.datetime.now().strftime('%d-%m-%Y %H:%M')}"]

    if scope == "points":
        items, _ = await points_repository.search_points("", category_ids or [], None, 0, uncategorized, statuses or [], "", "")
        rows = []
        for p in items:
            sim, ip = (p.get("sim_number") or "").strip(), (p.get("ip_address") or "").strip()
            if not sim and not ip:
                continue
            reg_ip = (registry.get(eq.sim_key(sim)) or {}).get("ip_address", "") if sim else ""
            rows.append([sim, ip, p["id"], p.get("category_name") or "—", p.get("status") or "—", p.get("address") or "",
                         p.get("inventory_number") or "", reg_ip if reg_ip and reg_ip != ip else ""])
        rows.sort(key=lambda r: (r[3], str(r[2])))
        if category_ids or uncategorized:
            conditions.append("Категории комплексов: " + ", ".join(sorted({r[3] for r in rows})) if rows else "Категории комплексов: выбранные")
        if statuses:
            conditions.append("Статусы: " + ", ".join(statuses))
        conditions.append(f"Комплексов с SIM или IP: {len(rows)}")
        return ["SIM", "IP-адрес", "Комплекс", "Категория", "Статус", "Адрес", "Инвентарный номер", "Другой IP в реестре SIM↔IP"], rows, conditions

    points = await points_repository.list_points()
    on_points: dict[str, list[str]] = {}
    for p in points:
        sim = (p.get("sim_number") or "").strip()
        if sim:
            on_points.setdefault(eq.sim_key(sim), []).append(p["id"])
    in_equipment = await _equipment_sims()

    def where(sim: str) -> list[str]:
        out = [f"Комплекс {pid}" for pid in on_points.get(sim, [])]
        out += in_equipment.get(sim, [])
        return out

    if scope == "free":
        rows = [[sim, r["ip_address"], format_datetime(r["updated_at"])] for sim, r in sorted(registry.items()) if not where(sim)]
        conditions.append("В реестре SIM↔IP, но нет ни на одном комплексе и нет в учёте оборудования")
        conditions.append(f"Свободных SIM: {len(rows)} из {len(registry)} в реестре")
        return ["SIM", "IP-адрес", "В реестре обновлено"], rows, conditions

    rows = []
    for sim, r in sorted(registry.items()):
        places = where(sim)
        rows.append([sim, r["ip_address"], "; ".join(places) if places else "нигде не числится", format_datetime(r["updated_at"])])
    conditions.append(f"Записей в реестре: {len(rows)}; не числятся нигде: {sum(1 for r in rows if r[2] == 'нигде не числится')}")
    return ["SIM", "IP-адрес", "Где числится", "В реестре обновлено"], rows, conditions


def build_workbook(headers: list[str], rows: list[list[Any]], conditions: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "SIM и IP"
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = _HEAD_FILL, _HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    widths = [12, 16, 26, 22, 20, 44, 20, 24]
    for i in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(i)].width = widths[i - 1] if i <= len(widths) else 24
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    info = wb.create_sheet("Условия")
    info.column_dimensions["A"].width = 100
    for line in conditions:
        info.append([line])
    info["A1"].font = Font(bold=True, size=13)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
