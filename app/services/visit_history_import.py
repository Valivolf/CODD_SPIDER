"""Загрузка ИСТОРИИ ВЫЕЗДОВ из Excel: «когда, кто, какая точка, что было сделано, какой статус».

Раньше эти сведения велись вне системы; здесь они попадают в журнал «История выездов» у карточки комплекса
(таблица task_visit_reports, строки помечены imported=1). Загрузка ничего не меняет в самой работе приложения:
статус комплекса, «упал / восстановлен», календарь, склад — не трогаются, это справка о прошлом. В статистике
по выбранному периоду такие записи учитываются (тип выезда и результат за те даты).

Столбцы ищутся по заголовку (порядок любой, регистр и «ё» не важны), лишние столбцы игнорируются; если заголовка
нет, а в первой строке стоит дата, берётся порядок «Дата, Кто, Комплекс, Что сделано, Статус, Тип выезда».

«Журнал выездов» (ведётся параллельно: №, Дата, Номер, Код ICC, IP-адрес, Адрес, Инв. №, Статус, Причина, Выполненные работы,
Исполнитель, Требуется, Примечания) принимается КАК ЕСТЬ, без подгонки: «Номер» — комплекс, «Статус» — итог, «Причина» — из-за чего
не работал, «Выполненные работы» и «Примечания» — комментарий, «Требуется» — что нужно для восстановления, «Исполнитель» — кто
выезжал; ICC, IP, инвентарный номер и адрес нужны только для поиска комплекса (если номера нет или он не нашёлся), остальные
столбцы и служебные листы («Последний выезд», «Сводка», «Адреса»…) отбрасываются.

Дубли: запись о выезде на комплекс в тот же день, которая уже есть в системе — отчёт бота, диспетчера, загруженный из таблицы
Яндекс Форм или загруженный из журнала раньше, — не создаётся второй раз. Если уже есть настоящий отчёт (бот / форма / диспетчер),
из журнала в него добавляется только то, чего в нём нет (причина, «что нужно»); прежняя загрузка из журнала обновляется по журналу
(журнал ведётся дальше: поправили строку — повторная загрузка поправит запись, а не создаст новую), а если настоящий отчёт на этот
день появился позже, дублирующая запись из журнала убирается (ничего из журнала не теряется — причина и «что нужно» переносятся).
Необязательные столбцы: «Комментарий», «Что неисправно / причина» (из-за чего не работало или не работает до сих пор),
«Что нужно для восстановления» — они попадают в журнал истории выездов рядом с типом работы и итогом. Повторная загрузка
того же файла не дублирует записи, а дополняет уже загруженные недостающими причиной и «что нужно»."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core.text import decode_entities
from app.core.work_types import COMBINED_SEPARATOR, REPAIR_TYPE_NAME, is_aux_type, is_repair_type, work_type_names
from app.db.database import get_connection
from app.repositories import work_types_repository
from app.services.point_finder import PointFinder, norm_address
from app.repositories.task_visit_reports_repository import RESULT_STATUSES
from app.services.excel_service import _zip_bomb_error

MAX_ROWS = 50_000
MAX_FILE_SIZE = 10 * 1024 * 1024
_MIN_DATE = dt.date(2000, 1, 1)
_MAX_WHO = 200
_MAX_COMMENT = 4000
ID_PREFIX = "imp-"

TEMPLATE_HEADERS = [
    "Дата", "Кто выезжал", "Комплекс", "Что было сделано", "Статус", "Тип выезда (необязательно)",
    "Комментарий (необязательно)", "Что неисправно / причина (необязательно)", "Что нужно для восстановления (необязательно)",
]
# Поле → названия столбца (без регистра, «ё» = «е», без знаков препинания); заголовок подходит, если он равен
# названию или начинается с него («Тип выезда (необязательно)» = «тип выезда»).
_ALIASES: dict[str, list[str]] = {
    "date": ["дата", "когда", "дата выезда", "дата и время", "дата время", "день"],
    "who": ["кто выезжал", "кто", "исполнитель", "исполнители", "монтер", "монтеры", "фио", "выезжал",
            "сотрудник", "сотрудники", "ответственный", "бригада"],
    "point": ["комплекс", "точка", "апк", "номер комплекса", "номер апк", "номер", "точка ремонта", "объект"],
    "work": ["что было сделано", "что сделано", "сделано", "выполненные работы", "выполнено", "работы",
             "проделанная работа", "комментарий", "описание", "примечание"],
    "status": ["статус", "какой статус", "статус комплекса", "состояние", "результат", "итог"],
    "type": ["тип выезда", "тип работ", "вид работ", "вид выезда", "тип", "вид"],
    "note": ["комментарий", "комментарии", "примечание", "примечания", "описание", "заметка", "заметки", "пояснение"],
    "fault": ["что неисправно", "неисправность", "причина неисправности", "причина", "из за чего не работает",
              "из за чего не работал", "из за чего не работало", "из за чего", "почему не работает", "почему не работал",
              "почему не работало", "что сломано", "дефект", "проблема", "причина простоя"],
    "needs": ["что нужно для восстановления", "что нужно для исправления", "что нужно для ремонта", "что нужно",
              "что необходимо", "необходимо", "что требуется", "требуется", "для восстановления", "для исправления"],
    "removed": ["снято оборудование", "снято", "демонтировано"],
    "installed": ["установлено оборудование", "установлено", "поставлено"],
    # нужны только для поиска комплекса (если номера нет или он не нашёлся)
    "icc": ["код icc", "icc", "iccid", "номер sim", "номер сим", "сим карта", "sim карта", "код sim", "сим", "sim"],
    "ip": ["ip адрес", "ip", "айпи"],
    "inv": ["инв номер", "инвентарный номер", "инвентарный"],
    "addr": ["адрес", "адрес комплекса", "место установки"],
}
_FIELD_ORDER = ["date", "who", "point", "work", "status", "type"]    # без заголовка — в таком порядке столбцов
_HEADER_SCAN_ROWS = 15


def _key(value: Any) -> str:
    """Для сравнения текста: без регистра, «ё» = «е», только буквы/цифры/пробелы."""
    s = decode_entities(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]|_", " ", s).split())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)   # номер комплекса 123, который Excel хранит как 123.0
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d-%m-%Y")
    return decode_entities(value).strip()


@dataclass
class RawRow:
    sheet: str
    row: int
    date: dt.date
    who: str
    point: str
    work: str
    status: str
    vtype: str
    removed: str
    installed: str
    note: str = ""
    fault: str = ""
    needs: str = ""
    icc: str = ""
    ip: str = ""
    inv: str = ""
    addr: str = ""


@dataclass
class Parsed:
    rows: list[RawRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rows_read: int = 0


_ISO_RE = re.compile(r"^(\d{4})\D(\d{1,2})\D(\d{1,2})(?:\D.*)?$")
_DMY_RE = re.compile(r"^(\d{1,2})\D(\d{1,2})\D(\d{2}|\d{4})(?:\D.*)?$")


def parse_date(value: Any) -> dt.date | None:
    """Ячейка-дата Excel, число-серийник или текст «05.03.2026», «5-3-26», «2026-03-05»; иначе None."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 <= value <= 80000:      # серийный номер даты Excel (1954…2118)
            return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
        return None
    s = _text(value)
    if not s:
        return None
    try:
        m = _ISO_RE.match(s)
        if m:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _DMY_RE.match(s)
        if m:
            year = int(m.group(3))
            if year < 100:
                year += 2000
            return dt.date(year, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


def _match_header(cells: list[str]) -> dict[str, int]:
    """Поле → номер столбца по строке заголовков; каждый столбец достаётся одному полю."""
    keys = [_key(c) for c in cells]
    found: dict[str, int] = {}
    used: set[int] = set()
    # Сначала точные совпадения, потом «начинается с» — чтобы «тип выезда» не съел «тип» раньше времени.
    for exact in (True, False):
        for field_name, aliases in _ALIASES.items():
            if field_name in found:
                continue
            for alias in aliases:
                hit = next(
                    (i for i, k in enumerate(keys) if i not in used and k
                     and (k == alias if exact else k.startswith(alias + " "))),
                    None,
                )
                if hit is not None:
                    found[field_name] = hit
                    used.add(hit)
                    break
    return found


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

    today = dt.date.today()
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        header_idx, mapping = None, {}
        for i, raw in enumerate(rows[:_HEADER_SCAN_ROWS]):
            candidate = _match_header([_text(c) for c in raw])
            if "date" in candidate and "point" in candidate:
                header_idx, mapping = i, candidate
                break
        if header_idx is not None:
            date_header = _key(_text(rows[header_idx][mapping["date"]]))
            if date_header.startswith("дата последн"):      # «Последний выезд» журнала — сводка формулами по листу «Выезды», не записи
                result.notes.append(f"Лист «{ws.title}» пропущен: это сводка, а не записи о выездах.")
                continue
        if header_idx is None:
            first = next(((i, r) for i, r in enumerate(rows) if any(c not in (None, "") for c in r)), None)
            if first and parse_date(first[1][0] if first[1] else None):
                header_idx, mapping = first[0] - 1, {name: i for i, name in enumerate(_FIELD_ORDER)}
                result.notes.append(f"Лист «{ws.title}»: заголовков нет — столбцы по порядку: {', '.join(TEMPLATE_HEADERS[:5])}.")
            else:
                if rows:
                    result.notes.append(f"Лист «{ws.title}» пропущен: не нашёл столбцы «Дата» и «Комплекс».")
                continue
        for offset, raw in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            cells = list(raw)

            def cell(name: str, _cells=cells):
                idx = mapping.get(name)
                return _cells[idx] if idx is not None and idx < len(_cells) else None

            if all(c in (None, "") for c in cells):
                continue
            point = _text(cell("point"))
            raw_date = cell("date")
            alt = _text(cell("icc")) or _text(cell("ip")) or _text(cell("inv")) or _text(cell("addr"))
            if not point and raw_date in (None, ""):
                continue                       # строка без даты и комплекса — мусор/примечание под таблицей/пустая строка с формулами
            result.rows_read += 1
            where = f"Лист «{ws.title}», строка {offset}"
            if result.rows_read > MAX_ROWS:
                result.errors.append(f"Слишком много строк (больше {MAX_ROWS}) — разбейте файл на части.")
                return result
            day = parse_date(raw_date)
            if day is None:
                result.errors.append(f"{where}: не понял дату «{_text(raw_date)}» (нужно, например, 05.03.2026)")
                continue
            if day < _MIN_DATE or day > today + dt.timedelta(days=1):
                result.errors.append(f"{where}: дата {day.strftime('%d-%m-%Y')} вне допустимого диапазона")
                continue
            if not point and not alt:
                result.errors.append(f"{where}: не указан комплекс")
                continue
            result.rows.append(RawRow(
                sheet=ws.title, row=offset, date=day, who=_text(cell("who")), point=point,
                work=_text(cell("work")), status=_text(cell("status")), vtype=_text(cell("type")),
                removed=_text(cell("removed")), installed=_text(cell("installed")),
                note=_text(cell("note")), fault=_text(cell("fault")), needs=_text(cell("needs")),
                icc=_text(cell("icc")), ip=_text(cell("ip")), inv=_text(cell("inv")), addr=_text(cell("addr")),
            ))
    if not result.rows and not result.errors and not result.notes:
        result.errors.append("В файле не найдено строк для загрузки")
    return result


# ---------------------------------------------------------------- статус, тип выезда
def normalize_status(text: str) -> str:
    """«не работает», «неисправен», «работает», «демонтирован», «установлен»… → один из RESULT_STATUSES, иначе ''."""
    k = _key(text)
    if not k:
        return ""
    if re.search(r"не\s*работ|неисправ|не\s*раб\b|нет (доступа|связи)|недоступ|не доступ|не отвеча|не пингу|отвал|упал"
                 r"|нет (напряжения|питания|электропитания)|не подключен\w* (питани|электропитани)|отсутствует (питание|напряжение)", k):
        return "Не работает"
    if re.search(r"демонтирован\w* плат|снят\w* плат|плат\w* (демонтирован|снят)|демонтаж платы", k):
        return "Не работает"          # снята только плата — комплекс не работает, но целиком не демонтирован
    if re.search(r"демонт|\bснят|снятие|на склад", k):
        return "Демонтирован"
    if re.search(r"установ|смонтир|поставлен|монтаж", k):
        return "Установлен"
    if re.search(r"работает|работал|рабоч|исправ|\bок\b|\bok\b|восстанов|в норме|норма|доступен|пингуется|есть доступ|есть связь", k):
        return "Работает"
    return ""


def _type_lookup() -> dict[str, str]:
    return {_key(n): n for n in work_type_names() if not is_aux_type(n)}


def _resolve_type(raw: str, lookup: dict[str, str]) -> str | None:
    """Название типа выезда из справочника («Настройки → Типы выездов») или None, если такого нет."""
    k = _key(raw)
    if not k:
        return None
    if k in lookup:
        return lookup[k]
    m = re.match(r"^(.*?)\s*\+\s*ремонт$", decode_entities(raw).strip(), re.I)
    if m and _key(m.group(1)) in lookup:
        return lookup[_key(m.group(1))] + COMBINED_SEPARATOR + REPAIR_TYPE_NAME
    return None


# ---------------------------------------------------------------- план загрузки
MSK = dt.timezone(dt.timedelta(hours=3))        # день отчёта — по местному времени (как у людей в календаре)
_GENERIC_REASONS = {"прочее", "прочие", "прочая", "-", "нет", "н/д"}      # «Причина: Прочее» — не причина, а пустышка; в историю не пишем


@dataclass
class Item:
    task_id: str
    point_id: str
    visit_type: str
    result_status: str
    outcome: str
    comment: str
    removed: str
    installed: str
    day: dt.date
    who: str
    sheet: str
    row: int
    fault: str = ""
    needs: str = ""

    @property
    def created_at(self) -> str:
        # Дата без времени: полдень по UTC, чтобы в любом часовом поясе РФ день остался тем же.
        return f"{self.day.isoformat()}T12:00:00+00:00"


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)          # новые, их можно записывать
    duplicates: int = 0                                       # такая запись уже есть, и она совпадает с файлом
    errors: list[str] = field(default_factory=list)
    unknown_points: Counter = field(default_factory=Counter)
    unknown_statuses: Counter = field(default_factory=Counter)
    unknown_types: Counter = field(default_factory=Counter)
    loose_points: int = 0
    notes: list[str] = field(default_factory=list)
    rows_read: int = 0
    in_system: int = 0                                        # выезд в тот же день уже есть настоящим отчётом (бот / форма / диспетчер)
    # Дополнения уже существующих записей: (task_id, поля, режим): 'fill' — только пустое (настоящие отчёты),
    # 'set' — по журналу (записи, загруженные из него раньше)
    updates: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)           # записи из журнала, вытесненные настоящим отчётом
    found_by: Counter = field(default_factory=Counter)        # как найден комплекс, если не по номеру: sim / ip / inventory / address


