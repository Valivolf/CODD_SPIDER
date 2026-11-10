"""«Статистика» → вкладка «Счётчики»: именованные счётчики, каждый считает по набору выбранных фильтров.

Счётчик — это сохранённый набор условий (словарь, хранится в stat_counters.filters как JSON; числа нигде не хранятся и считаются при каждом открытии вкладки).
Что считаем (`count`):
  • «points»  — комплексы, подходящие под ВСЕ условия (по умолчанию);
  • «visits»  — выезды (отчёты) за период на подходящие комплексы;
  • «changes» — смены статуса за период у подходящих комплексов.
Условия (пустое значение — «не важно»):
  • о самом комплексе: категории, статус сейчас, питание, поверка (действует / просрочена / нет данных / истекает в ближайшие N дней), есть ли SIM / IP / описание неисправности,
    подгруппы, текст в номере/адресе/описании, «в текущем статусе не менее N дней»;
  • о выездах за период: «были» / «не было», типы работ, итог выезда («Работает», «Не работает»…), не менее N выездов;
  • о сменах статуса за период: «были» / «не было», в какой статус перешёл, из какого.
Если выбран тип работ, итог выезда, статус «в/из» или считаются выезды / смены статуса, а «были / не было» оставлено «Не важно», это понимается как «были»: условие с таким
уточнением отбирает только комплексы, на которых такие выезды (смены) действительно были (effective_modes) — иначе выбранный тип или итог ничего бы не отбирали и в числе были бы все комплексы.
Период (`period`) общий для выездов и смен статуса: всё время, сегодня, последние N дней, этот / прошлый месяц, этот год или свои даты.
Разбивка (`group_by`): итог можно разложить по категориям, статусам, питанию или поверке.

Примеры: «сколько камер категории X сейчас работают и на них были выезды за 30 дней» — категории=X, статус=Работает, выезды=были, период=30 дней;
«сколько комплексов с действующей поверкой сломалось за месяц» — поверка=действует, смены статуса=были, в статус=«Не работает», период=этот месяц.
Всё, что здесь считается, только читается из базы; запись — только самих счётчиков (app/repositories/stat_counters_repository.py)."""
from __future__ import annotations

import datetime as dt
import io
from typing import Any, Optional

from app.core import work_types as wt
from app.core.point_ref import strict_key
from app.core.power_source import POWER_SOURCES
from app.db.database import get_connection
from app.repositories.task_visit_reports_repository import RESULT_STATUSES

NAME_MAX = 80
ITEM_MAX = 80
LIST_MAX = 60
TEXT_MAX = 100
LIST_LIMIT = 10000           # сколько комплексов показывает список «откуда число» и выгрузка в Excel (в справочнике порядка трёх тысяч комплексов — список полный)

UNKNOWN_STATUS = "Не известно"
NO_CATEGORY = "Без категории"
NO_POWER = "Не указано"

COUNT_WHAT = ("points", "visits", "changes")
PERIOD_KINDS = ("all", "today", "days", "month", "prev_month", "year", "range")
VERIFICATION = ("", "valid", "expired", "none", "soon")
TRI = ("", "yes", "no")
EVENT_MODE = ("", "had", "none")
GROUP_BY = ("", "category", "status", "power", "verification")

DEFAULTS: dict[str, Any] = {
    "count": "points",
    "period": {"kind": "all", "days": 30, "from": "", "to": ""},
    "categories": [], "statuses": [], "power": [],
    "verification": "", "verification_days": 30,
    "sim": "", "ip": "", "fault": "",
    "groups": [], "text": "", "days_in_status": None,
    "visits": "", "visit_types": [], "visit_results": [], "visits_min": None,
    "changes": "", "change_to": [], "change_from": [],
    "group_by": "",
}


class CounterError(ValueError):
    """Набор условий неверен — текст показывается пользователю."""


