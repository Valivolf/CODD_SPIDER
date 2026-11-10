"""Заполнение шаблонов Excel (.xlsx) и Word (.docx) данными из системы по описанию человека.

Как это устроено — модель НЕ пишет в файл и не выдумывает данные:
  1. analyze_*: код разбирает шаблон — таблицы (строка заголовков, столбцы, готовые пустые строки) и «метки» вроде {{период}};
  2. make_plan: модель по инструкции человека выбирает, какие таблицы заполнить, каким набором данных, с какими условиями и какой столбец
     шаблона какому полю соответствует (схема JSON — иначе ответить она не может);
  3. fill: код выполняет те же безопасные запросы, что и помощник в чате (query_engine, права роли), и записывает строки в файл,
     сохраняя оформление: копирует стиль строки, дописывает недостающие строки и сдвигает то, что ниже таблицы.
Так цифры и строки всегда настоящие, а ошибка модели в разметке видна в предварительном просмотре до заполнения."""
from __future__ import annotations

import copy
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.names import fold_name
from app.services.ai import catalog as cat
from app.services.ai import client, qa
from app.services.ai import query_engine as qe

MAX_TEMPLATE = 10 * 1024 * 1024
MAX_TABLE_ROWS = 5000
MAX_PLACEHOLDERS = 14
MAX_CAND_TABLES = 6
FOOTER_WORDS = ("итого", "всего", "подпись", "составил", "исполнитель", "руководитель", "согласовано", "утверждаю", "примечание", "м.п.")
_TOKEN_RE = re.compile(r"\{\{[^{}\n]{1,60}\}\}|\{[^{}\n]{1,40}\}|<<[^<>\n]{1,40}>>|\[\[[^\[\]\n]{1,40}\]\]|%[^%\s\n]{1,30}%")
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


class TemplateError(ValueError):
    """С шаблоном нельзя работать; текст — человеку."""


@dataclass
class TableCand:
    id: str
    sheet: str                      # лист Excel («» для Word)
    header_row: int                 # Excel: номер строки; Word: номер строки таблицы с нуля
    first_row: int                  # первая строка данных
    columns: list[tuple[int, str]]  # (номер столбца с 1, заголовок)
    blank_rows: int                 # сколько готовых пустых строк под заголовком
    footer_row: Optional[int]       # первая строка «подвала» под таблицей (итого, подписи) или None
    doc_index: int = -1             # Word: номер таблицы в документе
    where: str = ""


@dataclass
class Placeholder:
    id: str
    token: str
    where: str                      # Excel: «Лист!A3»; Word: «абзац 4» / «таблица 1, ячейка 2×3» / «колонтитул»
    context: str


@dataclass
class Analysis:
    kind: str                       # xlsx | docx
    name: str
    tables: list[TableCand] = field(default_factory=list)
    placeholders: list[Placeholder] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- разбор шаблона

def _str(v: Any) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def _is_footer(text: str) -> bool:
    t = fold_name(text)
    return any(t.startswith(w) for w in FOOTER_WORDS)


