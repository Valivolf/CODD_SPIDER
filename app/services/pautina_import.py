"""«Импорт из Паутины»: выгрузка комплексов из другой программы → обновление данных УЖЕ заведённых комплексов.

Устроен нестрого, чтобы файл не «падал» от лишнего или недостающего:
  • столбцы ищутся по ЗАГОЛОВКУ (порядок любой, регистр и «ё» не важны, лишние столбцы пропускаются);
  • обязателен один столбец — название/номер комплекса («Наименование», «Номер», «Комплекс»…); всё остальное необязательно:
    хватит названия и «Сертификат поверки до»;
  • значение, которое не удалось разобрать (кривая дата, координата не число), не бросает ошибку на всю строку — поле
    пропускается, в итоговом файле остаётся предупреждение;
  • пустая ячейка ничего не затирает; заполненная заменяет текущее значение;
  • комплекс ищется среди заведённых без учёта регистра/пробелов/знаков и по инвентарному номеру (см. point_matching);
    НОВЫЕ комплексы не создаются — тех, кого нет в системе, программа выписывает в Excel-файл итогов.
Файл итогов (.xlsx): «Итоги», «Не найдены в системе» (со всеми столбцами исходного файла), «Обновлено» (что было → что стало),
«Предупреждения»."""
from __future__ import annotations

import datetime as dt
import io
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.config import get_settings
from app.core.dates import format_date
from app.core.names import fold_name
from app.core.power_source import normalize_power_source
from app.core.text import decode_entities
from app.repositories import point_statuses_repository, points_repository
from app.services.excel_service import _cert_date, _coord, _zip_bomb_error
from app.services.point_matching import PointIndex

MAX_ROWS = 100_000
MAX_FILE_SIZE = 20 * 1024 * 1024
_HEADER_SCAN_ROWS = 25
_REPORT_KEEP_DAYS = 14
_CLEAR_STATUS_WORDS = {"не известно", "неизвестно"}

# Поле → (название для людей, заголовки столбцов в файле). Сравнение по «ключу»: без регистра, ё = е, знаки → пробелы.
FIELDS: dict[str, tuple[str, list[str]]] = {
    "id": ("номер / название комплекса", [
        "наименование", "название", "наименование комплекса", "название комплекса", "номер комплекса", "номер апк",
        "наименование апк", "номер название", "номер точки", "комплекс", "апк", "точка", "номер", "идентификатор", "id", "код", "имя",
    ]),
    "address": ("адрес", ["адрес", "адрес установки", "место установки", "местоположение", "расположение", "место"]),
    "description": ("описание", ["описание", "описание неисправность"]),
    "lat": ("широта", ["широта", "lat", "latitude"]),
    "lon": ("долгота", ["долгота", "lon", "lng", "long", "longitude"]),
    "coords": ("координаты (широта, долгота)", ["координаты", "координата", "широта долгота", "широта и долгота", "gps"]),
    "verification_valid_until": ("сертификат поверки до", [
        "сертификат поверки до", "сертификат поверки", "поверка до", "поверен до", "срок поверки", "дата поверки",
        "окончание поверки", "дата окончания поверки", "срок действия поверки", "срок действия сертификата", "сертификат до",
        "сертификат действует до", "свидетельство о поверке до", "свидетельство о поверке", "действует до", "поверка",
    ]),
    "ip_address": ("IP-адрес", ["ip адрес", "ip", "адрес ip", "ай пи"]),
    "port": ("порт", ["порт", "port", "порт веб интерфейса", "веб порт"]),
    "sim_number": ("номер SIM", ["номер sim", "sim", "сим", "sim карта", "сим карта", "номер сим", "номер сим карты"]),
    "inventory_number": ("инвентарный номер", ["инвентарный номер", "инв номер", "инвентарный", "инв"]),
    "category": ("категория", ["категория", "категория апк", "тип комплекса", "вид комплекса"]),
    "login": ("логин от веб-интерфейса", ["логин от веб интерфейса", "логин", "login"]),
    "password": ("пароль от веб-интерфейса", ["пароль от веб интерфейса", "пароль", "password"]),
    "power_source": ("питание", ["питание сэс электросеть", "питание", "источник питания", "тип питания"]),
    "status": ("статус", ["статус комплекса", "статус", "состояние"]),
}
# Поля, которые можно записать комплексу (coords раскладывается на lat/lon, id — только для поиска).
_WRITABLE = ["address", "description", "lat", "lon", "verification_valid_until", "ip_address", "port", "sim_number",
             "inventory_number", "category", "login", "password", "power_source", "status"]