def effective_modes(f: dict[str, Any]) -> tuple[str, str]:
    """(режим выездов, режим смен статуса): '' / 'had' / 'none'. «Не важно» при выбранных уточнениях (тип работ, итог выезда, статус «в/из», «считаем выезды / смены»)
    означает «были»: иначе уточнение ничего не отбирало бы (в числе и в списке оказывались бы все комплексы)."""
    visits = f["visits"] or ("had" if (f["visit_types"] or f["visit_results"] or f["count"] == "visits") else "")
    changes = f["changes"] or ("had" if (f["change_to"] or f["change_from"] or f["count"] == "changes") else "")
    return visits, changes


def _enum(raw: dict, key: str, allowed: tuple, label: str) -> str:
    value = raw.get(key, DEFAULTS[key])
    value = "" if value is None else str(value)
    if value not in allowed:
        raise CounterError(f"Неизвестное значение условия «{label}»")
    return value


def _strings(raw: dict, key: str, label: str) -> list[str]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise CounterError(f"Условие «{label}» должно быть списком")
    out: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())[:ITEM_MAX]
        if text and text not in out:
            out.append(text)
    if len(out) > LIST_MAX:
        raise CounterError(f"В условии «{label}» слишком много значений (не больше {LIST_MAX})")
    return out


def _int(raw: dict, key: str, label: str, lo: int, hi: int, default: Optional[int]) -> Optional[int]:
    value = raw.get(key)
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise CounterError(f"Условие «{label}»: нужно целое число") from None
    if not lo <= n <= hi:
        raise CounterError(f"Условие «{label}»: число от {lo} до {hi}")
    return n


def _date(value: Any, label: str) -> str:
    try:
        return dt.date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        raise CounterError(f"{label}: укажите дату") from None


