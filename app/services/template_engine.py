"""Fills operator-authored .docx/.xlsx report templates with app data,
keeping the uploaded file's own formatting — the operator designs the
document in Word/Excel however they like and writes {{variable}}
placeholders where data should go (see
app/models/report_fields.py::template_variable_catalog for the available
keys). Two kinds of placeholder:

  - a plain {{key}} anywhere in the document is replaced with a single
    scalar value from the context.
  - a table row containing any {{row.<field>}} placeholder is treated as
    a template row: it's cloned once per record (one record per
    date+crew in the chosen period — see reports_repository.
    build_template_context), each clone filled from its own record. Only
    the first such row found in the document is treated this way — a
    template with more than one repeating table isn't supported, and any
    {{row.*}} placeholder outside the first one is left blank and
    reported as "missing". A merged cell inside the template row also
    isn't preserved across the generated copies (an openpyxl/python-docx
    limitation, not something this module works around).

Word in particular loves to split a single piece of visible text across
several XML <w:r> "runs" (spell-check, autocorrect, prior edits), so a
placeholder can straddle run boundaries invisibly. The standard
workaround — used throughout this module — is to merge a paragraph's run
texts, do the substitution on the merged string, and write the whole
result back into the first run while blanking the rest; the first run's
own formatting (font, bold, color, ...) then applies to the substituted
text. This loses only the formatting of any *other* run that used to
share the paragraph with the placeholder, which is the standard
trade-off every docx mail-merge script makes.
"""
from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl
from docx import Document
from docx.table import Table

from app.models.report_fields import TEMPLATE_PERIOD_VARIABLES

VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
ROW_PREFIX = "row."


@dataclass
class RenderResult:
    content: bytes
    missing_variables: list[str] = field(default_factory=list)


class _Substituter:
    """Callable regex replacement that also records any placeholder key
    with no matching entry in the context, so the caller can warn the
    operator their template references a variable that doesn't exist
    (usually a typo)."""

    def __init__(self, context: dict[str, Any]):
        self.context = context
        self.missing: set[str] = set()

    def __call__(self, match: re.Match) -> str:
        key = match.group(1)
        if key not in self.context:
            self.missing.add(key)
            return ""
        value = self.context[key]
        return "" if value is None else str(value)


def _paragraph_text(paragraph) -> str:
    return "".join(run.text for run in paragraph.runs)


def _substitute_paragraph(paragraph, sub: _Substituter) -> None:
    text = _paragraph_text(paragraph)
    if "{{" not in text:
        return
    new_text = VARIABLE_PATTERN.sub(sub, text)
    if not paragraph.runs:
        paragraph.add_run(new_text)
        return
    paragraph.runs[0].text = new_text
    for run in paragraph.runs[1:]:
        run.text = ""


def _iter_paragraphs(container) -> list:
    """All paragraphs directly in this container (document/cell/header/
    footer) plus recursively inside any tables it contains — but NOT
    inside a nested container's own tables-within-tables beyond one level
    deep, which covers every template this app is likely to see."""
    paragraphs = list(container.paragraphs)
    tables = getattr(container, "tables", [])
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    return paragraphs


def extract_docx_variables(content: bytes) -> list[str]:
    doc = Document(io.BytesIO(content))
    keys: set[str] = set()
    containers = [doc]
    for section in doc.sections:
        containers.append(section.header)
        containers.append(section.footer)
    for container in containers:
        for paragraph in _iter_paragraphs(container):
            keys.update(VARIABLE_PATTERN.findall(_paragraph_text(paragraph)))
    return sorted(keys)


def extract_xlsx_variables(content: bytes) -> list[str]:
    wb = openpyxl.load_workbook(io.BytesIO(content))
    keys: set[str] = set()
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    keys.update(VARIABLE_PATTERN.findall(cell.value))
    return sorted(keys)


def extract_variables(file_type: str, content: bytes) -> list[str]:
    if file_type == "docx":
        return extract_docx_variables(content)
    if file_type == "xlsx":
        return extract_xlsx_variables(content)
    raise ValueError(f"Неподдерживаемый тип файла: {file_type}")


def classify_mode(variables: list[str]) -> str:
    """'period' if the template uses a repeating row or any period-only
    variable (a range narrower than a single day wouldn't make sense);
    'day' otherwise — a template built only from single-day fields is
    meant to be filled for exactly one date."""
    period_only_keys = {v["key"] for v in TEMPLATE_PERIOD_VARIABLES if v["key"] != "date"}
    if any(v.startswith(ROW_PREFIX) for v in variables):
        return "period"
    if period_only_keys & set(variables):
        return "period"
    return "day"