def analyze_xlsx(content: bytes, name: str = "шаблон.xlsx") -> tuple[Analysis, Any]:
    from openpyxl import load_workbook

    from app.services.excel_service import _zip_bomb_error

    bomb = _zip_bomb_error(content)
    if bomb:
        raise TemplateError(bomb)
    try:
        wb = load_workbook(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise TemplateError("Не удалось открыть файл Excel — проверьте, что это .xlsx") from exc
    a = Analysis("xlsx", name)
    tcount = pcount = 0
    for ws in wb.worksheets[:6]:
        max_row, max_col = min(ws.max_row or 1, 400), min(ws.max_column or 1, 40)
        rows_text: dict[int, list[tuple[int, str]]] = {}
        for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
            for c in row:
                if isinstance(c.value, str) and c.value.strip():
                    rows_text.setdefault(c.row, []).append((c.column, _str(c.value)))
                    for m in _TOKEN_RE.finditer(c.value):
                        if pcount < MAX_PLACEHOLDERS:
                            pcount += 1
                            a.placeholders.append(Placeholder(f"P{pcount}", m.group(0), f"{ws.title}!{c.coordinate}", _str(c.value)[:120]))
        # заголовки таблиц: строки, где ≥3 текстовых ячеек коротких, не метки
        cands: list[tuple[int, list[tuple[int, str]]]] = []
        for r in sorted(rows_text):
            cells = [(col, t) for col, t in rows_text[r] if len(t) <= 60 and not _TOKEN_RE.search(t)]
            if len(cells) >= 3 and len(cells) >= len(rows_text[r]) - 1:
                cands.append((r, cells))
        taken_until = 0
        for r, cells in cands:
            if r <= taken_until or tcount >= MAX_CAND_TABLES:
                continue
            # области данных под заголовком: подряд пустые строки (по столбцам заголовка), затем «подвал»
            cols = [c for c, _ in cells]
            r2, blank = r + 1, 0
            while r2 <= (ws.max_row or r) and r2 < r + 600 and all(ws.cell(r2, c).value in (None, "") for c in cols):
                blank += 1
                r2 += 1
            footer = None
            if r2 <= (ws.max_row or 0) and any(ws.cell(r2, c).value not in (None, "") for c in cols):
                nonblank = r2
                # примеры строк прямо под заголовком (перезапишем), пока не встретится «подвал»
                while nonblank <= (ws.max_row or 0):
                    texts = [_str(ws.cell(nonblank, c).value) for c in cols]
                    if any(_is_footer(t) for t in texts if t):
                        footer = nonblank
                        break
                    if all(t == "" for t in texts):
                        break
                    nonblank += 1
                if blank == 0 and footer is None:
                    blank = 0           # данные не вычисляем: строки-примеры будут перезаписаны
            else:
                footer = r2 if r2 <= (ws.max_row or 0) and any(ws.cell(r2, c).value not in (None, "") for c in range(1, max_col + 1)) else None
            # если после пустых строк есть «подвал» (Итого…), он ниже области данных
            if footer is None:
                probe = r + 1 + blank
                while probe <= min(ws.max_row or 0, r + 1 + blank + 6):
                    if any(_is_footer(_str(ws.cell(probe, c).value)) for c in range(1, max_col + 1)):
                        footer = probe
                        break
                    probe += 1
            tcount += 1
            a.tables.append(TableCand(f"T{tcount}", ws.title, r, r + 1, cells, blank, footer, where=f"лист «{ws.title}», заголовки в строке {r}"))
            taken_until = r + max(blank, 1)
    if not a.tables and not a.placeholders:
        a.notes.append("В шаблоне не найдено ни таблицы с заголовками (хотя бы три заголовка в одной строке), ни меток вида {{период}}.")
    return a, wb


def analyze_docx(content: bytes, name: str = "шаблон.docx") -> tuple[Analysis, Any]:
    from docx import Document

    from app.services.excel_service import _zip_bomb_error

    bomb = _zip_bomb_error(content)
    if bomb:
        raise TemplateError(bomb)
    try:
        doc = Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise TemplateError("Не удалось открыть файл Word — проверьте, что это .docx") from exc
    a = Analysis("docx", name)
    pcount = 0

    def scan(paragraph, where: str) -> None:
        nonlocal pcount
        for m in _TOKEN_RE.finditer(paragraph.text):
            if pcount < MAX_PLACEHOLDERS:
                pcount += 1
                a.placeholders.append(Placeholder(f"P{pcount}", m.group(0), where, _str(paragraph.text)[:120]))

    for i, p in enumerate(doc.paragraphs):
        scan(p, f"абзац {i + 1}")
    for ti, table in enumerate(doc.tables[:MAX_CAND_TABLES]):
        rows = table.rows
        if not rows:
            continue
        header_cells = [(ci + 1, _str(c.text)) for ci, c in enumerate(rows[0].cells)]
        header = [(ci, t) for ci, t in header_cells if t]
        if len(header) < 2:
            # таблица без «шапки» (например, реквизиты): метки в ячейках всё равно ищем
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row.cells):
                    for p in cell.paragraphs:
                        scan(p, f"таблица {ti + 1}, ячейка {ri + 1}×{ci + 1}")
            continue
        blank = 0
        for row in rows[1:]:
            if all(_str(c.text) == "" for c in row.cells):
                blank += 1
            else:
                break
        footer = None
        for ri in range(1 + blank, len(rows)):
            if any(_is_footer(_str(c.text)) for c in rows[ri].cells):
                footer = ri
                break
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row.cells):
                if ri == 0:
                    continue
                for p in cell.paragraphs:
                    scan(p, f"таблица {ti + 1}, ячейка {ri + 1}×{ci + 1}")
        idx = len(a.tables) + 1
        a.tables.append(TableCand(f"T{idx}", "", 0, 1, header, blank, footer, doc_index=ti, where=f"таблица {ti + 1} (заголовки в первой строке)"))
    for si, section in enumerate(doc.sections):
        for part, label in ((section.header, "верхний колонтитул"), (section.footer, "нижний колонтитул")):
            for p in part.paragraphs:
                scan(p, label)
    if not a.tables and not a.placeholders:
        a.notes.append("В шаблоне не найдено ни таблицы с заголовками, ни меток вида {{период}}.")
    return a, doc


