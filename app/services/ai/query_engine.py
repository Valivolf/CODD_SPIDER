"""Безопасное выполнение «запроса помощника»: модель выдаёт описание запроса (набор, период, фильтры, группировка, подсчёт), а здесь оно
проверяется по каталогу полей РОЛИ вошедшего человека и превращается в параметризованный SELECT. Своего SQL у модели нет: значения
идут только параметрами, имена полей — только из каталога (catalog.py), запрещённое роли поле — ошибка «нет доступа», а не отсутствие
проверки. Тот же движок заполняет таблицы шаблонов Excel/Word (doc_fill.py) — данные всегда из базы, не из «воображения» модели."""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.names import fold_name
from app.db.database import get_connection
from app.services.ai import catalog as cat

MAX_ROWS_LIST = 1000
DEFAULT_LIMIT = 50
TEXT_OPS = {"=", "!=", "contains", "not_contains", "starts", "in", "is_empty", "not_empty"}
NUM_OPS = {"=", "!=", ">", ">=", "<", "<=", "between", "is_empty", "not_empty"}
DATE_OPS = {"=", "!=", ">", ">=", "<", "<=", "between", "is_empty", "not_empty"}
BOOL_OPS = {"=", "!="}
AGG_FNS = {"count", "count_distinct", "sum", "avg", "min", "max"}
BUCKETS = {"day", "week", "month", "year"}
PRESETS = {
    "today": "сегодня", "yesterday": "вчера", "this_week": "эта неделя", "last_week": "прошлая неделя", "this_month": "этот месяц",
    "last_month": "прошлый месяц", "this_quarter": "этот квартал", "last_quarter": "прошлый квартал", "this_year": "этот год",
    "last_year": "прошлый год", "last_7_days": "последние 7 дней", "last_30_days": "последние 30 дней", "last_90_days": "последние 90 дней",
    "last_365_days": "последние 365 дней",
}


class QueryError(ValueError):
    """Запрос нельзя выполнить; текст — человеку (на русском)."""


class AccessDenied(QueryError):
    """Такие данные этой роли недоступны."""


# ---------------------------------------------------------------- значения для сверки и подсказок модели

_enum_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def enum_values(force: bool = False) -> dict[str, list[str]]:
    """Допустимые значения «перечислимых» полей (типы выездов, статусы, категории…) — из справочников приложения."""
    now = time.monotonic()
    if not force and _enum_cache["value"] is not None and now - _enum_cache["at"] < 60:
        return _enum_cache["value"]
    from app.core.work_types import work_type_names

    async def col(conn, sql: str) -> list[str]:
        cur = await conn.execute(sql)
        return [str(r[0]) for r in await cur.fetchall() if r[0] not in (None, "")]

    async with get_connection() as conn:
        outcomes = await col(conn, "select distinct label from work_type_outcomes order by label")
        value = {
            "work_types": [n for n in work_type_names() if n != "Вспомогательная точка"],
            "visit_statuses": ["Работает", "Не работает", "Демонтирован", "Установлен", *outcomes],
            "point_categories": await col(conn, "select name from point_categories order by name"),
            "point_statuses": await col(conn, "select name from point_statuses order by name"),
            "power_sources": ["СЭС", "Электросеть"],
            "equipment_categories": await col(conn, "select name from equipment_categories order by name"),
            "equipment_statuses": await col(conn, "select name from equipment_statuses order by sort_order, name"),
            "equipment_places": ["на складе", "у монтёра", "на комплексе", "неизвестно"],
            "equipment_held": ["выдано монтёру", "снято с комплекса, ждёт приёмки", "снятая SIM осталась у монтёра"],
            "visit_sources": ["бот/диспетчер", "Яндекс Форма", "архив/журнал"],
            "people_roles": ["монтёр", "водитель", "оператор", "склад"],
            "crew_equip": await col(conn, "select distinct equip from crews where equip != '' order by equip"),
        }
    _enum_cache.update(at=now, value=value)
    return value


# ---------------------------------------------------------------- даты и периоды

