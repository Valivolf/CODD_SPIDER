"""План-задание как Word-файл (.docx).

Страница /plan-print сама собирает листы (см. layoutTeam в
templates/plan_print.html): решает, какие блоки на каком листе, сколько
пустых строк «под доп. комплексы» и куда уходит дефектная ведомость. Для
«Скачать Word» браузер присылает сюда уже готовую раскладку — листы, блоки
и их ТЕКСТ (в том числе то, что оператор поправил на бланке перед
скачиванием), — а здесь она превращается в настоящий .docx: одна секция на
бригаду, строгие разрывы страниц между листами, строки таблицы заданной
минимальной высоты, а сноска про дефектную ведомость — в нижнем колонтитуле
первой страницы бригады.

Вёрстка Word не измеряется на сервере, поэтому высоты строк берутся с
запасом (HEIGHT_SCALE) — лучше чуть меньше места под доп. комплексы, чем
блок подписей, уехавший на следующую страницу.
"""
from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt

FONT = "Times New Roman"
PAGE_W_MM, PAGE_H_MM, MARGIN_MM = 210, 297, 10
TEXT_W_MM = PAGE_W_MM - 2 * MARGIN_MM
HEIGHT_SCALE = 0.94         # запас по высоте строк (Word считает строки чуть иначе, чем браузер)
BODY_PT = 10
TABLE_PT = 9.5

_MAX_TEAMS, _MAX_SHEETS, _MAX_BLOCKS, _MAX_ROWS, _MAX_TEXT = 100, 400, 20, 500, 4000

PLAN_COLS_PCT = [5, 30, 11, 11, 33, 10]
DEFECT_COLS_PCT = [16, 18, 26, 20, 20]


# ------------------------------------------------------------------ input hygiene
def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value if value is not None else "").replace("\r", "").replace("\x00", "")[:limit]


def _list(value: Any, limit: int) -> list:
    return list(value)[:limit] if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, n))


# ------------------------------------------------------------------ low-level docx helpers
def _style_run(run, size: float = BODY_PT, bold: bool = False, superscript: bool = False) -> None:
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run._element.rPr.rFonts.set(qn("w:cs"), FONT)
    run.font.size = Pt(size)
    run.bold = bold
    if superscript:
        run.font.superscript = True


def _para(container, text: str = "", *, size: float = BODY_PT, bold: bool = False,
          align=None, before: float = 0, after: float = 0, keep_next: bool = False,
          page_break_before: bool = False, tab_mm: float | None = None):
    p = container.add_paragraph()
    fmt = p.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing_rule = WD_LINE_SPACING.SINGLE
    fmt.keep_with_next = keep_next
    fmt.keep_together = True
    fmt.page_break_before = page_break_before
    if align is not None:
        p.alignment = align
    if tab_mm is not None:
        fmt.tab_stops.add_tab_stop(Mm(tab_mm))
    if text:
        _style_run(p.add_run(text), size, bold)
    return p


def _cell_paragraphs(cell, text: str, *, size: float, bold: bool = False, align=None, before: float = 2) -> None:
    """Fills a cell, one paragraph per line of `text` (the first paragraph
    python-docx pre-creates is reused)."""
    lines = text.split("\n") if text else [""]
    for i, line in enumerate(lines):
        p = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        p.paragraph_format.space_before = Pt(before if i == 0 else 0)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.SINGLE
        if align is not None:
            p.alignment = align
        if line:
            _style_run(p.add_run(line), size, bold)