_LABELS = {k: v[0] for k, v in FIELDS.items()}


def _key(value: Any) -> str:
    s = decode_entities(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]|_", " ", s).split())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d-%m-%Y")
    return decode_entities(value).strip()


def match_header(cells: list[Any]) -> dict[str, int]:
    """Поле → номер столбца по строке заголовков (каждый столбец достаётся одному полю)."""
    keys = [_key(c) for c in cells]
    found: dict[str, int] = {}
    used: set[int] = set()
    for exact in (True, False):
        for name, (_label, aliases) in FIELDS.items():
            if name in found:
                continue
            for alias in aliases:
                hit = next((i for i, k in enumerate(keys)
                            if i not in used and k and (k == alias if exact else k.startswith(alias + " "))), None)
                if hit is not None:
                    found[name] = hit
                    used.add(hit)
                    break
    return found


@dataclass
class FileRow:
    sheet: str
    row: int
    values: dict[str, Any]          # поле → «сырое» значение ячейки
    original: list[Any]             # вся строка как в файле
    headers: list[str]


@dataclass
class Parsed:
    rows: list[FileRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    columns: list[dict[str, str]] = field(default_factory=list)     # распознанные столбцы: {header, field, label}
    ignored: list[str] = field(default_factory=list)                # столбцы, которым в системе нет места
    rows_read: int = 0


def parse(content: bytes) -> Parsed:
    result = Parsed()
    bomb = _zip_bomb_error(content)
    if bomb:
        result.errors.append(bomb)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — показываем человеку, а не 500
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result
    seen_cols: set[str] = set()
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        header_idx, mapping = None, {}
        for i, raw in enumerate(rows[:_HEADER_SCAN_ROWS]):
            candidate = match_header(list(raw))
            if "id" in candidate:
                header_idx, mapping = i, candidate
                break
        if header_idx is None:
            if any(any(c not in (None, "") for c in r) for r in rows):
                result.notes.append(f"Лист «{ws.title}» пропущен: не нашёл столбец с названием или номером комплекса (например «Наименование»).")
            continue
        headers = [_text(c) for c in rows[header_idx]]
        for name, idx in mapping.items():
            sig = f"{ws.title}|{name}"
            if sig not in seen_cols:
                seen_cols.add(sig)
                result.columns.append({"header": headers[idx] if idx < len(headers) else "", "field": name, "label": _LABELS[name], "sheet": ws.title})
        used = set(mapping.values())
        for idx, h in enumerate(headers):
            if h and idx not in used and h not in result.ignored:
                result.ignored.append(h)
        for offset, raw in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            cells = list(raw)
            if all(c in (None, "") for c in cells):
                continue
            result.rows_read += 1
            if result.rows_read > MAX_ROWS:
                result.errors.append(f"Слишком много строк (больше {MAX_ROWS}) — разбейте файл на части.")
                return result
            values = {name: (cells[idx] if idx < len(cells) else None) for name, idx in mapping.items()}
            if not _text(values.get("id")):
                continue                      # строка без названия — примечание под таблицей, пропускаем молча
            result.rows.append(FileRow(sheet=ws.title, row=offset, values=values, original=cells, headers=headers))
    if not result.rows and not result.errors and not result.notes:
        result.errors.append("В файле не найдено строк с названием комплекса")
    return result


# ---------------------------------------------------------------- план
@dataclass
class Plan:
    updates: list[tuple[str, dict[str, Any], dict[str, tuple[str, str]]]] = field(default_factory=list)   # id, новые значения, {поле: (было, стало)}
    unchanged: int = 0
    not_found: list[FileRow] = field(default_factory=list)
    warnings: list[tuple[str, int, str, str]] = field(default_factory=list)                              # лист, строка, комплекс, текст
    repeats: int = 0
    matched: int = 0
    fields_changed: dict[str, int] = field(default_factory=dict)


def _split_coords(text: str) -> Optional[tuple[str, str]]:
    parts = [p for p in re.split(r"[;,\s]+", text.strip()) if p]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _same(field_name: str, old: str, new: str) -> bool:
    if field_name in ("lat", "lon"):
        try:
            return abs(float(old.replace(",", ".")) - float(new)) < 1e-9
        except ValueError:
            return old == new
    return old.strip() == new.strip()


async def build_plan(parsed: Parsed) -> Plan:
    plan = Plan()
    points = await points_repository.list_points()
    index = PointIndex(points)
    status_names = {s["name"].casefold(): s["name"] for s in await point_statuses_repository.list_statuses()}
    per_point: dict[str, dict[str, Any]] = {}        # id → накопленные значения (повторные строки файла дополняют друг друга)
    first_row: dict[str, FileRow] = {}

    for r in parsed.rows:
        label = _text(r.values.get("id"))
        point = index.find(label)
        if point is None:
            plan.not_found.append(r)
            continue
        pid = point["id"]
        plan.matched += 1
        if pid in per_point:
            plan.repeats += 1
            plan.warnings.append((r.sheet, r.row, label, "комплекс повторяется в файле — значения из этой строки дополняют предыдущие"))
        values = per_point.setdefault(pid, {})
        first_row.setdefault(pid, r)

        def warn(text: str, _r=r, _label=label) -> None:
            plan.warnings.append((_r.sheet, _r.row, _label, text))

        raw = r.values
        for name in ("address", "description", "ip_address", "port", "inventory_number", "login", "password", "category"):
            v = _text(raw.get(name))
            if v:
                values[name] = v
        sim = _text(raw.get("sim_number"))
        if sim:
            values["sim_number"] = sim
        # координаты: отдельные столбцы или один «широта, долгота»
        lat_raw, lon_raw = _text(raw.get("lat")), _text(raw.get("lon"))
        if not lat_raw and not lon_raw and _text(raw.get("coords")):
            both = _split_coords(_text(raw.get("coords")))
            if both:
                lat_raw, lon_raw = both
            else:
                warn(f"координаты «{_text(raw.get('coords'))}» не разобраны — пропущены")
        for name, text, lo, hi in (("lat", lat_raw, -90, 90), ("lon", lon_raw, -180, 180)):
            if not text:
                continue
            c = _coord(text)
            if not c:
                warn(f"{_LABELS[name]} «{text}» — не число, поле пропущено")
            elif not lo <= float(c) <= hi:
                warn(f"{_LABELS[name]} «{text}» вне допустимого диапазона, поле пропущено")
            else:
                values[name] = c
        cert_raw = raw.get("verification_valid_until")
        if _text(cert_raw):
            iso = _cert_date(cert_raw)
            if iso:
                values["verification_valid_until"] = iso
            else:
                warn(f"дата сертификата «{_text(cert_raw)}» не распознана, поле пропущено")
        power = _text(raw.get("power_source"))
        if power:
            norm = normalize_power_source(power)
            if norm:
                values["power_source"] = norm
            else:
                warn(f"питание «{power}» — нужно «СЭС» или «Электросеть», поле пропущено")
        status = _text(raw.get("status"))
        if status:
            key = " ".join(status.split()).casefold()
            if key in _CLEAR_STATUS_WORDS:
                values["status"] = ""
            elif key in status_names:
                values["status"] = status_names[key]
            else:
                warn(f"статус «{status}» нет в справочнике статусов, поле пропущено")

    by_id = {p["id"]: p for p in points}
    for pid, values in per_point.items():
        point = by_id[pid]
        changes: dict[str, tuple[str, str]] = {}
        merged = dict(point)
        for name, new in values.items():
            if name == "category":
                old = str(point.get("category_name") or "")
            else:
                old = str(point.get(name) or "")
            if _same(name, old, str(new)):
                continue
            changes[name] = (old, str(new))
            if name == "category":
                merged["category_name"] = new
                merged["category_color"] = ""
            else:
                merged[name] = new
        if changes:
            plan.updates.append((pid, merged, changes))
            for name in changes:
                plan.fields_changed[name] = plan.fields_changed.get(name, 0) + 1
        else:
            plan.unchanged += 1
    return plan


async def apply_plan(plan: Plan) -> int:
    done = 0
    for pid, merged, _changes in plan.updates:
        await points_repository.update_point(pid, merged)
        done += 1
    return done


# ---------------------------------------------------------------- итоги
def summarize(parsed: Parsed, plan: Plan, *, dry_run: bool, filename: str, report_id: Optional[str], applied: Optional[int] = None) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "filename": filename,
        "rows_read": parsed.rows_read,
        "rows_used": len(parsed.rows),
        "matched": plan.matched,
        "to_update": len(plan.updates),
        "updated": applied,
        "unchanged": plan.unchanged,
        "not_found": len(plan.not_found),
        "not_found_sample": [_text(r.values.get("id")) for r in plan.not_found[:30]],
        "warnings": len(plan.warnings),
        "warnings_sample": [f"Лист «{s}», строка {n}: {label} — {text}" for s, n, label, text in plan.warnings[:20]],
        "repeats": plan.repeats,
        "columns": parsed.columns,
        "ignored_columns": parsed.ignored,
        "fields_changed": [{"field": k, "label": _LABELS[k], "count": v} for k, v in sorted(plan.fields_changed.items(), key=lambda kv: -kv[1])],
        "notes": parsed.notes,
        "errors": parsed.errors,
        "report_id": report_id,
    }


