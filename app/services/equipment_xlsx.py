"""Excel-выгрузки учёта оборудования (openpyxl, как у остальных выгрузок приложения)."""
from __future__ import annotations

import io
from typing import Any, Iterable, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.core import equipment as eq

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

ITEM_FIELDS: dict[str, tuple[str, Any]] = {
    "serial_number": ("Серийный номер", lambda r: r["serial_number"]),
    "category": ("Категория", lambda r: r["category_name"]),
    "model": ("Модель", lambda r: r["model_name"]),
    "status": ("Состояние", lambda r: r["status"] or ""),
    "place": ("Где", lambda r: r["place_label"]),
    "point": ("Комплекс", lambda r: r["point_id"] or ""),
    "point_inventory": ("Инв. номер комплекса", lambda r: r["point_inventory"] or ""),
    "person": ("Монтёр (числится)", lambda r: r["person_name"] or ""),
    "held_reason": ("Почему у монтёра", lambda r: eq.HELD_LABEL.get(r["held_reason"] or "", "")),
    "received_date": ("Дата приёмки", lambda r: r["received_date"] or ""),
    "issued_date": ("Дата выдачи", lambda r: r["issued_date"] or ""),
    "installed_date": ("Дата установки", lambda r: r["installed_date"] or ""),
    "installed_by": ("Установил", lambda r: r["installed_by"] or ""),
    "removed_date": ("Дата снятия", lambda r: r["removed_date"] or ""),
    "removed_by": ("Снял", lambda r: r["removed_by"] or ""),
    "removed_point": ("Снято с комплекса", lambda r: r["removed_point_id"] or ""),
    "removal_reason": ("Причина снятия", lambda r: r["removal_reason"] or ""),
    "accepted_date": ("Принято на склад", lambda r: r["accepted_date"] or ""),
    "accepted_by": ("Принял", lambda r: r["accepted_by"] or ""),
    "notes": ("Примечание", lambda r: r["notes"] or ""),
}
DEFAULT_ITEM_FIELDS = [
    "serial_number", "category", "model", "status", "place", "point", "person", "received_date",
    "installed_date", "removed_by", "removed_point", "removal_reason", "accepted_by", "notes",
]


def _tint(hex_color: str, amount: float = 0.78) -> str:
    h = (hex_color or "#64748b").lstrip("#")
    if len(h) != 6:
        h = "64748b"
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: int(c + (255 - c) * amount)  # noqa: E731
    return f"{mix(r):02X}{mix(g):02X}{mix(b):02X}"


def _write_sheet(ws, headers: list[str], rows: list[list[Any]], status_col: Optional[int] = None, status_colors: Optional[list[str]] = None) -> None:
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        cell.border = _BORDER
    ws.freeze_panes = "A2"
    widths = [len(str(h)) + 2 for h in headers]
    for r_idx, row in enumerate(rows, start=2):
        ws.append(row)
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)) + 2 if val is not None else widths[i])
            ws.cell(row=r_idx, column=i + 1).border = _BORDER
        if status_col is not None and status_colors and r_idx - 2 < len(status_colors):
            cell = ws.cell(row=r_idx, column=status_col + 1)
            cell.fill = PatternFill(start_color=_tint(status_colors[r_idx - 2]), end_color=_tint(status_colors[r_idx - 2]), fill_type="solid")
            cell.font = Font(bold=True)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(w, 10), 50)


