"""Excel (.xlsx) import/export.

Two independent flows, mirroring the original app's data sources:
  - Points directory import/template (replaces the old client-side CSV
    import) — just the repair-points reference data: id, address,
    coordinates, description.
  - Daily plan ("план-наряд") export — team/task assignments, which are
    still created via the KML "Конструктор карт" import flow, not Excel.

Uses openpyxl directly (no pandas) to keep the dependency footprint the
same as the original app while moving CSV import to a real .xlsx flow.
"""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Optional

import xlsxwriter
from dateutil import parser as date_parser
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core.text import decode_entities
from app.core.dates import format_date, format_datetime
from app.core.power_source import normalize_power_source
from app.core.trips import is_trip_first_day, trip_visible_crew_ids
from app.core.work_types import is_aux_type

POINT_HEADERS = [
    "Номер / название", "Адрес", "Широта", "Долгота", "Описание / неисправность",
    "IP-адрес", "Порт", "Номер SIM", "Инвентарный номер", "Категория",
    "Сертификат поверки до", "Логин от веб-интерфейса", "Пароль от веб-интерфейса",
    "Питание (СЭС / Электросеть)", "Статус",
]

# .xlsx is a zip archive; without this check a small, highly-compressed
# upload could decompress into gigabytes of XML and exhaust server memory.
MAX_UNCOMPRESSED_BYTES = 80 * 1024 * 1024


