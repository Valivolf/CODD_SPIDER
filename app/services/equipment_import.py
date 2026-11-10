"""Импорт оборудования из таблиц Excel (.xlsx): склад ведётся в десятках разных файлов — роутеры, SIM-карты, платы, блоки
питания, счётчики… — у каждого свой набор столбцов. Поэтому импорт не привязан к одному шаблону:

  1. `analyze` читает файлы, находит строку заголовков и для каждого столбца предлагает роль (серийный номер, модель,
     комплекс, дата монтажа, монтёр, состояние, примечание…) — всё, что не подошло, предлагается как ДОПОЛНИТЕЛЬНОЕ ПОЛЕ
     (IMEI, № коробки, «старый роутер»… — см. таблицы equipment_fields / equipment_field_values);
  2. человек в веб-панели поправляет роли (и сопоставление значений столбца «статус»), выбирает категорию;
  3. `run_import` применяет настройки к строкам: сначала как проверка (dry_run — всё выполняется в транзакции и
     откатывается, поэтому отчёт «что будет» совпадает с реальным результатом), затем по-настоящему.

Правила: оборудование ищется по серийному номеру; режим «merge» ничего не двигает у уже известной единицы и лишь дополняет
пустое, «overwrite» подтягивает место/состояние/даты из файла. SIM-карта — служебная категория: её номер приводится к
последним 7 цифрам и связывается с полем «Номер SIM» комплекса."""
from __future__ import annotations

import datetime as dt
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from openpyxl import load_workbook

from app.core import equipment as eq
from app.db.database import get_connection
from app.repositories import equipment_repository as repo
from app.services import equipment_service as svc
from app.services.excel_service import _zip_bomb_error

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_ROWS = 40000
MAX_DISTINCT = 60          # столько разных значений столбца показываем для сопоставления «статуса»
SAMPLE_ROWS = 6

ROLES: dict[str, str] = {
    "ignore": "— не загружать —",
    "serial": "Серийный номер",
    "category": "Категория",
    "model": "Модель / наименование",
    "point": "Комплекс (номер АПК)",
    "point_inv": "Инвентарный номер комплекса",
    "status": "Состояние / статус",
    "person": "Монтёр (кому выдано)",
    "issued_date": "Дата выдачи",
    "installed_date": "Дата установки (монтажа)",
    "installed_by": "Кто установил (исполнитель)",
    "received_date": "Дата приёмки (получения)",
    "notes": "Примечание",
    "extra": "Дополнительное поле…",
    "serial_of": "Серийный № категории…",
}
SINGLE_ROLES = {"serial", "category", "model", "point", "point_inv", "status", "person", "issued_date", "installed_date",
                "installed_by", "received_date", "notes"}

# Название файла/листа → вероятная категория (подсказка, человек может поменять).
CATEGORY_HINTS: list[tuple[str, str]] = [
    (r"sim|сим[- ]?карт", eq.SIM_CATEGORY_NAME),
    (r"роутер|router|keenetic|d-?link|teleofis", "Роутер"),
    (r"плат[аы]\s*управл|плат[аы]", "Плата управления"),
    (r"блок питан|\bбп\b", "Блок питания"),
    (r"счетчик|счётчик|пульсар", "Счётчик"),
    (r"сэс|электроснабж", "Система электроснабжения (СЭС)"),
    (r"щу-?1н|щит", "Щит учётный"),
    (r"солн|панел", "Солнечная панель"),
    (r"антенн", "Антенна"),
    (r"автомат", "Автомат"),
    (r"пн-?92|преобразов|f-?1000", "Преобразователь напряжения"),
]

# Автоопределение роли столбца по заголовку. Порядок важен: более конкретные правила — раньше.
HEADER_RULES: list[tuple[str, str]] = [
    ("issued_date", r"дата\s*выдачи"),
    ("installed_date", r"дата\s*(монтажа|установки)"),
    ("received_date", r"дата\s*(получения|прием|приём|поступлени)"),
    ("point_inv", r"\bинв"),
    ("serial", r"серийн|заводск|iccid|^icc$|imei|\bs/?n\b"),
    ("status", r"^статус|состояни"),
    ("notes", r"коммент|примечан"),
    ("installed_by", r"исполнител|кто установил"),
    ("person", r"кому выдан|получател|^выдано$|^получил|эл\.?\s*монтер"),
    ("point", r"апк|комплекс|место\s*(монтажа|установки)"),
    ("category", r"категори"),
    ("model", r"наименован|модель|^тип$"),
]

DATE_RES = [
    re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)"),
    re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"),
]


class ImportError_(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# ----------------------------------------------------------------------------------------------------- чтение файла

def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, dt.time):
        return None          # «чистое время» без даты (часто 00:00:00 у пустых дат) — не значение
    if isinstance(v, (dt.datetime, dt.date)):
        return v
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else str(v)
    text = str(v).replace("\xa0", " ").strip()
    text = re.sub(r"\s*[\r\n]+\s*", " ", text)
    return text or None