_HEAD_FILL = PatternFill("solid", fgColor="1E293B")
_HEAD_FONT = Font(color="F8FAFC", bold=True)


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list[Any]], widths: list[int]) -> None:
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = _HEAD_FILL, _HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append(row)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def build_report(parsed: Parsed, plan: Plan, summary: dict[str, Any]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Итоги"
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 70
    lines: list[tuple[str, Any]] = [
        ("Импорт из «Паутины»", ""),
        ("Файл", summary["filename"]),
        ("Когда", dt.datetime.now().strftime("%d-%m-%Y %H:%M")),
        ("Режим", "ПРОВЕРКА — в систему ничего не записано" if summary["dry_run"] else "загрузка выполнена"),
        ("", ""),
        ("Строк с комплексами в файле", summary["rows_used"]),
        ("Найдено в системе", summary["matched"]),
        ("Обновлено комплексов" if not summary["dry_run"] else "Будет обновлено комплексов", summary["updated"] if summary["updated"] is not None else summary["to_update"]),
        ("Без изменений (данные уже такие же)", summary["unchanged"]),
        ("НЕ найдено в системе (см. лист «Не найдены в системе»)", summary["not_found"]),
        ("Предупреждений (см. лист «Предупреждения»)", summary["warnings"]),
        ("", ""),
        ("Распознанные столбцы файла", ""),
    ]
    for c in parsed.columns:
        lines.append((f"   «{c['header']}»", f"→ {c['label']}"))
    if parsed.ignored:
        lines += [("", ""), ("Столбцы, которых нет в системе (не загружены)", ", ".join(parsed.ignored))]
    for n in parsed.notes:
        lines.append(("Примечание", n))
    for row in lines:
        ws.append(list(row))
    ws["A1"].font = Font(bold=True, size=13)
    for r in range(1, ws.max_row + 1):
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True, vertical="top")

    # не найдены: все столбцы исходного файла
    if plan.not_found:
        headers = plan.not_found[0].headers
        width = max(len(headers), max(len(r.original) for r in plan.not_found))
        head = ["Лист", "Строка"] + [h or f"Столбец {i + 1}" for i, h in enumerate(headers)] + [f"Столбец {i + 1}" for i in range(len(headers), width)]
        rows = [[r.sheet, r.row] + [_text(c) for c in r.original] for r in plan.not_found]
        _sheet(wb, "Не найдены в системе", head, rows, [18, 8] + [24] * width)
    else:
        _sheet(wb, "Не найдены в системе", ["Все комплексы из файла найдены в системе"], [], [60])
    rows = []
    for pid, _merged, changes in plan.updates:
        for name, (old, new) in changes.items():
            rows.append([pid, _LABELS.get(name, name),
                         format_date(old) if name == "verification_valid_until" and old else old,
                         format_date(new) if name == "verification_valid_until" and new else new])
    _sheet(wb, "Обновлено", ["Комплекс", "Поле", "Было", "Стало"], rows, [26, 26, 40, 40])
    _sheet(wb, "Предупреждения", ["Лист", "Строка", "Комплекс", "Что не так"], [[s, n, label, text] for s, n, label, text in plan.warnings], [18, 8, 26, 90])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _reports_dir() -> Path:
    d = get_settings().data_dir / "import_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_report(content: bytes) -> str:
    d = _reports_dir()
    cutoff = time.time() - _REPORT_KEEP_DAYS * 86400
    for old in d.glob("pautina-*.xlsx"):          # старые итоги чистим при каждом новом
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    rid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    (d / f"pautina-{rid}.xlsx").write_bytes(content)
    return rid


def report_path(report_id: str) -> Optional[Path]:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", report_id or ""):
        return None
    p = _reports_dir() / f"pautina-{report_id}.xlsx"
    return p if p.is_file() else None