def _zip_bomb_error(content: bytes) -> Optional[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total_uncompressed = sum(info.file_size for info in zf.infolist())
    except zipfile.BadZipFile:
        return None  # not a valid zip at all — load_workbook will raise its own clear error
    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
        return "Файл подозрительно большой после распаковки — проверьте, что это корректный .xlsx"
    return None

HEADER_FILL = PatternFill("solid", fgColor="1E293B")
HEADER_FONT = Font(color="F8FAFC", bold=True)


def build_import_template() -> io.BytesIO:
    """Build a blank .xlsx template for the points directory import."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Точки ремонта"
    ws.append(POINT_HEADERS)
    for col_idx in range(1, len(POINT_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([20, 34, 14, 14, 42, 16, 10, 16, 18, 18, 20, 22, 22, 22, 20], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.append(["T-100", "ул. Ленина, 14", "55.7522", "37.6156", "Замена лампы на опоре", "192.168.1.10", "80", "89001234567", "СКАТ-ПП-2511016", "Камера", "31-12-2026", "admin", "admin123", "СЭС", "Работает"])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 100
    lines = [
        "Как заполнять шаблон импорта точек ремонта",
        "",
        "1. Не меняйте порядок и названия столбцов на листе «Точки ремонта».",
        "2. Номер / название — уникальный идентификатор точки. Если точка с таким "
        "номером уже есть в справочнике, её данные будут обновлены; если нет — создана заново.",
        "3. Широта и Долгота — необязательны, числом (можно с запятой или точкой).",
        "4. IP-адрес, Порт, Номер SIM, Инвентарный номер, Сертификат поверки до — "
        "необязательны; пустое значение не затирает то, что уже указано в справочнике "
        "для существующей точки. Так работает импорт в целом: заполненная ячейка "
        "заменяет текущее значение, пустая — оставляет как было.",
        "4а. Категория — необязательна, произвольный текст. Новое название создаст "
        "новую категорию (цвет по умолчанию, его можно сменить в «Настройки → Категории "
        "АПК»); существующее название переиспользует уже заданный цвет.",
        "4б. Сертификат поверки до — до какого числа действует сертификат метрологической "
        "поверки оборудования. Дату можно писать почти в любом виде (31-12-2026, "
        "31.12.2026, 31/12/2026 и т.п.) — везде, где день/месяц можно перепутать, "
        "он читается первым, как в обычной русской записи даты.",
        "4в. Логин/пароль от веб-интерфейса — необязательны; видны и редактируются "
        "только оператором и админом (склад их не видит и в свою выгрузку не получает).",
        "4г. Питание — чем запитан комплекс: «СЭС» или «Электросеть» (необязательно). "
        "Если не указано, бот спросит у монтёра при взятии комплекса в работу.",
        "4д. Статус — статус комплекса из «Настройки → Статусы АПК» («Работает», «Не работает», "
        "«На складе», «Установлен» и т. д.; название как в справочнике, регистр не важен). "
        "Пусто — статус не меняется; «Не известно» — сбросить статус. Статуса, которого нет в "
        "справочнике, импорт не создаёт: строка загрузится, а статус будет пропущен с пометкой в "
        "итоге. Фоновая проверка доступности потом может сама перезаписать «авто»-статусы.",
        "5. Пустые строки и строка-пример (вторая строка) при импорте игнорируются "
        "— замените её своими данными.",
        "6. Сохраните файл в формате .xlsx и загрузите его через кнопку «Импорт Excel».",
        "7. Кнопка «Скачать все точки» выгружает текущий справочник в этом же формате — "
        "удобно как основа для массового редактирования и повторного импорта.",
        "",
        "Привязка точек к бригадам и датам (задания) выполняется отдельно, через "
        "импорт KML из Конструктора карт — кнопка «Карты KML».",
    ]
    for i, line in enumerate(lines, start=1):
        guide.cell(row=i, column=1, value=line)
    guide.cell(row=1, column=1).font = Font(bold=True, size=13)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _format_cert_date(iso_value: Any) -> str:
    """Stored/compared as ISO ("YYYY-MM-DD", see _cert_date) everywhere
    internally, but always shown to a person as "ДД-ММ-ГГГГ" — this is the
    one place that conversion happens for the points export; the re-import
    side (_cert_date, via dateutil) reads it back correctly either way."""
    s = str(iso_value or "")
    try:
        return dt.date.fromisoformat(s).strftime("%d-%m-%Y")
    except ValueError:
        return s


def build_points_export(points: list[dict[str, Any]], include_credentials: bool = False) -> io.BytesIO:
    """Same columns/order as the import template, but filled with the
    current directory instead of one example row — download, edit in
    place, re-upload through the same import: an untouched cell keeps its
    current value, only what actually changed needs filling in.

    `include_credentials` gates the login/password COLUMNS specifically —
    the caller (routes_excel.py) passes True only for admin/user, mirroring
    routes_points.py's _strip_credentials; any other role gets the same
    sheet with those two cells left blank rather than the columns removed,
    so the file still round-trips cleanly through re-import."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Точки ремонта"
    ws.append(POINT_HEADERS)
    for col_idx in range(1, len(POINT_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([20, 34, 14, 14, 42, 16, 10, 16, 18, 18, 20, 22, 22, 22, 20], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    for p in points:
        ws.append([
            decode_entities(p.get("id")), decode_entities(p.get("address")), p.get("lat") or "", p.get("lon") or "",
            decode_entities(p.get("description")), p.get("ip_address") or "", p.get("port") or "",
            p.get("sim_number") or "", decode_entities(p.get("inventory_number")), decode_entities(p.get("category_name")),
            _format_cert_date(p.get("verification_valid_until")),
            (p.get("login") or "") if include_credentials else "",
            (p.get("password") or "") if include_credentials else "",
            p.get("power_source") or "",
            p.get("status") or "",
        ])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@dataclass
class ImportRow:
    row_number: int
    id: str
    address: str
    description: str
    lat: str
    lon: str
    ip_address: str = ""
    port: str = ""
    sim_number: str = ""
    inventory_number: str = ""
    category: str = ""
    verification_valid_until: str = ""
    login: str = ""
    password: str = ""
    power_source: str = ""
    status: str = ""


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _clean(v: Any) -> str:
    """Some source spreadsheets carry Cyrillic text as literal HTML numeric
    entities (e.g. "&#1052;&#1045;..." for "МЕ...") — a copy-paste artifact
    from wherever the cell content originally came from, not something
    openpyxl decodes on its own. See app/core/text.py — a no-op on plain
    text, so this is safe for every other cell."""
    return decode_entities(v).strip()


def _coord(v: Any) -> Optional[str]:
    s = _clean(v)
    if not s:
        return ""
    try:
        return str(float(s.replace(",", ".")))
    except ValueError:
        return None


# A leading 4-digit year ("2026-10-05") is handled separately, below,
# rather than left to dateutil's dayfirst=True — counter-intuitively,
# dateutil applies day-first swapping to whichever two fields are
# ambiguous even when the year is unambiguously first, so
# dateutil.parser.parse("2026-10-05", dayfirst=True) comes back as
# 2026-05-10, silently swapping month/day. Explicitly parsing this shape
# ourselves avoids that trap for anything that's actually ISO-ordered
# (e.g. a file this app itself exported before it switched to ДД-ММ-ГГГГ).
_ISO_SHAPE_RE = re.compile(r"^(\d{4})\D(\d{1,2})\D(\d{1,2})$")
# Otherwise, just a shape check ("3 numeric groups") before handing the
# string to dateutil — without it, a stray single number (e.g. an
# inventory number typed into the wrong column) can get "successfully"
# parsed as a date by borrowing today's month/year for the missing fields,
# silently turning garbage into a plausible-looking certificate date
# instead of a row error.
_DATE_SHAPE_RE = re.compile(r"^\d{1,4}\D+\d{1,4}\D+\d{1,4}$")


def _cert_date(v: Any) -> Optional[str]:
    """Accepts a real Excel date cell (openpyxl hands back a date/datetime
    for those) or free-form text in essentially any common written format —
    any separator (dots/dashes/slashes/spaces), any field order, 2- or
    4-digit year. Ambiguous day/month order (e.g. "05.10.2026") is read
    day-first, matching how a dispatcher actually types a date here; a
    leading 4-digit year ("2026-10-05", e.g. from a file this app itself
    exported before it switched to ДД-ММ-ГГГГ) is read as year-month-day
    instead — see _ISO_SHAPE_RE above for why that can't just be left to
    dateutil's own dayfirst handling. Always returns "YYYY-MM-DD"
    internally — same convention as every other date in this app
    (tasks.date, entered via <input type=date>); "ДД-ММ-ГГГГ" is only how
    it's ever displayed back to a person (see static/js/ui.js's
    certificateBadgeHtml). Returns None for anything unparseable, reported
    as a row error by the caller."""
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = _clean(v)
    if not s:
        return ""
    m = _ISO_SHAPE_RE.match(s)
    if m:
        year, month, day = (int(g) for g in m.groups())
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None
    if not _DATE_SHAPE_RE.match(s):
        return None
    try:
        parsed = date_parser.parse(s, dayfirst=True)
    except (ValueError, OverflowError):
        return None
    return parsed.date().isoformat()


def parse_import_file(content: bytes) -> ImportResult:
    result = ImportResult()
    bomb_error = _zip_bomb_error(content)
    if bomb_error:
        result.errors.append(bomb_error)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a row-less error
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result

    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        result.errors.append("Файл пуст")
        return result

    header = [_clean(c) for c in rows[0]]
    is_template_header = header[:2] == POINT_HEADERS[:2]
    data_rows = rows[1:] if is_template_header else rows

    for offset, raw in enumerate(data_rows, start=2 if is_template_header else 1):
        cells = list(raw) + [None] * (len(POINT_HEADERS) - len(raw))
        (
            point_id, address, lat, lon, description,
            ip_address, port, sim_number, inventory_number, category,
            verification_valid_until, login, password, power_source, status,
        ) = cells[:15]
        if all(c in (None, "") for c in cells):
            continue  # blank row

        point_id = _clean(point_id)
        if not point_id:
            result.errors.append(f"Строка {offset}: не заполнен номер / название точки")
            continue

        lat_v, lon_v = _coord(lat), _coord(lon)
        if lat_v is None or lon_v is None:
            result.errors.append(f"Строка {offset}: неверный формат координат")
            continue

        cert_v = _cert_date(verification_valid_until)
        if cert_v is None:
            result.errors.append(f"Строка {offset}: не удалось распознать дату сертификата поверки")
            continue

        power_v = normalize_power_source(_clean(power_source))
        if power_v is None:
            result.errors.append(f"Строка {offset}: питание — «СЭС» или «Электросеть»")
            continue

        result.rows.append(
            ImportRow(
                row_number=offset,
                id=point_id,
                address=_clean(address),
                description=_clean(description),
                lat=lat_v,
                lon=lon_v,
                ip_address=_clean(ip_address),
                port=_clean(port),
                sim_number=_clean(sim_number),
                inventory_number=_clean(inventory_number),
                category=_clean(category),
                verification_valid_until=cert_v,
                login=_clean(login),
                password=_clean(password),
                power_source=power_v,
                status=_clean(status),
            )
        )
    return result


POINT_PLAN_HEADERS = ["Номер комплекса", "Что нужно сделать", "Группа", "Тип работы"]       # два последних — необязательные


def build_point_plan_import_template() -> io.BytesIO:
    """Blank .xlsx for bulk-adding to «План» (point_plans) — just the point
    number and a free-text description. Тип работы/приоритет are set
    afterwards in the app itself (one at a time or via multi-select), not
    from the file — see routes_point_plans.py's /assign and app.js/apk.js's
    planManagerForm."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Импорт плана"
    ws.append(POINT_PLAN_HEADERS)
    for col_idx in range(1, len(POINT_PLAN_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([20, 60, 24, 24], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.append(["T-100", "Инвентаризация", "Рейд 1", "Инвентаризация"])
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@dataclass
class PointPlanImportRow:
    row_number: int
    point_id: str
    description: str
    group: str = ""
    work_type: str = ""


@dataclass
class PointPlanImportResult:
    rows: list[PointPlanImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_point_plan_import(content: bytes) -> PointPlanImportResult:
    result = PointPlanImportResult()
    bomb_error = _zip_bomb_error(content)
    if bomb_error:
        result.errors.append(bomb_error)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a row-less error
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result

    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        result.errors.append("Файл пуст")
        return result

    header = [_clean(c) for c in rows[0]]
    is_template_header = header[:2] == POINT_PLAN_HEADERS[:2]
    data_rows = rows[1:] if is_template_header else rows

    for offset, raw in enumerate(data_rows, start=2 if is_template_header else 1):
        cells = list(raw) + [None] * (4 - len(raw))
        point_id, description, group, work_type = cells[:4]
        if all(c in (None, "") for c in cells):
            continue  # blank row
        point_id = _clean(point_id)
        if not point_id:
            result.errors.append(f"Строка {offset}: не заполнен номер комплекса")
            continue
        result.rows.append(PointPlanImportRow(row_number=offset, point_id=point_id, description=_clean(description),
                                              group=_clean(group), work_type=_clean(work_type)))
    return result


SIM_IP_HEADERS = ["Комплекс", "Номер SIM (серийник)", "IP-адрес"]


def build_sim_ip_import_template() -> io.BytesIO:
    """Blank .xlsx template for bulk-filling IP address (and SIM serial)
    by matching an existing point in the directory — see
    parse_sim_ip_import_file for the two normalization rules this expects
    (last 7 digits of the SIM serial; IP octets without leading zeros)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "SIM и IP"
    ws.append(SIM_IP_HEADERS)
    for col_idx in range(1, len(SIM_IP_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([20, 26, 18], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.append(["T-100", "8901234567123459876", "192.168.001.143"])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 100
    lines = [
        "Как заполнять шаблон импорта SIM/IP",
        "",
        "1. Не меняйте порядок и названия столбцов на листе «SIM и IP».",
        "2. Комплекс — номер / название точки, которая уже есть в справочнике "
        "(«Все АПК»). Строки с неизвестным номером будут отмечены как ошибка.",
        "3. Номер SIM — можно вставлять полный номер/серийник как есть: "
        "система сама возьмёт из него последние 7 цифр (именно они хранятся "
        "как номер SIM). Пусто — не менять уже сохранённый номер.",
        "4. IP-адрес — можно с нулями в начале октетов (например, "
        "192.168.001.143) — они уберутся автоматически. Пусто — не менять "
        "уже сохранённый IP.",
        "5. Пустые строки и строка-пример (вторая строка) при импорте игнорируются.",
    ]
    for i, line in enumerate(lines, start=1):
        guide.cell(row=i, column=1, value=line)
    guide.cell(row=1, column=1).font = Font(bold=True, size=13)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _last7_digits(value: Any) -> Optional[str]:
    """The SIM 'number' printed on the card is really a long serial —
    only the last 7 digits are used/stored as the identifier (see the
    template's own instructions sheet). Returns None for a blank cell,
    or the digits found (fewer than 7 if that's all there are — a short
    test value shouldn't be rejected outright)."""
    s = _clean(value)
    if not s:
        return None
    digits = "".join(c for c in s if c.isdigit())
    return digits[-7:] if digits else None


def _normalize_ip(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Strips leading zeros from each octet (192.168.001.143 -> 192.168.1.143).
    Returns (normalized_ip, error) — exactly one of the two is set; a blank
    cell returns (None, None), meaning "leave the existing IP untouched"."""
    s = _clean(value)
    if not s:
        return None, None
    parts = s.split(".")
    if len(parts) != 4:
        return None, f"неверный формат IP «{s}»"
    octets = []
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 255):
            return None, f"неверный формат IP «{s}»"
        octets.append(str(int(p)))
    return ".".join(octets), None


@dataclass
class SimIpImportRow:
    row_number: int
    point_id: str
    sim_number: Optional[str] = None
    ip_address: Optional[str] = None


@dataclass
class SimIpImportResult:
    rows: list[SimIpImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_sim_ip_import_file(content: bytes) -> SimIpImportResult:
    result = SimIpImportResult()
    bomb_error = _zip_bomb_error(content)
    if bomb_error:
        result.errors.append(bomb_error)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a row-less error
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result

    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        result.errors.append("Файл пуст")
        return result

    header = [_clean(c) for c in rows[0]]
    is_template_header = header[:2] == SIM_IP_HEADERS[:2]
    data_rows = rows[1:] if is_template_header else rows

    for offset, raw in enumerate(data_rows, start=2 if is_template_header else 1):
        cells = list(raw) + [None] * (len(SIM_IP_HEADERS) - len(raw))
        point_id, sim_number, ip_address = cells[:3]
        if all(c in (None, "") for c in cells):
            continue  # blank row

        point_id = _clean(point_id)
        if not point_id:
            result.errors.append(f"Строка {offset}: не заполнен «Комплекс»")
            continue

        ip_normalized, ip_error = _normalize_ip(ip_address)
        if ip_error:
            result.errors.append(f"Строка {offset}: {ip_error}")
            continue

        result.rows.append(
            SimIpImportRow(
                row_number=offset,
                point_id=point_id,
                sim_number=_last7_digits(sim_number),
                ip_address=ip_normalized,
            )
        )
    return result


SIM_IP_REGISTRY_HEADERS = ["Номер SIM (серийник)", "IP-адрес"]


def build_sim_ip_registry_template() -> io.BytesIO:
    """Blank .xlsx template for the standalone SIM↔IP реестр (app/repositories/
    sim_ip_registry_repository.py) — no «Комплекс» column, this is NOT tied
    to any specific point. A SIM's IP here gets applied to whichever point
    currently has that SIM number, automatically, whenever either changes —
    see routes_excel.py's sim-ip-registry-import and routes_bot.py's
    set_point_sim. Re-uploading the same file later with updated IPs (some
    SIMs may have moved to a different IP) is the normal way to refresh it."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр SIM-IP"
    ws.append(SIM_IP_REGISTRY_HEADERS)
    for col_idx in range(1, len(SIM_IP_REGISTRY_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([26, 18], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.append(["8901234567123459876", "192.168.001.143"])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 100
    lines = [
        "Как заполнять реестр SIM↔IP",
        "",
        "1. Не меняйте порядок и названия столбцов на листе «Реестр SIM-IP».",
        "2. Это НЕ привязка к конкретному комплексу — просто список «у какой "
        "SIM сейчас какой IP». Загружать можно повторно, тем же файлом, "
        "когда IP у части SIM-карт поменялся — новые значения просто "
        "заменят старые.",
        "3. Номер SIM — можно вставлять полный номер/серийник как есть: "
        "система сама возьмёт из него последние 7 цифр.",
        "4. IP-адрес — можно с нулями в начале октетов (например, "
        "192.168.001.143) — они уберутся автоматически.",
        "5. Как только IP для SIM известен из реестра — он автоматически "
        "проставится тому комплексу, у которого сейчас указана эта же SIM "
        "(и будет обновляться при каждом повторном импорте или когда "
        "монтёр через бота укажет/поменяет номер SIM на точке).",
        "6. Пустые строки и строка-пример (вторая строка) при импорте игнорируются.",
    ]
    for i, line in enumerate(lines, start=1):
        guide.cell(row=i, column=1, value=line)
    guide.cell(row=1, column=1).font = Font(bold=True, size=13)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@dataclass
class SimIpRegistryRow:
    row_number: int
    sim_number: str
    ip_address: str


@dataclass
class SimIpRegistryResult:
    rows: list[SimIpRegistryRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_sim_ip_registry_file(content: bytes) -> SimIpRegistryResult:
    result = SimIpRegistryResult()
    bomb_error = _zip_bomb_error(content)
    if bomb_error:
        result.errors.append(bomb_error)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a row-less error
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result

    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        result.errors.append("Файл пуст")
        return result

    header = [_clean(c) for c in rows[0]]
    is_template_header = header[:2] == SIM_IP_REGISTRY_HEADERS[:2]
    data_rows = rows[1:] if is_template_header else rows

    for offset, raw in enumerate(data_rows, start=2 if is_template_header else 1):
        cells = list(raw) + [None] * (len(SIM_IP_REGISTRY_HEADERS) - len(raw))
        sim_number, ip_address = cells[:2]
        if all(c in (None, "") for c in cells):
            continue  # blank row

        sim_normalized = _last7_digits(sim_number)
        if not sim_normalized:
            result.errors.append(f"Строка {offset}: не заполнен «Номер SIM»")
            continue

        ip_normalized, ip_error = _normalize_ip(ip_address)
        if ip_error:
            result.errors.append(f"Строка {offset}: {ip_error}")
            continue
        if not ip_normalized:
            result.errors.append(f"Строка {offset}: не заполнен «IP-адрес»")
            continue

        result.rows.append(SimIpRegistryRow(row_number=offset, sim_number=sim_normalized, ip_address=ip_normalized))
    return result


WAREHOUSE_HEADERS = [
    "Комплекс", "Компонент", "На складе", "Описание компонента", "Статус паутины", "Дата поступления", "Информация",
]
# Прежний шаблон (до компонентов): «Комплекс / Ящик / Камера / Плата / Статус
# паутины / Дата поступления / Информация» — файлы в нём импортируются как
# раньше, значения Ящик/Камера/Плата становятся компонентами.
_LEGACY_WAREHOUSE_START = ["Комплекс", "Ящик"]
_WEB_VALUES = {"архив": "Архив", "синий": "Синий", "красный": "Красный"}
_STOCK_YES = {"да", "д", "yes", "y", "+", "1", "true", "есть", "на складе", "✓", "x", "х"}
_STOCK_NO = {"нет", "н", "no", "n", "-", "0", "false", "не на складе"}


def build_warehouse_template() -> io.BytesIO:
    """Blank .xlsx template for the warehouse page's own import. One row per
    COMPONENT of a complex (камера, ящик, плата… — any names, any number);
    rows with the same «Комплекс» belong to the same complex. A complex that
    is not in the directory yet is created."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Склад"
    ws.append(WAREHOUSE_HEADERS)
    for col_idx in range(1, len(WAREHOUSE_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col_idx, width in enumerate([20, 18, 12, 36, 16, 16, 40], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.append(["T-100", "Камера", "да", "Исправна", "Синий", "15-01-2026", "Экспертиза от 15-01-2026"])
    ws.append(["T-100", "Ящик", "нет", "Остался на месте", "", "", ""])
    ws.append(["T-101", "Плата", "да", "Диагностика", "", "", ""])
    ws.append(["T-102", "", "", "", "", "", "Весь комплекс снят целиком"])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 110
    lines = [
        "Как заполнять шаблон импорта склада",
        "",
        "1. Не меняйте порядок и названия столбцов на листе «Склад». Одна строка — один "
        "КОМПОНЕНТ комплекса; строки с одним и тем же номером в столбце «Комплекс» относятся "
        "к одному комплексу (три компонента — три строки).",
        "2. Комплекс — номер / название. Если такого комплекса ещё нет в справочнике «Все АПК», "
        "он будет создан.",
        "3. Компонент — любое название (камера, ящик, плата, блок питания, кронштейн…), сколько "
        "угодно у одного комплекса. Компонент с таким же названием у комплекса обновляется, "
        "новый — добавляется; компоненты, которых нет в файле, остаются как были.",
        "4. На складе — «да» или «нет» (пусто = «да»). Комплекс получает статус «На складе», если "
        "хотя бы один его компонент на складе; если на складе не все компоненты (у остальных «нет»), "
        "он показывается как «Частично на складе». Если «нет» у всех — на склад он не попадает.",
        "5. Описание компонента — произвольный текст (состояние, серийный номер…); пусто — не менять.",
        "6. Если у комплекса не указан ни один компонент (пустой столбец «Компонент»), он считается "
        "перенесённым на склад целиком — статус «На складе», все его компоненты (если они уже "
        "заведены) отмечаются как находящиеся на складе.",
        "7. Статус паутины — «Архив», «Синий» или «Красный»; Дата поступления — в любом читаемом "
        "формате; Информация — произвольный текст. Это сведения о комплексе в целом — достаточно "
        "указать их в любой одной строке комплекса; пусто — не менять.",
        "8. Пустые строки при импорте игнорируются; примеры (строки 2–5) замените своими данными.",
        "9. Файл прежнего шаблона («Ящик / Камера / Плата») тоже принимается: заполненные значения "
        "становятся компонентами с описанием, пустые — компонентами «не на складе».",
    ]
    for i, line in enumerate(lines, start=1):
        guide.cell(row=i, column=1, value=line)
        guide.cell(row=i, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    guide.cell(row=1, column=1).font = Font(bold=True, size=13)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@dataclass
class WarehouseImportRow:
    row_number: int
    point_id: str
    component: Optional[str] = None       # None — строка про комплекс в целом
    in_stock: bool = True
    description: str = ""
    web_status: Optional[str] = None
    received_at: Optional[str] = None
    info: Optional[str] = None


@dataclass
class WarehouseImportResult:
    rows: list[WarehouseImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _pick(value: Any, allowed: dict[str, str], row_number: int, label: str, errors: list[str]) -> Optional[str]:
    s = _clean(value)
    if not s:
        return None
    picked = allowed.get(s.casefold())
    if picked is None:
        errors.append(f"Строка {row_number}: неизвестное значение «{s}» в столбце «{label}»")
        return None
    return picked


def _stock_flag(value: Any) -> Optional[bool]:
    """«да»/«нет»/… → bool; пусто → True (на складе по умолчанию); непонятное → None."""
    s = _clean(value).casefold()
    if not s:
        return True
    if s in _STOCK_YES:
        return True
    if s in _STOCK_NO:
        return False
    return None


def parse_warehouse_import_file(content: bytes) -> WarehouseImportResult:
    result = WarehouseImportResult()
    bomb_error = _zip_bomb_error(content)
    if bomb_error:
        result.errors.append(bomb_error)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a row-less error
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result

    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        result.errors.append("Файл пуст")
        return result

    header = [_clean(c) for c in rows[0]]
    legacy = header[:2] == _LEGACY_WAREHOUSE_START
    is_template_header = legacy or header[:2] == WAREHOUSE_HEADERS[:2]
    data_rows = rows[1:] if is_template_header else rows

    for offset, raw in enumerate(data_rows, start=2 if is_template_header else 1):
        cells = list(raw) + [None] * (len(WAREHOUSE_HEADERS) - len(raw))
        if all(c in (None, "") for c in cells):
            continue  # blank row
        point_id = _clean(cells[0])
        if not point_id:
            result.errors.append(f"Строка {offset}: не заполнен «Комплекс»")
            continue

        if legacy:
            _, box, camera, board, web, received_at, info = cells[:7]
            component_cells = [("Ящик", _clean(box)), ("Камера", _clean(camera)), ("Плата", _clean(board))]
            descriptions = None
            stock_flags = None
        else:
            _, component, in_stock, description, web, received_at, info = cells[:7]
            component = _clean(component)
            descriptions = _clean(description)
            stock_flags = _stock_flag(in_stock)
            if component and stock_flags is None:
                result.errors.append(f"Строка {offset}: в столбце «На складе» нужно «да» или «нет»")
                continue

        # Дата поступления хранится как ISO (её же правит <input type=date>
        # в карточке); в файле она может быть в любом виде — Excel-ячейка
        # с датой или текст вроде «15-01-2026» / «15.01.26».
        received_iso = _cert_date(received_at)
        if received_iso is None:
            result.errors.append(f"Строка {offset}: не удалось распознать дату поступления")
            continue
        web_v = _pick(web, _WEB_VALUES, offset, "Статус паутины", result.errors)
        common = dict(web_status=web_v, received_at=received_iso or None, info=_clean(info) or None)

        if legacy:
            filled = any(state for _, state in component_cells)
            result.rows.append(WarehouseImportRow(row_number=offset, point_id=point_id, **common))
            if filled:
                for name, state in component_cells:
                    result.rows.append(
                        WarehouseImportRow(row_number=offset, point_id=point_id, component=name, in_stock=bool(state), description=state)
                    )
        elif component:
            result.rows.append(
                WarehouseImportRow(
                    row_number=offset, point_id=point_id, component=component,
                    in_stock=bool(stock_flags), description=descriptions or "", **common,
                )
            )
        else:
            result.rows.append(WarehouseImportRow(row_number=offset, point_id=point_id, **common))
    return result


def build_visit_reports_export(rows: list[dict[str, Any]], fields: list[tuple[str, str]]) -> io.BytesIO:
    """История ремонта/выездов (task_visit_reports) — one row per report,
    only the columns in `fields` (key, header label — a subset/reorder of
    task_visit_reports_repository.EXPORT_FIELDS, picked by the checkboxes
    on /stats)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "История выездов"
    headers = [label for _, label in fields]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = 22
    for row in rows:
        ws.append([format_datetime(row.get(key), seconds=True) if key == "created_at" else (row.get(key) or "") for key, _ in fields])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _down_days(port_down_since: Any, now: dt.datetime) -> Any:
    """Whole days a point has been down (its port — plain ping if it has no
    port — has been failing), "" while it is up."""
    if not port_down_since:
        return ""
    try:
        since = dt.datetime.fromisoformat(str(port_down_since))
    except ValueError:
        return ""
    if since.tzinfo is None:
        since = since.replace(tzinfo=dt.timezone.utc)
    return max(0, (now - since).days)


# Порядок статусов в «Детально»: сначала те, что не работают (дольше всех
# недоступные — выше), затем «только пинг», потом остальные по номеру.
_DETAIL_STATUS_ORDER = {"Не работает": 0, "Есть только пинг": 1}


def build_status_export(
    matrix: list[dict[str, Any]], detail: list[dict[str, Any]], now: Optional[dt.datetime] = None
) -> io.BytesIO:
    """«Выгрузка статусов АПК» (кнопка на /stats): лист 1 — таблица «По
    категориям (текущий срез)» со страницы статистики (категории в строках,
    статусы в столбцах, плюс «Итого»), лист 2 — «Детально»: по каждому
    комплексу номер, статус, сколько дней не работает, IP и адрес (без
    координат) и категория."""
    now = now or dt.datetime.now(dt.timezone.utc)

    # Те же порядок категорий/статусов и подсчёт, что в categoryMatrixHtml
    # (static/js/stats.js): по первому появлению в данных.
    categories = list(dict.fromkeys(r["category"] for r in matrix))
    statuses = list(dict.fromkeys(r["status"] for r in matrix))
    counts = {(r["category"], r["status"]): r["count"] for r in matrix}

    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    headers = ["Категория", *statuses, "Итого"]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="left" if col_idx == 1 else "center", vertical="center", wrap_text=True)
    column_totals = [0] * len(statuses)
    for category in categories:
        row_counts = [counts.get((category, s), 0) for s in statuses]
        for i, n in enumerate(row_counts):
            column_totals[i] += n
        ws.append([category, *[n or None for n in row_counts], sum(row_counts)])
    ws.append(["Итого", *column_totals, sum(column_totals)])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    for row in ws.iter_rows(min_row=2, min_col=2):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 30
    for col_idx in range(2, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 18
    ws.freeze_panes = "B2"

    ws2 = wb.create_sheet("Детально")
    headers2 = ["Номер комплекса", "Статус", "Сколько дней не работает", "IP", "Адрес", "Категория"]
    ws2.append(headers2)
    for col_idx in range(1, len(headers2) + 1):
        cell = ws2.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    rows = [
        {
            "point_id": decode_entities(d["point_id"]),
            "status": d["status"],
            "days": _down_days(d["port_down_since"], now),
            "ip": d["ip_address"] or "",
            "address": decode_entities(d["address"] or ""),
            "category": d["category"],
        }
        for d in detail
    ]
    rows.sort(
        key=lambda r: (
            _DETAIL_STATUS_ORDER.get(r["status"], 2),
            0 if r["days"] != "" else 1,
            -(r["days"] if r["days"] != "" else 0),
            r["point_id"].lower(),
        )
    )
    for r in rows:
        ws2.append([r["point_id"], r["status"], r["days"], r["ip"], r["address"], r["category"]])
    for col, width in zip("ABCDEF", [22, 20, 16, 18, 60, 24]):
        ws2.column_dimensions[col].width = width
    for row in ws2.iter_rows(min_row=2, min_col=3, max_col=3):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = ws2.dimensions

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def build_point_summaries_export(rows: list[dict[str, Any]], fields: list[tuple[str, str]]) -> io.BytesIO:
    """«Итоги по комплексам» (monitoring_repository.point_summaries) — one
    row per visit report, joined with its task's crew/planned work, only
    the columns in `fields` (key, header label — a subset/reorder of
    monitoring_repository.EXPORT_FIELDS, picked by the checkboxes on
    /reports)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Итоги по комплексам"
    headers = [label for _, label in fields]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = 22
    for row in rows:
        ws.append([format_date(row.get(key)) if key == "date" else (row.get(key) or "") for key, _ in fields])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def build_crew_summary_export(rows: list[dict[str, Any]]) -> io.BytesIO:
    """Свод по бригадам (план/факт) за период — one row per (date, crew),
    from reports_repository.crew_day_breakdown(): assigned tasks vs
    submitted visit reports, plus a breakdown by outcome. Mirrors the
    "план/факт/отклонение по бригадам" table dispatchers used to keep by
    hand, minus the per-work-type columns (tasks don't carry a controlled
    work-type field to plan against — see task_visit_reports for that
    vocabulary, which only exists once a visit is actually reported)."""
    from app.repositories.task_visit_reports_repository import RESULT_STATUSES

    wb = Workbook()
    ws = wb.active
    ws.title = "Свод по бригадам"
    headers = ["Дата", "Бригада", "Состав", "Назначено", "Обследовано", "Не выполнено", *RESULT_STATUSES]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        members = ", ".join(f"{m['full_name']}" for m in row["members"])
        ws.append([
            format_date(row["date"]),
            row["crew_label"],
            members,
            row["assigned"],
            row["reported"],
            row["assigned"] - row["reported"],
            *[row["result_statuses"].get(s, 0) for s in RESULT_STATUSES],
        ])
    for col_idx, width in enumerate([12, 22, 34, 12, 12, 14, 14, 16, 14, 14], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def build_daily_report_export(day: str, fields: list[dict[str, Any]], values: dict[str, str]) -> io.BytesIO:
    """«Ежедневный отчёт» as .xlsx — a plain Показатель/Значение sheet,
    grouped by the same sections as the form on /reports (see
    app/models/report_fields.py::DAILY_REPORT_FIELDS), one row per field."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Ежедневный отчёт"
    ws.append([f"Отчёт за {format_date(day)}"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=13)
    ws.append(["Показатель", "Значение"])
    for col_idx in range(1, 3):
        cell = ws.cell(row=2, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT

    last_group = None
    for f in fields:
        if f["group"] and f["group"] != last_group:
            ws.append([f["group"], ""])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
            last_group = f["group"]
        ws.append([f["label"], values.get(f["key"], "")])
    ws.column_dimensions["A"].width = 60
    ws.column_dimensions["B"].width = 18
    ws.freeze_panes = "A3"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def build_plan_workbook(
    day: str,
    crews: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    points: list[dict[str, Any]],
) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "План-наряд"
    ws.append(["План-наряд", format_date(day)])
    ws.append(["Бригада", "Точка", "Адрес", "Работа", "Тип работы", "Работа, ч", "Дорога, ч", "Оснащение"])
    for col_idx in range(1, 9):
        ws.cell(row=2, column=col_idx).font = Font(bold=True)

    pts = {p["id"]: p for p in points if "id" in p}
    for crew in crews:
        # A trip crew's rows are only exported on its first day, pulling in
        # every task assigned to it across the whole trip — see
        # app/core/trips.py and routes_plan_print.py's matching logic.
        if not is_trip_first_day(crew):
            continue
        visible_ids = {str(i) for i in trip_visible_crew_ids(crew)}
        is_trip = bool(crew.get("trip_id"))
        for t in tasks:
            if str(t.get("team")) not in visible_ids:
                continue
            if not is_trip and t.get("date") != day:
                continue
            p = pts.get(t.get("point"), {})
            ws.append(
                [
                    crew.get("name", ""),
                    decode_entities(t.get("point", "")),
                    decode_entities(p.get("address") or t.get("address", "")),
                    decode_entities(t.get("work") or p.get("description", "")),
                    t.get("type", ""),
                    t.get("hours", 0),
                    t.get("travel", 0),
                    crew.get("equip", ""),
                ]
            )
    for col, width in zip("ABCDEFGH", [22, 26, 40, 48, 18, 15, 15, 20]):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = ws.dimensions

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


YANDEX_HEADERS = ["Широта", "Долгота", "Описание", "Подпись", "Номер метки", "preset"]
_YANDEX_DEFAULT_PRESET = "islands#blueIcon"
# Приоритетные точки (tasks.priority — отмечается в задании/плане, см.
# app/api/routes_point_plans.py и routes_excel.py) получают жёлтый маркер
# и пометку в описании — но только здесь, в этом экспорте: сам work/
# description точки нигде в приложении не меняется.
_YANDEX_PRIORITY_PRESET = "islands#yellowIcon"
# Выполненные точки (сдан отчёт о выезде) — зелёные.
_YANDEX_DONE_PRESET = "islands#greenIcon"
# Вспомогательные точки (метка с информацией, не работа — см. app/core/work_types.py) — фиолетовые.
_YANDEX_AUX_PRESET = "islands#violetIcon"


def _yandex_workbook_bytes(rows: list[list[Any]]) -> bytes:
    """One crew's sheet for Yandex Constructor's Excel import, written with
    XlsxWriter rather than openpyxl. openpyxl saves every non-ASCII letter
    as a numeric character reference ("&#1052;&#1045;..." for "МЕ...") inside
    inline-string cells of an XML part with no declaration — valid XML, and
    Excel reads it fine, but Yandex's importer takes those codes literally,
    so every Russian name/description landed on the map as "&#1052;...".
    (Re-saving the file in Excel "fixed" it, which rewrites it in exactly
    the form XlsxWriter produces: a shared-strings table, real UTF-8 text
    and a declaration on every part.) Every cell is written as text, like
    before — coordinates included."""
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, {"in_memory": True})
    ws = wb.add_worksheet("Точки")
    header_fmt = wb.add_format({"bold": True, "font_color": "#F8FAFC", "bg_color": "#1E293B"})
    for col, title in enumerate(YANDEX_HEADERS):
        ws.write_string(0, col, title, header_fmt)
    for r, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            ws.write_string(r, col, "" if value is None else str(value))
    for col, width in enumerate([12, 12, 40, 28, 14, 20]):
        ws.set_column(col, col, width)
    wb.close()
    return out.getvalue()


# Цвета маркеров Конструктора карт (preset «islands#…Icon») — ближайший к цвету
# статуса из «Настройки → Статусы АПК» выбирается по расстоянию в RGB.
_YANDEX_PRESET_COLORS = {
    "islands#blueIcon": "1E98FF", "islands#darkBlueIcon": "177BC9", "islands#lightBlueIcon": "82CDFF",
    "islands#nightIcon": "0E4779", "islands#redIcon": "ED4543", "islands#darkOrangeIcon": "E6761B",
    "islands#orangeIcon": "FF931E", "islands#yellowIcon": "FFD21E", "islands#greenIcon": "56DB40",
    "islands#darkGreenIcon": "1BAD03", "islands#oliveIcon": "97A100", "islands#pinkIcon": "F371D1",
    "islands#violetIcon": "B51EFF", "islands#brownIcon": "793D0E", "islands#grayIcon": "B3B3B3",
    "islands#blackIcon": "595959",
}


def _hex_rgb(value: str) -> Optional[tuple[int, int, int]]:
    h = (value or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def yandex_preset_for_color(color: str) -> str:
    """Ближайший маркер Яндекса к цвету статуса (серый — если цвет не разобрать)."""
    rgb = _hex_rgb(color)
    if rgb is None:
        return "islands#grayIcon"
    return min(
        _YANDEX_PRESET_COLORS,
        key=lambda name: sum((a - b) ** 2 for a, b in zip(rgb, _hex_rgb(_YANDEX_PRESET_COLORS[name]))),
    )


def build_yandex_points_export(
    points: list[dict[str, Any]], status_colors: dict[str, str]
) -> tuple[bytes, int, int]:
    """Один .xlsx для импорта в Конструктор карт Яндекса — комплексы из
    справочника (те, что отдал фильтр на «Все АПК», со всего справочника, а не
    только загруженная страница). Подпись метки — номер комплекса, описание —
    адрес, описание комплекса и статус; цвет метки — по цвету статуса.
    Комплексы без координат на карту не поставить — они пропускаются.
    Возвращает (файл, выгружено, пропущено без координат)."""
    rows: list[list[Any]] = []
    skipped = 0
    for p in points:
        lat, lon = str(p.get("lat") or "").strip(), str(p.get("lon") or "").strip()
        try:
            has_coords = bool(lat and lon) and not (float(lat) == 0 and float(lon) == 0)
        except ValueError:
            has_coords = False
        if not has_coords:
            skipped += 1
            continue
        status = p.get("status") or ""
        if status == "На складе" and p.get("stock_partial"):
            status_text = "Частично на складе"
        else:
            status_text = status or "Не известно"
        desc_parts = [
            decode_entities(p.get("address") or ""),
            decode_entities(p.get("description") or ""),
            f"Статус: {status_text}",
        ]
        rows.append(
            [
                lat,
                lon,
                " · ".join(part for part in desc_parts if part),
                decode_entities(p.get("id") or ""),
                "",
                yandex_preset_for_color(status_colors.get(status, "")),
            ]
        )
    return _yandex_workbook_bytes(rows), len(rows), skipped


def _safe_filename(name: str) -> str:
    """Strips characters Windows/zip readers choke on in a filename."""
    cleaned = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    return cleaned or "Бригада"


def _yandex_crew_rows(
    day: str, crew: dict[str, Any], tasks: list[dict[str, Any]], pts: dict[str, dict[str, Any]]
) -> tuple[list[list[Any]], int]:
    """Строки импорта в Конструктор карт для одной бригады: все её задания с
    координатами (у командировки — за всю поездку, иначе за `day`). Подпись метки —
    название (номер) комплекса, а не инвентарный номер, у приоритетных — жёлтый маркер и
    пометка в описании. Возвращает (строки, пропущено заданий без координат)."""
    visible_ids = {str(i) for i in trip_visible_crew_ids(crew)}
    is_trip = bool(crew.get("trip_id"))
    rows: list[list[Any]] = []
    skipped = 0
    for t in tasks:
        if str(t.get("team")) not in visible_ids:
            continue
        if not is_trip and t.get("date") != day:
            continue
        if is_aux_type(t.get("type")):
            # Координаты у такой точки свои, в самом задании; подпись — её название, описание — «что это».
            aux_lat, aux_lon = str(t.get("aux_lat") or "").strip(), str(t.get("aux_lon") or "").strip()
            if not aux_lat or not aux_lon:
                skipped += 1
                continue
            info = decode_entities(t.get("work") or "").strip()
            rows.append(
                [aux_lat, aux_lon, f"Вспомогательная точка. {info}" if info else "Вспомогательная точка",
                 decode_entities(t.get("point") or "Точка"), "",
                 # Монтёр нажал в боте «Принято» — точка зелёная, как выполненная.
                 _YANDEX_DONE_PRESET if t.get("reported") else _YANDEX_AUX_PRESET]
            )
            continue
        p = pts.get(t.get("point"), {})
        lat, lon = p.get("lat"), p.get("lon")
        if not lat or not lon:
            skipped += 1
            continue
        desc = decode_entities(t.get("work") or p.get("description") or "")
        is_priority = bool(t.get("priority"))
        if is_priority:
            desc = f"ПРИОРИТЕТ. {desc}".strip()
        # Точка, на которой уже были и сдали отчёт, — зелёная (видно, что осталось объехать).
        if t.get("reported"):
            preset = _YANDEX_DONE_PRESET
        else:
            preset = _YANDEX_PRIORITY_PRESET if is_priority else _YANDEX_DEFAULT_PRESET
        rows.append(
            [
                lat,
                lon,
                desc,
                decode_entities(t.get("point") or p.get("id") or ""),         # подпись — название комплекса (не инвентарный номер)
                "",
                preset,
            ]
        )
    return rows, skipped


def build_yandex_crew_file(
    crew: dict[str, Any], tasks: list[dict[str, Any]], points: list[dict[str, Any]]
) -> tuple[bytes, int, int]:
    """Один .xlsx для импорта в Конструктор карт — точки одной бригады (её робот
    загружает в Конструктор сам). Возвращает (файл, точек в файле, пропущено без координат)."""
    pts = {p["id"]: p for p in points if "id" in p}
    rows, skipped = _yandex_crew_rows(crew["date"], crew, tasks, pts)
    return _yandex_workbook_bytes(rows), len(rows), skipped


def build_yandex_export(
    day: str,
    crews: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    points: list[dict[str, Any]],
) -> io.BytesIO:
    """One .xlsx per crew, in the column layout Yandex Constructor Maps'
    own Excel import expects (Широта/Долгота/Описание/Подпись/Номер
    метки/preset) — zipped together so a dispatcher gets one download for
    the whole day and re-imports each crew's file into its own map.
    Points without coordinates can't be placed on a map, so they're
    skipped rather than exported with blank lat/lon.
    """
    pts = {p["id"]: p for p in points if "id" in p}
    crews_by_id = {c["id"]: c for c in crews}

    # Grouped by the crew's OWN id (not each task's raw t["team"]) so every
    # day of a trip's tasks land in one file under the trip's first day —
    # same reasoning as build_plan_workbook, see app/core/trips.py.
    rows_by_crew: dict[int, list[list[Any]]] = {}
    for crew in crews:
        if not is_trip_first_day(crew):
            continue
        rows, _ = _yandex_crew_rows(day, crew, tasks, pts)
        if rows:
            rows_by_crew[crew["id"]] = rows

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        used_names: set[str] = set()
        for crew_id, rows in rows_by_crew.items():
            crew = crews_by_id.get(crew_id, {})
            book_bytes = _yandex_workbook_bytes(rows)

            base_name = _safe_filename(crew.get("name") or f"Бригада {crew_id}")
            name = f"{base_name}.xlsx"
            n = 2
            while name in used_names:
                name = f"{base_name} ({n}).xlsx"
                n += 1
            used_names.add(name)
            zf.writestr(name, book_bytes)

    out.seek(0)
    return out


# ---------------------------------------------------------------------------
# «Выезды бригад по дням» — за выбранные дни (любой набор, не обязательно подряд):
# в какие дни какие были бригады, у них какие комплексы, что было задано по
# каждому и чем закончился выезд — со всей информацией по заданию и отчёту.
# ---------------------------------------------------------------------------
PERIOD_COLUMNS = [
    ("Дата", 12), ("День недели", 8), ("Бригада", 20), ("Состав бригады", 40), ("Оснащение", 12), ("Место", 18),
    ("Командировка", 24), ("Комплекс", 16), ("Адрес", 38), ("Категория", 16), ("Инв. номер", 16),
    ("Тип работы", 20), ("Приоритет", 11), ("Задание", 40), ("Заметка к заданию", 28),
    ("Часов работы", 9), ("Часов дороги", 9), ("Статус комплекса сейчас", 20),
    ("Итог", 22), ("Комментарий отчёта", 40), ("Снято оборудование", 26), ("Установлено оборудование", 26),
    ("Отчёт сдал", 20), ("Отчёт сдан", 17),
]
PERIOD_HEADERS = [name for name, _ in PERIOD_COLUMNS]
_PERIOD_OUTCOME_COL = PERIOD_HEADERS.index("Итог") + 1
PERIOD_NOT_VISITED = "Не выехали"          # день прошёл, отчёта по заданию нет
PERIOD_NOT_YET = "Ещё не было выезда"       # день ещё не наступил / сегодня, отчёта пока нет
PERIOD_NO_TASKS = "Нет заданий"             # бригада была, заданий на этот день у неё нет
PERIOD_NO_STATUS = "Отчёт без статуса"

_PERIOD_ROLE_LABEL = {"fitter": "монтёр", "driver": "водитель"}
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_PERIOD_FILLS = {
    PERIOD_NOT_VISITED: "FFE4E6",
    PERIOD_NOT_YET: "F1F5F9",
    PERIOD_NO_TASKS: "F8FAFC",
    "Работает": "DCFCE7",
    "Не работает": "FEE2E2",
    "Демонтирован": "DBEAFE",
    "Установлен": "DBEAFE",
}


def _period_crew_text(crew: dict[str, Any]) -> str:
    parts = []
    for m in crew.get("members") or []:
        role = _PERIOD_ROLE_LABEL.get(m.get("role_type") or "", "")
        name = decode_entities(m.get("full_name") or "")
        parts.append(f"{name} ({role})" if role else name)
    return ", ".join(p for p in parts if p)


def period_outcome(report: Optional[dict[str, Any]], day: str, today: str) -> str:
    """Итог выезда по заданию для выгрузки. Есть отчёт — его статус (статус
    работы или «статус выезда» типа), даже если день сегодняшний: выезд уже был.
    Нет отчёта: день прошёл — «Не выехали», день ещё не наступил или сегодня —
    «Ещё не было выезда»."""
    if report:
        return (report.get("outcome") or report.get("result_status") or PERIOD_NO_STATUS).strip()
    return PERIOD_NOT_VISITED if day < today else PERIOD_NOT_YET


def reporter_name(created_by: Any) -> str:
    """Кто сдал отчёт: из бота — «vk:Иванов И.», из панели — логин."""
    value = str(created_by or "").strip()
    return value[3:].strip() if value.lower().startswith("vk:") else value


def _hours(value: Any) -> Any:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    return "" if n == 0 else (int(n) if n == int(n) else n)


def _point_status_text(point: dict[str, Any]) -> str:
    status = point.get("status") or ""
    if not status:
        return "Не известно"
    return "Частично на складе" if status == "На складе" and point.get("stock_partial") else status


def _trip_text(crew: dict[str, Any]) -> str:
    if not crew.get("trip_id"):
        return ""
    return f"{format_date(crew.get('trip_from'))} — {format_date(crew.get('trip_to'))}"


def _period_weekday(day: str) -> str:
    try:
        return _WEEKDAYS[dt.date.fromisoformat(day).weekday()]
    except ValueError:
        return ""


def build_period_report(
    days: list[str],
    crews_by_day: dict[str, list[dict[str, Any]]],
    crew_lookup: dict[Any, dict[str, Any]],
    tasks: list[dict[str, Any]],
    points: list[dict[str, Any]],
    reports: dict[str, dict[str, Any]],
    today: str,
) -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Выезды по дням"
    ws.append(PERIOD_HEADERS)
    for col_idx in range(1, len(PERIOD_HEADERS) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    pts = {p["id"]: p for p in points if "id" in p}
    summary: list[list[Any]] = []
    for day in days:
        day_tasks = [t for t in tasks if t.get("date") == day]
        crews = list(crews_by_day.get(day, []))
        known = {str(c["id"]) for c in crews}
        # Задание может числиться за бригадой другого дня (командировка) —
        # такая бригада тоже попадает в этот день.
        for t in day_tasks:
            team = str(t.get("team"))
            if team not in known:
                known.add(team)
                crews.append(crew_lookup.get(t.get("team")) or crew_lookup.get(team) or {"id": team, "name": f"Бригада {team}", "members": []})
        crew_count = len(crews)
        task_count = reported = not_visited = not_yet = 0
        for crew in crews:
            crew_tasks = [t for t in day_tasks if str(t.get("team")) == str(crew["id"])]
            crew_part = [
                decode_entities(crew.get("name") or ""),
                _period_crew_text(crew),
                crew.get("equip") or "",
                decode_entities(crew.get("place") or ""),
                _trip_text(crew),
            ]
            base = [format_date(day), _period_weekday(day), *crew_part]
            if not crew_tasks:
                row = base + [""] * (len(PERIOD_HEADERS) - len(base))
                row[_PERIOD_OUTCOME_COL - 1] = PERIOD_NO_TASKS
                ws.append(row)
                ws.cell(row=ws.max_row, column=_PERIOD_OUTCOME_COL).fill = PatternFill("solid", fgColor=_PERIOD_FILLS[PERIOD_NO_TASKS])
                continue
            for t in crew_tasks:
                p = pts.get(t.get("point"), {})
                report = reports.get(t.get("id"))
                outcome = period_outcome(report, day, today)
                task_count += 1
                if report:
                    reported += 1
                elif outcome == PERIOD_NOT_VISITED:
                    not_visited += 1
                else:
                    not_yet += 1
                r = report or {}
                ws.append(
                    base
                    + [
                        decode_entities(t.get("point") or ""),
                        decode_entities(p.get("address") or t.get("address") or ""),
                        decode_entities(p.get("category_name") or ""),
                        decode_entities(p.get("inventory_number") or ""),
                        r.get("visit_type") or t.get("type") or "",
                        "Приоритет" if t.get("priority") else "",
                        decode_entities(t.get("work") or p.get("description") or ""),
                        decode_entities(t.get("note") or ""),
                        _hours(t.get("hours")),
                        _hours(t.get("travel")),
                        _point_status_text(p) if p else "",
                        outcome,
                        decode_entities(r.get("comment") or ""),
                        decode_entities(r.get("equipment_removed") or ""),
                        decode_entities(r.get("equipment_installed") or ""),
                        reporter_name(r.get("created_by")),
                        format_datetime(r.get("updated_at") or r.get("created_at")) if r else "",
                    ]
                )
                fill = _PERIOD_FILLS.get(outcome, "E0E7FF")
                ws.cell(row=ws.max_row, column=_PERIOD_OUTCOME_COL).fill = PatternFill("solid", fgColor=fill)
        summary.append([format_date(day), _period_weekday(day), crew_count, task_count, reported, not_visited, not_yet])

    for col_idx, (_, width) in enumerate(PERIOD_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    sheet = wb.create_sheet("Сводка по дням")
    headers = ["Дата", "День недели", "Бригад", "Заданий", "С отчётом", PERIOD_NOT_VISITED, PERIOD_NOT_YET]
    sheet.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = sheet.cell(row=1, column=col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in summary:
        sheet.append(row)
    if summary:
        sheet.append(["Итого", "", sum(r[2] for r in summary), sum(r[3] for r in summary), sum(r[4] for r in summary),
                      sum(r[5] for r in summary), sum(r[6] for r in summary)])
        for col_idx in range(1, len(headers) + 1):
            sheet.cell(row=sheet.max_row, column=col_idx).font = Font(bold=True)
    for col_idx, width in enumerate([12, 12, 10, 10, 12, 14, 20], start=1):
        sheet.column_dimensions[get_column_letter(col_idx)].width = width
    sheet.freeze_panes = "A2"

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out
