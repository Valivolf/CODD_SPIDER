"""Вкладка «Проверка» («Все АПК» → Проверка): человек вставляет список комплексов (по одному названию на строку) и получает по каждому всё, что известно системе, в
понятном виде — статус, питание, адрес, какие были выезды, почему не работает и что нужно для восстановления, — а затем может выгрузить это в Excel, выбрав столбцы.

Номер из списка ищется так же, как в остальных импортах (app/services/point_finder.py): по номеру, затем по SIM, IP-адресу и инвентарному номеру. Неоднозначность (под запись
подходят два комплекса) и «не найден» показываются отдельно — угадывать за человека система не станет. Данные читаются из базы при КАЖДОМ запросе (в том числе при выгрузке в Excel),
ничего не хранится между проверками, поэтому повторная проверка того же списка всегда показывает актуальное состояние."""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any, Optional

from app.core import permissions as P
from app.db.database import get_connection
from app.repositories import apk_groups_repository, points_repository
from app.services.point_finder import PointFinder, digits_key
from app.services.point_matching import PointIndex

MAX_ITEMS = 500
MSK = dt.timezone(dt.timedelta(hours=3))
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_NUMERIC_RE = re.compile(r"^[\d\s\-]+$")

# (ключ, подпись столбца, включён по умолчанию, роли, которым доступен) — порядок = порядок в окне выбора и в Excel
COLUMNS: list[tuple[str, str, bool, Optional[str]]] = [
    ("query", "Запрос (как вписали)", True, None),
    ("point_id", "Комплекс", True, None),
    ("found", "Найден в справочнике", False, None),
    ("match", "Как найден", False, None),
    ("address", "Адрес", True, None),
    ("category", "Категория", False, None),
    ("status", "Статус", True, None),
    ("down_since", "Не отвечает с", False, None),
    ("down_days", "Дней без связи", False, None),
    ("power_source", "Питание", True, None),
    ("fault_reason", "Почему не работает", True, None),
    ("fault_need", "Что нужно для восстановления", True, None),
    ("fault_source", "Откуда причина (отчёт)", False, None),
    ("visits_count", "Выездов всего", True, None),
    ("last_visit_date", "Последний выезд — дата", True, None),
    ("last_visit_type", "Последний выезд — тип", False, None),
    ("last_visit_result", "Последний выезд — итог", True, None),
    ("last_visit_executor", "Последний выезд — исполнитель", False, None),
    ("last_visit_comment", "Последний выезд — что сделано", False, None),
    ("visits_history", "История выездов (последние)", False, None),
    ("planned", "Запланированные выезды", False, None),
    ("in_plan", "В плане выездов", False, None),
    ("groups", "Подгруппы АПК", False, None),
    ("group_notes", "Описание и «что нужно» в подгруппах", False, None),
    ("description", "Описание (карточка)", False, None),
    ("notes", "Примечание (карточка)", False, None),
    ("ip_address", "IP-адрес", False, "points.network"),
    ("port", "Порт", False, "points.network"),
    ("sim_number", "Номер SIM", False, None),
    ("inventory_number", "Инвентарный номер", False, None),
    ("lat", "Широта", False, None),
    ("lon", "Долгота", False, None),
    ("certificate", "Сертификат поверки", False, None),
    ("health_checked_at", "Связь проверена", False, None),
    ("login", "Логин веб-интерфейса", False, "points.secrets"),
    ("password", "Пароль веб-интерфейса", False, "points.secrets"),
]
MATCH_TEXT = {"number": "", "part": "по номеру — части названия", "sim": "по номеру SIM", "ip": "по IP-адресу", "inventory": "по инвентарному номеру", "address": "по адресу",
              "ambiguous": "подходит несколько комплексов", "none": "не найден"}