@dataclass
class Sheet:
    file: str
    name: str
    rows: list[list[Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.file}::{self.name}"


def load_sheets(raw: bytes, filename: str) -> list[Sheet]:
    if filename.lower().endswith(".xls"):
        raise ImportError_(f"«{filename}»: формат .xls не поддерживается — откройте файл в Excel и сохраните как .xlsx")
    if len(raw) > MAX_FILE_BYTES:
        raise ImportError_(f"«{filename}»: файл слишком большой (больше {MAX_FILE_BYTES // 1024 // 1024} МБ)")
    err = _zip_bomb_error(raw)
    if err:
        raise ImportError_(f"«{filename}»: {err}")
    try:
        wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ImportError_(f"«{filename}»: не удалось прочитать файл Excel ({exc})")
    sheets: list[Sheet] = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        rows: list[list[Any]] = []
        for r in ws.iter_rows(values_only=True):
            rows.append([_cell(c) for c in r])
            if len(rows) >= MAX_ROWS:
                break
        while rows and not any(c is not None for c in rows[-1]):
            rows.pop()
        if not rows:
            continue
        width = max(len(r) for r in rows)
        for r in rows:
            r.extend([None] * (width - len(r)))
        sheets.append(Sheet(filename, ws.title, rows))
    return sheets


def _is_num(c: Any) -> bool:
    return isinstance(c, (int, float)) or (isinstance(c, str) and bool(re.fullmatch(r"\d+([.,]\d+)?", c.strip())))


def detect_header_row(rows: list[list[Any]]) -> int:
    """Номер (с нуля) строки заголовков: ближайшая непустая строка над первой строкой данных (та начинается с
    порядкового номера). Нет такой — строка с наибольшим числом коротких текстовых ячеек."""
    for i, r in enumerate(rows[:300]):
        cells = [c for c in r if c not in (None, "")]
        if len(cells) >= 2 and _is_num(cells[0]):
            for j in range(i - 1, -1, -1):
                if sum(1 for c in rows[j] if c not in (None, "")) >= 2:
                    return j
            break
    best, best_n = 0, -1
    for i, r in enumerate(rows[:15]):
        n = sum(1 for c in r if isinstance(c, str) and 0 < len(c) <= 40)
        if n > best_n:
            best, best_n = i, n
    return best


def _header_text(c: Any) -> str:
    return re.sub(r"\s+", " ", str(c or "")).strip()


# В заголовках встречаются латинские буквы, похожие на русские («Cерийный номер» с латинской C) — сравниваем и так, и так.
_LOOKALIKE = str.maketrans("acepoxykmtbh", "асерохукмтвн")


def _role_by_header(low: str) -> Optional[str]:
    variants = (low, low.translate(_LOOKALIKE))
    for role, rx in HEADER_RULES:
        if any(re.search(rx, v) for v in variants):
            return role
    return None


# ----------------------------------------------------------------------------------------------------- значения

def parse_date(value: Any) -> Optional[str]:
    """Дата из ячейки: datetime, «31.03.2026», «02.04.26», «2026-05-06» или текст с датой внутри («03.10.2025 передал»).
    Невозможную дату («02.10.20026») не угадываем — None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = str(value)
    for idx, rx in enumerate(DATE_RES):
        m = rx.search(text)
        if not m:
            continue
        try:
            if idx == 2:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if y < 100:
                    y += 2000
            return dt.date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.date().isoformat() if isinstance(value, dt.datetime) else value.isoformat()
    return str(value).strip()


def point_key(text: Any) -> str:
    return re.sub(r"[\s\-_.]+", "", str(text or "")).casefold()


def person_key(name: Any) -> tuple[str, str]:
    """(фамилия, инициалы) из «Голодный С.А.», «(Голодный С.А.)» или «Голодный Сергей Александрович»."""
    text = re.sub(r"[()\[\],;]", " ", str(name or "")).replace(".", " ")
    parts = [p for p in text.split() if p]
    if not parts:
        return "", ""
    return parts[0].casefold(), "".join(p[0] for p in parts[1:]).casefold()


def _looks_like_name(text: str) -> bool:
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]{3,}", text)) and not parse_date(text)


def _auto_status_action(raw: str) -> dict[str, Any]:
    low = raw.casefold()
    if re.search(r"демонтир|снят|демонтаж", low):
        return {"action": "removed"}
    if re.search(r"установлен|стоит|на объект", low):
        return {"action": "installed"}
    if re.search(r"не работает|брак|битый|битые|неисправ|дефект|сломан", low):
        return {"action": "state", "role": eq.ROLE_DEFECT}
    if re.search(r"исправ|работает|^нов|новый|новые|\bок\b|ok", low):
        return {"action": "state", "role": eq.ROLE_OK}
    return {"action": "ignore"}


# ----------------------------------------------------------------------------------------------------- анализ

def category_guess(filename: str, sheet: str) -> str:
    hay = f"{filename} {sheet}".casefold()
    for rx, name in CATEGORY_HINTS:
        if re.search(rx, hay):
            return name
    return ""


def analyze_sheet(sh: Sheet, header_row: Optional[int] = None) -> dict[str, Any]:
    """Структура листа и предложенное сопоставление столбцов."""
    rows = sh.rows
    h = detect_header_row(rows) if header_row is None else max(0, min(header_row - 1, len(rows) - 1))
    headers = [_header_text(c) for c in rows[h]]
    data = [r for r in rows[h + 1:] if any(c is not None for c in r)]
    width = len(headers)
    columns: list[dict[str, Any]] = []
    used: set[str] = set()
    rownum_col = None
    for i in range(width):
        values = [r[i] for r in data if i < len(r) and r[i] not in (None, "")]
        texts = [_text(v) for v in values]
        distinct: dict[str, int] = {}
        for t in texts:
            distinct[t] = distinct.get(t, 0) + 1
        filled = len(values)
        col = {
            "index": i, "header": headers[i], "filled": filled,
            "unique_ratio": round(len(distinct) / filled, 3) if filled else 0,
            "sample": texts[:SAMPLE_ROWS], "role": "ignore", "field": "",
            "distinct": distinct if 0 < len(distinct) <= MAX_DISTINCT else None,
        }
        low = headers[i].casefold()
        if low in ("№", "n", "no", "№ п/п", "п/п", "#"):
            rownum_col = i
            columns.append(col)
            continue
        if filled == 0 and not headers[i]:
            columns.append(col)
            continue
        role = _role_by_header(low)
        if role and role in SINGLE_ROLES and role in used:
            role = "extra" if headers[i] else None
        if role:
            col["role"] = role
            used.add(role)
            if role == "extra":
                col["field"] = headers[i]
        elif headers[i] and filled:
            col["role"], col["field"] = "extra", headers[i]
        columns.append(col)

    # «Кому выдано» с датами и «Дата выдачи» с фамилиями (в таблицах это бывает перепутано) — меняем роли местами.
    def _frac(col, pred) -> float:
        vals = [s for s in (col["distinct"] or {}) for _ in range(1)] if col["distinct"] else col["sample"]
        return (sum(1 for v in vals if pred(v)) / len(vals)) if vals else 0.0

    persons = [c for c in columns if c["role"] == "person"]
    issued_cols = [c for c in columns if c["role"] == "issued_date"]
    for pc in persons:
        if _frac(pc, lambda v: bool(parse_date(v)) and not _looks_like_name(v)) >= 0.7:
            twin = next((c for c in issued_cols if _frac(c, _looks_like_name) >= 0.7), None)
            if twin is not None:
                pc["role"], twin["role"] = "issued_date", "person"
            elif "issued_date" not in used:
                pc["role"] = "issued_date"
                used.discard("person")
                used.add("issued_date")
    notes_hints: list[str] = []
    if any(re.search(r"количеств|остаток", c["header"].casefold()) for c in columns):
        notes_hints.append("Похоже на журнал выдачи без серийных номеров («Количество», «Остаток») — для учёта по номерам не подходит.")

    # Нет серийного номера по заголовку — берём столбец почти без повторов (наименование с кодами, «Заводской № АПК»…).
    # Столбцы, у которых уже есть понятная роль (комплекс, монтёр, даты), не трогаем.
    if "serial" not in used and data and not notes_hints:
        best = None
        for c in columns:
            if c["filled"] < 0.5 * len(data) or c["unique_ratio"] < 0.9 or c["index"] == rownum_col:
                continue
            if c["role"] not in ("ignore", "model"):
                continue
            if c["sample"] and all(re.search(r"^\d+\s*шт", s) for s in c["sample"]):
                continue
            if all(parse_date(s) for s in c["sample"]) and c["sample"]:
                continue
            if best is None or c["unique_ratio"] * c["filled"] > best["unique_ratio"] * best["filled"]:
                best = c
        if best is not None:
            if best["role"] in SINGLE_ROLES:
                used.discard(best["role"])
            best["role"], best["field"] = "serial", ""
            used.add("serial")

    # «Статус» с десятками разных значений («03.10.2025 передал в ТО»…) — это свободный текст: в дополнительное поле.
    for c in columns:
        if c["role"] == "status" and c["filled"] and c["distinct"] is None:
            c["role"], c["field"] = "extra", (c["header"] or "Статус") + " (из таблицы)"
            used.discard("status")
    # Значения столбца «статус»: предлагаем действие для каждого.
    status_suggest: dict[str, dict[str, Any]] = {}
    for c in columns:
        if c["role"] == "status" and c["distinct"]:
            for raw in c["distinct"]:
                status_suggest[raw.casefold()] = {"value": raw, **_auto_status_action(raw)}
    has_serial = "serial" in used
    models = [c for c in columns if c["role"] == "model" and c["sample"]]
    can_auto = (not has_serial and not notes_hints and bool(models)
                and any(c["role"] in ("point", "person", "installed_date", "issued_date") and c["filled"] for c in columns))
    prefix = ""
    if models:
        prefix = re.sub(r"\s+", " ", models[0]["sample"][0]).strip()[:24]
    if not has_serial and not can_auto and not notes_hints:
        notes_hints.append("Не найден столбец с серийным номером — выберите его в списке столбцов или включите автонумерацию.")
    guessed = category_guess(sh.file, sh.name)
    return {
        "file": sh.file, "sheet": sh.name, "key": sh.key, "rows_total": len(rows), "data_rows": len(data),
        "header_row": h + 1, "columns": columns, "category": guessed,
        "target": "panels" if guessed == "Солнечная панель" else "equipment",
        "has_serial": has_serial, "rownum_col": rownum_col, "status_suggest": status_suggest,
        "notes": notes_hints, "enabled": (has_serial or can_auto) and not any("журнал выдачи" in n for n in notes_hints),
        "auto_serial": can_auto, "serial_prefix": prefix,
        "preview": [[_text(c) for c in r[:width]] for r in rows[max(0, h - 1): h + 1 + 3]],
    }


def analyze(files: list[tuple[str, bytes]], header_rows: Optional[dict[str, int]] = None) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, raw in files:
        try:
            for sh in load_sheets(raw, name):
                out.append(analyze_sheet(sh, (header_rows or {}).get(sh.key)))
        except ImportError_ as exc:
            errors.append(exc.detail)
    return {"sheets": out, "errors": errors, "roles": ROLES}


# ----------------------------------------------------------------------------------------------------- применение

@dataclass
class Rec:
    file: str
    sheet: str
    row: int
    serial: str
    category: str
    model: str
    point_raw: str = ""
    point_inv_raw: str = ""
    status_raw: str = ""
    person_raw: str = ""
    issued_date: Optional[str] = None
    installed_date: Optional[str] = None
    received_date: Optional[str] = None
    installed_by: str = ""
    notes: str = ""
    extras: dict[str, str] = field(default_factory=dict)
    full_serial: str = ""


def _strip_spaces(s: str) -> str:
    return re.sub(r"\s+", "", s)


def build_records(sh: Sheet, cfg: dict[str, Any], warnings: list[str]) -> tuple[list[Rec], dict[str, int]]:
    """Строки листа → записи по настройкам. Возвращает (записи, счётчики пропусков)."""
    cols: dict[int, dict[str, Any]] = {int(k): v for k, v in (cfg.get("columns") or {}).items()}
    h = max(0, min(int(cfg.get("header_row") or 1) - 1, len(sh.rows) - 1))
    headers = [_header_text(c) for c in sh.rows[h]]
    role_cols: dict[str, int] = {}
    for idx, c in cols.items():
        r = c.get("role")
        if r in SINGLE_ROLES and r not in role_cols:
            role_cols[r] = idx
    extras_cols = {idx: (c.get("field") or "").strip() for idx, c in cols.items() if c.get("role") == "extra" and (c.get("field") or "").strip()}
    wide_cols = {idx: c for idx, c in cols.items() if c.get("role") == "serial_of" and (c.get("category") or "").strip()}
    rownum_col = cfg.get("rownum_col")
    default_cat = (cfg.get("category") or "").strip()
    default_model = (cfg.get("model") or "").strip()
    auto_serial = bool(cfg.get("auto_serial"))
    prefix = (cfg.get("serial_prefix") or "").strip() or (default_cat or default_model or "Ед.")
    strip_spaces = cfg.get("strip_spaces", True)
    stats = {"empty": 0, "no_serial": 0, "repeated_header": 0, "title": 0}
    recs: list[Rec] = []

    def get(row: list[Any], role: str) -> Any:
        idx = role_cols.get(role)
        return row[idx] if idx is not None and idx < len(row) else None

    header_set = {x.casefold() for x in headers if x}
    for offset, row in enumerate(sh.rows[h + 1:], start=h + 2):
        if not any(c is not None for c in row):
            stats["empty"] += 1
            continue
        texts = {_text(c).casefold() for c in row if c is not None}
        if len(texts & header_set) >= max(2, len(header_set) // 2):
            stats["repeated_header"] += 1     # повторная шапка таблицы посреди листа
            continue
        # Серийные номера строки: основной столбец и/или «широкие» столбцы (по категории на столбец).
        serial_items: list[tuple[str, str, str]] = []   # (serial, category, model)
        category = _text(get(row, "category")) or default_cat
        model = _text(get(row, "model")) or default_model
        raw_serial = _text(get(row, "serial"))
        if raw_serial:
            serial_items.append((raw_serial, category, model))
        for idx, c in wide_cols.items():
            v = _text(row[idx]) if idx < len(row) else ""
            if v:
                serial_items.append((v, c["category"].strip(), (c.get("model") or "").strip() or model))
        if not serial_items:
            if auto_serial and (get(row, "model") is not None or get(row, "point") is not None or get(row, "person") is not None
                                or get(row, "installed_date") is not None or get(row, "issued_date") is not None):
                num = _text(row[rownum_col]) if rownum_col is not None and rownum_col < len(row) and row[rownum_col] is not None else str(offset)
                serial_items.append((f"{prefix}-{num.zfill(4)}", category, model))
            else:
                stats["no_serial"] += 1
                continue
        issued, installed, received = parse_date(get(row, "issued_date")), parse_date(get(row, "installed_date")), parse_date(get(row, "received_date"))
        for role_name, parsed in (("issued_date", issued), ("installed_date", installed), ("received_date", received)):
            raw_date = get(row, role_name)
            raw_txt = _text(raw_date)
            if (raw_date not in (None, "") and parsed is None and len(warnings) < 200 and len(raw_txt) >= 6
                    and any(ch.isdigit() for ch in raw_txt) and not _looks_like_name(raw_txt)):
                warnings.append(f"{sh.file} / {sh.name}, строка {offset}: дата «{raw_txt}» не распознана — пропущена")
        person_raw, installed_by = _text(get(row, "person")), _text(get(row, "installed_by"))
        # В таблицах бывает перепутано «Кому выдано» / «Дата выдачи»: имя в столбце даты и дата в столбце имени,
        # либо в «Выдано» стоит только дата.
        if person_raw and parse_date(person_raw) and not _looks_like_name(person_raw):
            swapped = _text(get(row, "issued_date"))
            if swapped and _looks_like_name(swapped) and not parse_date(swapped):
                person_raw, issued = swapped, parse_date(person_raw)
            else:
                issued, person_raw = issued or parse_date(person_raw), ""
        notes = _text(get(row, "notes"))
        extras = {name: _text(row[idx]) for idx, name in extras_cols.items() if idx < len(row) and row[idx] not in (None, "")}
        for serial, cat, mdl in serial_items:
            full = serial
            if strip_spaces:
                serial = _strip_spaces(serial)
            if not cat and cfg.get("target") == "panels":
                cat = "Солнечная панель"      # у листа панелей категории нет — это вкладка «Солнечные панели»
            if not cat:
                warnings.append(f"{sh.file} / {sh.name}, строка {offset}: не указана категория — строка пропущена")
                stats["no_serial"] += 1
                continue
            recs.append(Rec(
                file=sh.file, sheet=sh.name, row=offset, serial=serial, category=cat, model=mdl or "Без модели",
                point_raw=_text(get(row, "point")), point_inv_raw=_text(get(row, "point_inv")), status_raw=_text(get(row, "status")),
                person_raw=person_raw, issued_date=issued, installed_date=installed, received_date=received,
                installed_by=installed_by, notes=notes, extras=dict(extras), full_serial=full,
            ))
    return recs, stats


@dataclass
class Ctx:
    conn: Any
    mode: str
    unknown_point: str
    performed_by: Optional[str]
    status_map: dict[str, dict[str, Any]]
    points: dict[str, str] = field(default_factory=dict)          # ключ → id комплекса
    point_ids: set[str] = field(default_factory=set)
    people: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    serials: dict[str, int] = field(default_factory=dict)         # serial.casefold() → id оборудования
    cats: dict[str, dict[str, Any]] = field(default_factory=dict)  # name.casefold() → {id, kind}
    models: dict[tuple[int, str], int] = field(default_factory=dict)
    fields_: dict[str, int] = field(default_factory=dict)
    states: dict[str, int] = field(default_factory=dict)
    panels: dict[str, tuple[int, Optional[str]]] = field(default_factory=dict)   # номер панели → (id, комплекс)
    panel_points: set[str] = field(default_factory=set)
    report: dict[str, Any] = field(default_factory=dict)


async def _load_ctx(conn, cfg: dict[str, Any], performed_by: Optional[str]) -> Ctx:
    ctx = Ctx(conn=conn, mode="overwrite" if cfg.get("mode") == "overwrite" else "merge",
              unknown_point="stock" if cfg.get("unknown_point") == "stock" else "skip", performed_by=performed_by,
              status_map={str(k).casefold(): v for k, v in (cfg.get("status_map") or {}).items()})
    cur = await conn.execute("select id, inventory_number from points")
    dup: set[str] = set()
    for pid, inv in await cur.fetchall():
        ctx.point_ids.add(pid)
        for k in (point_key(pid), point_key(inv) if inv else ""):
            if not k:
                continue
            if k in ctx.points and ctx.points[k] != pid:
                dup.add(k)
            ctx.points[k] = pid
    for k in dup:
        ctx.points.pop(k, None)    # один ключ у двух комплексов — не угадываем
    cur = await conn.execute("select id, full_name, role_type from people where active = 1")
    for pid, name, role in await cur.fetchall():
        ctx.people.setdefault(person_key(name), []).append({"id": pid, "full_name": name, "role": role})
    cur = await conn.execute("select id, serial_number from equipment")
    ctx.serials = {(s or "").casefold(): i for i, s in await cur.fetchall()}
    cur = await conn.execute("select id, name, kind from equipment_categories")
    ctx.cats = {(n or "").casefold(): {"id": i, "kind": k or ""} for i, n, k in await cur.fetchall()}
    cur = await conn.execute("select id, category_id, name from equipment_models")
    ctx.models = {(c, (n or "").casefold()): i for i, c, n in await cur.fetchall()}
    cur = await conn.execute("select id, name from equipment_fields")
    ctx.fields_ = {(n or "").casefold(): i for i, n in await cur.fetchall()}
    cur = await conn.execute("select id, name from equipment_statuses")
    ctx.states = {(n or "").casefold(): i for i, n in await cur.fetchall()}
    cur = await conn.execute("select id, panel_number, point_id from solar_panels")
    for i, n, p in await cur.fetchall():
        ctx.panels[(n or "").casefold()] = (i, p)
        if p:
            ctx.panel_points.add(p)
    return ctx


def _count(report: dict[str, Any], key: str, n: int = 1) -> None:
    report["totals"][key] = report["totals"].get(key, 0) + n


def _find_person(ctx: Ctx, raw: str) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    sur, ini = person_key(raw)
    if not sur:
        return None
    found = [p for (s, i), ps in ctx.people.items() if s == sur and (not ini or not i or i.startswith(ini) or ini.startswith(i)) for p in ps]
    found = [p for p in found if p["role"] == "fitter"] or found
    return found[0] if len(found) == 1 else None


def _find_point(ctx: Ctx, rec: Rec) -> Optional[str]:
    for raw in (rec.point_raw, rec.point_inv_raw):
        if not raw or re.search(r"[;,]", raw):
            continue
        pid = ctx.points.get(point_key(raw))
        if pid:
            return pid
    return None


async def _category_id(ctx: Ctx, name: str) -> tuple[int, str]:
    key = name.casefold()
    got = ctx.cats.get(key)
    if got is None:
        cid = await repo.category_by_name_conn(ctx.conn, name)
        got = ctx.cats[key] = {"id": cid, "kind": ""}
        _count(ctx.report, "categories_created")
        ctx.report["created_names"]["categories"].append(name)
    return got["id"], got["kind"]


async def _model_id(ctx: Ctx, category_id: int, name: str, kind: str) -> int:
    key = (category_id, name.casefold())
    mid = ctx.models.get(key)
    if mid is None:
        mid = await repo.model_by_name_conn(ctx.conn, category_id, name)
        ctx.models[key] = mid
        _count(ctx.report, "models_created")
    return mid


async def _field_id(ctx: Ctx, name: str) -> int:
    key = name.casefold()
    fid = ctx.fields_.get(key)
    if fid is None:
        fid = await repo.field_by_name_conn(ctx.conn, name)
        ctx.fields_[key] = fid
        _count(ctx.report, "fields_created")
        ctx.report["created_names"]["fields"].append(name)
    return fid  # type: ignore[return-value]


async def _state_id(ctx: Ctx, spec: dict[str, Any]) -> Optional[int]:
    if spec.get("role"):
        return await repo.role_status_id_conn(ctx.conn, spec["role"])
    if spec.get("state_id"):
        return int(spec["state_id"])
    name = (spec.get("state_name") or "").strip()
    if name:
        sid = ctx.states.get(name.casefold())
        if sid is None:
            sid = await repo.status_by_name_conn(ctx.conn, name)
            ctx.states[name.casefold()] = sid
            _count(ctx.report, "states_created")
            ctx.report["created_names"]["states"].append(name)
        return sid
    return None


async def _apply_rec(ctx: Ctx, rec: Rec, sheet_report: dict[str, Any]) -> None:
    conn, report = ctx.conn, ctx.report
    where = f"{rec.file} / {rec.sheet}, строка {rec.row}"
    cat_id, kind = await _category_id(ctx, rec.category)
    is_sim = kind == eq.CATEGORY_KIND_SIM
    serial = eq.sim_key(rec.serial) if is_sim else rec.serial
    if not serial:
        sheet_report["skipped"] += 1
        return
    seen: set[str] = sheet_report["_seen"]
    if serial.casefold() in seen:
        sheet_report["skipped"] += 1
        sheet_report["warnings"].append(f"{where}: №{serial} повторяется в этом листе — пропущено")
        return
    seen.add(serial.casefold())
    extras = dict(rec.extras)
    if is_sim and len("".join(c for c in rec.full_serial if c.isdigit())) > 7:
        extras.setdefault("Полный номер SIM (ICCID)", re.sub(r"\s+", "", rec.full_serial))

    # ---- что хотим: где должно оказаться оборудование
    status_spec = ctx.status_map.get(rec.status_raw.casefold()) if rec.status_raw else None
    action = (status_spec or {}).get("action", "ignore")
    point_id = _find_point(ctx, rec)
    person = _find_person(ctx, rec.person_raw)
    has_point_text = bool(rec.point_raw or rec.point_inv_raw)
    if has_point_text and not point_id:
        label = rec.point_raw or rec.point_inv_raw
        report["unmatched_points"][label] = report["unmatched_points"].get(label, 0) + 1
        if ctx.unknown_point == "skip":
            sheet_report["skipped"] += 1
            _count(report, "skipped_no_point")      # поимённо — в списке «комплексы, которых нет в календаре»
            return
    if rec.person_raw and person is None and not point_id:
        report["unmatched_people"][rec.person_raw] = report["unmatched_people"].get(rec.person_raw, 0) + 1
    place, want_state = eq.PLACE_STOCK, await _state_id(ctx, status_spec) if action == "state" else None
    removed_from = None
    if action == "removed":
        place = eq.PLACE_STOCK
        removed_from = point_id or None
        want_state = want_state or await repo.role_status_id_conn(conn, eq.ROLE_DEFECT)
    elif point_id:
        place = eq.PLACE_POINT
    elif person is not None:
        place = eq.PLACE_PERSON

    # ---- единица оборудования
    existing_id = ctx.serials.get(serial.casefold())
    item = await repo.get_item_conn(conn, existing_id) if existing_id else None
    day = rec.installed_date or rec.issued_date or rec.received_date or repo.today()
    installer = rec.installed_by or (person["full_name"] if person else None) or (rec.person_raw or None)
    note_bits = [rec.notes]
    if rec.person_raw and person is None:
        note_bits.append(f"Выдано (по таблице): {rec.person_raw}")
    if has_point_text and not point_id:
        note_bits.append(f"Комплекс по таблице: {rec.point_raw or rec.point_inv_raw}")
    note = "; ".join(b for b in note_bits if b) or None
    src = f"Импорт из Excel: {rec.file}, лист «{rec.sheet}», строка {rec.row}"

    if item is None:
        model_id = await _model_id(ctx, cat_id, rec.model, kind)
        ok_state = await repo.role_status_id_conn(conn, eq.ROLE_OK)
        state = want_state or ok_state
        now = repo.now_iso()
        cur = await conn.execute(
            "insert into equipment (model_id, serial_number, place, status_id, received_date, issued_date, notes, created_at, updated_at) "
            "values (?, ?, 'stock', ?, ?, ?, ?, ?, ?)",
            (model_id, serial, state, rec.received_date or (rec.issued_date if place != eq.PLACE_POINT else None), None, note, now, now),
        )
        eid = cur.lastrowid
        ctx.serials[serial.casefold()] = eid
        item = await repo.get_item_conn(conn, eid)
        await repo.add_history_conn(conn, eq.ACT_IMPORT_XLSX, item=item, performed_by=ctx.performed_by, comment=src, event_date=day)
        sheet_report["created"] += 1
        _count(report, "created")
        is_new = True
        if place == eq.PLACE_STOCK:         # монтёр раньше поставил такое оборудование на комплекс по последним символам номера — привязываем (пробный прогон откатывается целиком)
            if await svc.link_pending_conn(conn, item):
                _count(report, "linked_pending")
                item = await repo.get_item_conn(conn, eid)
    else:
        is_new = False
        if item["category_id"] != cat_id:
            sheet_report["warnings"].append(f"{where}: №{serial} уже есть в категории «{item['category_name']}» (в таблице — «{rec.category}»)")
        if ctx.mode == "overwrite" and want_state:
            await conn.execute("update equipment set status_id = ?, updated_at = ? where id = ?", (want_state, repo.now_iso(), item["id"]))
        sets, vals = [], []
        if note and not item["notes"]:
            sets.append("notes = ?")
            vals.append(note)
        if rec.received_date and not item["received_date"]:
            sets.append("received_date = ?")
            vals.append(rec.received_date)
        if sets:
            await conn.execute(f"update equipment set {', '.join(sets)}, updated_at = ? where id = ?", (*vals, repo.now_iso(), item["id"]))

    # «Демонтирован» по таблице: оборудование на складе помнит, с какого комплекса снято (для новой единицы — всегда,
    # для известной — если загрузка разрешена менять данные).
    if removed_from and (is_new or ctx.mode == "overwrite") and item["removed_point_id"] != removed_from:
        await conn.execute(
            "update equipment set removed_point_id = ?, removed_date = coalesce(?, removed_date), updated_at = ? where id = ?",
            (removed_from, rec.installed_date or rec.issued_date, repo.now_iso(), item["id"]),
        )
        item = await repo.get_item_conn(conn, item["id"])

    # ---- место
    moved = False
    if place != item["place"] or (place == eq.PLACE_POINT and item["point_id"] != point_id) or (place == eq.PLACE_PERSON and item["person_id"] != (person or {}).get("id")):
        if is_new or ctx.mode == "overwrite":
            moved = await _move(ctx, item, place, point_id, person, installer, day, removed_from, rec, src, is_sim, sheet_report, where)
        else:
            _count(report, "conflicts")
            if len(report["conflict_samples"]) < 25:
                report["conflict_samples"].append({
                    "serial": serial, "now": item["place_label"] + (f" · {item['point_id']}" if item["point_id"] else ""),
                    "in_file": {eq.PLACE_POINT: f"на комплексе {point_id}", eq.PLACE_PERSON: f"у {person['full_name']}" if person else "", eq.PLACE_STOCK: "на складе"}[place],
                })
    if not is_new:
        if moved or (want_state and ctx.mode == "overwrite"):
            sheet_report["updated"] += 1
            _count(report, "updated")
        else:
            sheet_report["existing"] += 1
            _count(report, "existing")
    if moved:
        _count(report, {eq.PLACE_POINT: "placed_point", eq.PLACE_PERSON: "placed_person", eq.PLACE_STOCK: "placed_stock"}[place])
    elif is_new:
        _count(report, "placed_stock")

    # ---- дополнительные поля
    if extras:
        values = {}
        for fname, fval in extras.items():
            values[await _field_id(ctx, fname)] = fval
        await repo.set_extra_conn(conn, item["id"], values, only_blank=(ctx.mode == "merge" and not is_new))


async def _move(ctx: Ctx, item, place, point_id, person, installer, day, removed_from, rec: Rec, src: str, is_sim: bool, sheet_report, where) -> bool:
    conn = ctx.conn
    ok = await repo.role_status_id_conn(conn, eq.ROLE_OK)
    if place == eq.PLACE_POINT and point_id:
        if is_sim:
            others = [s for s in await repo.find_installed_sim_conn(conn, point_id) if s["id"] != item["id"]]
            if others:
                if ctx.mode != "overwrite":
                    sheet_report["warnings"].append(f"{where}: на комплексе «{point_id}» уже стоит SIM {others[0]['serial_number']} — №{item['serial_number']} оставлена на складе")
                    return False
                for o in others:
                    await svc._detach_conn(conn, o, point_id=point_id, to_person=None, date=day, performed_by=ctx.performed_by,
                                           comment=f"Заменена при импорте на SIM {item['serial_number']}", status_id=ok, action=eq.ACT_DETACH, sync_point=False)
        # снять с прежнего места (комплекс/монтёр) и поставить на новый комплекс
        if item["place"] == eq.PLACE_POINT and item["point_id"] and item["point_id"] != point_id:
            await svc._detach_conn(conn, item, point_id=item["point_id"], to_person=None, date=day, performed_by=ctx.performed_by,
                                   comment="Перенесено при импорте из Excel", status_id=ok, action=eq.ACT_DETACH)
            item = await repo.get_item_conn(conn, item["id"])
        await svc._put_on_point_conn(conn, item, point_id=point_id, date=day, installed_by=installer, performed_by=ctx.performed_by,
                                     action=eq.ACT_ATTACH, comment=src)
        if is_sim:
            await svc._set_point_sim_conn(conn, point_id, item["serial_number"])
        return True
    if place == eq.PLACE_PERSON and person is not None:
        if item["place"] == eq.PLACE_POINT and item["point_id"]:
            await svc._detach_conn(conn, item, point_id=item["point_id"], to_person=None, date=day, performed_by=ctx.performed_by,
                                   comment="Перенесено при импорте из Excel", status_id=ok, action=eq.ACT_DETACH)
        await conn.execute(
            "update equipment set place = 'person', person_id = ?, held_reason = 'issued', point_id = NULL, issued_date = ?, "
            "installed_date = NULL, installed_by = NULL, updated_at = ? where id = ?",
            (person["id"], rec.issued_date or day, repo.now_iso(), item["id"]),
        )
        await repo.add_history_conn(conn, eq.ACT_ISSUE, item=item, person_id=person["id"], person_name=person["full_name"],
                                    performed_by=ctx.performed_by, comment=src, event_date=rec.issued_date or day)
        return True
    if place == eq.PLACE_STOCK:
        if item["place"] == eq.PLACE_POINT and item["point_id"]:
            await svc._detach_conn(conn, item, point_id=item["point_id"], to_person=None, date=day, performed_by=ctx.performed_by,
                                   comment=src, status_id=None, action=eq.ACT_DETACH)
            return True
        if item["place"] == eq.PLACE_PERSON:
            await conn.execute(
                "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, updated_at = ? where id = ?",
                (repo.now_iso(), item["id"]),
            )
            await repo.add_history_conn(conn, eq.ACT_RETURN, item=item, person_id=item["person_id"], person_name=item["person_name"],
                                        performed_by=ctx.performed_by, comment=src, event_date=day)
            return True
        if removed_from:
            await conn.execute(
                "update equipment set removed_point_id = ?, removed_date = coalesce(removed_date, ?), updated_at = ? where id = ?",
                (removed_from, day, repo.now_iso(), item["id"]),
            )
    return False


async def _apply_panel_rec(ctx: Ctx, rec: Rec, sheet_report: dict[str, Any]) -> None:
    """Лист с солнечными панелями → вкладка «Солнечные панели» (номер панели, комплекс, примечание), а не оборудование."""
    conn = ctx.conn
    number = rec.serial
    seen: set[str] = sheet_report["_seen"]
    if number.casefold() in seen:
        sheet_report["skipped"] += 1
        sheet_report["warnings"].append(f"{rec.file} / {rec.sheet}, строка {rec.row}: панель {number} повторяется в этом листе — пропущено")
        return
    seen.add(number.casefold())
    point_id = _find_point(ctx, rec)
    if point_id and point_id in ctx.panel_points:
        sheet_report["warnings"].append(f"{rec.file} / {rec.sheet}, строка {rec.row}: к комплексу «{point_id}» уже привязана другая панель — {number} оставлена без комплекса")
        point_id = None
    bits = [f"Статус: {rec.status_raw}" if rec.status_raw else "", rec.notes] + [f"{k}: {v}" for k, v in rec.extras.items()]
    if rec.person_raw:
        bits.append(f"Выдано: {rec.person_raw}")
    notes = "; ".join(b for b in bits if b) or None
    now = repo.now_iso()
    got = ctx.panels.get(number.casefold())
    if got is None:
        cur = await conn.execute(
            "insert into solar_panels (panel_number, point_id, location, notes, at_warehouse, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
            (number, point_id, None, notes, 0 if point_id else 1, now, now),
        )
        ctx.panels[number.casefold()] = (cur.lastrowid, point_id)
        if point_id:
            ctx.panel_points.add(point_id)
        sheet_report["created"] += 1
        _count(ctx.report, "panels_created")
        return
    pid, cur_point = got
    changed = False
    if ctx.mode == "overwrite":
        await conn.execute("update solar_panels set notes = coalesce(?, notes), updated_at = ? where id = ?", (notes, now, pid))
        changed = notes is not None
        if point_id and not cur_point:
            await conn.execute("update solar_panels set point_id = ?, at_warehouse = 0 where id = ?", (point_id, pid))
            ctx.panels[number.casefold()] = (pid, point_id)
            ctx.panel_points.add(point_id)
            changed = True
    else:
        cur = await conn.execute("select notes from solar_panels where id = ?", (pid,))
        row = await cur.fetchone()
        if notes and not (row and row[0]):
            await conn.execute("update solar_panels set notes = ?, updated_at = ? where id = ?", (notes, now, pid))
            changed = True
    if changed:
        sheet_report["updated"] += 1
        _count(ctx.report, "panels_updated")
    else:
        sheet_report["existing"] += 1
        _count(ctx.report, "existing")


def _new_report(dry_run: bool, mode: str) -> dict[str, Any]:
    return {
        "dry_run": dry_run, "mode": mode, "sheets": [], "totals": {}, "unmatched_points": {}, "unmatched_people": {},
        "conflict_samples": [], "created_names": {"categories": [], "fields": [], "states": []}, "warnings": [],
    }


async def run_import(
    files: list[tuple[str, bytes]], config: dict[str, Any], *, dry_run: bool = True, performed_by: Optional[str] = None,
) -> dict[str, Any]:
    """Применяет настройки к строкам. dry_run — всё делается в транзакции и откатывается (отчёт тот же, что при записи)."""
    by_file = dict(files)
    sheets_cfg = [s for s in (config.get("sheets") or []) if s.get("enabled", True)]
    if not sheets_cfg:
        raise ImportError_("Не выбрано ни одного листа для загрузки")
    loaded: dict[str, Sheet] = {}
    for name, raw in files:
        for sh in load_sheets(raw, name):
            loaded[sh.key] = sh
    async with get_connection() as conn:
        ctx = await _load_ctx(conn, config, performed_by)
        ctx.report = report = _new_report(dry_run, ctx.mode)
        try:
            for scfg in sheets_cfg:
                key = f"{scfg.get('file')}::{scfg.get('sheet')}"
                sh = loaded.get(key)
                if sh is None:
                    report["warnings"].append(f"Лист «{scfg.get('sheet')}» файла «{scfg.get('file')}» не найден среди загруженных")
                    continue
                warnings: list[str] = []
                recs, stats = build_records(sh, scfg, warnings)
                sheet_report = {
                    "file": sh.file, "sheet": sh.name, "records": len(recs), "created": 0, "updated": 0, "existing": 0, "skipped": 0,
                    "stats": stats, "warnings": warnings, "_seen": set(),
                }
                for rec in recs:
                    try:
                        if scfg.get("target") == "panels":
                            await _apply_panel_rec(ctx, rec, sheet_report)
                        else:
                            await _apply_rec(ctx, rec, sheet_report)
                    except svc.EquipmentError as exc:
                        sheet_report["skipped"] += 1
                        sheet_report["warnings"].append(f"{rec.file} / {rec.sheet}, строка {rec.row}: {exc.detail}")
                sheet_report.pop("_seen", None)
                report["sheets"].append(sheet_report)
            if dry_run:
                await conn.rollback()
            else:
                await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    # Отчёт короче: предупреждения по листам обрезаем, общий список собираем.
    for sh_rep in report["sheets"]:
        report["warnings"].extend(sh_rep["warnings"][:15])
        if len(sh_rep["warnings"]) > 15:
            report["warnings"].append(f"{sh_rep['file']} / {sh_rep['sheet']}: ещё предупреждений — {len(sh_rep['warnings']) - 15}")
        sh_rep["warnings"] = sh_rep["warnings"][:15]
    report["unmatched_points"] = dict(sorted(report["unmatched_points"].items(), key=lambda kv: -kv[1])[:40])
    report["unmatched_people"] = dict(sorted(report["unmatched_people"].items(), key=lambda kv: -kv[1])[:40])
    return report
