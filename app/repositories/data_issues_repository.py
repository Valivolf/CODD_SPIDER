"""«Ошибки данных» — вкладка в уведомлениях: то, что в самих данных выглядит подозрительно и что стоит поправить руками.

Два вида находок:
  • day  — одна и та же точка в один день стоит в нескольких заданиях: дважды в одной бригаде или в разных бригадах
           (правило «одна точка в день один раз», см. crew_move_repository);
  • sim  — один и тот же номер SIM записан в карточках нескольких комплексов.

Совпадения SIM НЕ фильтруются по IP/порту/типу комплекса: часть комплексов (в основном Пульсары) законно сидит на одной
SIM в общей сети и отличается только портом, но та же SIM может оказаться записана на Пульсаре и на POST, которые в одной
сети быть точно не могут, а порты у них различаются в разы. Поэтому показываются все совпадения, а IP/порт/тип каждого
комплекса и «подсказки» (общий IP, разные типы, полные номера не совпали…) лишь помогают диспетчеру решить самому.

Отметка «считать нормой» ничего не удаляет: группа уходит в свёрнутый список и сама возвращается, если её состав изменился.
Если комплексы с одной SIM действительно работают вместе, их можно СВЯЗАТЬ (point_links_repository): такая группа больше
не считается ошибкой, а смена SIM на одном комплексе связки меняет её у остальных. Связки показываются отдельным списком (kind=link)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.core import equipment as eq
from app.core.warehouse import WAREHOUSE_STATUS
from app.core.work_types import AUX_TYPE_NAME
from app.db.database import get_connection
from app.repositories import point_links_repository as links

KIND_DAY = "day"
KIND_SIM = "sim"
KIND_LINK = "link"
KINDS = (KIND_DAY, KIND_SIM, KIND_LINK)
ACK_KINDS = (KIND_DAY, KIND_SIM)       # «считать нормой» — для связок не нужна: связка и есть явное «так задумано»

# Статусы, при которых комплекса на месте нет — запись SIM у такого комплекса чаще всего «хвост» после демонтажа.
_GONE_STATUSES = {WAREHOUSE_STATUS.lower(), "демонтирован"}

HINT_FILTERS = {
    "": "Все совпадения",
    "mixed_types": "Разные типы комплексов (например, Пульсар и POST)",
    "diff_ip": "Разные IP-адреса",
    "same_ip": "Один IP, порты разные",
    "diff_full": "Полные номера SIM отличаются",
    "gone": "Есть демонтированный / на складе",
}


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _digits(value: Optional[str]) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def _port_number(value: Optional[str]) -> Optional[int]:
    text = (value or "").strip()
    return int(text) if text.isdigit() and int(text) < 65536 else None


# ------------------------------------------------------------------ дубли точек в днях

async def _day_groups(conn, date_from: str, date_to: str) -> list[dict[str, Any]]:
    where = "t.type != ? and trim(t.point) != ''"
    params: list[Any] = [AUX_TYPE_NAME]
    if date_from:
        where += " and t.date >= ?"
        params.append(date_from)
    if date_to:
        where += " and t.date <= ?"
        params.append(date_to)
    cur = await conn.execute(
        f"""select t.id, t.date, t.team, t.point, t.type, t.work, t.hours, coalesce(c.name, ''),
                   case when r.task_id is not null then 1 else 0 end
            from tasks t
            left join crews c on c.id = t.team
            left join task_visit_reports r on r.task_id = t.id
            where {where} order by t.date, t.rowid""",
        params,
    )
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for tid, day, team, point, ttype, work, hours, crew_name, done in await cur.fetchall():
        buckets.setdefault((day, _norm(point)), []).append({
            "task_id": tid, "crew_id": team, "crew_name": crew_name, "type": ttype or "", "work": work or "",
            "hours": hours, "done": bool(done), "point": point,
        })
    dup = {k: v for k, v in buckets.items() if len(v) > 1}
    if not dup:
        return []

    crew_ids = sorted({t["crew_id"] for v in dup.values() for t in v})
    members: dict[int, list[str]] = {}
    if crew_ids:
        cur = await conn.execute(
            "select cm.crew_id, p.full_name from crew_members cm join people p on p.id = cm.person_id "
            f"where cm.crew_id in ({','.join('?' * len(crew_ids))}) order by p.full_name", crew_ids,
        )
        for cid, name in await cur.fetchall():
            members.setdefault(cid, []).append(name)
    cur = await conn.execute("select id, address, status from points")
    points = {_norm(r[0]): {"id": r[0], "address": r[1] or "", "status": r[2] or ""} for r in await cur.fetchall()}

    out: list[dict[str, Any]] = []
    for (day, npoint), tasks in dup.items():
        info = points.get(npoint) or {"id": tasks[0]["point"], "address": "", "status": ""}
        per_crew: dict[int, int] = {}
        for t in tasks:
            per_crew[t["crew_id"]] = per_crew.get(t["crew_id"], 0) + 1
            t["members"] = members.get(t["crew_id"], [])
        out.append({
            "kind": KIND_DAY, "key": f"{day}|{npoint}", "date": day,
            "point_id": info["id"], "address": info["address"], "status": info["status"],
            "in_directory": npoint in points,
            "same_crew": any(n > 1 for n in per_crew.values()),     # дважды в одной бригаде
            "cross_crew": len(per_crew) > 1,                          # в разных бригадах
            "tasks": tasks,
            "signature": ",".join(sorted(t["task_id"] for t in tasks)),
        })
    return out


# ------------------------------------------------------------------ одинаковые SIM у нескольких комплексов

def _sim_hints(points: list[dict[str, Any]]) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """Подсказки к группе (только информация, ничего не отсеивает) и флаги для фильтра."""
    hints: list[dict[str, str]] = []
    flags = {"mixed_types": False, "diff_ip": False, "same_ip": False, "diff_full": False, "gone": False}

    types = sorted({p["category_name"] for p in points if p["category_name"]})
    if len(types) > 1:
        flags["mixed_types"] = True
        hints.append({"tone": "warn", "text": f"Разные типы комплексов: {', '.join(types)} — в одной сети они обычно не бывают, одна из записей, скорее всего, ошибочна."})
    elif types:
        hints.append({"tone": "info", "text": f"Все одного типа: {types[0]}."})

    ips = sorted({p["ip_address"] for p in points if p["ip_address"]})
    with_ip = [p for p in points if p["ip_address"]]
    if len(ips) == 1 and len(with_ip) > 1:
        ports = sorted({p["port"] for p in with_ip if p["port"]}, key=lambda x: (_port_number(x) is None, _port_number(x) or 0, x))
        flags["same_ip"] = True
        hints.append({"tone": "info", "text": f"Общий IP {ips[0]}, порты: {', '.join(ports) if ports else 'не указаны'} — похоже на общую сеть (доступ к комплексам по порту)."})
    elif len(ips) > 1:
        flags["diff_ip"] = True
        hints.append({"tone": "warn", "text": f"IP-адреса разные ({len(ips)}): {', '.join(ips[:4])}{'…' if len(ips) > 4 else ''} — общая сеть маловероятна, стоит проверить, на каком из комплексов SIM на самом деле."})
    nums = [n for n in (_port_number(p["port"]) for p in with_ip) if n is not None]
    if len(nums) > 1 and max(nums) - min(nums) >= 1000:
        hints.append({"tone": "info", "text": f"Порты сильно различаются ({min(nums)} … {max(nums)})."})

    fulls = {_digits(p["sim_raw"])[-10:] for p in points if len(_digits(p["sim_raw"])) >= 10}
    if len(fulls) > 1:
        flags["diff_full"] = True
        hints.append({"tone": "warn", "text": "Полные номера записаны по-разному — совпали только последние 7 цифр, возможно, это разные SIM."})

    gone = [p for p in points if _norm(p["status"]) in _GONE_STATUSES]
    if gone:
        flags["gone"] = True
        hints.append({"tone": "info", "text": f"Не на месте (демонтирован / на складе): {', '.join(p['id'] for p in gone[:5])} — запись SIM у такого комплекса, скорее всего, устарела."})
    return hints, flags


_POINT_SELECT = """select p.id, p.address, p.status, p.sim_number, p.ip_address, p.port, p.login, p.password,
                  coalesce(pc.name, ''), coalesce(pc.color, ''), coalesce(pc.is_legacy, 0), p.lat, p.lon
           from points p left join point_categories pc on pc.id = p.category_id"""


def _point_dict(row, link_of: dict[str, int]) -> dict[str, Any]:
    pid, address, status, sim, ip, port, login, password, cat, color, legacy, lat, lon = row
    return {
        "id": pid, "address": address or "", "status": status or "", "sim_raw": sim or "", "lat": lat or "", "lon": lon or "",
        "ip_address": (ip or "").strip(), "port": (port or "").strip(),
        "category_name": cat, "category_color": color, "category_is_legacy": bool(legacy),
        "login": login or "", "password": password or "", "link_group": link_of.get(pid),
    }


def _shown(p: dict[str, Any], include_credentials: bool, include_ip: bool) -> dict[str, Any]:
    q = dict(p)
    if not include_credentials:
        q["login"] = q["password"] = ""
    if not include_ip:
        q["ip_address"] = ""
    return q


async def _link_map(conn) -> tuple[dict[int, list[str]], dict[str, int]]:
    groups = await links.groups_conn(conn)
    return groups, {pid: gid for gid, ids in groups.items() for pid in ids}


async def _link_groups(conn, include_credentials: bool, include_ip: bool) -> list[dict[str, Any]]:
    """Все связки комплексов с общей SIM — со всеми комплексами, типами, IP и портами."""
    groups, link_of = await _link_map(conn)
    if not groups:
        return []
    cur = await conn.execute(_POINT_SELECT + " where p.id in (select point_id from point_sim_links)")
    info = {r[0]: _point_dict(r, link_of) for r in await cur.fetchall()}
    out: list[dict[str, Any]] = []
    for gid, ids in sorted(groups.items()):
        pts = [info[i] for i in ids if i in info]
        if len(pts) < 2:
            continue
        keys = sorted({eq.sim_key(p["sim_raw"]) for p in pts})
        hints, flags = _sim_hints(pts)
        out.append({
            "kind": KIND_LINK, "key": str(gid), "group_id": gid, "sim": keys[0] if len(keys) == 1 else "",
            "drift": len(keys) > 1,        # номера SIM у членов связки разошлись (правили в обход системы) — стоит свериться
            "points": [_shown(p, include_credentials, include_ip) for p in pts], "installed_point": "",
            "hints": hints, "flags": flags, "ack": None, "signature": ",".join(sorted(p["id"] for p in pts)),
        })
    return out


async def _sim_groups(conn, include_credentials: bool, include_ip: bool) -> list[dict[str, Any]]:
    _, link_of = await _link_map(conn)
    cur = await conn.execute(_POINT_SELECT + " where trim(coalesce(p.sim_number, '')) != '' order by p.id")
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in await cur.fetchall():
        key = eq.sim_key(row[3])
        if not eq.sim_key_valid(key):
            continue
        groups.setdefault(key, []).append(_point_dict(row, link_of))
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    if not dup:
        return []
    cur = await conn.execute(
        "select e.serial_number, e.point_id from equipment e join equipment_models m on m.id = e.model_id "
        "join equipment_categories c on c.id = m.category_id where c.kind = ? and e.place = 'point'", (eq.CATEGORY_KIND_SIM,),
    )
    installed = {s: pid for s, pid in await cur.fetchall()}

    out: list[dict[str, Any]] = []
    for key, pts in sorted(dup.items()):
        hints, flags = _sim_hints(pts)
        link_ids = {p["link_group"] for p in pts}
        out.append({
            "kind": KIND_SIM, "key": key, "sim": key, "points": [_shown(p, include_credentials, include_ip) for p in pts],
            "installed_point": installed.get(key, ""),
            "hints": hints, "flags": flags,
            "fully_linked": len(link_ids) == 1 and None not in link_ids,     # все комплексы группы уже в одной связке
            "signature": ",".join(sorted(p["id"] for p in pts)),
        })
    return out


# ------------------------------------------------------------------ отметки «считать нормой»

async def _acks(conn) -> dict[tuple[str, str], dict[str, str]]:
    cur = await conn.execute("select kind, issue_key, signature, acked_by, acked_at from data_issue_ack")
    return {(k, key): {"signature": sig, "acked_by": by, "acked_at": at} for k, key, sig, by, at in await cur.fetchall()}


def _split_acked(groups: list[dict[str, Any]], acks: dict[tuple[str, str], dict[str, str]]) -> None:
    """Проставляет group['ack'] (запись об отметке) — только если состав группы с момента отметки не менялся."""
    for g in groups:
        a = acks.get((g["kind"], g["key"]))
        g["ack"] = a if a and a["signature"] == g["signature"] else None


def _matches(g: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    needle = q.strip().lower()
    if g["kind"] == KIND_DAY:
        hay = [g["point_id"], g["address"], g["date"], *[t["crew_name"] for t in g["tasks"]], *[m for t in g["tasks"] for m in t["members"]]]
    else:
        hay = [g["sim"], *[p["sim_raw"] for p in g["points"]]]
        for p in g["points"]:
            hay += [p["id"], p["address"], p["ip_address"], p["port"], p["category_name"]]
    return any(needle in (h or "").lower() for h in hay)


# ------------------------------------------------------------------ публичные функции

async def list_issues(
    kind: str, *, date_from: str = "", date_to: str = "", q: str = "", hint: str = "", acked: bool = False,
    limit: int = 50, offset: int = 0, include_credentials: bool = False, include_ip: bool = True, today: str = "",
) -> dict[str, Any]:
    async with get_connection() as conn:
        if kind == KIND_DAY:
            groups = await _day_groups(conn, date_from, date_to)
        elif kind == KIND_LINK:
            groups = await _link_groups(conn, include_credentials, include_ip)
        else:
            groups = [g for g in await _sim_groups(conn, include_credentials, include_ip) if not g["fully_linked"]]
        if kind != KIND_LINK:
            _split_acked(groups, await _acks(conn))
        else:
            acked = False
    if kind == KIND_DAY:
        # Планируемые дни — ближайший первым; прошедшие — свежие сверху.
        groups.sort(key=lambda g: (g["date"], g["point_id"].lower()), reverse=not (date_from and date_from >= (today or dt.date.today().isoformat())))
    open_total = sum(1 for g in groups if not g["ack"])
    acked_total = len(groups) - open_total
    rows = [g for g in groups if bool(g["ack"]) == acked and _matches(g, q)]
    if kind in (KIND_SIM, KIND_LINK) and hint:
        rows = [g for g in rows if g["flags"].get(hint)]
    return {"kind": kind, "total": len(rows), "open_total": open_total, "acked_total": acked_total, "items": rows[offset:offset + limit]}


async def summary(date_from: str = "") -> dict[str, int]:
    async with get_connection() as conn:
        acks = await _acks(conn)
        day = await _day_groups(conn, date_from, "")
        sim = [g for g in await _sim_groups(conn, False, False) if not g["fully_linked"]]
        linked = await _link_groups(conn, False, False)
    _split_acked(day, acks)
    _split_acked(sim, acks)
    d = sum(1 for g in day if not g["ack"])
    s = sum(1 for g in sim if not g["ack"])
    return {"day": d, "sim": s, "link": len(linked), "total": d + s}      # связки — не ошибки, в бейдж не входят


async def set_ack(kind: str, key: str, value: bool, username: str) -> bool:
    """Отметить группу «считать нормой» (value=True) или снять отметку. False — такой группы сейчас нет."""
    if kind not in ACK_KINDS:
        return False
    async with get_connection() as conn:
        if value:
            day = key.split("|", 1)[0]
            groups = await (_day_groups(conn, day, day) if kind == KIND_DAY else _sim_groups(conn, False, False))
            found = next((g for g in groups if g["key"] == key), None)
            if not found:
                return False
            await conn.execute(
                "insert into data_issue_ack (kind, issue_key, signature, acked_by, acked_at) values (?, ?, ?, ?, ?) "
                "on conflict(kind, issue_key) do update set signature = excluded.signature, acked_by = excluded.acked_by, acked_at = excluded.acked_at",
                (kind, key, found["signature"], username, dt.datetime.now(dt.timezone.utc).isoformat()),
            )
        else:
            await conn.execute("delete from data_issue_ack where kind = ? and issue_key = ?", (kind, key))
        await conn.commit()
    return True