def _loose(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold().replace("ё", "е"))


def _item_id(day: str, point: str, n: int) -> str:
    """Постоянный номер записи из файла: день + комплекс + порядковый номер среди строк этого дня."""
    return ID_PREFIX + hashlib.sha1(f"{day}|{_loose(point)}|{n}".encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def default_type_name(requested: str | None) -> str:
    lookup = _type_lookup()
    resolved = _resolve_type(requested or "", lookup)
    if resolved:
        return resolved
    if REPAIR_TYPE_NAME in lookup.values():
        return REPAIR_TYPE_NAME
    return next(iter(lookup.values()), REPAIR_TYPE_NAME)


def _local_day(created_at: str, imported: bool) -> str:
    """День записи по местному времени: у загруженных из Excel время условное (полдень), у остальных — настоящее, в UTC."""
    if imported:
        return created_at[:10]
    try:
        moment = dt.datetime.fromisoformat(created_at)
    except ValueError:
        return created_at[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(MSK).date().isoformat()


async def build_plan(parsed: Parsed, default_type: str | None, allow_unknown_points: bool) -> Plan:
    plan = Plan(errors=list(parsed.errors), notes=list(parsed.notes), rows_read=parsed.rows_read)
    fallback_type = default_type_name(default_type)
    lookup = _type_lookup()

    async with get_connection() as conn:
        cur = await conn.execute("select id, address, inventory_number, sim_number, ip_address from points")
        points = [{"id": r[0], "address": r[1] or "", "inventory_number": r[2] or "", "sim_number": r[3] or "", "ip_address": r[4] or ""}
                  for r in await cur.fetchall()]
        # Что уже есть в системе за период файла (с запасом в день: время отчётов хранится в UTC): отчёты бота, диспетчера, форм,
        # прежние загрузки — по ним определяется, какие выезды из файла уже записаны.
        existing: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if parsed.rows:
            low = (min(r.date for r in parsed.rows) - dt.timedelta(days=1)).isoformat()
            high = (max(r.date for r in parsed.rows) + dt.timedelta(days=2)).isoformat()
            cur = await conn.execute(
                "select task_id, point_id, created_at, imported, coalesce(comment, ''), coalesce(fault, ''), coalesce(needs, ''), "
                "result_status, outcome, coalesce(created_by, ''), source from task_visit_reports where created_at >= ? and created_at < ?",
                (low, high),
            )
            for r in await cur.fetchall():
                # Своими («загруженными из журнала») считаются только записи самой этой загрузки (id «imp-…»): отчёт из таблицы Яндекс Форм
                # без задания тоже хранится как запись истории, но принадлежит форме — журнал его только дополняет, не переписывает.
                rec = {"task_id": r[0], "point_id": r[1], "imported": bool(r[3]) and str(r[0]).startswith(ID_PREFIX), "comment": r[4], "fault": r[5], "needs": r[6],
                       "result_status": r[7] or "", "outcome": r[8] or "", "who": r[9], "source": r[10] or "", "created_at": r[2] or ""}
                existing.setdefault((r[1], _local_day(r[2], bool(r[3]))), []).append(rec)
        cur = await conn.execute("select task_id from task_visit_reports where task_id like ?", (ID_PREFIX + "%",))
        used_ids = {r[0] for r in await cur.fetchall()}
    finder = PointFinder(points)
    outcomes_cache: dict[str, dict[str, str]] = {}

    async def outcomes_of(visit_type: str) -> dict[str, str]:
        if visit_type not in outcomes_cache:
            base = visit_type.split(COMBINED_SEPARATOR)[0]
            labels = [] if is_repair_type(visit_type) else await work_types_repository.outcomes_for(base)
            outcomes_cache[visit_type] = {_key(label): label for label in labels}
        return outcomes_cache[visit_type]

    groups: dict[tuple[str, str], list[Item]] = {}
    for r in parsed.rows:
        where = f"Лист «{r.sheet}», строка {r.row}"
        point, how, cands = finder.find(r.point.strip(), icc=r.icc, ip=r.ip, inventory=r.inv, address=r.addr)
        if point is None:
            label = r.point.strip() or r.icc or r.ip or r.inv or r.addr
            if not allow_unknown_points or how == "ambiguous":
                plan.unknown_points[label + (f" (подходят: {', '.join(cands)})" if cands else "")] += 1
                continue
            point = r.point.strip() or label
        elif how == "number":
            if point.strip().casefold() != r.point.strip().casefold():
                plan.loose_points += 1          # «МЕ 123» / «МЕ-123» / номер без названия — один комплекс, путаницы нет
        else:
            plan.found_by[how] += 1

        vtype = fallback_type
        comment = r.work
        if r.note and _key(r.note) != _key(r.work):
            comment = (comment + "\n" if comment else "") + r.note
        if r.vtype:
            if is_aux_type(r.vtype):
                plan.errors.append(f"{where}: «Вспомогательная точка» — не выезд")
                continue
            resolved = _resolve_type(r.vtype, lookup)
            if resolved:
                vtype = resolved
            else:
                plan.unknown_types[r.vtype] += 1
                comment = f"Тип выезда в файле: {r.vtype}" + (f". {comment}" if comment else "")

        result_status, outcome = "", ""
        if r.status:
            matched = (await outcomes_of(vtype)).get(_key(r.status))
            if matched:
                outcome = matched
            else:
                result_status = normalize_status(r.status)
                if not result_status:
                    plan.unknown_statuses[r.status] += 1
                    comment = (comment + "\n" if comment else "") + f"Статус в файле: {r.status}"
                elif "плат" in _key(r.status):       # «демонтирована плата»: в статусе только «Не работает» — слова журнала сохраняем в тексте
                    comment = f"Статус в журнале: {r.status.strip()}" + (f"\n{comment}" if comment else "")
        fault = "" if _key(r.fault) in _GENERIC_REASONS else r.fault
        groups.setdefault((point, r.date.isoformat()), []).append(Item(
            task_id="", point_id=point, visit_type=vtype, result_status=result_status, outcome=outcome,
            comment=comment[:_MAX_COMMENT], removed=r.removed if is_repair_type(vtype) else "",
            installed=r.installed if is_repair_type(vtype) else "", day=r.date, who=r.who[:_MAX_WHO],
            sheet=r.sheet, row=r.row, fault=fault[:_MAX_COMMENT], needs=r.needs[:_MAX_COMMENT],
        ))

    for (point, day), rows in groups.items():
        found = existing.get((point, day), [])
        # Настоящие отчёты — первыми: журнал описывает тот же выезд. Порядок фиксированный (по времени и номеру), чтобы
        # повторная загрузка того же файла сопоставляла строки с теми же записями, а не перемешивала их.
        real = sorted((e for e in found if not e["imported"]), key=lambda e: (e["created_at"], e["task_id"]))
        imp = {e["task_id"]: e for e in found if e["imported"]}
        free = sorted(imp)                                                    # загруженные раньше из файла и ещё не занятые строкой
        paired: list[dict[str, Any] | None] = [None] * len(rows)
        for idx in range(len(rows)):
            if idx < len(real):
                paired[idx] = real[idx]
        # строка журнала → её же прежняя запись (id считается от дня, комплекса и номера строки внутри дня)
        for idx in range(len(rows)):
            if paired[idx] is None:
                task_id = _item_id(day, point, idx)
                if task_id in imp and task_id in free:
                    paired[idx] = imp[task_id]
                    free.remove(task_id)
        for idx in range(len(rows)):                                         # остальным — любые свободные прежние записи этого дня
            if paired[idx] is None and free:
                paired[idx] = imp[free.pop(0)]
        for idx, item in enumerate(rows):
            e = paired[idx]
            if e is not None:
                if not e["imported"]:
                    plan.in_system += 1
                    fill = {}
                    if item.fault and not e["fault"]:
                        fill["fault"] = item.fault
                    if item.needs and not e["needs"]:
                        fill["needs"] = item.needs
                    if item.comment and not e["comment"]:
                        fill["comment"] = item.comment
                    if fill:
                        plan.updates.append((e["task_id"], fill, "fill"))
                    continue
                change = {}
                if item.comment and item.comment != e["comment"]:
                    change["comment"] = item.comment
                if item.fault and item.fault != e["fault"]:
                    change["fault"] = item.fault
                if item.needs and item.needs != e["needs"]:
                    change["needs"] = item.needs
                if item.who and item.who != e["who"]:
                    change["who"] = item.who
                if (item.result_status or item.outcome) and (item.result_status, item.outcome) != (e["result_status"], e["outcome"]):
                    change["result_status"], change["outcome"] = item.result_status, item.outcome
                if change:
                    plan.updates.append((e["task_id"], change, "set"))
                else:
                    plan.duplicates += 1
                continue
            n = idx
            while True:
                task_id = _item_id(day, point, n)
                if task_id not in used_ids:
                    break
                n += 1
            used_ids.add(task_id)
            item.task_id = task_id
            plan.items.append(item)
        if real:
            plan.merged.extend(free)               # настоящий отчёт на этот день есть — оставшиеся записи из журнала были лишними
    plan.items.sort(key=lambda i: (i.day, i.point_id, i.task_id))
    return plan



def new_batch_id() -> str:
    return f"imp-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


async def apply_plan(plan: Plan, batch: str) -> int:
    """Записывает новые строки, дополняет настоящие отчёты тем, чего в них нет, обновляет прежние загрузки по журналу и убирает
    дубли, вытесненные настоящим отчётом; возвращает, сколько новых строк добавлено."""
    if not plan.items and not plan.updates and not plan.merged:
        return 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    added = 0
    async with get_connection() as conn:
        if plan.items:
            await conn.executemany(
                "insert or ignore into task_visit_reports "
                "(task_id, point_id, visit_type, result_status, comment, equipment_removed, equipment_installed, "
                "outcome, fault, needs, created_at, updated_at, created_by, imported, import_batch) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [
                    (i.task_id, i.point_id, i.visit_type, i.result_status, i.comment, i.removed or None, i.installed or None,
                     i.outcome, i.fault or None, i.needs or None, i.created_at, now, i.who or None, batch)
                    for i in plan.items
                ],
            )
        for task_id, fields, mode in plan.updates:
            sets, params = [], []
            for key, value in fields.items():
                column = "created_by" if key == "who" else key
                if mode == "fill":
                    sets.append(f"{column} = case when coalesce({column}, '') = '' then ? else {column} end")
                else:
                    sets.append(f"{column} = ?")
                params.append(value or None if column in ("fault", "needs", "created_by") else value)
            sets.append("updated_at = ?")
            params.append(now)
            where = "task_id = ?" + (" and imported = 1" if mode == "set" else "")
            await conn.execute(f"update task_visit_reports set {', '.join(sets)} where {where}", (*params, task_id))
        for task_id in plan.merged:
            await conn.execute("delete from task_visit_reports where task_id = ? and imported = 1", (task_id,))
        await conn.commit()
        if plan.items:
            cur = await conn.execute("select count(*) from task_visit_reports where import_batch = ?", (batch,))
            (added,) = await cur.fetchone()
    return added


def summarize(plan: Plan, *, dry_run: bool, batch: str | None = None, added: int | None = None) -> dict[str, Any]:
    days = [i.day for i in plan.items]
    top = lambda counter, key: [{key: k, "count": c} for k, c in counter.most_common(50)]  # noqa: E731
    return {
        "dry_run": dry_run,
        "batch": batch,
        "rows_read": plan.rows_read,
        "to_add": len(plan.items),
        "added": added,
        "duplicates": plan.duplicates,
        "in_system": plan.in_system,
        "enriched": len(plan.updates),
        "merged": len(plan.merged),
        "found_by": dict(plan.found_by),
        "skipped_unknown_points": sum(plan.unknown_points.values()),
        "unknown_points": top(plan.unknown_points, "point"),
        "unknown_statuses": top(plan.unknown_statuses, "status"),
        "unknown_types": top(plan.unknown_types, "type"),
        "loose_points": plan.loose_points,
        "errors": plan.errors[:50],
        "error_count": len(plan.errors),
        "notes": plan.notes,
        "date_from": min(days).isoformat() if days else None,
        "date_to": max(days).isoformat() if days else None,
        "sample": [
            {
                "date": i.day.isoformat(), "who": i.who, "point": i.point_id, "visit_type": i.visit_type,
                "status": i.result_status or i.outcome, "comment": i.comment, "fault": i.fault, "needs": i.needs,
            }
            for i in plan.items[:8]
        ],
    }


async def list_batches() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select import_batch, count(*), min(created_at), max(created_at), max(updated_at) "
            "from task_visit_reports where imported = 1 group by import_batch order by max(updated_at) desc limit 30"
        )
        return [
            {"batch": r[0], "count": r[1], "date_from": r[2][:10], "date_to": r[3][:10], "imported_at": r[4]}
            for r in await cur.fetchall()
        ]


