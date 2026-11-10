"""Отчёты из конструктора: шаблон + данные → готовый документ.

Работа в два шага, чтобы предпросмотр на странице и сами файлы всегда
совпадали:

  1. expand(design, data) — «разворачивает» шаблон: секции «для каждой
     бригады» повторяются по бригадам, строки таблиц «для каждого задания» —
     по заданиям, все {{переменные}} заменяются значениями. Результат — плоский
     список элементов (текст / таблица / отступ / разрыв страницы) с уже
     готовыми строками; его же отдаёт предпросмотр.
  2. render_xlsx / render_docx — пишут этот список в Excel или Word.

Данные собирает report_design_data.collect().
"""
from __future__ import annotations

import io
import math
import re
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.pagebreak import Break

VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

DOCX_FONT = "Times New Roman"
XLSX_FONT = "Calibri"


# ---------------------------------------------------------------------------
# 1. Разворачивание
# ---------------------------------------------------------------------------
class _Scope:
    """Значения переменных в текущей точке документа."""

    def __init__(self, context: dict[str, Any], warnings: dict[str, str], crew: dict | None = None, task: dict | None = None):
        self.context = context
        self.warnings = warnings
        self.crew = crew
        self.task = task

    def with_task(self, task: dict) -> "_Scope":
        return _Scope(self.context, self.warnings, self.crew, task)

    def _lookup(self, key: str) -> Any:
        if key.startswith("crew."):
            if self.crew is None:
                self.warnings.setdefault(key, f"{{{{{key}}}}} стоит вне секции «для каждой бригады» — будет пустым")
                return ""
            return self.crew.get(key[5:], "")
        if key.startswith("task."):
            if self.task is None:
                self.warnings.setdefault(key, f"{{{{{key}}}}} работает только в строке «для каждого задания» — будет пустым")
                return ""
            return self.task.get(key[5:], "")
        if key in self.context:
            return self.context[key]
        self.warnings.setdefault(key, f"{{{{{key}}}}} — нет такой переменной или нет значения за этот период")
        return ""

    def sub(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            value = self._lookup(m.group(1))
            return "" if value is None else str(value)

        return VARIABLE_RE.sub(repl, text or "")


def _expand_table(blk: dict, scope: _Scope, tasks: list[dict], fmt: str) -> dict:
    rows_out: list[dict] = []
    for row in blk["rows"]:
        if row.get("repeat") == "task":
            for t in tasks:
                s = scope.with_task(t)
                rows_out.append({"cells": [_expand_cell(c, s) for c in row["cells"]]})
        else:
            rows_out.append({"cells": [_expand_cell(c, scope) for c in row["cells"]]})
    out: dict[str, Any] = {"type": "table", "border": blk.get("border", True), "rows": rows_out}
    if fmt == "docx":
        out["cols"] = blk.get("cols") or []
    return out


def _expand_cell(cell: dict, scope: _Scope) -> dict:
    out = {"text": scope.sub(cell.get("text", "")), "cs": cell.get("cs", 1), "rs": cell.get("rs", 1)}
    if cell.get("style"):
        out["style"] = cell["style"]
    return out


def expand(design: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    fmt = design["format"]
    warnings: dict[str, str] = {}
    base_ctx = data["context"]
    items: list[dict[str, Any]] = []
    empty_crew_sections = 0

    for sec in design["sections"]:
        if sec["repeat"] == "crew":
            instances = [i for i in data["instances"] if i["tasks"] or not sec.get("skip_empty", True)]
            if not instances:
                empty_crew_sections += 1
        else:
            instances = [None]
        for n, inst in enumerate(instances):
            if n and fmt == "docx" and sec.get("page_break"):
                items.append({"type": "pagebreak"})
            if inst is None:
                crew = None
                tasks = [{**t, "index": t["global_index"]} for t in data["tasks"]]
            else:
                crew = inst["crew"]
                tasks = inst["tasks"]
            scope = _Scope(base_ctx, warnings, crew=crew)
            for blk in sec["blocks"]:
                if blk["type"] == "text":
                    item = {"type": "text", "text": scope.sub(blk.get("text", ""))}
                    if blk.get("style"):
                        item["style"] = blk["style"]
                    items.append(item)
                elif blk["type"] == "table":
                    items.append(_expand_table(blk, scope, tasks, fmt))
                elif blk["type"] == "spacer":
                    items.append({"type": "gap", "rows": blk.get("rows", 1)})
            last = n == len(instances) - 1
            skip_gap = fmt == "docx" and sec.get("page_break") and not last
            if sec.get("gap") and not skip_gap:
                items.append({"type": "gap", "rows": sec["gap"]})

    while items and items[-1]["type"] in ("gap", "pagebreak"):
        items.pop()
    return {
        "format": fmt,
        "page": design["page"],
        "columns": design.get("columns") or [],
        "items": items,
        "warnings": sorted(warnings.values()),
        "empty_crew_sections": empty_crew_sections,
    }


# ---------------------------------------------------------------------------
# 2a. Excel
# ---------------------------------------------------------------------------
_THIN = Side(style="thin", color="FF94A3B8")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_VA = {"top": "top", "middle": "center", "bottom": "bottom"}


def _argb(color: str) -> str:
    return "FF" + color.lstrip("#").upper()


def _xlsx_font(style: dict, default_size: int) -> Font:
    kwargs: dict[str, Any] = {
        "name": XLSX_FONT,
        "size": style.get("sz") or default_size,
        "bold": bool(style.get("b")),
        "italic": bool(style.get("i")),
        "underline": "single" if style.get("u") else None,
    }
    if style.get("fg"):
        kwargs["color"] = _argb(style["fg"])
    return Font(**kwargs)


def _est_lines(text: str, width_chars: float) -> int:
    width = max(width_chars, 1.0)
    return sum(max(1, math.ceil(len(line) / width)) for line in (text or "").split("\n"))


def render_xlsx(exp: dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт"
    widths = exp.get("columns") or [14] * 5
    ncols = len(widths)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    default_size = exp["page"].get("font_size", 11)
    r = 1

    def span_width(c1: int, cs: int) -> float:
        return sum(widths[c1 - 1:c1 - 1 + cs]) or 10

    for item in exp["items"]:
        kind = item["type"]
        if kind == "gap":
            r += item["rows"]
        elif kind == "pagebreak":
            if r > 1:
                ws.row_breaks.append(Break(id=r - 1))
        elif kind == "text":
            style = item.get("style") or {}
            cell = ws.cell(row=r, column=1, value=item["text"])
            cell.font = _xlsx_font(style, default_size)
            cell.alignment = Alignment(horizontal=style.get("al", "left"), vertical="center", wrap_text=True)
            if style.get("bg"):
                cell.fill = PatternFill("solid", fgColor=_argb(style["bg"]))
            if ncols > 1:
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
            size = style.get("sz") or default_size
            lines = _est_lines(item["text"], span_width(1, ncols) * 1.05)
            if lines > 1 or size > 12:
                ws.row_dimensions[r].height = max(15, lines * size * 1.45)
            r += 1
        elif kind == "table":
            occupied: set[tuple[int, int]] = set()
            for row in item["rows"]:
                col = 1
                row_height = 0.0
                for cell in row["cells"]:
                    while (r, col) in occupied:
                        col += 1
                    if col > ncols:
                        break
                    cs = min(cell.get("cs", 1), ncols - col + 1)
                    rs = cell.get("rs", 1)
                    style = cell.get("style") or {}
                    target = ws.cell(row=r, column=col, value=cell["text"] or None)
                    font = _xlsx_font(style, default_size)
                    align = Alignment(
                        horizontal=style.get("al", "left"),
                        vertical=_VA.get(style.get("va", "top"), "top"),
                        wrap_text=style.get("wrap") is not False,
                    )
                    fill = PatternFill("solid", fgColor=_argb(style["bg"])) if style.get("bg") else None
                    bordered = item.get("border", True) and style.get("bd") is not False
                    for dr in range(rs):
                        for dc in range(cs):
                            cc = ws.cell(row=r + dr, column=col + dc)
                            cc.font, cc.alignment = font, align
                            if fill:
                                cc.fill = fill
                            if bordered:
                                cc.border = _BORDER
                            if dr or dc:
                                occupied.add((r + dr, col + dc))
                    if cs > 1 or rs > 1:
                        ws.merge_cells(start_row=r, start_column=col, end_row=r + rs - 1, end_column=col + cs - 1)
                    if cell["text"]:
                        lines = _est_lines(cell["text"], span_width(col, cs) * 1.05)
                        size = style.get("sz") or default_size
                        row_height = max(row_height, math.ceil(lines / rs) * size * 1.45)
                    col += cs
                if row_height > 18:
                    ws.row_dimensions[r].height = row_height
                r += 1

    ws.page_setup.orientation = exp["page"].get("orientation", "portrait")
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# 2b. Word
# ---------------------------------------------------------------------------
_ALIGN = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT}
_VALIGN = {
    "top": WD_CELL_VERTICAL_ALIGNMENT.TOP,
    "middle": WD_CELL_VERTICAL_ALIGNMENT.CENTER,
    "bottom": WD_CELL_VERTICAL_ALIGNMENT.BOTTOM,
}


def _docx_run(run, style: dict, default_size: float) -> None:
    run.font.name = DOCX_FONT
    rpr = run._element.get_or_add_rPr()
    rpr.rFonts.set(qn("w:eastAsia"), DOCX_FONT)
    run.font.size = Pt(style.get("sz") or default_size)
    run.bold = bool(style.get("b"))
    run.italic = bool(style.get("i"))
    run.underline = bool(style.get("u"))
    if style.get("fg"):
        run.font.color.rgb = RGBColor.from_string(style["fg"].lstrip("#").upper())


def _docx_write(paragraph, text: str, style: dict, default_size: float) -> None:
    paragraph.alignment = _ALIGN.get(style.get("al", "left"), WD_ALIGN_PARAGRAPH.LEFT)
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.space_before = Pt(0)
    run = paragraph.add_run(text)
    _docx_run(run, style, default_size)


def _docx_shade(cell, color: str) -> None:
    tc_pr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color.lstrip("#").upper())
    tc_pr.append(shd)


def render_docx(exp: dict[str, Any]) -> bytes:
    doc = Document()
    page = exp["page"]
    default_size = page.get("font_size", 11)
    section = doc.sections[0]
    landscape = page.get("orientation") == "landscape"
    if landscape:
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width, section.page_height = Mm(297), Mm(210)
    else:
        section.page_width, section.page_height = Mm(210), Mm(297)
    margin = 15
    section.left_margin = section.right_margin = Mm(margin)
    section.top_margin = section.bottom_margin = Mm(15)
    text_width_mm = (297 if landscape else 210) - 2 * margin

    normal = doc.styles["Normal"]
    normal.font.name = DOCX_FONT
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), DOCX_FONT)
    normal.font.size = Pt(default_size)

    for item in exp["items"]:
        kind = item["type"]
        if kind == "gap":
            for _ in range(item["rows"]):
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
        elif kind == "pagebreak":
            doc.add_page_break()
        elif kind == "text":
            _docx_write(doc.add_paragraph(), item["text"], item.get("style") or {}, default_size)
        elif kind == "table":
            rows = item["rows"]
            if not rows:
                continue
            ncols = max(len(item.get("cols") or []), max((sum(c.get("cs", 1) for c in r["cells"]) for r in rows), default=1))
            weights = (item.get("cols") or [10] * ncols)[:ncols]
            weights += [10] * (ncols - len(weights))
            total = sum(weights) or 1
            widths_mm = [text_width_mm * w / total for w in weights]
            table = doc.add_table(rows=len(rows), cols=ncols)
            if item.get("border", True):
                table.style = "Table Grid"
            table.autofit = False
            occupied: set[tuple[int, int]] = set()
            for ri, row in enumerate(rows):
                col = 0
                for cell in row["cells"]:
                    while (ri, col) in occupied:
                        col += 1
                    if col >= ncols:
                        break
                    cs = min(cell.get("cs", 1), ncols - col)
                    rs = min(cell.get("rs", 1), len(rows) - ri)
                    for dr in range(rs):
                        for dc in range(cs):
                            if dr or dc:
                                occupied.add((ri + dr, col + dc))
                    target = table.cell(ri, col)
                    if cs > 1 or rs > 1:
                        target = target.merge(table.cell(ri + rs - 1, col + cs - 1))
                        # merge() переносит в ячейку абзацы всех объединённых — лишние пустые убираем
                        for extra in target.paragraphs[1:]:
                            if not extra.text:
                                extra._element.getparent().remove(extra._element)
                    target.width = Mm(sum(widths_mm[col:col + cs]))
                    style = cell.get("style") or {}
                    para = target.paragraphs[0]
                    _docx_write(para, cell["text"], style, default_size)
                    target.vertical_alignment = _VALIGN.get(style.get("va", "top"), WD_CELL_VERTICAL_ALIGNMENT.TOP)
                    if style.get("bg"):
                        _docx_shade(target, style["bg"])
                    col += cs
            for ci in range(ncols):
                table.columns[ci].width = Mm(widths_mm[ci])
            # пустая строка после таблицы нужна Word, иначе соседние таблицы склеиваются
            doc.add_paragraph().paragraph_format.space_after = Pt(0)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