def columns_for(perms: Any) -> list[dict[str, Any]]:
    """Столбцы, доступные по правам роли (IP и порт — «Видеть IP-адрес и порт», логин и пароль — «Видеть и копировать логины и пароли»)."""
    return [{"key": k, "label": label, "default": on} for k, label, on, need in COLUMNS if need is None or P.has(perms, need)]


def fmt_date(value: Any) -> str:
    s = str(value or "")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else s


def _report_day(created_at: str, imported: bool) -> str:
    """День отчёта по местному времени: у записей из архива/журнала время условное — берётся дата как есть."""
    s = str(created_at or "")
    if imported:
        return s[:10]
    try:
        moment = dt.datetime.fromisoformat(s)
    except ValueError:
        return s[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(MSK).date().isoformat()


def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    return f"{n} {many if 11 <= m100 <= 14 else one if m10 == 1 else few if 2 <= m10 <= 4 else many}"


def locate(token: str, index: PointIndex, finder: PointFinder) -> tuple[Optional[str], str, list[str]]:
    """(комплекс, как найден, кандидаты). Осторожнее, чем импорты из файлов: человек вписал номер сам, и «НЕТ-1» не должен превращаться в «P-1» только из-за
    одинаковых цифр. Название с буквами ищется строго (регистр, пробелы, знаки, русские/латинские двойники, хвост «+1», инвентарный номер); одни цифры — ещё и по
    цифрам номера и SIM; IP-адрес — по адресу. Нет совпадения — «не найден», а комплексы с теми же цифрами предлагаются как подсказка."""
    if _IP_RE.match(token):
        pid, how, _ = finder.find(ip=token)
        return (pid, how, []) if pid else (None, "none", [])
    if _NUMERIC_RE.match(token):
        # Одни цифры — это номер комплекса, а название у него длиннее («POST-1019771», «Пульсар-1025-26717»), поэтому ищем по номеру: целиком по цифрам названия или как ОДНУ из групп цифр
        # названия («26717» → «Пульсар-1025-26717»). Номера в списке вписаны точно, поэтому по инвентарному номеру цифры НЕ ищутся: там цифр всегда больше, и случайное совпадение с
        # куском чужого инвентарного номера не должно подставлять не тот комплекс. Затем — по ICC (SIM).
        digits = digits_key(token)
        hit = index.find_id(token)
        if hit:
            return hit["id"], "number", []
        for table, key in ((finder.by_digits, digits), (finder.by_digits0, digits.lstrip("0") or "0")):
            cands = list(dict.fromkeys(table.get(key, [])))
            if len(cands) == 1:
                return cands[0], "number", []
            if len(cands) > 1:
                return None, "ambiguous", cands[:6]
        if len(digits) >= 3:
            for table, key in ((finder.by_group, digits), (finder.by_group0, digits.lstrip("0") or "0")):
                cands = list(dict.fromkeys(table.get(key, [])))
                if len(cands) == 1:
                    return cands[0], "part", []
                if len(cands) > 1:
                    return None, "ambiguous", cands[:6]
        pid, how, _cands = finder.find(icc=token)
        if pid:
            return pid, how, []
        return None, "none", []
    hit = index.find(token)
    if hit:
        return hit["id"], "number", []
    digits = digits_key(token)
    hints = list(dict.fromkeys(finder.by_digits.get(digits, []) if len(digits) >= 3 else []))[:5]
    return None, "none", hints


def tokens_of(text: str) -> list[str]:
    # В проверке одна строка — одно название комплекса: пробелы внутри имени значимы.
    tokens = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return list(dict.fromkeys(tokens))[:MAX_ITEMS + 1]


async def _visits(ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    async with get_connection() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                "select r.point_id, r.created_at, r.imported, r.visit_type, coalesce(nullif(r.result_status, ''), nullif(r.outcome, ''), ''), "
                "coalesce(pe.full_name, r.created_by, ''), coalesce(r.comment, ''), coalesce(r.fault, ''), coalesce(r.needs, ''), coalesce(c.name, '') "
                "from task_visit_reports r left join people pe on pe.id = r.author_person_id left join tasks t on t.id = r.task_id left join crews c on c.id = t.team "
                f"where r.point_id in ({marks}) order by r.created_at desc, r.rowid desc", chunk)
            for pid, created, imported, vtype, result, who, comment, fault, needs, crew in await cur.fetchall():
                who = who[3:].strip() if who.lower().startswith("vk:") else who
                out.setdefault(pid, []).append({
                    "date": _report_day(created, bool(imported)), "visit_type": vtype or "", "result": result, "executor": who,
                    "comment": comment, "fault": fault, "needs": needs, "crew": crew})
    return out


async def last_visits(ids: list[str]) -> dict[str, dict[str, str]]:
    """Последний выезд по отчётам для каждого комплекса из `ids` (карточка в списке «Все АПК»): день, вид выезда, итог, что неисправно и что нужно для восстановления."""
    out: dict[str, dict[str, str]] = {}
    for pid, vs in (await _visits(ids)).items():
        lv = vs[0]
        out[pid] = {"date": lv["date"], "type": lv["visit_type"], "result": lv["result"], "fault": lv["fault"], "needs": lv["needs"]}
    return out


async def _planned(ids: list[str], today: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    async with get_connection() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                "select t.point, t.date, coalesce(c.name, ''), t.type from tasks t left join crews c on c.id = t.team "
                f"where t.point in ({marks}) and t.date >= ? and t.type != 'Вспомогательная точка' "
                "and not exists (select 1 from task_visit_reports r where r.task_id = t.id) order by t.date", [*chunk, today])
            for pid, day, crew, vtype in await cur.fetchall():
                out.setdefault(pid, []).append(f"{fmt_date(day)}{' · ' + crew if crew else ''}{' · ' + vtype if vtype else ''}")
    return out


async def _plans(ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    async with get_connection() as conn:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(f"select point_id, group_name, work_type, description, priority from point_plans where point_id in ({marks})", chunk)
            for pid, group, wtype, desc, prio in await cur.fetchall():
                out[pid] = {"group": group or "", "work_type": wtype or "", "description": desc or "", "priority": bool(prio)}
    return out


def _certificate(until: str, today: str) -> str:
    if not until:
        return "нет данных"
    return f"действует до {fmt_date(until)}" if until >= today else f"истёк {fmt_date(until)}"


def _summary(r: dict[str, Any]) -> str:
    """Все главное одним абзацем, понятным языком."""
    parts = []
    status = r["status"] or "статус не известен"
    head = f"Статус: {status}"
    if r["down_since"] and r["status"] != "Работает":
        head += f" (нет связи с {r['down_since']}" + (f", {_plural(r['down_days'], 'день', 'дня', 'дней')}" if r["down_days"] else "") + ")"
    parts.append(head + ".")
    if r["power_source"]:
        parts.append(f"Питание: {r['power_source']}.")
    if r["visits_count"]:
        lv = r["last_visit"]
        tail = ", ".join(x for x in (lv["visit_type"], f"итог «{lv['result']}»" if lv["result"] else "", lv["executor"]) if x)
        parts.append(f"Выездов было {r['visits_count']}, последний — {lv['date_text']}" + (f" ({tail})" if tail else "") + ".")
    else:
        parts.append("Выездов по отчётам не было.")
    if r["fault_reason"]:
        parts.append(f"Почему не работает: {r['fault_reason']}.")
    if r["fault_need"]:
        parts.append(f"Нужно для восстановления: {r['fault_need']}.")
    if not r["fault_reason"] and not r["fault_need"] and r["status"] == "Не работает":
        parts.append("Причина в отчётах не указана.")
    if r["planned"]:
        parts.append("Запланировано: " + "; ".join(r["planned"][:3]) + ".")
    if r["address"]:
        parts.append(f"Адрес: {r['address']}.")
    return " ".join(parts)


async def check(text_or_tokens: Any, perms: Any) -> dict[str, Any]:
    tokens = tokens_of(text_or_tokens) if isinstance(text_or_tokens, str) else list(dict.fromkeys(t.strip() for t in text_or_tokens if t and t.strip()))[:MAX_ITEMS + 1]
    truncated = len(tokens) > MAX_ITEMS
    tokens = tokens[:MAX_ITEMS]
    points = await points_repository.list_points()
    finder = PointFinder(points)
    index = finder.index
    by_id = {p["id"]: p for p in points}
    today = dt.date.today().isoformat()

    matches: list[tuple[str, Optional[str], str, list[str]]] = []
    for token in tokens:
        pid, how, cands = locate(token, index, finder)
        matches.append((token, pid, how, cands))
    found_ids = list(dict.fromkeys(pid for _, pid, _, _ in matches if pid))
    visits = await _visits(found_ids) if found_ids else {}
    planned = await _planned(found_ids, today) if found_ids else {}
    plans = await _plans(found_ids) if found_ids else {}
    member = await apk_groups_repository.groups_of_points(found_ids) if found_ids else {}
    see_cred = P.secrets_ok(perms)
    see_ip = P.network_ok(perms)

    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for token, pid, how, cands in matches:
        if not pid:
            items.append({"query": token, "found": False, "match": how, "match_text": MATCH_TEXT.get(how, ""), "candidates": cands, "point_id": "",
                          "summary": ("Подходит несколько комплексов: " + ", ".join(cands) + " — уточните номер.") if how == "ambiguous"
                          else "В справочнике такого комплекса нет." + (" Возможно, имелось в виду: " + ", ".join(cands) + "." if cands else "")})
            continue
        p = by_id[pid]
        vs = visits.get(pid, [])
        lv = vs[0] if vs else None
        down_since = str(p.get("port_down_since") or "")[:10] if p.get("port_down_since") else ""
        down_days = None
        if down_since:
            try:
                down_days = max(0, (dt.date.today() - dt.date.fromisoformat(down_since)).days)
            except ValueError:
                down_days = None
        groups = member.get(pid, [])
        plan = plans.get(pid)
        fault_by = f" ({p['fault_by']})" if p.get("fault_by") else ""
        row = {
            "query": token, "found": True, "match": how, "match_text": MATCH_TEXT.get(how, ""), "duplicate": pid in seen_ids, "candidates": [],
            "point_id": pid, "address": p.get("address") or "", "category": p.get("category_name") or "", "status": p.get("status") or "",
            "down_since": fmt_date(down_since) if down_since else "", "down_days": down_days, "power_source": p.get("power_source") or "",
            "fault_reason": p.get("fault_reason") or "", "fault_need": p.get("fault_need") or "",
            "fault_source": (f"отчёт от {fmt_date(str(p['fault_at'])[:10])}{fault_by}" if p.get("fault_at") and (p.get("fault_reason") or p.get("fault_need")) else ""),
            "visits_count": len(vs),
            "last_visit": {"date": lv["date"] if lv else "", "date_text": fmt_date(lv["date"]) if lv else "", "visit_type": lv["visit_type"] if lv else "",
                           "result": lv["result"] if lv else "", "executor": lv["executor"] if lv else "", "comment": lv["comment"] if lv else ""},
            "visits": [{**v, "date_text": fmt_date(v["date"])} for v in vs[:8]],
            "planned": planned.get(pid, [])[:5],
            "in_plan": ({"group": plan["group"], "work_type": plan["work_type"], "priority": plan["priority"],
                         "text": "; ".join(x for x in (plan["group"] and f"группа «{plan['group']}»", plan["work_type"], plan["description"], "приоритет" if plan["priority"] else "") if x)} if plan else None),
            "groups": groups,
            "description": p.get("description") or "", "notes": p.get("notes") or "",
            "ip_address": p.get("ip_address") or "" if see_ip else "", "port": p.get("port") or "" if see_ip else "",
            "sim_number": p.get("sim_number") or "", "inventory_number": p.get("inventory_number") or "", "lat": p.get("lat") or "", "lon": p.get("lon") or "",
            "certificate": _certificate(p.get("verification_valid_until") or "", today), "health_checked_at": str(p.get("health_checked_at") or ""),
            "login": p.get("login") or "" if see_cred else "", "password": p.get("password") or "" if see_cred else "",
        }
        seen_ids.add(pid)
        row["summary"] = _summary(row)
        items.append(row)
    found = [i for i in items if i["found"]]
    return {
        "items": items,
        "stats": {"total": len(items), "found": len(found), "missing": sum(1 for i in items if not i["found"] and i["match"] != "ambiguous"),
                  "ambiguous": sum(1 for i in items if i["match"] == "ambiguous"), "not_working": sum(1 for i in found if i["status"] == "Не работает"),
                  "working": sum(1 for i in found if i["status"] == "Работает")},
        "truncated": truncated, "max_items": MAX_ITEMS, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def _cell(row: dict[str, Any], key: str) -> Any:
    if key == "found":
        return "да" if row["found"] else ("несколько" if row["match"] == "ambiguous" else "нет")
    if key == "match":
        return row.get("match_text") or ("по номеру" if row["found"] else "")
    if key == "point_id":
        return row["point_id"] or ("" if row["found"] else "—")
    if key == "status":
        return row.get("status") or ("не найден" if not row["found"] else "не известен")
    if not row["found"]:
        return row["summary"] if key == "address" else ""
    if key.startswith("last_visit_"):
        lv = row["last_visit"]
        return {"date": lv["date_text"], "type": lv["visit_type"], "result": lv["result"], "executor": lv["executor"], "comment": lv["comment"]}[key[len("last_visit_"):]]
    if key == "visits_history":
        return "\n".join(
            f"{v['date_text']} — {v['visit_type'] or 'выезд'}{' — ' + v['result'] if v['result'] else ''}{' — ' + v['executor'] if v['executor'] else ''}"
            f"{(': ' + v['comment']) if v['comment'] else ''}" for v in row["visits"])
    if key == "planned":
        return "\n".join(row["planned"])
    if key == "in_plan":
        return (row["in_plan"] or {}).get("text", "")
    if key == "groups":
        return ", ".join(g["name"] for g in row["groups"])
    if key == "group_notes":
        return "\n".join(f"{g['name']}: " + "; ".join(x for x in (g["work_type"], g["note"], f"нужно: {g['need']}" if g["need"] else "") if x) for g in row["groups"])
    if key == "health_checked_at":
        return fmt_date(row["health_checked_at"][:10]) + " " + row["health_checked_at"][11:16] if row["health_checked_at"] else ""
    value = row.get(key, "")
    return "" if value is None else value


def build_xlsx(items: list[dict[str, Any]], keys: list[str], perms: Any) -> bytes:
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    allowed = {k: label for k, label, _, need in COLUMNS if need is None or P.has(perms, need)}
    keys = [k for k in dict.fromkeys(keys) if k in allowed]
    if not keys:
        raise ValueError("Не выбрано ни одного столбца")
    wb = Workbook()
    ws = wb.active
    ws.title = "Проверка"
    ws.append([allowed[k] for k in keys])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4F46E5")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for item in items:
        ws.append([_cell(item, k) for k in keys])
    wide = {"address": 42, "fault_reason": 40, "fault_need": 40, "visits_history": 60, "last_visit_comment": 44, "description": 40, "notes": 34, "group_notes": 50, "planned": 36}
    for i, k in enumerate(keys, start=1):
        ws.column_dimensions[get_column_letter(i)].width = wide.get(k, 20 if k not in ("query", "point_id") else 22)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
