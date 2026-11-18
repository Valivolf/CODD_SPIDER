"""Export only the safe accounting projection; no technical point data."""
from __future__ import annotations

import datetime as dt
import io
import re
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def _text(cell, value):
    # Explicit strings preserve leading zeros and prevent Excel formula execution.
    cell.value = ILLEGAL_CHARACTERS_RE.sub("", str(value if value is not None else ""))
    cell.data_type = "s"
    cell.number_format = "@"


def _field(cell, field):
    value = str(field.get("value") or "")
    _text(cell, value)
    if field["name"] == "Год выпуска" and re.fullmatch(r"\d{1,4}(?:[.,]0+)?", value):
        cell.value = int(Decimal(value.replace(",", ".")))
        cell.number_format = "0"
    elif field["field_type"] == "date" and value:
        try:
            cell.value = dt.date.fromisoformat(value)
            cell.number_format = "DD.MM.YYYY"
        except ValueError:
            pass  # Preserve legacy invalid dates as text.
    elif field["field_type"] == "currency" and value:
        try:
            number = Decimal(value.replace(",", "."))
            # Excel has only 15 significant digits. Longer/more precise values stay text.
            if number.is_finite() and len(number.as_tuple().digits) <= 15 and -2 <= number.as_tuple().exponent <= 0:
                cell.value = number
                cell.number_format = '#,##0.00'
        except InvalidOperation:
            pass


def export_accounting_xlsx(items: list[dict]) -> bytes:
    fields = {}
    for item in items:
        for field in item["accounting_fields"]:
            if field["name"].lower().replace("ё", "е") == "наименование в бухгалтерском учете":
                continue
            fields.setdefault(field["id"], field)
    wb = Workbook()
    ws = wb.active
    ws.title = "Бухгалтерия АПК"
    headers = ["Наименование в бухгалтерском учете", "Адрес", "Широта", "Долгота", *[f["name"] for f in fields.values()]]
    for col, title in enumerate(headers, 1):
        cell = ws.cell(1, col)
        _text(cell, title)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="334155")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = 48 if title in ("Адрес", "Ремонтные работы") else 28
    ws.row_dimensions[1].height = 42
    for row, item in enumerate(items, 2):
        for col, key in enumerate(("accounting_name", "address", "lat", "lon"), 1):
            _text(ws.cell(row, col), item.get(key))
        by_id = {f["id"]: f for f in item["accounting_fields"]}
        for col, fid in enumerate(fields, 5):
            if fid in by_id:
                _field(ws.cell(row, col), by_id[fid])
        for cell in ws[row]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.zoomScale = 85
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    return output.getvalue()