_DATE_FORMS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "ymd"), (re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{4})-(\d{2})$"), "ym"), (re.compile(r"^(\d{4})$"), "y"),
)


def parse_date(text: Any, *, end: bool = False) -> Optional[dt.date]:
    """«2026-09-30», «30.09.2026», «2026-09» (начало или конец месяца), «2026» (год) → дата. Не дата — None."""
    s = str(text or "").strip()
    for rx, kind in _DATE_FORMS:
        m = rx.match(s)
        if not m:
            continue
        try:
            if kind == "ymd":
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "dmy":
                return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            if kind == "ym":
                y, mo = int(m.group(1)), int(m.group(2))
                first = dt.date(y, mo, 1)
                return (dt.date(y + (mo == 12), mo % 12 + 1, 1) - dt.timedelta(days=1)) if end else first
            y = int(m.group(1))
            return dt.date(y, 12, 31) if end else dt.date(y, 1, 1)
        except ValueError:
            return None
    return None


def period_range(period: Optional[dict], today: dt.date) -> Optional[tuple[dt.date, dt.date]]:
    """{"preset"|"from"/"to"} → (с, по) включительно или None («за всё время»)."""
    if not period:
        return None
    preset = str(period.get("preset") or "").strip()
    if preset and preset != "all":
        if preset not in PRESETS:
            raise QueryError(f"Не понял период «{preset}».")
        if preset == "today":
            return today, today
        if preset == "yesterday":
            d = today - dt.timedelta(days=1)
            return d, d
        if preset == "this_week":
            start = today - dt.timedelta(days=today.weekday())
            return start, start + dt.timedelta(days=6)
        if preset == "last_week":
            start = today - dt.timedelta(days=today.weekday() + 7)
            return start, start + dt.timedelta(days=6)
        if preset == "this_month":
            return today.replace(day=1), parse_date(f"{today:%Y-%m}", end=True)  # type: ignore[return-value]
        if preset == "last_month":
            last = today.replace(day=1) - dt.timedelta(days=1)
            return last.replace(day=1), last
        if preset in ("this_quarter", "last_quarter"):
            q = (today.month - 1) // 3
            if preset == "last_quarter":
                q -= 1
            year = today.year + (q // 4 if q < 0 else 0)
            q %= 4
            start = dt.date(year, q * 3 + 1, 1)
            end_month = q * 3 + 3
            return start, parse_date(f"{year}-{end_month:02d}", end=True)  # type: ignore[return-value]
        if preset == "this_year":
            return dt.date(today.year, 1, 1), dt.date(today.year, 12, 31)
        if preset == "last_year":
            return dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31)
        days = {"last_7_days": 7, "last_30_days": 30, "last_90_days": 90, "last_365_days": 365}[preset]
        return today - dt.timedelta(days=days - 1), today
    start_raw, end_raw = period.get("from"), period.get("to")
    if not start_raw and not end_raw:
        return None
    start = parse_date(start_raw) if start_raw else None
    end = parse_date(end_raw, end=True) if end_raw else None
    if start_raw and start is None:
        raise QueryError(f"Не понял дату начала периода «{start_raw}».")
    if end_raw and end is None:
        raise QueryError(f"Не понял дату конца периода «{end_raw}».")
    start = start or dt.date(2000, 1, 1)
    end = end or dt.date(2100, 12, 31)
    if start > end:
        start, end = end, start
    return start, end


def fmt_date(d: Any) -> str:
    s = str(d or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else s


# ---------------------------------------------------------------- разбор и проверка запроса

def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, "")]
    return [str(value)]


def _resolve_enum(field: cat.Field, raw: str, enums: dict[str, list[str]]) -> tuple[str, bool]:
    """Значение из запроса → точное значение справочника: (значение, найдено). Сначала без учёта регистра, потом по началу/вхождению."""
    values = enums.get(field.enum or "", [])
    if not values:
        return raw, True
    f = fold_name(raw)
    for v in values:
        if fold_name(v) == f:
            return v, True
    found = [v for v in values if f and (fold_name(v).startswith(f) or f in fold_name(v))]
    if len(found) == 1:
        return found[0], True
    stem = f[:5]
    stems = [v for v in values if len(stem) >= 4 and fold_name(v).startswith(stem)]
    if len(stems) == 1:
        return stems[0], True
    return raw, False


