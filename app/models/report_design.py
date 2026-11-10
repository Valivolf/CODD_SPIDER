"""Шаблон отчёта из онлайн-конструктора (/reports/builder).

Шаблон — обычный JSON, который рисует и правит редактор в браузере; здесь он
нормализуется (типы, лимиты, согласованная сетка объединённых ячеек), потому
что приходит от клиента и сохраняется как есть:

    {
      "version": 1,
      "format": "xlsx" | "docx",
      "page": {"orientation": "portrait"|"landscape", "font_size": 11},
      "columns": [22, 16, ...],            # только xlsx: ширины колонок листа (в знаках)
      "sections": [
        {"id": "s1", "title": "Бригада",
         "repeat": "none" | "crew",         # «для каждой бригады» (за каждый день периода)
         "gap": 2,                          # xlsx: пустых строк после каждого повтора; docx: пустых абзацев
         "page_break": false,               # docx: каждая бригада с новой страницы
         "skip_empty": true,                # пропускать бригады без заданий
         "blocks": [
           {"type": "text", "text": "…{{crew.name}}…", "style": {...}},
           {"type": "table", "cols": [..], "border": true,   # cols — только docx (у xlsx ширины общие)
            "rows": [{"repeat": "" | "task", "cells": [{"text": "…", "cs": 1, "rs": 1, "style": {...}}]}]},
           {"type": "spacer", "rows": 1}
         ]}
      ]
    }

Ячейки таблицы хранятся как в HTML: только «якорные» (cs/rs — объединение
вправо/вниз), покрытые объединением в строке не записываются. Стиль ячейки:
b/i/u — начертание, al — выравнивание, va — вертикальное, bg/fg — заливка и
цвет текста, sz — размер шрифта (pt), bd — рамка, wrap — перенос.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

FORMATS = ("xlsx", "docx")

MAX_JSON_BYTES = 400_000
MAX_SECTIONS = 20
MAX_BLOCKS = 40
MAX_ROWS = 200
MAX_COLS = 20
MAX_TEXT = 2000
MAX_CELL_TEXT = 1000
MAX_GAP = 20

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ALIGN = ("left", "center", "right")
_VALIGN = ("top", "middle", "bottom")


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, n))


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return round(min(hi, max(lo, n)), 2)


def _str(value: Any, limit: int) -> str:
    return str(value if value is not None else "").replace("\r", "").replace("\x00", "")[:limit]


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any, limit: int) -> list:
    return list(value)[:limit] if isinstance(value, list) else []


def clean_style(raw: Any, *, cell: bool) -> dict[str, Any]:
    """Только известные ключи и допустимые значения; лишнее отбрасывается."""
    s = _dict(raw)
    out: dict[str, Any] = {}
    for key in ("b", "i", "u"):
        if s.get(key):
            out[key] = True
    if s.get("al") in _ALIGN:
        out["al"] = s["al"]
    if cell and s.get("va") in _VALIGN:
        out["va"] = s["va"]
    for key in ("bg", "fg"):
        value = s.get(key)
        if isinstance(value, str) and _COLOR_RE.match(value):
            out[key] = value.lower()
    if s.get("sz") not in (None, "", 0):
        out["sz"] = _int(s.get("sz"), 11, 6, 48)
    if cell:
        # Рамка и перенос включены по умолчанию — храним только отключённые.
        if s.get("bd") is False:
            out["bd"] = False
        if s.get("wrap") is False:
            out["wrap"] = False
    return out


def _clean_table(raw: dict, fmt: str, ncols_hint: int) -> dict[str, Any]:
    """Приводит таблицу к согласованной сетке: ни одна ячейка не вылезает за
    границы, покрытое объединением место не занято чужими ячейками, каждая
    строка заполнена ровно до ncols. Объединение вниз не может задевать
    строки, повторяющиеся по заданиям (их число заранее неизвестно)."""
    rows_in = [r for r in _list(raw.get("rows"), MAX_ROWS) if isinstance(r, dict)]
    repeats = [("task" if r.get("repeat") == "task" else "") for r in rows_in]

    ncols = 0
    for r in rows_in:
        width = sum(_int(_dict(c).get("cs"), 1, 1, MAX_COLS) for c in _list(r.get("cells"), MAX_COLS))
        ncols = max(ncols, min(width, MAX_COLS))
    ncols = max(ncols, 1 if not rows_in else 0, min(len(_list(raw.get("cols"), MAX_COLS)), MAX_COLS), ncols_hint if not rows_in else 0)
    ncols = max(1, min(ncols, MAX_COLS))

    occupied: dict[tuple[int, int], bool] = {}
    rows_out: list[dict[str, Any]] = []
    for ri, r in enumerate(rows_in):
        cells_out: list[dict[str, Any]] = []
        col = 0
        for c in _list(r.get("cells"), MAX_COLS):
            c = _dict(c)
            while col < ncols and occupied.get((ri, col)):
                col += 1
            if col >= ncols:
                break
            cs = min(_int(c.get("cs"), 1, 1, MAX_COLS), ncols - col)
            # не залезаем на уже занятые (вертикальным объединением) клетки справа
            while cs > 1 and any(occupied.get((ri, col + k)) for k in range(1, cs)):
                cs -= 1
            rs = _int(c.get("rs"), 1, 1, MAX_ROWS)
            rs = min(rs, len(rows_in) - ri)
            if rs > 1:
                spanned = range(ri, ri + rs)
                if any(repeats[k] for k in spanned):
                    rs = 1
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        occupied[(ri + dr, col + dc)] = True
            cell = {"text": _str(c.get("text"), MAX_CELL_TEXT), "cs": cs, "rs": rs}
            style = clean_style(c.get("style"), cell=True)
            if style:
                cell["style"] = style
            cells_out.append(cell)
            col += cs
        # добиваем строку пустыми ячейками до ncols
        while col < ncols:
            if not occupied.get((ri, col)):
                cells_out.append({"text": "", "cs": 1, "rs": 1})
            col += 1
        row = {"cells": cells_out}
        if repeats[ri]:
            row["repeat"] = "task"
        rows_out.append(row)

    table: dict[str, Any] = {"type": "table", "rows": rows_out, "border": raw.get("border") is not False}
    if fmt == "docx":
        cols = [_num(w, 10, 3, 200) for w in _list(raw.get("cols"), MAX_COLS)]
        cols = (cols + [10.0] * ncols)[:ncols]
        table["cols"] = cols
    return table


def _clean_block(raw: Any, fmt: str, ncols_hint: int) -> dict[str, Any] | None:
    b = _dict(raw)
    kind = b.get("type")
    if kind == "text":
        out: dict[str, Any] = {"type": "text", "text": _str(b.get("text"), MAX_TEXT)}
        style = clean_style(b.get("style"), cell=False)
        if style:
            out["style"] = style
        return out
    if kind == "table":
        return _clean_table(b, fmt, ncols_hint)
    if kind == "spacer":
        return {"type": "spacer", "rows": _int(b.get("rows"), 1, 1, MAX_GAP)}
    return None


def sanitize_design(raw: Any, fmt: str) -> dict[str, Any]:
    """Нормализованный шаблон. Бросает ValueError, если присланное совсем не
    похоже на шаблон или слишком большое."""
    if fmt not in FORMATS:
        raise ValueError("Формат файла — xlsx или docx")
    if not isinstance(raw, dict):
        raise ValueError("Шаблон должен быть объектом")
    if len(json.dumps(raw, ensure_ascii=False)) > MAX_JSON_BYTES:
        raise ValueError("Шаблон слишком большой")

    page_in = _dict(raw.get("page"))
    page = {
        "orientation": "landscape" if page_in.get("orientation") == "landscape" else "portrait",
        "font_size": _int(page_in.get("font_size"), 11, 8, 16),
    }
    columns = [_num(w, 14, 3, 80) for w in _list(raw.get("columns"), MAX_COLS)] if fmt == "xlsx" else []
    if fmt == "xlsx" and not columns:
        columns = [14.0] * 5

    sections_out: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for sec in _list(raw.get("sections"), MAX_SECTIONS):
        sec = _dict(sec)
        sid = _str(sec.get("id"), 40) or uuid.uuid4().hex[:8]
        while sid in used_ids:
            sid = uuid.uuid4().hex[:8]
        used_ids.add(sid)
        blocks = []
        for blk in _list(sec.get("blocks"), MAX_BLOCKS):
            cleaned = _clean_block(blk, fmt, len(columns) if fmt == "xlsx" else 4)
            if cleaned:
                blocks.append(cleaned)
        sections_out.append(
            {
                "id": sid,
                "title": _str(sec.get("title"), 60),
                "repeat": "crew" if sec.get("repeat") == "crew" else "none",
                "gap": _int(sec.get("gap"), 1 if fmt == "docx" else 2, 0, MAX_GAP),
                "page_break": bool(sec.get("page_break")) if fmt == "docx" else False,
                "skip_empty": sec.get("skip_empty") is not False,
                "blocks": blocks,
            }
        )
    if fmt == "xlsx":
        # Ширин должно хватать на самую широкую таблицу листа.
        widest = 1
        for sec in sections_out:
            for blk in sec["blocks"]:
                if blk["type"] == "table":
                    widest = max(widest, max((sum(c["cs"] for c in r["cells"]) for r in blk["rows"]), default=1))
        while len(columns) < widest:
            columns.append(14.0)
    return {"version": 1, "format": fmt, "page": page, "columns": columns, "sections": sections_out}


# ---------------------------------------------------------------- стартовые шаблоны
def _cell(text: str = "", cs: int = 1, rs: int = 1, **style: Any) -> dict[str, Any]:
    cell: dict[str, Any] = {"text": text, "cs": cs, "rs": rs}
    if style:
        cell["style"] = style
    return cell


def starter_design(fmt: str, kind: str = "crews") -> dict[str, Any]:
    """Заготовка для нового отчёта. «crews» — как в задаче: шапка, затем по
    одной таблице на каждую бригаду (с заданиями и итогами), затем общий
    итог; «blank» — пустой лист с одним абзацем."""
    if fmt not in FORMATS:
        raise ValueError("Формат файла — xlsx или docx")
    widths = [6, 16, 38, 40, 24] if fmt == "xlsx" else [6, 16, 34, 30, 14]
    n = len(widths)
    if kind == "blank":
        design = {
            "format": fmt,
            "page": {"orientation": "portrait", "font_size": 11},
            "columns": [14] * 5 if fmt == "xlsx" else [],
            "sections": [{"id": "s1", "title": "Отчёт", "repeat": "none", "gap": 1, "blocks": [
                {"type": "text", "text": "Отчёт за {{period_label}}", "style": {"b": True, "sz": 14, "al": "center"}},
            ]}],
        }
        return sanitize_design(design, fmt)

    crew_table = {
        "type": "table",
        "border": True,
        "cols": widths,
        "rows": [
            {"cells": [_cell("{{crew.name}} — {{crew.date}}", n, b=True, bg="#e0e7ff", sz=12)]},
            {"cells": [_cell("Состав: {{crew.members}}", n)]},
            {"cells": [
                _cell("№", b=True, al="center", bg="#f1f5f9"),
                _cell("Комплекс", b=True, bg="#f1f5f9"),
                _cell("Адрес", b=True, bg="#f1f5f9"),
                _cell("Задание", b=True, bg="#f1f5f9"),
                _cell("Итог", b=True, bg="#f1f5f9"),
            ]},
            {"repeat": "task", "cells": [
                _cell("{{task.index}}", al="center"),
                _cell("{{task.point}}"),
                _cell("{{task.address}}"),
                _cell("{{task.work}}"),
                _cell("{{task.outcome}}"),
            ]},
            {"cells": [_cell("Заданий: {{crew.tasks_total}} · с итогом: {{crew.tasks_done}} · не выехали: {{crew.tasks_missed}}", n, i=True)]},
        ],
    }
    design = {
        "format": fmt,
        "page": {"orientation": "landscape" if fmt == "xlsx" else "portrait", "font_size": 11},
        "columns": widths if fmt == "xlsx" else [],
        "sections": [
            {"id": "head", "title": "Шапка", "repeat": "none", "gap": 1, "blocks": [
                {"type": "text", "text": "Отчёт о выездах бригад за {{period_label}}", "style": {"b": True, "sz": 14, "al": "center"}},
            ]},
            {"id": "crew", "title": "Бригада (повторяется для каждой бригады)", "repeat": "crew", "gap": 2 if fmt == "xlsx" else 1,
             "skip_empty": True, "blocks": [crew_table]},
            {"id": "total", "title": "Итого", "repeat": "none", "gap": 0, "blocks": [
                {"type": "text", "text": "Всего заданий за период: {{total_assigned}}, с отчётом: {{total_reported}}, не выполнено: {{total_not_done}}", "style": {"b": True}},
            ]},
        ],
    }
    return sanitize_design(design, fmt)