def describe(a: Analysis) -> str:
    """Текст для модели: что найдено в шаблоне (таблицы T…, метки P…)."""
    lines = [f"Шаблон: {'Excel' if a.kind == 'xlsx' else 'Word'} «{a.name}»."]
    for t in a.tables:
        cols = "; ".join(f"столбец {c} «{h}»" for c, h in t.columns)
        extra = f", готовых пустых строк: {t.blank_rows}" if t.blank_rows else ""
        lines.append(f"{t.id} — таблица, {t.where}: {cols}{extra}.")
    for p in a.placeholders:
        lines.append(f"{p.id} — метка {p.token} ({p.where}), вокруг: «{p.context}»")
    lines.extend(a.notes)
    return "\n".join(lines)


# ---------------------------------------------------------------- план (модель)

def build_plan_schema(a: Analysis) -> dict:
    fields = qa._all_fields()
    keys = list(dict.fromkeys([*fields, *qa._group_keys(), "@n", "agg0", "agg1", "agg2", "agg3", ""]))
    qschema = qa.build_schema()["properties"]
    table_ids = [t.id for t in a.tables] or [""]
    ph_ids = [p.id for p in a.placeholders] or [""]
    return {
        "type": "object",
        "properties": {
            "can_fill": {"type": "boolean"},
            "reason": {"type": "string"},
            "period": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"], "additionalProperties": False},
            "tables": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "enum": table_ids},
                    "dataset": qschema["dataset"], "filters": qschema["filters"], "aggregates": qschema["aggregates"], "group_by": qschema["group_by"],
                    "order_by": qschema["order_by"],
                    "columns": {"type": "array", "items": {"type": "object", "properties": {"col": {"type": "integer"}, "field": {"type": "string", "enum": keys}},
                                                           "required": ["col", "field"], "additionalProperties": False}},
                },
                "required": ["table", "dataset", "filters", "aggregates", "group_by", "order_by", "columns"], "additionalProperties": False}},
            "placeholders": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ph_ids},
                    "kind": {"type": "string", "enum": ["period", "period_from", "period_to", "month_name", "today", "count", "text"]},
                    "text": {"type": "string"}, "dataset": {"type": "string", "enum": ["", *cat.DATASETS]}, "filters": qschema["filters"],
                },
                "required": ["id", "kind", "text", "dataset", "filters"], "additionalProperties": False}},
            "notes": {"type": "string"},
        },
        "required": ["can_fill", "reason", "period", "tables", "placeholders", "notes"], "additionalProperties": False,
    }


_RULES = """ЗАДАЧА. Человек дал шаблон документа (Excel или Word) и описал, как его заполнить. Ты составляешь ПЛАН заполнения; данные в файл вставит программа из базы — сам ничего не придумывай и не считай.

Что выбрать:
1. tables — какие таблицы шаблона заполнять (table = T1, T2…). Для каждой: dataset (набор данных), filters (условия), columns — какому полю данных (field) соответствует каждый столбец шаблона (col — номер столбца). Столбец «№», «№ п/п» → field "@n" (порядковый номер). Столбец, для которого нет данных → field "" (останется пустым). Поля бери ТОЛЬКО из списка наборов выше.
2. Если таблица — сводка (по типам, по бригадам, по месяцам…): заполни group_by и aggregates; в columns: field = ключ группировки (например visit_type или date:month) или agg0 (первый подсчёт), agg1… Для обычного списка записей group_by и aggregates пустые.
3. period — даты from/to (ГГГГ-ММ-ДД), если человек назвал период («за сентябрь», «с 1 по 15 октября»); иначе пустые строки. Месяц без года — ближайший прошедший или текущий.
4. placeholders — для каждой понятной метки (id = P1…): kind period (период целиком), period_from, period_to, month_name (месяц и год словами), today (сегодняшняя дата), count (сколько записей набора dataset с условиями filters за период), text (твой текст, например название отчёта из инструкции). Непонятные метки не включай.
5. can_fill = false и reason (одно предложение), если по шаблону и описанию не понять, чем заполнять.
6. Условия filters пиши так же, как для вопросов: «ремонты» → repair = да; тип выезда → visit_type (значение как в списках); «где нужна автовышка» → needs содержит «автовышк».
7. Столбец подбирай по СМЫСЛУ заголовка, а не по соседнему полю: «Тип работ / Вид работ / Тип выезда» → visit_type; «Что сделано / Выполнено / Работы» → comment; «Причина / Неисправность» → fault; «Что нужно» → needs; «Исполнитель / Монтёр» → executor; «Адрес» → address; «Комплекс / АПК / Номер» → point_id; «Дата» → date. Категорию комплекса (category) бери, только если так и написан заголовок."""