def _row_context(base: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    ctx = dict(base)
    for key, value in record.items():
        ctx[f"{ROW_PREFIX}{key}"] = value
    return ctx


# ---------------------------------------------------------------- DOCX ----

def _find_template_row(doc: Document) -> tuple[Table, int] | None:
    for table in doc.tables:
        for idx, row in enumerate(table.rows):
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    if ROW_PREFIX in _paragraph_text(paragraph):
                        return table, idx
    return None


def render_docx(content: bytes, context: dict[str, Any], row_records: list[dict[str, Any]]) -> RenderResult:
    doc = Document(io.BytesIO(content))
    sub = _Substituter(context)

    template_row_info = _find_template_row(doc)
    if template_row_info is not None:
        table, row_idx = template_row_info
        template_tr = table.rows[row_idx]._tr
        records = row_records or [{}]
        anchor = template_tr
        for record in records:
            clone = copy.deepcopy(template_tr)
            anchor.addnext(clone)
            anchor = clone
            row_sub = _Substituter(_row_context(context, record))
            clone_row = _tr_to_row(table, clone)
            for cell in clone_row.cells:
                for paragraph in cell.paragraphs:
                    _substitute_paragraph(paragraph, row_sub)
            sub.missing |= {k for k in row_sub.missing if not k.startswith(ROW_PREFIX)}
        template_tr.getparent().remove(template_tr)

    containers = [doc]
    for section in doc.sections:
        containers.append(section.header)
        containers.append(section.footer)
    for container in containers:
        for paragraph in _iter_paragraphs(container):
            _substitute_paragraph(paragraph, sub)

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return RenderResult(content=out.getvalue(), missing_variables=sorted(sub.missing))


def _tr_to_row(table: Table, tr_element):
    """python-docx has no public "wrap this <w:tr> element in a Row"
    constructor — Table._cells/rows resolve by position in table.tbl, not
    by object identity, so a freshly inserted clone isn't reachable via
    table.rows until python-docx re-walks the XML, which it does on every
    property access. This just does exactly that lookup for one element."""
    from docx.table import _Row

    return _Row(tr_element, table)


# ---------------------------------------------------------------- XLSX ----

_STYLE_ATTRS = ("font", "fill", "border", "alignment", "number_format", "protection")


def _copy_cell_style(src_cell, dst_cell) -> None:
    # openpyxl exposes cell.font/.fill/... as a StyleProxy, not the
    # underlying (hashable, poolable) style object — copy.copy() on it is
    # openpyxl's own documented way to detach a real, assignable copy.
    for attr in _STYLE_ATTRS:
        setattr(dst_cell, attr, copy.copy(getattr(src_cell, attr)))


def _find_template_row_xlsx(ws) -> int | None:
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and ROW_PREFIX in cell.value:
                return row[0].row
    return None


def render_xlsx(content: bytes, context: dict[str, Any], row_records: list[dict[str, Any]]) -> RenderResult:
    wb = openpyxl.load_workbook(io.BytesIO(content))
    sub = _Substituter(context)
    missing: set[str] = set()

    for ws in wb.worksheets:
        template_row_idx = _find_template_row_xlsx(ws)
        if template_row_idx is not None:
            max_col = ws.max_column
            template_values = [ws.cell(row=template_row_idx, column=c).value for c in range(1, max_col + 1)]
            template_styles = [ws.cell(row=template_row_idx, column=c) for c in range(1, max_col + 1)]
            row_height = ws.row_dimensions[template_row_idx].height

            records = row_records or [{}]
            if len(records) > 1:
                ws.insert_rows(template_row_idx + 1, amount=len(records) - 1)

            for offset, record in enumerate(records):
                r = template_row_idx + offset
                row_sub = _Substituter(_row_context(context, record))
                ws.row_dimensions[r].height = row_height
                for c in range(1, max_col + 1):
                    cell = ws.cell(row=r, column=c)
                    _copy_cell_style(template_styles[c - 1], cell)
                    value = template_values[c - 1]
                    if isinstance(value, str) and "{{" in value:
                        cell.value = VARIABLE_PATTERN.sub(row_sub, value)
                    elif offset > 0:
                        cell.value = value
                missing |= {k for k in row_sub.missing if not k.startswith(ROW_PREFIX)}

        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and "{{" in cell.value:
                    cell.value = VARIABLE_PATTERN.sub(sub, cell.value)

    missing |= sub.missing
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return RenderResult(content=out.getvalue(), missing_variables=sorted(missing))


def render(file_type: str, content: bytes, context: dict[str, Any], row_records: list[dict[str, Any]]) -> RenderResult:
    if file_type == "docx":
        return render_docx(content, context, row_records)
    if file_type == "xlsx":
        return render_xlsx(content, context, row_records)
    raise ValueError(f"Неподдерживаемый тип файла: {file_type}")