@dataclass
class Compiled:
    dataset: cat.Dataset
    sql: str
    params: list
    count_sql: Optional[str]
    count_params: list
    columns: list[dict]
    mode: str                       # list | aggregate
    limit: int
    spec: dict
    notes: list[str] = field(default_factory=list)


def _field_sql(f: cat.Field) -> tuple[str, list]:
    if f.dynamic is not None:
        return f.dynamic()
    return f.sql, []


def normalize_spec(raw: Any) -> dict:
    """Приводит ответ модели к единому виду; лишнее отбрасывает, недостающее заполняет."""
    if not isinstance(raw, dict):
        raise QueryError("Не удалось разобрать запрос.")
    period = raw.get("period") if isinstance(raw.get("period"), dict) else {}
    filters = []
    for f in raw.get("filters") or []:
        if not isinstance(f, dict) or not f.get("field"):
            continue
        filters.append({"field": str(f["field"]).strip(), "op": str(f.get("op") or "=").strip().lower(),
                        "value": f.get("value"), "values": f.get("values") if isinstance(f.get("values"), list) else []})
    aggs = []
    for a in raw.get("aggregates") or []:
        if isinstance(a, dict) and a.get("fn"):
            aggs.append({"fn": str(a["fn"]).strip().lower(), "field": (str(a["field"]).strip() if a.get("field") else None), "alias": str(a.get("alias") or "")})
    order = []
    for o in raw.get("order_by") or []:
        if isinstance(o, dict) and o.get("field"):
            order.append({"field": str(o["field"]).strip(), "dir": "desc" if str(o.get("dir") or "").lower() == "desc" else "asc"})
    try:
        limit = int(raw.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return {
        "dataset": str(raw.get("dataset") or "").strip(),
        "period": {"preset": str(period.get("preset") or "").strip() or None, "from": period.get("from") or None, "to": period.get("to") or None},
        "filters": filters, "aggregates": aggs, "group_by": [str(g).strip() for g in (raw.get("group_by") or []) if g],
        "select": [str(s).strip() for s in (raw.get("select") or []) if s], "order_by": order, "limit": limit,
    }


def compile_query(spec_in: dict, role: str, enums: dict[str, list[str]], today: Optional[dt.date] = None, *, max_rows: int = MAX_ROWS_LIST) -> Compiled:
    spec = normalize_spec(spec_in)
    today = today or dt.date.today()
    ds = cat.DATASETS.get(spec["dataset"])
    if ds is None:
        raise QueryError(f"Не знаю набор данных «{spec['dataset']}».")
    if not cat.dataset_allowed(role, ds):
        raise AccessDenied(f"Раздел «{ds.label}» вашей роли недоступен.")
    visible = {f.name: f for f in cat.role_fields(role, ds)}

    def need(name: str, what: str = "поля") -> cat.Field:
        base = name.split(":")[0]
        f = visible.get(base)
        if f is None:
            if ds.field(base) is not None:
                raise AccessDenied(f"Данные «{ds.field(base).label}» (поле «{base}») вашей роли недоступны.")
            raise QueryError(f"В наборе «{ds.label}» нет {what} «{name}».")
        return f

    where: list[tuple[str, list]] = []
    notes: list[str] = []
    if ds.where_sql:
        where.append((ds.where_sql, []))

    # период
    rng = period_range(spec["period"], today)
    if rng:
        start, end = rng
        if ds.period_overlap:
            lo, hi = need(ds.period_overlap[0]), need(ds.period_overlap[1])
            lo_sql, lo_p = _field_sql(lo)
            hi_sql, hi_p = _field_sql(hi)
            where.append((f"({lo_sql} <= ? and {hi_sql} >= ?)", [*lo_p, end.isoformat(), *hi_p, start.isoformat()]))
        elif ds.date_field:
            df = need(ds.date_field)
            sql, p = _field_sql(df)
            where.append((f"({sql} >= ? and {sql} <= ?)", [*p, start.isoformat(), *p, end.isoformat()]))
        # набор без даты (комплексы, оборудование…): «сейчас/в этом месяце» к нему не относится — период просто не применяется

    # условия
    for flt in spec["filters"]:
        f = need(flt["field"])
        sql, p = _field_sql(f)
        op = flt["op"]
        allowed = {cat.TEXT: TEXT_OPS, cat.ENUM: TEXT_OPS, cat.DATE: DATE_OPS, cat.NUMBER: NUM_OPS, cat.BOOL: BOOL_OPS}[f.type]
        if op in ("==", "equals", "eq"):
            op = "="
        if op in ("<>", "ne", "not_equals"):
            op = "!="
        if op not in allowed:
            raise QueryError(f"Для поля «{f.label}» не подходит условие «{op}».")
        values = _as_list(flt["values"]) or _as_list(flt["value"])
        if op in ("is_empty", "not_empty"):
            if f.type == cat.NUMBER:
                frag, ps = f"({sql} is null)", list(p)
            else:
                frag, ps = f"({sql} = '' or {sql} is null)", [*p, *p]
            where.append((frag if op == "is_empty" else f"not {frag}", ps))
            continue
        if not values:
            raise QueryError(f"В условии по полю «{f.label}» не указано значение.")
        if f.type in (cat.TEXT, cat.ENUM):
            resolved: list[str] = []
            for v in values:
                if f.type == cat.ENUM and op in ("=", "!=", "in") and f.enum:
                    val, ok = _resolve_enum(f, v, enums)
                    if not ok:
                        known = enums.get(f.enum or "", [])
                        raise QueryError(f"Не нашёл значение «{v}» в поле «{f.label}»." + (f" Бывает: {', '.join(known[:14])}." if known else ""))
                    resolved.append(val)
                else:
                    resolved.append(v)
            if op == "=":
                where.append((f"pylower({sql}) = pylower(?)", [*p, resolved[0]]))
            elif op == "!=":
                where.append((f"pylower({sql}) != pylower(?)", [*p, resolved[0]]))
            elif op == "in":
                where.append((f"pylower({sql}) in ({','.join('pylower(?)' for _ in resolved)})", [*p, *resolved]))
            elif op == "contains":
                where.append((f"pylower({sql}) like pylower(?) escape '\\'", [*p, f"%{_like_escape(resolved[0])}%"]))
            elif op == "not_contains":
                where.append((f"pylower({sql}) not like pylower(?) escape '\\'", [*p, f"%{_like_escape(resolved[0])}%"]))
            elif op == "starts":
                where.append((f"pylower({sql}) like pylower(?) escape '\\'", [*p, f"{_like_escape(resolved[0])}%"]))
        elif f.type == cat.DATE:
            dates = []
            for i, v in enumerate(values):
                use_end = op in ("<=", ">") or (op == "between" and i == 1)     # «до сентября» — по конец месяца, «после сентября» — с его конца
                d = parse_date(v, end=use_end)
                if d is None:
                    raise QueryError(f"Не понял дату «{v}» в условии по полю «{f.label}».")
                dates.append(d.isoformat())
            if op == "between":
                if len(dates) < 2:
                    raise QueryError(f"Для условия «между» по полю «{f.label}» нужны две даты.")
                where.append((f"({sql} != '' and {sql} >= ? and {sql} <= ?)", [*p, *p, dates[0], *p, dates[1]]))
            elif op in ("<", "<="):
                where.append((f"({sql} != '' and {sql} {op} ?)", [*p, *p, dates[0]]))
            else:
                where.append((f"{sql} {op} ?", [*p, dates[0]]))
        elif f.type == cat.NUMBER:
            try:
                nums = [float(str(v).replace(",", ".")) for v in values]
            except ValueError:
                raise QueryError(f"В условии по полю «{f.label}» нужно число.")
            if op == "between":
                if len(nums) < 2:
                    raise QueryError(f"Для условия «между» по полю «{f.label}» нужны два числа.")
                where.append((f"({sql} >= ? and {sql} <= ?)", [*p, nums[0], *p, nums[1]]))
            else:
                where.append((f"{sql} {op} ?", [*p, nums[0]]))
        else:
            word = fold_name(values[0])
            yes = word in ("да", "true", "1", "yes")
            no = word in ("нет", "false", "0", "no")
            if not (yes or no):
                raise QueryError(f"В условии по полю «{f.label}» нужно «да» или «нет».")
            want = "да" if yes else "нет"
            where.append((f"{sql} {'=' if op == '=' else '!='} ?", [*p, want]))

    aggs, group_by, select = spec["aggregates"], spec["group_by"], spec["select"]
    mode = "aggregate" if (aggs or group_by) else "list"
    columns: list[dict] = []
    sel_parts: list[tuple[str, list]] = []
    group_parts: list[tuple[str, list]] = []
    group_cols: dict[str, int] = {}

    if mode == "aggregate":
        for g in group_by:
            base, _, bucket = g.partition(":")
            f = need(base)
            if not f.groupable:
                raise QueryError(f"По полю «{f.label}» группировать нельзя.")
            sql, p = _field_sql(f)
            label = f.label.split(" (")[0]            # короткая подпись столбца (пояснение в скобках — только модели)
            if bucket:
                if f.type != cat.DATE or bucket not in BUCKETS:
                    raise QueryError(f"Группировка «{g}» не поддерживается.")
                if bucket == "month":
                    sql = f"substr({sql}, 1, 7)"
                elif bucket == "year":
                    sql = f"substr({sql}, 1, 4)"
                elif bucket == "week":
                    sql = f"strftime('%Y-W%W', {sql})"
                label = f"{label} ({ {'day': 'по дням', 'week': 'по неделям', 'month': 'по месяцам', 'year': 'по годам'}[bucket] })"
            group_cols[g] = len(columns)
            columns.append({"key": g, "label": label, "type": f.type if not bucket or bucket == "day" else cat.TEXT, "enum": f.enum})
            sel_parts.append((sql, list(p)))
            group_parts.append((sql, list(p)))
        if not aggs:
            aggs = [{"fn": "count", "field": None, "alias": ""}]
        for i, a in enumerate(aggs):
            fn = a["fn"]
            if fn not in AGG_FNS:
                raise QueryError(f"Не знаю, как посчитать «{fn}».")
            if fn == "count":
                columns.append({"key": f"agg{i}", "label": f"Количество ({ds.row_label})", "type": cat.NUMBER})
                sel_parts.append(("count(*)", []))
                continue
            if not a["field"]:
                raise QueryError(f"Для подсчёта «{fn}» нужно поле.")
            f = need(a["field"])
            sql, p = _field_sql(f)
            if fn == "count_distinct":
                columns.append({"key": f"agg{i}", "label": f"Сколько разных: {f.label}", "type": cat.NUMBER})
                sel_parts.append((f"count(distinct nullif({sql}, ''))", list(p)))
            elif fn in ("sum", "avg"):
                if f.type != cat.NUMBER:
                    raise QueryError(f"Сумму и среднее можно считать только по числовым полям, а «{f.label}» — не число.")
                columns.append({"key": f"agg{i}", "label": ("Сумма: " if fn == "sum" else "Среднее: ") + f.label, "type": cat.NUMBER})
                sel_parts.append((f"sum({sql})" if fn == "sum" else f"round(avg({sql}), 2)", list(p)))
            else:
                if f.type not in (cat.NUMBER, cat.DATE):
                    raise QueryError(f"Наименьшее и наибольшее считаются по числам и датам, а «{f.label}» — не то и не другое.")
                if f.type == cat.DATE:
                    label = ("Самое раннее: " if fn == "min" else "Самое позднее: ") + f.label
                else:
                    label = ("Наименьшее: " if fn == "min" else "Наибольшее: ") + f.label
                columns.append({"key": f"agg{i}", "label": label, "type": f.type})
                sel_parts.append((f"{fn}(nullif({sql}, ''))" if f.type == cat.DATE else f"{fn}({sql})", list(p)))
    else:
        if select:
            names = list(dict.fromkeys(select))[:14]
            for n in names:
                need(n)                       # запрошено явно: чужое или несуществующее — ошибка, а не молчаливое «пропало»
        else:
            names = [n for n in ds.default_select if n in visible]
        for n in names:
            f = need(n)
            sql, p = _field_sql(f)
            columns.append({"key": n, "label": f.label.split(" (")[0], "type": f.type, "enum": f.enum})
            sel_parts.append((sql, list(p)))

    # порядок
    order_parts: list[tuple[str, list]] = []
    orders = spec["order_by"] or ([{"field": o[0], "dir": o[1]} for o in ds.default_order if o[0] in visible] if mode == "list" else [])
    for o in orders:
        key = o["field"]
        direction = "desc" if o["dir"] == "desc" else "asc"
        if mode == "aggregate":
            idx = next((i for i, c in enumerate(columns) if c["key"] == key), None)          # по ключу группировки
            if idx is None:
                aliases = [a["alias"] for a in aggs]
                m = re.fullmatch(r"agg(\d+)", key)
                if key and key in aliases:
                    idx = len(group_by) + aliases.index(key)
                elif m and int(m.group(1)) < len(aggs):
                    idx = len(group_by) + int(m.group(1))
                elif key.strip().casefold() in ("count", "n", "total", "количество", "кол-во", "итого"):
                    idx = len(group_by)                                                    # «по количеству» — первый подсчёт
            if idx is None:
                notes.append(f"Сортировка по «{key}» пропущена: при подсчёте сортировать можно только по группировке или итогу.")
                continue
            order_parts.append((f"{idx + 1} {direction}", []))
        else:
            f = need(key)
            sql, p = _field_sql(f)
            order_parts.append((f"{sql} {direction}", list(p)))
    if mode == "aggregate" and not order_parts:
        if group_by and group_by[0].partition(":")[0] and cat_field_is_date(ds, group_by[0]):
            order_parts.append(("1 asc", []))
        else:
            order_parts.append((f"{len(group_by) + 1} desc", []))

    limit = spec["limit"] or DEFAULT_LIMIT
    limit = max(1, min(limit, max_rows))

    from_sql = ds.from_sql
    params: list = []
    sql_text = "select " + ", ".join(s for s, _ in sel_parts)
    for _, p in sel_parts:
        params.extend(p)
    sql_text += f" from {from_sql}"
    count_sql = count_params = None
    if where:
        w_sql = " and ".join(w for w, _ in where)
        sql_text += f" where {w_sql}"
        w_params = [x for _, p in where for x in p]
        params.extend(w_params)
    else:
        w_sql, w_params = "", []
    if mode == "aggregate" and group_parts:
        sql_text += " group by " + ", ".join(s for s, _ in group_parts)
        for _, p in group_parts:
            params.extend(p)
    if order_parts:
        sql_text += " order by " + ", ".join(s for s, _ in order_parts)
        for _, p in order_parts:
            params.extend(p)
    sql_text += " limit ?"
    params.append(limit + 1)                  # +1 — чтобы понять, что строк больше
    if mode == "list":
        count_sql = f"select count(*) from {from_sql}" + (f" where {w_sql}" if w_sql else "")
    return Compiled(ds, sql_text, params, count_sql, list(w_params), columns, mode, limit, spec, notes)


def cat_field_is_date(ds: cat.Dataset, name: str) -> bool:
    f = ds.field(name.partition(":")[0])
    return bool(f and f.type == cat.DATE)


# ---------------------------------------------------------------- выполнение и подпись

@dataclass
class Result:
    dataset: str
    dataset_label: str
    columns: list[dict]
    rows: list[list]
    total: Optional[int]
    truncated: bool
    mode: str
    description: str
    spec: dict


def _cell(value: Any, ctype: str) -> Any:
    if value is None:
        return ""
    if ctype == cat.DATE:
        return fmt_date(value)
    if isinstance(value, float):
        return int(value) if value.is_integer() else round(value, 2)
    return value


async def run(spec: dict, role: str, *, today: Optional[dt.date] = None, max_rows: int = MAX_ROWS_LIST) -> Result:
    enums = await enum_values()
    compiled = compile_query(spec, role, enums, today, max_rows=max_rows)
    async with get_connection() as conn:
        cur = await conn.execute(compiled.sql, compiled.params)
        raw = await cur.fetchall()
        total = None
        if compiled.count_sql and len(raw) > compiled.limit:
            cur = await conn.execute(compiled.count_sql, compiled.count_params)
            (total,) = await cur.fetchone()
    truncated = len(raw) > compiled.limit
    raw = raw[: compiled.limit]
    rows = [[_cell(v, compiled.columns[i]["type"]) for i, v in enumerate(r)] for r in raw]
    return Result(
        dataset=compiled.dataset.name, dataset_label=compiled.dataset.label, columns=[{k: v for k, v in c.items() if k != "enum"} for c in compiled.columns],
        rows=rows, total=total if truncated else len(rows), truncated=truncated, mode=compiled.mode,
        description=describe(compiled.spec, role, enums, today), spec=compiled.spec,
    )


def describe(spec: dict, role: str, enums: dict[str, list[str]], today: Optional[dt.date] = None) -> str:
    """«Как посчитано» человеческим языком: что за набор, период, условия, группировка. Без значений чувствительных полей."""
    today = today or dt.date.today()
    ds = cat.DATASETS.get(spec.get("dataset", ""))
    if ds is None:
        return ""
    parts = [ds.label.split(" (")[0]]
    try:
        rng = period_range(spec.get("period"), today)
    except QueryError:
        rng = None
    if rng and (ds.date_field or ds.period_overlap):
        parts.append(f"период {fmt_date(rng[0].isoformat())} – {fmt_date(rng[1].isoformat())}")
    ops = {"=": "=", "!=": "≠", ">": ">", ">=": "≥", "<": "<", "<=": "≤", "contains": "содержит", "not_contains": "не содержит", "starts": "начинается с",
           "in": "из списка", "between": "между", "is_empty": "пусто", "not_empty": "не пусто"}
    for f in spec.get("filters") or []:
        field_def = ds.field(f["field"]) if isinstance(f, dict) else None
        label = (field_def.label.split(" (")[0] if field_def else f.get("field"))
        if field_def is not None and field_def.sensitive:
            parts.append(f"{label}: …")
            continue
        vals = _as_list(f.get("values")) or _as_list(f.get("value"))
        shown = ", ".join(f"«{v}»" for v in vals)
        parts.append(f"{label} {ops.get(f.get('op'), f.get('op'))} {shown}".strip())
    gb = [g for g in spec.get("group_by") or []]
    if gb:
        labels = []
        for g in gb:
            base, _, bucket = g.partition(":")
            fd = ds.field(base)
            labels.append((fd.label.split(" (")[0] if fd else base) + (f" ({bucket})" if bucket else ""))
        parts.append("по: " + ", ".join(labels))
    return " · ".join(parts)


def render(result: Result) -> dict:
    """Ответ без участия модели: число или таблица + подпись «как посчитано»."""
    if result.mode == "aggregate" and len(result.columns) == 1 and len(result.rows) == 1:
        value = result.rows[0][0]
        return {"kind": "number", "label": result.columns[0]["label"], "value": value,
                "text": f"{result.columns[0]['label']}: {value}", "description": result.description}
    if not result.rows:
        return {"kind": "empty", "text": "По этому запросу ничего не нашлось.", "description": result.description}
    text = ""
    if result.truncated:
        text = f"Показаны первые {len(result.rows)} из {result.total if result.total else 'многих'}."
    elif result.mode == "list":
        text = f"Найдено: {len(result.rows)}."
    return {"kind": "table", "text": text, "columns": result.columns, "rows": result.rows, "total": result.total,
            "truncated": result.truncated, "description": result.description}