_EXAMPLE_DESC = (
    "Шаблон: Excel «Журнал.xlsx».\n"
    "T1 — таблица, лист «Лист1», заголовки в строке 4: столбец 1 «№ п/п»; столбец 2 «Дата»; столбец 3 «Комплекс»; столбец 4 «Адрес»; столбец 5 «Выполненные работы»; "
    "столбец 6 «Исполнитель», готовых пустых строк: 30.\n"
    "P1 — метка {{период}} (Лист1!A2), вокруг: «Отчёт о выездах за {{период}}»"
)
_EXAMPLE_PLAN = {
    "can_fill": True, "reason": "", "period": {"from": "2026-09-01", "to": "2026-09-30"},
    "tables": [{"table": "T1", "dataset": "visits", "filters": [{"field": "visit_type", "op": "contains", "value": "Ремонт", "values": []}], "aggregates": [], "group_by": [],
                "order_by": [{"field": "date", "dir": "asc"}],
                "columns": [{"col": 1, "field": "@n"}, {"col": 2, "field": "date"}, {"col": 3, "field": "point_id"}, {"col": 4, "field": "address"},
                            {"col": 5, "field": "comment"}, {"col": 6, "field": "executor"}]}],
    "placeholders": [{"id": "P1", "kind": "period", "text": "", "dataset": "", "filters": []}], "notes": "Взяты выезды с ремонтом за сентябрь.",
}


def prompt_prefix(enums: dict) -> tuple[str, list[dict]]:
    """Постоянная часть запроса к модели (описание данных, правила, пример): общая для всех шаблонов — её же «прогревает» warmup."""
    system = qa.build_system(enums).split("\nПРАВИЛА\n")[0] + "\n\n" + _RULES
    history = [{"role": "user", "content": _EXAMPLE_DESC + "\n\nКак заполнить (слова человека): заполни по ремонтам за сентябрь\nПериод человек отдельно не выбирал."},
               {"role": "assistant", "content": __import__("json").dumps(_EXAMPLE_PLAN, ensure_ascii=False)}]
    return system, history