def normalize(raw: Any) -> dict[str, Any]:
    """Проверенный и приведённый к полной форме набор условий (лишние ключи отбрасываются)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CounterError("Набор условий должен быть объектом")
    f: dict[str, Any] = {
        "count": _enum(raw, "count", COUNT_WHAT, "Что считаем") or "points",
        "verification": _enum(raw, "verification", VERIFICATION, "Поверка"),
        "sim": _enum(raw, "sim", TRI, "SIM"), "ip": _enum(raw, "ip", TRI, "IP-адрес"), "fault": _enum(raw, "fault", TRI, "Причина неисправности"),
        "visits": _enum(raw, "visits", EVENT_MODE, "Выезды"), "changes": _enum(raw, "changes", EVENT_MODE, "Смены статуса"),
        "group_by": _enum(raw, "group_by", GROUP_BY, "Разбивка"),
        "verification_days": _int(raw, "verification_days", "Поверка: дней", 1, 3650, 30),
        "days_in_status": _int(raw, "days_in_status", "Дней в текущем статусе", 1, 3650, None),
        "visits_min": _int(raw, "visits_min", "Выездов не менее", 1, 1000, None),
        "text": " ".join(str(raw.get("text") or "").split())[:TEXT_MAX],
    }
    for key, label in (("categories", "Категории"), ("statuses", "Статус сейчас"), ("power", "Питание"), ("visit_types", "Типы выездов"),
                       ("visit_results", "Итог выезда"), ("change_to", "Перешёл в статус"), ("change_from", "Перешёл из статуса")):
        f[key] = _strings(raw, key, label)
    groups = raw.get("groups") or []
    if not isinstance(groups, list):
        raise CounterError("Условие «Подгруппы» должно быть списком")
    ids: list[int] = []
    for g in groups:
        try:
            gid = int(g)
        except (TypeError, ValueError):
            raise CounterError("Подгруппа указана неверно") from None
        if gid not in ids:
            ids.append(gid)
    if len(ids) > LIST_MAX:
        raise CounterError("Выбрано слишком много подгрупп")
    f["groups"] = ids
    p = raw.get("period") or {}
    if not isinstance(p, dict):
        raise CounterError("Период указан неверно")
    kind = str(p.get("kind") or "all")
    if kind not in PERIOD_KINDS:
        raise CounterError("Неизвестный период")
    period: dict[str, Any] = {"kind": kind, "days": _int(p, "days", "Период: дней", 1, 3650, 30), "from": "", "to": ""}
    if kind == "range":
        period["from"], period["to"] = _date(p.get("from"), "Период: начало"), _date(p.get("to"), "Период: конец")
        if period["from"] > period["to"]:
            raise CounterError("Период: начальная дата позже конечной")
    f["period"] = period
    return f


def period_bounds(period: dict[str, Any], today: dt.date) -> Optional[tuple[str, str]]:
    """(с какого дня, по какой день включительно) в виде ISO-дат; None — за всё время."""
    kind = period.get("kind", "all")
    if kind == "all":
        return None
    if kind == "today":
        return today.isoformat(), today.isoformat()
    if kind == "days":
        n = int(period.get("days") or 30)
        return (today - dt.timedelta(days=n - 1)).isoformat(), today.isoformat()
    if kind == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if kind == "prev_month":
        last = today.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=1).isoformat(), last.isoformat()
    if kind == "year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return period["from"], period["to"]


def _ts_range(bounds: Optional[tuple[str, str]]) -> tuple[str, list[str]]:
    """Условие SQL по столбцу времени записи (ISO-дата-время; день «по» включительно) и его параметры."""
    if not bounds:
        return "", []
    return " and {col} >= ? and {col} <= ?", [bounds[0], bounds[1] + "T23:59:59.999999"]


def _fmt_day(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def period_text(period: dict[str, Any]) -> str:
    kind = period.get("kind", "all")
    if kind == "all":
        return "за всё время"
    if kind == "today":
        return "сегодня"
    if kind == "days":
        return f"за последние {int(period.get('days') or 30)} дн."
    if kind == "month":
        return "за этот месяц"
    if kind == "prev_month":
        return "за прошлый месяц"
    if kind == "year":
        return "за этот год"
    return f"с {_fmt_day(period['from'])} по {_fmt_day(period['to'])}"


def describe(f: dict[str, Any], group_names: Optional[dict[int, str]] = None) -> list[str]:
    """Условия счётчика словами — по одной строке на условие («Категория: A, B»); одна и та же для плитки на странице и для листа «Условия» в Excel."""
    names = group_names or {}
    lines = [{"points": "Считаем: комплексы", "visits": "Считаем: выезды (отчёты о выездах)", "changes": "Считаем: смены статуса"}[f["count"]]]
    if f["categories"]:
        lines.append("Категория: " + ", ".join(f["categories"]))
    if f["statuses"]:
        lines.append("Статус сейчас: " + ", ".join(f["statuses"]))
    if f["power"]:
        lines.append("Питание: " + ", ".join(f["power"]))
    if f["verification"]:
        lines.append("Поверка: " + {"valid": "действует", "expired": "просрочена", "none": "нет данных о сроке", "soon": f"истекает в ближайшие {f['verification_days']} дн."}[f["verification"]])
    for key, label, yes, no in (("sim", "SIM", "есть", "нет"), ("ip", "IP-адрес", "указан", "не указан"), ("fault", "Причина неисправности", "заполнена", "не заполнена")):
        if f[key]:
            lines.append(f"{label}: {yes if f[key] == 'yes' else no}")
    if f["groups"]:
        lines.append("В подгруппе: " + ", ".join(names.get(g, f"№{g}") for g in f["groups"]))
    if f["text"]:
        lines.append(f"В номере, адресе или описании есть «{f['text']}»")
    if f["days_in_status"]:
        lines.append(f"В текущем статусе не менее {f['days_in_status']} дн.")
    when = period_text(f["period"])
    v_mode, c_mode = effective_modes(f)
    if f["visits"] or f["count"] == "visits" or f["visit_types"] or f["visit_results"] or f["visits_min"]:
        part = {"had": "были", "none": "не было", "": "любые"}[v_mode] + f" {when}"
        extra = []
        if f["visit_types"]:
            extra.append("типы: " + ", ".join(f["visit_types"]))
        if f["visit_results"]:
            extra.append("итог: " + ", ".join(f["visit_results"]))
        if f["visits_min"]:
            extra.append(f"не менее {f['visits_min']}")
        lines.append("Выезды: " + part + (" (" + "; ".join(extra) + ")" if extra else ""))
    if f["changes"] or f["count"] == "changes" or f["change_to"] or f["change_from"]:
        part = {"had": "были", "none": "не было", "": "любые"}[c_mode] + f" {when}"
        extra = []
        if f["change_to"]:
            extra.append("в статус: " + ", ".join(f["change_to"]))
        if f["change_from"]:
            extra.append("из статуса: " + ", ".join(f["change_from"]))
        lines.append("Смены статуса: " + part + (" (" + "; ".join(extra) + ")" if extra else ""))
    if f["group_by"]:
        lines.append("Разбивка: по " + {"category": "категориям", "status": "статусам", "power": "питанию", "verification": "поверке"}[f["group_by"]])
    return lines


# ---------------------------------------------------------------- данные для подсчёта (читаются один раз на запрос и общие для всех счётчиков)
class _Data:
    def __init__(self, today: dt.date):
        self.today = today
        self.points: list[dict[str, Any]] = []
        self._cache: dict[Any, Any] = {}

    async def load_points(self) -> None:
        async with get_connection() as conn:
            cur = await conn.execute(
                """select p.id, p.address, p.description, p.status, p.verification_valid_until, p.power_source, p.sim_number, p.ip_address,
                          p.fault_reason, coalesce(pc.name, '')
                   from points p left join point_categories pc on pc.id = p.category_id order by p.id""")
            for pid, address, description, status, verify, power, sim, ip, fault, category in await cur.fetchall():
                self.points.append({
                    "id": pid, "address": address or "", "description": description or "", "status": (status or "").strip() or UNKNOWN_STATUS,
                    "verify": (verify or "").strip(), "power": (power or "").strip() or NO_POWER, "sim": bool((sim or "").strip()), "ip": bool((ip or "").strip()),
                    "fault": bool((fault or "").strip()), "category": category or NO_CATEGORY,
                })
        for p in self.points:
            p["verify_state"], p["verify_left"] = self._verification(p["verify"])

    def _verification(self, value: str) -> tuple[str, Optional[int]]:
        """(valid | expired | none, сколько дней до конца срока). Пусто или не дата — «нет данных»."""
        if not value:
            return "none", None
        try:
            left = (dt.date.fromisoformat(value[:10]) - self.today).days
        except ValueError:
            return "none", None
        return ("valid" if left >= 0 else "expired"), left

    async def visits(self, bounds, types: tuple, results: tuple) -> dict[str, int]:
        """{номер комплекса: сколько отчётов о выездах} за период, по типам работ и итогам."""
        key = ("visits", bounds, types, results)
        if key not in self._cache:
            cond, params = _ts_range(bounds)
            sql = "select point_id, visit_type, count(*) from task_visit_reports where 1 = 1" + cond.format(col="created_at")
            if results:
                sql += f" and result_status in ({','.join('?' for _ in results)})"
                params += list(results)
            sql += " group by point_id, visit_type"
            out: dict[str, int] = {}
            async with get_connection() as conn:
                cur = await conn.execute(sql, params)
                for pid, vtype, n in await cur.fetchall():
                    if types and not (set(types) & {t.strip() for t in (vtype or "").split(" + ")}):      # «Инвентаризация + Ремонт» — это и то, и другое
                        continue
                    out[pid] = out.get(pid, 0) + n
            self._cache[key] = out
        return self._cache[key]

    async def changes(self, bounds, to: tuple, frm: tuple) -> dict[str, int]:
        """{номер комплекса: сколько смен статуса} за период, в нужный статус и из нужного."""
        key = ("changes", bounds, to, frm)
        if key not in self._cache:
            cond, params = _ts_range(bounds)
            sql = "select point_id, count(*) from point_status_history where old_status != new_status" + cond.format(col="changed_at")
            for col, values in (("new_status", to), ("old_status", frm)):
                if values:
                    sql += f" and {col} in ({','.join('?' for _ in values)})"
                    params += ["" if v == UNKNOWN_STATUS else v for v in values]
            sql += " group by point_id"
            async with get_connection() as conn:
                cur = await conn.execute(sql, params)
                self._cache[key] = {pid: n for pid, n in await cur.fetchall()}
        return self._cache[key]

    async def status_since(self) -> dict[str, str]:
        """{номер: когда комплекс получил свой ТЕКУЩИЙ статус} — по истории смен; у комплекса без истории сведений нет."""
        if "since" not in self._cache:
            async with get_connection() as conn:
                cur = await conn.execute("select point_id, new_status, max(changed_at) from point_status_history group by point_id, new_status")
                latest = {(pid, (st or "").strip() or UNKNOWN_STATUS): at for pid, st, at in await cur.fetchall()}
            self._cache["since"] = {p["id"]: latest[(p["id"], p["status"])] for p in self.points if (p["id"], p["status"]) in latest}
        return self._cache["since"]

    async def members(self, group_ids: tuple) -> set[str]:
        key = ("members", group_ids)
        if key not in self._cache:
            async with get_connection() as conn:
                cur = await conn.execute(f"select distinct point_id from apk_group_points where group_id in ({','.join('?' for _ in group_ids)})", list(group_ids))
                self._cache[key] = {r[0] for r in await cur.fetchall()}
        return self._cache[key]


def _text_match(p: dict[str, Any], needle: str) -> bool:
    hay = f"{p['id']} {p['address']} {p['description']}"
    if needle.casefold() in hay.casefold():
        return True
    k = strict_key(needle)
    return bool(k) and k in strict_key(hay)


async def _evaluate(f: dict[str, Any], data: _Data) -> dict[str, Any]:
    """Подходящие комплексы и числа по набору условий: {rows: [{point, visits, changes, since_days}], count, ...}."""
    bounds = period_bounds(f["period"], data.today)
    use_visits = bool(f["visits"] or f["visits_min"] or f["visit_types"] or f["visit_results"] or f["count"] == "visits")
    use_changes = bool(f["changes"] or f["change_to"] or f["change_from"] or f["count"] == "changes")
    visits = await data.visits(bounds, tuple(f["visit_types"]), tuple(f["visit_results"])) if use_visits else {}
    changes = await data.changes(bounds, tuple(f["change_to"]), tuple(f["change_from"])) if use_changes else {}
    v_mode, c_mode = effective_modes(f)
    members = await data.members(tuple(f["groups"])) if f["groups"] else None
    since = await data.status_since() if f["days_in_status"] else {}
    cats, stats, powers = set(f["categories"]), set(f["statuses"]), set(f["power"])
    rows: list[dict[str, Any]] = []
    for p in data.points:
        if cats and p["category"] not in cats:
            continue
        if stats and p["status"] not in stats:
            continue
        if powers and p["power"] not in powers:
            continue
        ver = f["verification"]
        if ver:
            state, left = p["verify_state"], p["verify_left"]
            if ver == "soon":
                if not (state == "valid" and left is not None and left <= f["verification_days"]):
                    continue
            elif state != ver:
                continue
        if any(f[k] and (p[k] != (f[k] == "yes")) for k in ("sim", "ip", "fault")):
            continue
        if members is not None and p["id"] not in members:
            continue
        if f["text"] and not _text_match(p, f["text"]):
            continue
        days = None
        if f["days_in_status"]:
            at = since.get(p["id"])
            if not at:
                continue
            try:
                days = (data.today - dt.date.fromisoformat(at[:10])).days
            except ValueError:
                continue
            if days < f["days_in_status"]:
                continue
        v, c = visits.get(p["id"], 0), changes.get(p["id"], 0)
        if v_mode == "had" and v < (f["visits_min"] or 1):
            continue
        if v_mode == "none" and v:
            continue
        if v_mode == "" and f["visits_min"] and v < f["visits_min"]:
            continue
        if c_mode == "had" and not c:
            continue
        if c_mode == "none" and c:
            continue
        rows.append({"point": p, "visits": v, "changes": c, "since_days": days})
    what = f["count"]
    number = len(rows) if what == "points" else sum(r["visits" if what == "visits" else "changes"] for r in rows)
    return {"rows": rows, "count": number, "points": len(rows), "bounds": bounds}


_GROUP_LABEL = {
    "category": lambda p: p["category"], "status": lambda p: p["status"], "power": lambda p: p["power"],
    "verification": lambda p: {"valid": "Действует", "expired": "Просрочена", "none": "Нет данных"}[p["verify_state"]],
}


def _result(f: dict[str, Any], ev: dict[str, Any], total: int) -> dict[str, Any]:
    out: dict[str, Any] = {"count": ev["count"], "points": ev["points"], "total": total, "what": f["count"], "breakdown": [],
                           "period": {"from": ev["bounds"][0], "to": ev["bounds"][1]} if ev["bounds"] else None}
    if f["group_by"]:
        label_of = _GROUP_LABEL[f["group_by"]]
        sums: dict[str, int] = {}
        for r in ev["rows"]:
            label = label_of(r["point"])
            sums[label] = sums.get(label, 0) + (1 if f["count"] == "points" else r["visits" if f["count"] == "visits" else "changes"])
        out["breakdown"] = [{"label": k, "count": v} for k, v in sorted(sums.items(), key=lambda kv: (-kv[1], kv[0])) if v]
    return out


async def group_names() -> dict[int, str]:
    async with get_connection() as conn:
        cur = await conn.execute("select id, name from apk_groups order by id")
        return {gid: name for gid, name in await cur.fetchall()}


async def compute_many(counters: list[dict[str, Any]], today: Optional[dt.date] = None) -> list[dict[str, Any]]:
    """Числа для списка сохранённых счётчиков за один проход по данным: [{id, name, filters, summary, result | error}]."""
    data = _Data(today or dt.date.today())
    await data.load_points()
    names = await group_names()
    out = []
    for c in counters:
        item: dict[str, Any] = {"id": c["id"], "name": c["name"], "filters": c["filters"], "created_by": c.get("created_by", ""), "created_at": c.get("created_at", "")}
        try:
            f = normalize(c["filters"])
            item["filters"] = f
            item["summary"] = describe(f, names)
            item["result"] = _result(f, await _evaluate(f, data), len(data.points))
        except CounterError as e:        # условие устарело (например, подгруппа удалена и т. п.) — плитка покажет причину, остальные считаются
            item["summary"], item["result"], item["error"] = [], None, str(e)
        out.append(item)
    return out


async def compute_one(raw_filters: Any, today: Optional[dt.date] = None) -> dict[str, Any]:
    """Предпросмотр набора условий без сохранения."""
    f = normalize(raw_filters)
    data = _Data(today or dt.date.today())
    await data.load_points()
    return {"filters": f, "summary": describe(f, await group_names()), "result": _result(f, await _evaluate(f, data), len(data.points))}


async def points_of(raw_filters: Any, limit: int = LIST_LIMIT, today: Optional[dt.date] = None) -> dict[str, Any]:
    """Список комплексов, которые дают число счётчика («откуда число»): для окна «Показать комплексы» и выгрузки в Excel."""
    f = normalize(raw_filters)
    data = _Data(today or dt.date.today())
    await data.load_points()
    # в списке всегда видны выезды и смены статуса за период — с теми же типами/итогами, что у счётчика
    bounds = period_bounds(f["period"], data.today)
    visits = await data.visits(bounds, tuple(f["visit_types"]), tuple(f["visit_results"]))
    changes = await data.changes(bounds, tuple(f["change_to"]), tuple(f["change_from"]))
    ev = await _evaluate(f, data)
    rows = []
    for r in ev["rows"]:
        p = r["point"]
        rows.append({"id": p["id"], "address": p["address"], "category": p["category"], "status": p["status"], "verification": p["verify"], "verification_state": p["verify_state"],
                     "power": p["power"], "visits": visits.get(p["id"], 0), "changes": changes.get(p["id"], 0), "days_in_status": r["since_days"]})
    return {"filters": f, "summary": describe(f, await group_names()), "result": _result(f, ev, len(data.points)), "rows": rows[:limit], "truncated": len(rows) > limit, "limit": limit,
            "period": {"from": ev["bounds"][0], "to": ev["bounds"][1]} if ev["bounds"] else None}


async def options() -> dict[str, Any]:
    """Значения для выпадающих списков редактора счётчика: что есть в справочниках сейчас."""
    async with get_connection() as conn:
        cur = await conn.execute("select name from point_categories order by pylower(name)")
        categories = [r[0] for r in await cur.fetchall()] + [NO_CATEGORY]
        cur = await conn.execute("select name from point_statuses order by id")
        statuses = [r[0] for r in await cur.fetchall()]
        cur = await conn.execute("select distinct status from points where trim(status) != ''")
        statuses += sorted({r[0].strip() for r in await cur.fetchall()} - set(statuses))
        cur = await conn.execute("select id, name, kind from apk_groups order by (kind = ''), pylower(name), id")
        groups = [{"id": r[0], "name": r[1]} for r in await cur.fetchall()]
    return {"categories": categories, "statuses": statuses + [UNKNOWN_STATUS], "power": POWER_SOURCES + [NO_POWER],
            "work_types": wt.work_type_names(), "visit_results": list(RESULT_STATUSES), "groups": groups}


def build_xlsx(name: str, data: dict[str, Any]) -> io.BytesIO:
    """Excel: лист «Комплексы» (откуда число счётчика) и лист «Условия» (название, число, условия словами, за какие даты)."""
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    wb = Workbook()
    ws = wb.active
    ws.title = "Комплексы"
    head = ["Номер", "Адрес", "Категория", "Статус", "Поверка до", "Питание", "Выездов за период", "Смен статуса за период", "Дней в текущем статусе"]
    ws.append(head)
    for r in data["rows"]:
        ws.append([_safe_cell(r["id"]), _safe_cell(r["address"]), _safe_cell(r["category"]), _safe_cell(r["status"]), r["verification"], _safe_cell(r["power"]), r["visits"], r["changes"], r["days_in_status"]])
    fill = PatternFill("solid", fgColor="DDE6F7")
    for cell in ws[1]:
        cell.font, cell.fill, cell.alignment = Font(bold=True), fill, Alignment(wrap_text=True, vertical="center")
    for i, width in enumerate((22, 46, 20, 18, 14, 14, 12, 14, 14), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{max(1, len(data['rows']) + 1)}"
    info = wb.create_sheet("Условия")
    info.append(["Счётчик", _safe_cell(name)])
    res = data["result"]
    info.append(["Число", res["count"]])
    if res["what"] != "points":
        info.append(["Комплексов, на которых посчитано", res["points"]])
    info.append(["Всего комплексов в справочнике", res["total"]])
    if data.get("period"):
        info.append(["Даты периода", f"{_fmt_day(data['period']['from'])} — {_fmt_day(data['period']['to'])}"])
    info.append(["Выгружено", dt.datetime.now().strftime("%d.%m.%Y %H:%M")])
    if data.get("truncated"):
        info.append(["Внимание", f"в список вошли первые {LIST_LIMIT} комплексов"])
    info.append([])
    info.append(["Условия"])
    for line in data["summary"]:
        info.append([_safe_cell(line)])
    for row in info.iter_rows():
        if row and row[0].value in ("Счётчик", "Число", "Условия"):
            row[0].font = Font(bold=True)
    info.column_dimensions["A"].width = 44
    info.column_dimensions["B"].width = 48
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _safe_cell(value: Any) -> Any:
    """Текст, начинающийся с = + - @, Excel принял бы за формулу — перед такой строкой ставится апостроф."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text