def _shade(cell, fill_hex: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    tc_pr.append(shd)


def _repeat_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    tr_pr.append(el)


def _cant_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tr_pr.append(OxmlElement("w:cantSplit"))


def _set_widths(table, pcts: list[float]) -> None:
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    widths = [Mm(TEXT_W_MM * p / 100) for p in pcts]
    for row in table.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = w
    for col, w in zip(table.columns, widths):
        col.width = w


def _grid_table(doc, rows: int, pcts: list[float]):
    table = doc.add_table(rows=rows, cols=len(pcts))
    table.style = "Table Grid"
    _set_widths(table, pcts)
    return table


def _plain_table(doc, rows: int, widths_mm: list[float]):
    table = doc.add_table(rows=rows, cols=len(widths_mm))
    table.autofit = False
    for row in table.rows:
        for cell, w in zip(row.cells, widths_mm):
            cell.width = Mm(w)
    return table


def _row_height(row, mm: float, rule=WD_ROW_HEIGHT_RULE.AT_LEAST) -> None:
    row.height = Mm(max(4.0, mm))
    row.height_rule = rule


# ------------------------------------------------------------------ blocks
def _head(doc, block: dict, first_on_page: bool) -> None:
    approve = _dict(block.get("approve"))
    t = _plain_table(doc, 1, [TEXT_W_MM - 80, 80])
    cell = t.rows[0].cells[1]
    _cell_paragraphs(cell, _text(approve.get("title") or "УТВЕРЖДАЮ"), size=BODY_PT, before=0)
    p = cell.add_paragraph()
    p.paragraph_format.space_after = Pt(0)
    _style_run(p.add_run(_text(approve.get("org"))), 9.5)
    p = cell.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    _style_run(p.add_run("_________________  " + _text(approve.get("name"))), BODY_PT)

    title_size = _num(block.get("title_size_pt"), 15, 12, 24)
    title_align = {"left": WD_ALIGN_PARAGRAPH.LEFT, "right": WD_ALIGN_PARAGRAPH.RIGHT}.get(
        _text(block.get("title_align")), WD_ALIGN_PARAGRAPH.CENTER
    )
    header_shift = _num(block.get("header_shift_mm"), 0, -15, 15)
    _para(doc, _text(block.get("title")), size=title_size, bold=True, align=title_align,
          before=max(0, 22 + header_shift * 2.83), after=8)

    for item in _list(block.get("composition"), 30):
        item = _dict(item)
        label = _text(item.get("label"))
        _para(doc, f"{label}\t{_text(item.get('text'))}" if label else f"\t{_text(item.get('text'))}",
              size=BODY_PT, before=2, tab_mm=52)
    for line in _list(block.get("departures"), 10):
        _para(doc, _text(line), size=BODY_PT, before=5)
    _para(doc, _text(block.get("intro")), size=BODY_PT, before=8, after=4)


def _plan_table(doc, block: dict) -> None:
    rows = _list(block.get("rows"), _MAX_ROWS)
    headers = ["№", "Дислокация и вид работ", "Время начала", "Время окончания", "Результат", "Подпись"]
    table = _grid_table(doc, 1 + len(rows), PLAN_COLS_PCT)
    head_row = table.rows[0]
    _repeat_header(head_row)
    _row_height(head_row, 8)
    for i, (cell, title) in enumerate(zip(head_row.cells, headers)):
        _shade(cell, "F1F5F9")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _cell_paragraphs(cell, title, size=TABLE_PT, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, before=2)
    # «Результат¹⁾» — надстрочная сноска к тексту в колонтитуле первой страницы
    res_p = head_row.cells[4].paragraphs[0]
    _style_run(res_p.add_run("1)"), TABLE_PT, True, superscript=True)

    for row_data, row in zip(rows, table.rows[1:]):
        row_data = _dict(row_data)
        cells = _list(row_data.get("cells"), 6)
        cells += [""] * (6 - len(cells))
        height = _num(row_data.get("h"), 11, 4, 60)
        _row_height(row, height * HEIGHT_SCALE)
        _cant_split(row)
        for i, (cell, value) in enumerate(zip(row.cells, cells)):
            _cell_paragraphs(cell, _text(value), size=TABLE_PT, align=WD_ALIGN_PARAGRAPH.CENTER if i == 0 else None)


def _safety(doc, block: dict) -> None:
    items = _list(block.get("items"), 60)
    for index, item in enumerate(items):
        item = _dict(item)
        kind = item.get("t")
        if kind == "docs":
            lines = [_text(x) for x in _list(item.get("items"), 12)] or [""]
            _para(doc, f"{_text(item.get('label'))}\t{lines[0]}", size=BODY_PT, before=6, keep_next=True, tab_mm=36)
            for line in lines[1:]:
                _para(doc, f"\t{line}", size=BODY_PT, keep_next=True, tab_mm=36)
        elif kind == "agree":
            t = _plain_table(doc, 1, [110, TEXT_W_MM - 110])
            title_cell, name_cell = t.rows[0].cells
            _cell_paragraphs(title_cell, _text(item.get("title")), size=BODY_PT, before=8)
            name_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.BOTTOM
            _cell_paragraphs(name_cell, _text(item.get("name")), size=BODY_PT, align=WD_ALIGN_PARAGRAPH.RIGHT, before=0)
        else:
            # первый абзац блока часто идёт сразу за таблицей — даём ему отступ
            _para(doc, _text(item.get("text")), size=BODY_PT, before=14 if index == 0 else 6, keep_next=True)


def _defect(doc, block: dict) -> None:
    _para(doc, _text(block.get("title")) or "ДЕФЕКТНАЯ ВЕДОМОСТЬ", size=11.5, bold=True,
          align=WD_ALIGN_PARAGRAPH.CENTER, before=10, after=8, keep_next=True)
    headers = [_text(h) for h in _list(block.get("headers"), 5)]
    headers += [""] * (5 - len(headers))
    count = int(_num(block.get("rows"), 7, 1, 40))
    table = _grid_table(doc, 1 + count, DEFECT_COLS_PCT)
    head_row = table.rows[0]
    _repeat_header(head_row)
    _row_height(head_row, 10)
    for cell, title in zip(head_row.cells, headers):
        _shade(cell, "F1F5F9")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        _cell_paragraphs(cell, title, size=TABLE_PT, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, before=2)
    for row in table.rows[1:]:
        _row_height(row, 11 * HEIGHT_SCALE)
        _cant_split(row)


# ------------------------------------------------------------------ section / document
def _setup_section(section, note: str) -> None:
    section.page_width = Mm(PAGE_W_MM)
    section.page_height = Mm(PAGE_H_MM)
    section.left_margin = section.right_margin = Mm(MARGIN_MM)
    section.top_margin = section.bottom_margin = Mm(MARGIN_MM)
    section.header_distance = Mm(5)
    section.footer_distance = Mm(5)
    section.different_first_page_header_footer = True
    for footer in (section.footer, section.first_page_footer, section.header, section.first_page_header):
        footer.is_linked_to_previous = False
    # сноска — «нижний колонтитул первой страницы»: на первом листе бригады
    # всегда внизу, на остальных страницах колонтитул пустой
    footer = section.first_page_footer
    p = footer.paragraphs[0]
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    for r in list(p.runs):
        r._element.getparent().remove(r._element)
    if note:
        _style_run(p.add_run(note), 8)


def build_plan_docx(payload: dict) -> bytes:
    """`payload` — то, что собирает serializeSheets() на странице плана-
    задания: {"day": "05-10-2026", "teams": [{"note": "...", "sheets":
    [{"blocks": [{"type": "head"|"table"|"safety"|"defect", ...}]}]}]}.
    Любые лишние/битые поля игнорируются, тексты и размеры ограничены."""
    teams = _list(_dict(payload).get("teams"), _MAX_TEAMS)
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(BODY_PT)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    normal.paragraph_format.space_after = Pt(0)

    sheets_total = 0
    for ti, team in enumerate(teams):
        team = _dict(team)
        section = doc.sections[0] if ti == 0 else doc.add_section(WD_SECTION.NEW_PAGE)
        _setup_section(section, _text(team.get("note"), 1500))
        for si, sheet in enumerate(_list(team.get("sheets"), _MAX_SHEETS)):
            sheets_total += 1
            if sheets_total > _MAX_SHEETS:
                break
            if si > 0:
                # новый лист бригады: крошечный абзац с «разрывом перед», чтобы
                # лист мог начинаться и с таблицы
                spacer = _para(doc, "", size=1, page_break_before=True)
                spacer.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                spacer.paragraph_format.line_spacing = Pt(1)
            for block in _list(_dict(sheet).get("blocks"), _MAX_BLOCKS):
                block = _dict(block)
                kind = block.get("type")
                if kind == "head":
                    _head(doc, block, si == 0)
                elif kind == "table":
                    _plan_table(doc, block)
                elif kind == "safety":
                    _safety(doc, block)
                elif kind == "defect":
                    _defect(doc, block)
        tail = _para(doc, "", size=1)
        tail.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        tail.paragraph_format.line_spacing = Pt(1)

    if not teams:
        _para(doc, "Нет данных для плана-задания.", size=BODY_PT)
    doc.core_properties.title = f"План-задание {_text(_dict(payload).get('day'), 20)}".strip()
    doc.core_properties.author = ""
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