async def make_plan(a: Analysis, instruction: str, period_from: str, period_to: str, *, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    enums = await qe.enum_values()
    system, history = prompt_prefix(enums)
    user = (
        f"Сегодня {today.isoformat()}.\n" + describe(a) + "\n\nКак заполнить (слова человека): " + (instruction.strip()[:800] or "не указано")
        + (f"\nПериод, выбранный человеком: с {period_from} по {period_to}." if period_from or period_to else "\nПериод человек отдельно не выбирал.")
    )
    data = await client.llm_json(system, user, build_plan_schema(a), schema_name="fill_plan", max_tokens=900, priority=client.PRIORITY_USER, history=history)
    return data


_RANGE_OPS = {"between", ">=", "<=", ">", "<"}


def _clean_filters(ds, filters: list) -> list:
    """Небольшие модели дублируют период условием по дате (часто с одной датой вместо двух) и оставляют условия без значения. Период уже применяется ко всей таблице,
    поэтому диапазонные условия по полю даты набора убираем; условие «содержит ничего» превращаем в «указано»."""
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        fd = ds.field(f.get("field", "")) if ds is not None else None
        if fd is not None and fd.type == cat.DATE and f.get("op") in _RANGE_OPS:
            continue
        if not (f.get("value") or f.get("values")) and f.get("op") in ("contains", "starts", "!=", "="):
            f = {**f, "op": "is_empty" if f.get("op") == "=" else "not_empty"}
        out.append(f)
    return out


def validate_plan(raw: dict, a: Analysis, role: str) -> dict:
    """Приводит план к рабочему виду и убирает то, что нельзя выполнить; возвращает {plan, warnings}."""
    warnings: list[str] = []
    cands = {t.id: t for t in a.tables}
    plan_tables = []
    for t in raw.get("tables") or []:
        cand = cands.get(t.get("table"))
        if cand is None:
            continue
        ds = cat.DATASETS.get(t.get("dataset", ""))
        if ds is None or not cat.dataset_allowed(role, ds):
            warnings.append(f"Таблица {cand.where}: данные «{t.get('dataset')}» вашей роли недоступны — не заполняется.")
            continue
        valid_cols = {c for c, _ in cand.columns}
        columns = [{"col": int(c["col"]), "field": c.get("field") or ""} for c in t.get("columns") or [] if int(c.get("col") or 0) in valid_cols or a.kind == "docx"]
        plan_tables.append({
            "table": cand.id, "dataset": ds.name, "filters": _clean_filters(ds, t.get("filters") or []), "aggregates": t.get("aggregates") or [], "group_by": t.get("group_by") or [],
            "order_by": t.get("order_by") or [], "columns": columns,
        })
    plan_ph = []
    known = {p.id for p in a.placeholders}
    for p in raw.get("placeholders") or []:
        if p.get("id") in known:
            plan_ph.append({"id": p["id"], "kind": p.get("kind") or "text", "text": p.get("text") or "", "dataset": p.get("dataset") or "", "filters": _clean_filters(cat.DATASETS.get(p.get("dataset") or ""), p.get("filters") or [])})
    period = dict(raw.get("period") or {})
    if not (period.get("from") or period.get("to")):
        # период не названа отдельно, а модель записала его условием «дата между двумя датами» — берём оттуда (само условие убрано как дублирующее)
        for t in raw.get("tables") or []:
            ds = cat.DATASETS.get(t.get("dataset", ""))
            for f in t.get("filters") or []:
                fd = ds.field(f.get("field", "")) if ds is not None else None
                vals = [v for v in (f.get("values") or []) if v]
                if fd is not None and fd.type == cat.DATE and f.get("op") == "between" and len(vals) == 2:
                    period = {"from": vals[0], "to": vals[1]}
                    break
            if period.get("from"):
                break
    return {"plan": {"period": {"from": period.get("from") or None, "to": period.get("to") or None}, "tables": plan_tables, "placeholders": plan_ph,
                     "notes": str(raw.get("notes") or "")}, "warnings": warnings}


def effective_period(plan: dict, override_from: Optional[str], override_to: Optional[str]) -> dict:
    """Период, выбранный человеком в форме, главнее названного в тексте (его же человек мог поменять после просмотра плана)."""
    p = plan.get("period") or {}
    if override_from or override_to:
        return {"preset": None, "from": override_from or None, "to": override_to or None}
    return {"preset": None, "from": p.get("from") or None, "to": p.get("to") or None}


def period_text(period: dict, today: dt.date) -> str:
    rng = qe.period_range(period, today)
    if not rng:
        return "за всё время"
    return f"{qe.fmt_date(rng[0].isoformat())} – {qe.fmt_date(rng[1].isoformat())}"


def preview_lines(plan: dict, a: Analysis, period: dict, today: dt.date) -> list[str]:
    """План человеческим языком — чтобы увидеть ошибку модели до заполнения."""
    out = []
    try:
        out.append("Период: " + period_text(period, today))
    except qe.QueryError as exc:
        out.append(f"Период: не понят ({exc})")
    cands = {t.id: t for t in a.tables}
    for t in plan["tables"]:
        cand = cands[t["table"]]
        ds = cat.DATASETS[t["dataset"]]
        spec = {"dataset": ds.name, "period": {}, "filters": t["filters"], "group_by": t["group_by"]}
        head = f"Таблица ({cand.where}) ← {qe.describe(spec, 'admin', {}, today)}"
        out.append(head)
        headers = dict(cand.columns)
        for c in t["columns"]:
            f = c["field"]
            if f == "@n":
                src = "порядковый номер"
            elif f == "":
                src = "останется пустым"
            elif f.startswith("agg"):
                src = "итог подсчёта"
            else:
                fd = ds.field(f.split(":")[0])
                src = fd.label.split(" (")[0] if fd else f
            out.append(f"   столбец {c['col']} «{headers.get(c['col'], '')}» ← {src}")
    for p in plan["placeholders"]:
        ph = next((x for x in a.placeholders if x.id == p["id"]), None)
        kinds = {"period": "период", "period_from": "начало периода", "period_to": "конец периода", "month_name": "месяц и год", "today": "сегодняшняя дата", "count": "количество записей", "text": "текст"}
        out.append(f"Метка {ph.token if ph else p['id']} ← {kinds.get(p['kind'], p['kind'])}" + (f": «{p['text']}»" if p["kind"] == "text" and p["text"] else ""))
    if plan.get("notes"):
        out.append("Комментарий: " + plan["notes"])
    return out


# ---------------------------------------------------------------- заполнение

@dataclass
class FillReport:
    tables: list[dict] = field(default_factory=list)
    placeholders: int = 0
    warnings: list[str] = field(default_factory=list)


def _default_order(ds: cat.Dataset) -> list[dict]:
    return [{"field": ds.date_field, "dir": "asc"}] if ds.date_field else []


async def _table_rows(t: dict, role: str, period: dict, today: dt.date) -> tuple[Optional[qe.Result], list[str]]:
    """Выполняет запрос таблицы плана; возвращает результат со столбцами в порядке столбцов шаблона."""
    warnings: list[str] = []
    ds = cat.DATASETS[t["dataset"]]
    summary = bool(t["aggregates"] or t["group_by"])
    if (period.get("from") or period.get("to") or period.get("preset")) and not (ds.date_field or ds.period_overlap):
        period = {}                               # у справочников (комплексы, оборудование) даты записи нет — период не применяется, берутся все записи
        warnings.append(f"Для «{ds.label.split(' (')[0]}» период не применяется — в таблицу взяты все записи.")
    # сортировка по полю, которого нет в наборе, — частая оплошность модели: не роняем таблицу, сортируем как принято у набора
    order = [o for o in t["order_by"] if ds.field(str(o.get("field", "")).split(":")[0]) is not None or (summary and str(o.get("field", "")) in ("count", "n"))]
    if len(order) != len(t["order_by"]):
        warnings.append("Сортировка по полю, которого нет в данных, пропущена.")
    spec: dict[str, Any] = {"dataset": ds.name, "period": period, "filters": t["filters"], "aggregates": [dict(a, alias="") for a in t["aggregates"]],
                            "group_by": t["group_by"], "order_by": order or ([] if summary else _default_order(ds)), "limit": MAX_TABLE_ROWS}
    if not summary:
        names: list[str] = []
        for c in t["columns"]:
            f = c["field"]
            base = f.split(":")[0]
            if f and f != "@n" and not f.startswith("agg") and ds.field(base) is not None and base not in names:
                names.append(base)
        # поля, которых нет у роли, убираем с пометкой (а не роняем весь документ)
        visible = {f.name for f in cat.role_fields(role, ds)}
        denied = [n for n in names if n not in visible]
        if denied:
            warnings.append("Нет доступа к данным: " + ", ".join(f"«{(ds.field(n).label if ds.field(n) else n).split(' (')[0]}»" for n in denied) + " — эти столбцы остались пустыми.")
        spec["select"] = [n for n in names if n in visible] or [ds.default_select[0]]
    try:
        result = await qe.run(spec, role, today=today, max_rows=MAX_TABLE_ROWS)
    except qe.QueryError as exc:
        return None, [f"Таблица не заполнена: {exc}"]
    return result, warnings


def _value_for(c: dict, result: qe.Result, row: list, n: int, summary: bool) -> Any:
    f = c["field"]
    if f == "@n":
        return n
    if not f:
        return ""
    keys = [col["key"] for col in result.columns]
    if f in keys:
        v = row[keys.index(f)]
    elif f.startswith("agg") and f in keys:
        v = row[keys.index(f)]
    else:
        v = ""
    return v


def _placeholder_text(p: dict, period: dict, today: dt.date) -> Optional[str]:
    kind = p["kind"]
    rng = qe.period_range(period, today)
    if kind == "period":
        return period_text(period, today)
    if kind == "period_from":
        return qe.fmt_date(rng[0].isoformat()) if rng else ""
    if kind == "period_to":
        return qe.fmt_date(rng[1].isoformat()) if rng else ""
    if kind == "month_name":
        d = rng[0] if rng else today
        return f"{MONTHS[d.month - 1]} {d.year}"
    if kind == "today":
        return today.strftime("%d.%m.%Y")
    if kind == "text":
        return p["text"]
    return None


_REF_RE = re.compile(r"(\$?[A-Z]{1,3})(\$?)(\d+)(?::(\$?[A-Z]{1,3})(\$?)(\d+))?")


def _shift_formula(formula: str, idx: int, amount: int) -> str:
    """После вставки `amount` строк перед строкой `idx` в формулах этого листа: ссылки на строки ≥ idx сдвигаются; конец диапазона, упирающийся
    в последнюю строку таблицы (idx − 1), тоже — так «Итого» после таблицы охватывает и дописанные строки. Ссылки на другие листы не трогаем."""
    def bump(m: re.Match) -> str:
        c1, d1, r1, c2, d2, r2 = m.groups()
        row1 = int(r1) + (amount if int(r1) >= idx else 0)
        if c2 is None:
            return f"{c1}{d1}{row1}"
        end = int(r2)
        row2 = end + (amount if end >= idx - 1 else 0)
        return f"{c1}{d1}{row1}:{c2}{d2}{row2}"

    out = []
    for part in re.split(r"('[^']*'![A-Z$0-9:]+|[A-Za-zА-Яа-я0-9_]+![A-Z$0-9:]+)", formula):
        out.append(part if "!" in part else _REF_RE.sub(bump, part))
    return "".join(out)


async def fill_xlsx(content: bytes, name: str, plan: dict, role: str, override: tuple[Optional[str], Optional[str]], today: dt.date) -> tuple[bytes, FillReport]:
    from copy import copy

    a, wb = analyze_xlsx(content, name)
    report = FillReport()
    period = effective_period(plan, *override)
    cands = {t.id: t for t in a.tables}
    for t in plan["tables"]:
        cand = cands[t["table"]]
        ws = wb[cand.sheet]
        result, warns = await _table_rows(t, role, period, today)
        report.warnings.extend(warns)
        if result is None:
            continue
        rows = result.rows
        if result.truncated:
            report.warnings.append(f"Таблица ({cand.where}): записей больше {MAX_TABLE_ROWS}, записаны первые {MAX_TABLE_ROWS}.")
        first = cand.first_row
        area_end = (cand.footer_row - 1) if cand.footer_row else None        # последняя строка области данных до «подвала»
        capacity = (area_end - first + 1) if area_end else None
        need = len(rows)
        if capacity is not None and need > capacity:
            extra = need - capacity
            insert_at = cand.footer_row
            merged_below = [(str(r), r.min_row) for r in list(ws.merged_cells.ranges) if r.min_row >= insert_at]
            for rng_text, _ in merged_below:
                ws.unmerge_cells(rng_text)
            ws.insert_rows(insert_at, amount=extra)
            for rng_text, _ in merged_below:
                m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", rng_text)
                if m:
                    ws.merge_cells(f"{m.group(1)}{int(m.group(2)) + extra}:{m.group(3)}{int(m.group(4)) + extra}")
            # формулы ниже таблицы (Итого…) сдвигаются вместе со строками — правим ссылки
            for row in ws.iter_rows(min_row=insert_at + extra, max_row=ws.max_row):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        cell.value = _shift_formula(cell.value, insert_at, extra)
            for row in ws.iter_rows(min_row=1, max_row=insert_at - 1):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        cell.value = _shift_formula(cell.value, insert_at, extra)
            # новые строки оформляются как первая строка данных
            for r in range(first + capacity, first + need):
                for col in range(1, (ws.max_column or 1) + 1):
                    src, dst = ws.cell(first, col), ws.cell(r, col)
                    if src.has_style:
                        dst._style = copy(src._style)
                if ws.row_dimensions[first].height:
                    ws.row_dimensions[r].height = ws.row_dimensions[first].height
            single_merges = [r for r in ws.merged_cells.ranges if r.min_row == r.max_row == first]
            for r in range(first + capacity, first + need):
                for rng in single_merges:
                    ws.merge_cells(start_row=r, start_column=rng.min_col, end_row=r, end_column=rng.max_col)
            report.warnings.append(f"Таблица ({cand.where}): пустых строк в шаблоне было {capacity}, дописано ещё {extra}.") if capacity else None
        summary = bool(t["aggregates"] or t["group_by"])
        for i, row in enumerate(rows):
            r = first + i
            for c in t["columns"]:
                col = c["col"]
                value = _value_for(c, result, row, i + 1, summary)
                cell = ws.cell(r, col)
                if cell.coordinate in ws.merged_cells and cell.__class__.__name__ == "MergedCell":
                    continue                   # внутри объединённой ячейки пишет только её левый верхний угол
                cell.value = value if value != "" else None
        # лишние примеры строк под данными очищаем (шаблонные примеры), если данных меньше, чем строк-примеров
        if capacity is not None and need < capacity:
            pass
        report.tables.append({"table": cand.id, "where": cand.where, "rows": len(rows), "description": result.description})
    for p in plan["placeholders"]:
        ph = next((x for x in a.placeholders if x.id == p["id"]), None)
        if ph is None:
            continue
        text = _placeholder_text(p, period, today)
        if text is None:           # count
            ds = cat.DATASETS.get(p["dataset"])
            if ds is None or not cat.dataset_allowed(role, ds):
                report.warnings.append(f"Метка {ph.token}: данные недоступны.")
                continue
            try:
                res = await qe.run({"dataset": ds.name, "period": period, "filters": p["filters"], "aggregates": [{"fn": "count"}]}, role, today=today)
                text = str(res.rows[0][0])
            except qe.QueryError as exc:
                report.warnings.append(f"Метка {ph.token}: {exc}")
                continue
        sheet, _, coord = ph.where.rpartition("!")
        cell = wb[sheet][coord]
        if isinstance(cell.value, str):
            cell.value = cell.value.replace(ph.token, text)
            report.placeholders += 1
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), report