def _save(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _new() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def items_workbook(
    items: list[dict[str, Any]], fields: Optional[Iterable[str]] = None, extra_fields: Optional[list[dict[str, Any]]] = None,
) -> bytes:
    """`extra_fields` — дополнительные поля (справочник), которые попадают отдельными столбцами в конец таблицы;
    у единиц должен быть заполнен `extra` (repo.list_items(with_extra=True))."""
    keys = [f for f in (fields or DEFAULT_ITEM_FIELDS) if f in ITEM_FIELDS] or DEFAULT_ITEM_FIELDS
    extra_fields = extra_fields or []
    headers = ["№"] + [ITEM_FIELDS[k][0] for k in keys] + [f["name"] for f in extra_fields]

    def extras(r: dict[str, Any]) -> list[str]:
        by_id = {e["field_id"]: e["value"] for e in r.get("extra") or []}
        return [by_id.get(f["id"], "") for f in extra_fields]

    rows = [[i] + [ITEM_FIELDS[k][1](r) for k in keys] + extras(r) for i, r in enumerate(items, start=1)]
    wb = _new()
    _write_sheet(
        wb.create_sheet("Оборудование"), headers, rows,
        status_col=1 + keys.index("status") if "status" in keys else None, status_colors=[r["status_color"] or "" for r in items],
    )
    return _save(wb)


def summary_workbook(summary: dict[str, Any], items: list[dict[str, Any]]) -> bytes:
    cols = summary["columns"]
    headers = ["Категория"] + [c["label"] for c in cols] + ["Итого"]
    rows = [[r["category"]] + [r["counts"].get(c["key"], 0) for c in cols] + [r["total"]] for r in summary["rows"]]
    rows.append(["Итого"] + [summary["totals"].get(c["key"], 0) for c in cols] + [summary["total"]])
    wb = _new()
    _write_sheet(wb.create_sheet("Сводка по категориям"), headers, rows)
    keys = DEFAULT_ITEM_FIELDS
    _write_sheet(
        wb.create_sheet("Оборудование"), ["Серийный номер" if k == "serial_number" else ITEM_FIELDS[k][0] for k in keys],
        [[ITEM_FIELDS[k][1](r) for k in keys] for r in items], status_col=keys.index("status"),
        status_colors=[r["status_color"] or "" for r in items],
    )
    return _save(wb)


def people_workbook(groups: list[dict[str, Any]]) -> bytes:
    wb = _new()
    _write_sheet(
        wb.create_sheet("Сводка по монтёрам"), ["Монтёр", "Числится единиц", "Из них ждёт приёмки (снято)"],
        [[g["full_name"], g["count"], g["awaiting"]] for g in groups],
    )
    ws = wb.create_sheet("Оборудование по монтёрам")
    row_idx, grand = 1, 0
    widths = [18, 22, 20, 28, 16, 28]
    head = ["Серийный номер", "Модель", "Состояние", "Почему числится", "Дата", "Комплекс / примечание"]
    for g in groups:
        cell = ws.cell(row=row_idx, column=1, value=f"Монтёр: {g['full_name']}  —  всего {g['count']} шт.")
        cell.font = Font(bold=True, size=12, color="FFFFFF")
        cell.fill = _HEADER_FILL
        ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(head))
        row_idx += 1
        by_cat: dict[str, list[dict[str, Any]]] = {}
        for it in g["items"]:
            by_cat.setdefault(it["category_name"], []).append(it)
        for cat in sorted(by_cat, key=str.casefold):
            cell = ws.cell(row=row_idx, column=1, value=f"{cat} — {len(by_cat[cat])} шт.")
            cell.font = Font(bold=True, color="3730A3")
            cell.fill = PatternFill(start_color="EEF2FF", end_color="EEF2FF", fill_type="solid")
            ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(head))
            row_idx += 1
            for c, h in enumerate(head, start=1):
                hc = ws.cell(row=row_idx, column=c, value=h)
                hc.font = Font(bold=True, size=9, color="6B7280")
                hc.border = _BORDER
            row_idx += 1
            for it in by_cat[cat]:
                values = [
                    it["serial_number"], it["model_name"], it["status"] or "", eq.HELD_LABEL.get(it["held_reason"] or "", ""),
                    (it["removed_date"] if it["held_reason"] in eq.HELD_FROM_POINT else it["issued_date"]) or "",
                    "; ".join(p for p in (
                        f"снято с {it['removed_point_id']}" if it["removed_point_id"] and it["held_reason"] in eq.HELD_FROM_POINT else "",
                        f"причина: {it['removal_reason']}" if it.get("removal_reason") and it["held_reason"] in eq.HELD_FROM_POINT else "",
                    ) if p) or it["notes"] or "",
                ]
                for c, v in enumerate(values, start=1):
                    ws.cell(row=row_idx, column=c, value=v).border = _BORDER
                    widths[c - 1] = max(widths[c - 1], len(str(v)) + 2)
                row_idx += 1
        row_idx += 1
        grand += g["count"]
    ws.cell(row=row_idx, column=1, value=f"ИТОГО: {grand} шт.").font = Font(bold=True, size=12)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = min(max(w, 14), 50)
    return _save(wb)


def history_workbook(rows: list[dict[str, Any]]) -> bytes:
    headers = ["Дата", "Операция", "Серийный номер", "Модель", "Комплекс", "Монтёр", "Выполнил", "Комментарий"]
    data = [
        [
            r["event_date"] or (r["ts"] or "")[:10], r["action_type"], r["serial_number"] or "", r["model_name"] or "",
            r["point_id"] or "", r["person_name"] or "", r["performed_by"] or "", r["comment"] or "",
        ]
        for r in rows
    ]
    wb = _new()
    _write_sheet(wb.create_sheet("История действий"), headers, data)
    return _save(wb)


def points_workbook(points: list[dict[str, Any]], items_by_point: dict[str, list[dict[str, Any]]]) -> bytes:
    wb = _new()
    _write_sheet(
        wb.create_sheet("Комплексы"), ["Комплекс", "Инв. номер", "Адрес", "Категория", "Панель", "Единиц оборудования"],
        [[p["id"], p["inventory_number"], p["address"], p["category_name"], p["panel_number"], p["equipment_count"]] for p in points],
    )
    rows = [
        [
            p["id"], p["inventory_number"], it["serial_number"], it["category_name"], it["model_name"], it["status"] or "",
            it["installed_date"] or "", it["installed_by"] or "", it["notes"] or "",
        ]
        for p in points for it in items_by_point.get(p["id"], [])
    ]
    _write_sheet(
        wb.create_sheet("Оборудование по комплексам"),
        ["Комплекс", "Инв. номер", "Серийный номер", "Категория", "Модель", "Состояние", "Дата установки", "Установил", "Примечание"],
        rows,
    )
    return _save(wb)


def panels_workbook(panels: list[dict[str, Any]]) -> bytes:
    wb = _new()
    _write_sheet(
        wb.create_sheet("Солнечные панели"), ["Номер панели", "Комплекс", "Местонахождение", "На складе", "Примечание"],
        [[p["panel_number"], p["point_id"] or "", p["location"] or "", "да" if p["at_warehouse"] else "нет", p["notes"] or ""] for p in panels],
    )
    return _save(wb)