async def delete_batch(batch: str) -> int:
    async with get_connection() as conn:
        cur = await conn.execute("delete from task_visit_reports where imported = 1 and import_batch = ?", (batch,))
        await conn.commit()
        return cur.rowcount


async def delete_one(task_id: str) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from task_visit_reports where imported = 1 and task_id = ?", (task_id,))
        await conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------- шаблон
_HEADER_FILL = PatternFill("solid", fgColor="1E293B")
_HEADER_FONT = Font(color="F8FAFC", bold=True)


def build_template() -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "История выездов"
    ws.append(TEMPLATE_HEADERS)
    for col in range(1, len(TEMPLATE_HEADERS) + 1):
        c = ws.cell(row=1, column=col)
        c.fill, c.font = _HEADER_FILL, _HEADER_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for col, width in enumerate([14, 28, 18, 56, 18, 26, 40, 40, 40], start=1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"
    ws.append(["05.03.2026", "Иванов И.И., Петров П.П.", "T-100", "Заменили блок питания, проверили камеру", "Работает", "Ремонт", "", "", ""])
    ws.append(["17.04.2026", "Сидоров С.С.", "T-205", "Нет питания, комплекс снят и сдан на склад", "Демонтирован", "", "", "", ""])
    ws.append(["21.05.2026", "Иванов И.И.", "T-310", "Выезжали, на месте нет связи", "Не работает", "Ремонт",
               "Доступ к шкафу только через охрану", "Сгорел блок питания, роутер не загружается", "Новый блок питания 12 В, роутер на замену"])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 110
    types = [n for n in work_type_names() if not is_aux_type(n)]
    lines = [
        "Как заполнять файл с историей выездов",
        "",
        "1. Одна строка = один выезд на один комплекс (если бригада была на трёх комплексах — три строки).",
        "2. «Дата» — любая обычная дата: 05.03.2026, 5-3-26, 2026-03-05 или дата-ячейка Excel.",
        "3. «Кто выезжал» — фамилии как есть, текстом (можно несколько через запятую).",
        "4. «Комплекс» — номер комплекса так же, как в «Все АПК». Строки с неизвестным номером пропускаются и перечисляются в итоге.",
        "5. «Что было сделано» — любой текст.",
        "6. «Статус» — что стало с комплексом после выезда: " + ", ".join(RESULT_STATUSES) + ". Понимаются и обычные слова: «работает», «не работает», «неисправен», «снят», «поставлен»…",
        "7. «Тип выезда» — необязательно. Если пусто, подставится тип, выбранный при загрузке. Типы сейчас: " + ", ".join(types) + ".",
        "8. Необязательно: «Комментарий», «Что неисправно / причина» (из-за чего комплекс не работал или не работает до сих пор) и «Что нужно для восстановления» — они будут видны в истории комплекса. Если загрузить тот же файл повторно уже с этими столбцами, записи не задвоятся, а дополнятся.",
        "9. Столбцы можно переставлять и добавлять свои — ищутся по заголовку. Загружаются все листы файла, где есть столбцы «Дата» и «Комплекс».",
        "10. «Журнал выездов», который ведётся отдельно (№, Дата, Номер, Код ICC, IP-адрес, Адрес, Инв. №, Статус, Причина, Выполненные работы, Исполнитель, Требуется, Примечания), можно загружать КАК ЕСТЬ, без правки: «Номер» — комплекс, «Причина», «Выполненные работы» и «Примечания», «Требуется» попадут в историю комплекса, а ICC, IP, адрес и инвентарный номер используются только для поиска комплекса, если номера в строке нет. Служебные листы журнала («Сводка», «Справочники»…) пропускаются.",
        "11. Выезд, который в тот же день уже записан (отчёт бота, диспетчера, таблицы Яндекс Форм или прежняя загрузка), второй раз не создаётся: журнал лишь дополняет в нём причину и «что нужно», если там они пусты. Строку журнала можно поправить и загрузить файл снова — запись обновится, а не задвоится.",
        "",
        "Загрузка НЕ меняет сегодняшний статус комплекса и не создаёт заданий в календаре — записи просто появляются в «История выездов» у карточки комплекса.",
        "Повторная загрузка того же файла дубликатов не создаёт. Любую загрузку можно отменить целиком.",
    ]
    for line in lines:
        guide.append([line])
    guide["A1"].font = Font(bold=True, size=13)
    for row in guide.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out