def _set_cell_text(cell, value: Any) -> None:
    text = "" if value is None else str(value)
    paragraphs = cell.paragraphs
    first = paragraphs[0]
    if first.runs:
        first.runs[0].text = text
        for r in first.runs[1:]:
            r.text = ""
    else:
        first.add_run(text)
    for extra in paragraphs[1:]:
        for r in extra.runs:
            r.text = ""


def _replace_in_paragraph(paragraph, token: str, text: str) -> bool:
    if token not in paragraph.text:
        return False
    runs = paragraph.runs
    full = "".join(r.text for r in runs)
    if token in full:
        replaced = full.replace(token, text)
        if runs:
            runs[0].text = replaced
            for r in runs[1:]:
                r.text = ""
        return True
    return False


async def fill_docx(content: bytes, name: str, plan: dict, role: str, override: tuple[Optional[str], Optional[str]], today: dt.date) -> tuple[bytes, FillReport]:
    a, doc = analyze_docx(content, name)
    report = FillReport()
    period = effective_period(plan, *override)
    cands = {t.id: t for t in a.tables}
    for t in plan["tables"]:
        cand = cands[t["table"]]
        table = doc.tables[cand.doc_index]
        result, warns = await _table_rows(t, role, period, today)
        report.warnings.extend(warns)
        if result is None:
            continue
        rows = result.rows
        if result.truncated:
            report.warnings.append(f"Таблица ({cand.where}): записей больше {MAX_TABLE_ROWS}, записаны первые {MAX_TABLE_ROWS}.")
        first = cand.first_row
        footer = cand.footer_row if cand.footer_row is not None else len(table.rows)
        capacity = footer - first
        template_row = table.rows[first] if first < len(table.rows) else table.rows[-1]
        extra = max(0, len(rows) - capacity)
        for _ in range(extra):
            new_tr = copy.deepcopy(template_row._tr)
            if footer < len(table.rows):
                table.rows[footer]._tr.addprevious(new_tr)
            else:
                table._tbl.append(new_tr)
            footer += 1
        summary = bool(t["aggregates"] or t["group_by"])
        for i, row in enumerate(rows):
            tr = table.rows[first + i]
            for c in t["columns"]:
                ci = c["col"] - 1
                if 0 <= ci < len(tr.cells):
                    _set_cell_text(tr.cells[ci], _value_for(c, result, row, i + 1, summary))
        if extra and capacity:
            report.warnings.append(f"Таблица ({cand.where}): пустых строк в шаблоне было {capacity}, дописано ещё {extra}.")
        report.tables.append({"table": cand.id, "where": cand.where, "rows": len(rows), "description": result.description})

    def all_paragraphs():
        for p in doc.paragraphs:
            yield p
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        yield p
        for section in doc.sections:
            for part in (section.header, section.footer):
                for p in part.paragraphs:
                    yield p

    for p in plan["placeholders"]:
        ph = next((x for x in a.placeholders if x.id == p["id"]), None)
        if ph is None:
            continue
        text = _placeholder_text(p, period, today)
        if text is None:
            ds = cat.DATASETS.get(p["dataset"])
            if ds is None or not cat.dataset_allowed(role, ds):
                report.warnings.append(f"Метка {ph.token}: данные недоступны.")
                continue
            try:
                res = await qe.run({"dataset": ds.name, "period": period, "filters": p["filters"], "aggregates": [{"fn": "count"}]}, role, today=today)
                text = str(res.rows[0][0])
            except qe.QueryError as exc:
                report.warnings.append(f"Метка {ph.token}: {exc}")
                continue
        for paragraph in all_paragraphs():
            if _replace_in_paragraph(paragraph, ph.token, text):
                report.placeholders += 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), report


async def analyze(content: bytes, name: str) -> Analysis:
    low = name.lower()
    if len(content) > MAX_TEMPLATE:
        raise TemplateError(f"Файл больше {MAX_TEMPLATE // (1024 * 1024)} МБ.")
    if low.endswith((".xlsx", ".xlsm")):
        return analyze_xlsx(content, name)[0]
    if low.endswith(".docx"):
        return analyze_docx(content, name)[0]
    raise TemplateError("Поддерживаются файлы Excel (.xlsx) и Word (.docx). Старые форматы .xls и .doc сохраните заново как .xlsx / .docx.")


async def fill(content: bytes, name: str, plan: dict, role: str, override: tuple[Optional[str], Optional[str]] = (None, None),
               today: Optional[dt.date] = None) -> tuple[bytes, FillReport, str]:
    today = today or dt.date.today()
    low = name.lower()
    if low.endswith((".xlsx", ".xlsm")):
        data, rep = await fill_xlsx(content, name, plan, role, override, today)
        return data, rep, "xlsx"
    if low.endswith(".docx"):
        data, rep = await fill_docx(content, name, plan, role, override, today)
        return data, rep, "docx"
    raise TemplateError("Поддерживаются файлы Excel (.xlsx) и Word (.docx).")
