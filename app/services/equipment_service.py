"""Операции учёта оборудования. Каждая — одна транзакция: либо всё записалось (единица оборудования
и запись в журнал), либо ничего. Правила переходов:

  приёмка          → склад, состояние «исправно»
  выдача           склад (годное) → у монтёра («выдано»)
  установка        склад (годное) или у монтёра («выдано») → на комплексе
  снятие/замена    на комплексе → у монтёра («снято», ждёт приёмки) либо сразу на склад; состояние
                   автоматически «дефектное» (или то, что выбрали)
  приём от монтёра у монтёра → склад (снятое остаётся «дефектным» и помнит, кто и откуда его снял)
  смена состояния  только для лежащего на складе
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from app.core import equipment as eq
from app.db.database import get_connection
from app.repositories import equipment_repository as repo
from app.repositories import point_components_repository
from app.repositories import point_links_repository as links


logger = logging.getLogger(__name__)


class EquipmentError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def _sync_point_status_conn(conn, *point_ids: Optional[str]) -> None:
    for point_id in dict.fromkeys(pid for pid in point_ids if pid):
        await point_components_repository.recompute_status_conn(conn, point_id, "warehouse")


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


async def _person(conn, person_id: int) -> dict[str, Any]:
    cur = await conn.execute("select id, full_name, role_type, active from people where id = ?", (person_id,))
    row = await cur.fetchone()
    if not row:
        raise EquipmentError("Человек не найден", 404)
    if not row[3]:
        raise EquipmentError(f"«{row[1]}» деактивирован — оборудование на него записать нельзя", 409)
    return {"id": row[0], "full_name": row[1], "role_type": row[2]}


async def _require_item(conn, item_id: int) -> dict[str, Any]:
    item = await repo.get_item_conn(conn, item_id)
    if not item:
        raise EquipmentError("Оборудование не найдено", 404)
    return item


async def _require_point(conn, point_id: str) -> str:
    cur = await conn.execute("select id from points where id = ?", (point_id,))
    row = await cur.fetchone()
    if not row:
        raise EquipmentError("Комплекс не найден", 404)
    return row[0]


def _is_sim(item: dict[str, Any]) -> bool:
    return item.get("category_kind") == eq.CATEGORY_KIND_SIM


def _norm_serial(item_or_kind_sim: Any, serial: Optional[str]) -> str:
    """Серийный номер как он хранится: у SIM-карты — последние 7 цифр длинного номера (как points.sim_number)."""
    is_sim = item_or_kind_sim if isinstance(item_or_kind_sim, bool) else _is_sim(item_or_kind_sim)
    text = (serial or "").strip()
    return eq.sim_key(text) if is_sim else text


async def _set_point_sim_conn(conn, point_id: str, key: str) -> str:
    """Поле «Номер SIM» комплекса следует за установленной SIM-картой; IP подставляется из реестра SIM↔IP, если SIM там
    есть (так же делает бот при смене SIM). Возвращает IP из реестра или ''."""
    cur = await conn.execute("select ip_address from sim_ip_registry where sim_number = ?", (key,))
    row = await cur.fetchone()
    ip = row[0] if row and row[0] else ""
    if ip:
        await conn.execute("update points set sim_number = ?, ip_address = ? where id = ?", (key, ip, point_id))
    else:
        await conn.execute("update points set sim_number = ? where id = ?", (key, point_id))
    await links.propagate_sim_conn(conn, point_id, key, ip)     # комплексы в связке с этим получают тот же номер
    return ip


async def _clear_point_sim_conn(conn, point_id: Optional[str], key: str) -> None:
    """SIM снята с комплекса — если в его поле «Номер SIM» стоял именно этот номер, поле очищается."""
    if not point_id:
        return
    cur = await conn.execute("select sim_number from points where id = ?", (point_id,))
    row = await cur.fetchone()
    if row and row[0] and eq.sim_key(row[0]) == key:
        await conn.execute("update points set sim_number = '' where id = ?", (point_id,))
        await links.unlink_conn(conn, point_id)       # SIM с комплекса снята — из связки он выходит, остальные не затрагиваются


def _where_is(item: dict[str, Any]) -> str:
    if item["place"] == eq.PLACE_PERSON:
        return f"у монтёра «{item['person_name'] or '—'}»"
    if item["place"] == eq.PLACE_POINT:
        return f"на комплексе «{item['point_id']}»"
    if item["place"] == eq.PLACE_UNKNOWN:
        was = f" (числилось на «{item['removed_point_id']}»)" if item.get("removed_point_id") else ""
        return f"в неизвестном местонахождении{was}"
    return "на складе"


def _unavailable_reason(item: dict[str, Any]) -> str:
    return f"№{item['serial_number']} сейчас {_where_is(item)}"


def _check_usable(item: dict[str, Any]) -> None:
    if not item["status_usable"]:
        raise EquipmentError(
            f"№{item['serial_number']} имеет состояние «{item['status'] or '—'}» — выдавать и ставить такое нельзя. "
            "Сначала смените состояние на склад (например, «Отремонтировано»).", 409,
        )


# ------------------------------------------------------------------ приёмка

async def receive(
    model_id: int, serial_numbers: Iterable[str], *, received_date: Optional[str] = None,
    notes: Optional[str] = None, performed_by: Optional[str] = None, status_id: Optional[int] = None,
) -> dict[str, Any]:
    seen: set[str] = set()
    clean: list[str] = []
    for raw in serial_numbers:
        s = (raw or "").strip()
        if s and s.casefold() not in seen:
            seen.add(s.casefold())
            clean.append(s)
    if not clean:
        raise EquipmentError("Не указан ни один серийный номер")
    created: list[str] = []
    duplicates: list[str] = []
    linked: list[dict[str, Any]] = []
    async with get_connection() as conn:
        cur = await conn.execute(
            "select m.id, m.name, c.name, c.kind from equipment_models m join equipment_categories c on c.id = m.category_id where m.id = ?",
            (model_id,),
        )
        model = await cur.fetchone()
        if not model:
            raise EquipmentError("Модель не найдена", 404)
        if model[3] == eq.CATEGORY_KIND_SIM:
            # SIM-карта в системе — последние 7 цифр номера с карты; полный номер/ICCID можно потом записать в доп. поле.
            clean = list(dict.fromkeys(eq.sim_key(s) for s in clean if eq.sim_key(s)))
        if status_id:
            if not await repo.get_status_conn(conn, status_id):
                raise EquipmentError("Состояние не найдено", 404)
            sid = status_id
        else:
            sid = await repo.role_status_id_conn(conn, eq.ROLE_OK)
        date = _clean(received_date) or repo.today()
        now = repo.now_iso()
        for serial in clean:
            if await repo.find_by_serial_conn(conn, serial):
                duplicates.append(serial)
                continue
            cur = await conn.execute(
                "insert into equipment (model_id, serial_number, place, status_id, received_date, notes, created_at, updated_at) "
                "values (?, ?, 'stock', ?, ?, ?, ?, ?)",
                (model_id, serial, sid, date, _clean(notes), now, now),
            )
            await repo.add_history_conn(
                conn, eq.ACT_RECEIVE, equipment_id=cur.lastrowid, serial_number=serial, model_name=model[1],
                performed_by=performed_by, comment=f"Категория: {model[2]}", event_date=date,
            )
            created.append(serial)
            new_item = await repo.get_item_conn(conn, cur.lastrowid)
            link = await link_pending_conn(conn, new_item) if new_item else None
            if link:                       # монтёр раньше поставил это оборудование на комплекс по последним символам номера — теперь оно в учёте и встало на место
                linked.append({"serial": serial, **link})
        await conn.commit()
    return {"created": created, "duplicates": duplicates, "linked": linked}


# ------------------------------------------------------------------ выдача / приём

async def issue(
    person_id: int, equipment_ids: Iterable[int], *, date: Optional[str] = None,
    performed_by: Optional[str] = None, comment: Optional[str] = None,
) -> dict[str, Any]:
    ids = list(dict.fromkeys(equipment_ids))
    if not ids:
        raise EquipmentError("Не выбрано ни одной единицы оборудования")
    issued: list[int] = []
    skipped: list[dict[str, Any]] = []
    affected_points: set[str] = set()
    async with get_connection() as conn:
        person = await _person(conn, person_id)
        day = _clean(date) or repo.today()
        for eid in ids:
            item = await repo.get_item_conn(conn, eid)
            if not item:
                skipped.append({"id": eid, "reason": "не найдено"})
                continue
            if item["place"] != eq.PLACE_STOCK:
                skipped.append({"id": eid, "serial_number": item["serial_number"], "reason": _unavailable_reason(item)})
                continue
            if not item["status_usable"]:
                skipped.append({
                    "id": eid, "serial_number": item["serial_number"],
                    "reason": f"состояние «{item['status'] or '—'}» — выдавать нельзя",
                })
                continue
            if item.get("removed_point_id"):
                affected_points.add(item["removed_point_id"])
            await conn.execute(
                "update equipment set place = 'person', person_id = ?, held_reason = 'issued', issued_date = ?, "
                "updated_at = ? where id = ?",
                (person["id"], day, repo.now_iso(), eid),
            )
            await repo.add_history_conn(
                conn, eq.ACT_ISSUE, item=item, person_id=person["id"], person_name=person["full_name"],
                performed_by=performed_by, comment=_clean(comment), event_date=day,
            )
            issued.append(eid)
        await _sync_point_status_conn(conn, *sorted(affected_points))
        await conn.commit()
    return {"issued": issued, "skipped": skipped}


async def accept_from_person(
    equipment_ids: Iterable[int], *, performed_by: Optional[str] = None, comment: Optional[str] = None,
    status_id: Optional[int] = None, date: Optional[str] = None, person_id: Optional[int] = None,
) -> dict[str, Any]:
    """Приём на склад всего, что числится на монтёре: снятое остаётся «дефектным» (или получает выбранное
    состояние), невыбранное из выданного просто возвращается на склад."""
    ids = list(dict.fromkeys(equipment_ids))
    if not ids:
        raise EquipmentError("Не выбрано ни одной единицы оборудования")
    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    affected_points: set[str] = set()
    async with get_connection() as conn:
        if status_id and not await repo.get_status_conn(conn, status_id):
            raise EquipmentError("Состояние не найдено", 404)
        day = _clean(date) or repo.today()
        for eid in ids:
            item = await repo.get_item_conn(conn, eid)
            if not item:
                skipped.append({"id": eid, "reason": "не найдено"})
                continue
            if item["place"] != eq.PLACE_PERSON or (person_id and item["person_id"] != person_id):
                skipped.append({"id": eid, "serial_number": item["serial_number"], "reason": f"не числится на этом монтёре — {_where_is(item)}"})
                continue
            removed = item["held_reason"] in eq.HELD_FROM_POINT
            if item.get("removed_point_id"):
                affected_points.add(item["removed_point_id"])
            await conn.execute(
                "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, issued_date = NULL, "
                "accepted_date = ?, accepted_by = ?, status_id = coalesce(?, status_id), updated_at = ? where id = ?",
                (day, _clean(performed_by), status_id, repo.now_iso(), eid),
            )
            extra = ""
            if removed:
                extra = f"Снято с комплекса «{item['removed_point_id'] or '—'}»"
                if item["removed_date"]:
                    extra += f" ({item['removed_date']})"
            await repo.add_history_conn(
                conn, eq.ACT_ACCEPT if removed else eq.ACT_RETURN, item=item, person_id=item["person_id"],
                person_name=item["person_name"], point_id=item["removed_point_id"] if removed else None,
                performed_by=performed_by, comment=". ".join(p for p in (extra, _clean(comment)) if p) or None, event_date=day,
            )
            accepted.append({"id": eid, "serial_number": item["serial_number"], "removed": removed})
        await _sync_point_status_conn(conn, *sorted(affected_points))
        await conn.commit()
    return {"accepted": accepted, "skipped": skipped}


async def accept_substitute(
    expected_id: int, *, actual_equipment_id: Optional[int] = None, actual_serial: Optional[str] = None,
    create_missing: bool = False, performed_by: Optional[str] = None, comment: Optional[str] = None,
    status_id: Optional[int] = None, person_id: Optional[int] = None, date: Optional[str] = None,
) -> dict[str, Any]:
    """Приёмка с другим номером: по системе на монтёре числится №A (`expected`), а он сдал №B (`actual`).
    №B принимается на склад как сданное этим монтёром (снятое остаётся «дефектным», с теми же «снял / откуда»),
    а №A получает пометку «неизвестное местонахождение»."""
    async with get_connection() as conn:
        expected = await _require_item(conn, expected_id)
        if expected["place"] != eq.PLACE_PERSON or (person_id and expected["person_id"] != person_id):
            raise EquipmentError(f"№{expected['serial_number']} не числится на этом монтёре — {_where_is(expected)}", 409)
        if status_id and not await repo.get_status_conn(conn, status_id):
            raise EquipmentError("Состояние не найдено", 404)
        actual = await _resolve_actual(conn, expected, actual_equipment_id, actual_serial, create_missing, performed_by)
        if actual is None:
            raise EquipmentError("Укажите другой номер — этот и так числится на монтёре")
        day = _clean(date) or repo.today()
        removed = expected["held_reason"] in eq.HELD_FROM_POINT
        defect = await repo.role_status_id_conn(conn, eq.ROLE_DEFECT)
        prev = _where_is(actual)
        if removed:
            # Реально сданное — это и есть снятое: дефектное, снято тем же монтёром с того же комплекса.
            await conn.execute(
                "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, point_id = NULL, "
                "issued_date = NULL, installed_date = NULL, installed_by = NULL, status_id = ?, removed_date = ?, "
                "removed_by = ?, removed_point_id = ?, accepted_date = ?, accepted_by = ?, updated_at = ? where id = ?",
                (status_id or defect, expected["removed_date"] or day, expected["removed_by"] or expected["person_name"],
                 expected["removed_point_id"], day, _clean(performed_by), repo.now_iso(), actual["id"]),
            )
        else:
            await conn.execute(
                "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, point_id = NULL, "
                "issued_date = NULL, installed_date = NULL, installed_by = NULL, status_id = coalesce(?, status_id), "
                "accepted_date = ?, accepted_by = ?, updated_at = ? where id = ?",
                (status_id, day, _clean(performed_by), repo.now_iso(), actual["id"]),
            )
        src = f"Снято с комплекса «{expected['removed_point_id'] or '—'}»" if removed else "Возврат"
        await repo.add_history_conn(
            conn, eq.ACT_ACCEPT if removed else eq.ACT_RETURN, item=actual, person_id=expected["person_id"],
            person_name=expected["person_name"], point_id=expected["removed_point_id"] if removed else None,
            performed_by=performed_by, event_date=day,
            comment=". ".join(p for p in (
                f"{src}. Принято вместо №{expected['serial_number']} (по системе числилось на монтёре); раньше числилось: {prev}",
                _clean(comment) or "",
            ) if p),
        )
        await _mark_unknown_conn(
            conn, expected, point_id=expected["removed_point_id"] if removed else None, date=day, performed_by=performed_by,
            comment=f"Монтёр {expected['person_name'] or '—'} сдал другой номер (№{actual['serial_number']}) — "
                    "где это оборудование сейчас, неизвестно",
        )
        await _sync_point_status_conn(conn, expected.get("removed_point_id"))
        await conn.commit()
    return {"accepted": actual["serial_number"], "unknown": expected["serial_number"], "removed": removed}


async def found_unknown(
    equipment_id: int, *, performed_by: Optional[str] = None, comment: Optional[str] = None, date: Optional[str] = None,
) -> dict[str, Any]:
    """Оборудование с неизвестным местонахождением нашлось: возвращается на склад (состояние не меняется)."""
    async with get_connection() as conn:
        item = await _require_item(conn, equipment_id)
        if item["place"] != eq.PLACE_UNKNOWN:
            raise EquipmentError(f"№{item['serial_number']} не числится как потерянное — оно {_where_is(item)}", 409)
        day = _clean(date) or repo.today()
        await conn.execute(
            "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, point_id = NULL, "
            "accepted_date = ?, accepted_by = ?, updated_at = ? where id = ?",
            (day, _clean(performed_by), repo.now_iso(), equipment_id),
        )
        await repo.add_history_conn(conn, eq.ACT_FOUND, item=item, performed_by=performed_by, comment=_clean(comment), event_date=day)
        await _sync_point_status_conn(conn, item.get("removed_point_id"))
        await conn.commit()
    return {"id": equipment_id, "serial_number": item["serial_number"]}


# ------------------------------------------------------------------ установка / снятие / замена

async def _detach_conn(
    conn, item: dict[str, Any], *, point_id: str, to_person: Optional[dict[str, Any]], date: str,
    performed_by: Optional[str], comment: Optional[str], status_id: Optional[int], action: str, extra: str = "",
    reason: Optional[str] = None, sync_point: bool = True, sync_status: bool = True,
) -> None:
    """Снять с комплекса. `reason` — почему снимается (какая проблема): хранится у единицы, пока она ждёт приёмки и
    лежит на складе, и пишется в журнал. `sync_point=False` — поле «Номер SIM» комплекса не трогать (им управляет вызывающий)."""
    new_status = status_id or await repo.role_status_id_conn(conn, eq.ROLE_DEFECT)
    removed_by = to_person["full_name"] if to_person else _clean(performed_by)
    reason = _clean(reason)
    # Любая снятая SIM-карта остаётся у монтёра без обязательной сдачи на склад (он может поставить её снова); остальное — ждёт приёмки.
    keep = bool(to_person) and _is_sim(item)
    if to_person:
        await conn.execute(
            "update equipment set place = 'person', person_id = ?, held_reason = ?, point_id = NULL, "
            "installed_date = NULL, installed_by = NULL, status_id = ?, removed_date = ?, removed_by = ?, "
            "removed_point_id = ?, accepted_date = NULL, accepted_by = NULL, removal_reason = ?, updated_at = ? where id = ?",
            (to_person["id"], eq.HELD_KEPT if keep else eq.HELD_REMOVED, new_status, date, removed_by, point_id, reason,
             repo.now_iso(), item["id"]),
        )
    else:
        await conn.execute(
            "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, point_id = NULL, "
            "installed_date = NULL, installed_by = NULL, status_id = ?, removed_date = ?, removed_by = ?, "
            "removed_point_id = ?, accepted_date = ?, accepted_by = ?, removal_reason = ?, updated_at = ? where id = ?",
            (new_status, date, removed_by, point_id, date, _clean(performed_by), reason, repo.now_iso(), item["id"]),
        )
    if sync_point and _is_sim(item):
        await _clear_point_sim_conn(conn, point_id, item["serial_number"])
    pieces = [extra]
    if reason:
        pieces.append(f"Причина снятия: {reason}")
    if keep:
        pieces.append(f"Осталась у монтёра {to_person['full_name']} — сдавать на склад не обязательно, он может поставить её снова")
    elif to_person:
        pieces.append(f"Числится на монтёре {to_person['full_name']} — сдать на склад")
    pieces.append(_clean(comment) or "")
    await repo.add_history_conn(
        conn, action, item=item, point_id=point_id, person_id=to_person["id"] if to_person else None,
        person_name=to_person["full_name"] if to_person else None, performed_by=performed_by,
        comment=". ".join(p for p in pieces if p) or None, event_date=date,
    )
    if sync_status:
        await _sync_point_status_conn(conn, point_id)


async def _attach_conn(
    conn, item: dict[str, Any], *, point_id: str, person: Optional[dict[str, Any]], installed_by: Optional[str],
    date: str, performed_by: Optional[str], comment: Optional[str], action: str, extra: str = "",
    sync_point: bool = True, sync_status: bool = True,
) -> None:
    if item["place"] == eq.PLACE_STOCK:
        _check_usable(item)
    elif item["place"] == eq.PLACE_PERSON and item["held_reason"] in eq.HELD_FIT_FOR_POINT:
        if person and item["person_id"] != person["id"]:
            raise EquipmentError(
                f"№{item['serial_number']} {'числится на другом монтёре' if item['held_reason'] == eq.HELD_KEPT else 'выдано другому монтёру'}"
                f" — {item['person_name']}", 409,
            )
        person = person or {"id": item["person_id"], "full_name": item["person_name"]}
    else:
        raise EquipmentError(f"{_unavailable_reason(item)} — поставить на комплекс нельзя", 409)
    by = _clean(installed_by) or (person["full_name"] if person else None)
    extra = ". ".join(p for p in (extra, await _reuse_kept_conn(conn, item)) if p)
    await conn.execute(
        "update equipment set place = 'point', point_id = ?, person_id = NULL, held_reason = NULL, issued_date = NULL, "
        "installed_date = ?, installed_by = ?, removed_date = NULL, removed_by = NULL, removed_point_id = NULL, "
        "accepted_date = NULL, accepted_by = NULL, removal_reason = NULL, updated_at = ? where id = ?",
        (point_id, date, by, repo.now_iso(), item["id"]),
    )
    if sync_point and _is_sim(item):
        await _set_point_sim_conn(conn, point_id, item["serial_number"])
    await repo.add_history_conn(
        conn, action, item=item, point_id=point_id, person_id=person["id"] if person else None,
        person_name=by, performed_by=performed_by,
        comment=". ".join(p for p in ((f"Монтёр: {by}" if by else ""), extra, _clean(comment) or "") if p) or None,
        event_date=date,
    )
    if sync_status:
        await _sync_point_status_conn(conn, point_id)


async def _reuse_kept_conn(conn, item: dict[str, Any]) -> str:
    """Монтёр снова ставит на комплекс SIM, снятую раньше и оставшуюся у него: если она значилась нерабочей, то раз он решил её
    использовать — теперь она «Исправно». Возвращает пояснение для журнала (или '')."""
    if item.get("held_reason") != eq.HELD_KEPT:
        return ""
    note = "Поставлена повторно — раньше была снята с комплекса и осталась у монтёра"
    if not item.get("status_usable"):
        ok = await repo.role_status_id_conn(conn, eq.ROLE_OK)
        if ok:
            await conn.execute("update equipment set status_id = ? where id = ?", (ok, item["id"]))
            note += f" (состояние «{item.get('status') or '—'}» → «Исправно»)"
    return note


async def _resolve_actual(
    conn, expected: dict[str, Any], actual_id: Optional[int], actual_serial: Optional[str], create_missing: bool,
    performed_by: Optional[str],
) -> Optional[dict[str, Any]]:
    """Сверка: «фактически было другое оборудование». Находит его по id или номеру; если такого номера нет в
    базе и разрешено `create_missing` — заводит запись (модель как у ожидавшегося). None — сверка не нужна
    (ничего не указано либо указан тот же самый номер)."""
    if actual_id:
        item = await _require_item(conn, actual_id)
    else:
        serial = (actual_serial or "").strip()
        if not serial:
            return None
        item = await repo.find_by_serial_conn(conn, serial)
        if item is None:
            if not create_missing:
                raise EquipmentError(
                    f"Номер «{serial}» не найден в базе. Его можно записать как новое оборудование "
                    f"(модель как у №{expected['serial_number']}).", 404,
                )
            now = repo.now_iso()
            defect = await repo.role_status_id_conn(conn, eq.ROLE_DEFECT)
            cur = await conn.execute(
                "insert into equipment (model_id, serial_number, place, status_id, notes, created_at, updated_at) "
                "values (?, ?, 'stock', ?, ?, ?, ?)",
                (expected["model_id"], serial, defect, "Добавлено при сверке: номера не было в базе", now, now),
            )
            await repo.add_history_conn(
                conn, eq.ACT_RECEIVE, equipment_id=cur.lastrowid, serial_number=serial, model_name=expected["model_name"],
                performed_by=performed_by,
                comment=f"Добавлено при сверке: фактически было вместо №{expected['serial_number']}, номера не было в базе",
            )
            item = await repo.get_item_conn(conn, cur.lastrowid)
    if item["id"] == expected["id"]:
        return None
    return item


async def _mark_unknown_conn(
    conn, item: dict[str, Any], *, point_id: Optional[str], date: str, performed_by: Optional[str], comment: str,
) -> None:
    """Оборудование, которое по системе должно быть здесь, но фактически другое: местонахождение неизвестно.
    Где оно числилось, помним в removed_point_id."""
    await conn.execute(
        "update equipment set place = 'unknown', point_id = NULL, person_id = NULL, held_reason = NULL, "
        "issued_date = NULL, installed_date = NULL, installed_by = NULL, removed_date = ?, "
        "removed_point_id = coalesce(?, removed_point_id), updated_at = ? where id = ?",
        (date, point_id, repo.now_iso(), item["id"]),
    )
    if _is_sim(item):
        await _clear_point_sim_conn(conn, item.get("point_id") or point_id, item["serial_number"])
    await repo.add_history_conn(
        conn, eq.ACT_UNKNOWN, item=item, point_id=point_id, performed_by=performed_by, comment=comment, event_date=date,
    )


async def attach(
    point_id: str, equipment_id: int, *, person_id: Optional[int] = None, installed_by: Optional[str] = None,
    date: Optional[str] = None, performed_by: Optional[str] = None, comment: Optional[str] = None,
) -> None:
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        item = await _require_item(conn, equipment_id)
        person = await _person(conn, person_id) if person_id else None
        if _is_sim(item):
            others = [s for s in await repo.find_installed_sim_conn(conn, pid) if s["id"] != item["id"]]
            if others:
                raise EquipmentError(
                    f"На комплексе «{pid}» уже стоит SIM-карта №{others[0]['serial_number']} — используйте «Заменить»", 409,
                )
        await _attach_conn(
            conn, item, point_id=pid, person=person, installed_by=installed_by, date=_clean(date) or repo.today(),
            performed_by=performed_by, comment=comment, action=eq.ACT_ATTACH,
        )
        await conn.commit()


async def _apply_actual_conn(
    conn, expected: dict[str, Any], actual: Optional[dict[str, Any]], *, point_id: str, date: str,
    performed_by: Optional[str],
) -> tuple[dict[str, Any], str, bool]:
    """Сверка при снятии. `expected` — что по системе стоит на комплексе, `actual` — что реально стояло.
    Возвращает (единица, которая снимается на самом деле, пояснение для журнала, помечено ли ожидавшееся
    неизвестным). Если реально стояло другое —
    ожидавшееся помечается «неизвестное местонахождение»; если указанная единица тоже числится на этом
    комплексе — это просто выбор другой стоящей единицы, ничего не помечается."""
    if actual is None:
        return expected, "", False
    if actual["place"] == eq.PLACE_POINT and actual["point_id"] == point_id:
        return actual, "", False
    note = (f"Фактически стояло №{actual['serial_number']} (по системе — №{expected['serial_number']}); "
            f"раньше числилось: {_where_is(actual)}")
    await _mark_unknown_conn(
        conn, expected, point_id=point_id, date=date, performed_by=performed_by,
        comment=f"На комплексе «{point_id}» стояло другое оборудование (№{actual['serial_number']}) — "
                "где оно сейчас, неизвестно",
    )
    return actual, note, True


async def detach(
    point_id: str, equipment_id: int, *, to_person_id: Optional[int] = None, date: Optional[str] = None,
    performed_by: Optional[str] = None, comment: Optional[str] = None, status_id: Optional[int] = None,
    actual_equipment_id: Optional[int] = None, actual_serial: Optional[str] = None, create_missing: bool = False,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Снять с комплекса без замены: либо на монтёра (ждёт приёмки), либо сразу на склад.
    Если на самом деле стояло оборудование с другим номером (`actual_*`), снимается оно, а то, что должно было
    стоять по системе, получает пометку «неизвестное местонахождение»."""
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        item = await _require_item(conn, equipment_id)
        if item["place"] != eq.PLACE_POINT or item["point_id"] != pid:
            raise EquipmentError("Это оборудование не установлено на данном комплексе", 409)
        if status_id and not await repo.get_status_conn(conn, status_id):
            raise EquipmentError("Состояние не найдено", 404)
        person = await _person(conn, to_person_id) if to_person_id else None
        day = _clean(date) or repo.today()
        actual = await _resolve_actual(conn, item, actual_equipment_id, actual_serial, create_missing, performed_by)
        removed, note, marked = await _apply_actual_conn(conn, item, actual, point_id=pid, date=day, performed_by=performed_by)
        await _detach_conn(
            conn, removed, point_id=pid, to_person=person, date=day, performed_by=performed_by,
            comment=comment, status_id=status_id, action=eq.ACT_DETACH, extra=note, reason=reason,
        )
        await conn.commit()
    return {"removed": removed["serial_number"], "unknown": item["serial_number"] if marked else ""}


async def replace(
    point_id: str, old_equipment_id: int, new_equipment_id: int, *, person_id: Optional[int] = None,
    installed_by: Optional[str] = None, date: Optional[str] = None, performed_by: Optional[str] = None,
    comment: Optional[str] = None, removed_status_id: Optional[int] = None, removed_to_person: bool = True,
    actual_equipment_id: Optional[int] = None, actual_serial: Optional[str] = None, create_missing: bool = False,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Замена: снятое уходит на монтёра (`person_id`, если он указан и removed_to_person) как дефектное,
    новое ставится из склада или из выданного этому монтёру. Если на самом деле стояло оборудование с другим
    номером (`actual_*`), снимается оно, а ожидавшееся по системе получает «неизвестное местонахождение»."""
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        old = await _require_item(conn, old_equipment_id)
        new = await _require_item(conn, new_equipment_id)
        if old["place"] != eq.PLACE_POINT or old["point_id"] != pid:
            raise EquipmentError("Снимаемое оборудование не установлено на данном комплексе", 409)
        if old["id"] == new["id"]:
            raise EquipmentError("Нельзя заменить оборудование на него же")
        if removed_status_id and not await repo.get_status_conn(conn, removed_status_id):
            raise EquipmentError("Состояние не найдено", 404)
        person = await _person(conn, person_id) if person_id else None
        day = _clean(date) or repo.today()
        actual = await _resolve_actual(conn, old, actual_equipment_id, actual_serial, create_missing, performed_by)
        if actual is not None and actual["id"] == new["id"]:
            raise EquipmentError("Нельзя заменить оборудование на него же")
        # Новое проверяем ДО снятия старого — чтобы при отказе ничего не изменилось.
        await _attach_conn(
            conn, new, point_id=pid, person=person, installed_by=installed_by, date=day, performed_by=performed_by,
            comment=comment, action=eq.ACT_REPLACE_IN, extra=f"Заменено взамен №{(actual or old)['serial_number']}",
            sync_status=False,
        )
        removed, note, marked = await _apply_actual_conn(conn, old, actual, point_id=pid, date=day, performed_by=performed_by)
        await _detach_conn(
            conn, removed, point_id=pid, to_person=person if removed_to_person else None, date=day,
            performed_by=performed_by, comment=comment, status_id=removed_status_id, action=eq.ACT_REPLACE_OUT,
            extra=". ".join(p for p in (f"Заменено на №{new['serial_number']}", note) if p), reason=reason,
            sync_point=not _is_sim(removed), sync_status=False,   # новая SIM уже записана в поле комплекса — старую оттуда не «снимаем»
        )
        await _sync_point_status_conn(conn, pid)
        await conn.commit()
    return {
        "removed": removed["serial_number"], "installed": new["serial_number"],
        "unknown": old["serial_number"] if marked else "",
    }


# ------------------------------------------------------------------ установка по последним символам номера
# Монтёр может не искать оборудование в выданном ему, а просто указать последние 6+ символов серийного номера. Единица найдётся в учёте (на складе, у него
# или даже у другого монтёра — раз он её поставил, значит она у него в руках), а если такого номера в учёте ещё нет, запоминается «ожидающая привязка»:
# когда оборудование добавят в систему (приёмка, импорт), оно само встанет на этот комплекс (link_pending_conn).

MIN_SUFFIX = 6


def _suffix_or_fail(raw: Optional[str]) -> str:
    suffix = repo.suffix_key(raw)
    if len(suffix) < MIN_SUFFIX:
        raise EquipmentError(f"Введите не меньше {MIN_SUFFIX} последних цифр серийного номера", 400)
    return suffix


def _suffix_state(item: dict[str, Any]) -> tuple[bool, str]:
    """(можно ли поставить на комплекс, где оно сейчас)."""
    if item["place"] == eq.PLACE_STOCK:
        if item["status_usable"]:
            return True, "на складе"
        return False, f"на складе, но состояние «{item['status'] or '—'}» — ставить такое нельзя"
    if item["place"] == eq.PLACE_PERSON and item["held_reason"] in eq.HELD_FIT_FOR_POINT:
        return True, _where_is(item)
    return False, _where_is(item)


async def candidates_by_suffix(suffix: str, *, category_id: Optional[int] = None) -> list[dict[str, Any]]:
    """Кого можно поставить по этим последним символам номера: единицы учёта с пометкой `usable` и пояснением `why`, где они сейчас."""
    key = _suffix_or_fail(suffix)
    async with get_connection() as conn:
        items = await repo.find_by_suffix_conn(conn, key, category_id)
    return [{**i, "usable": _suffix_state(i)[0], "why": _suffix_state(i)[1]} for i in items]


async def install_by_suffix(
    point_id: str, suffix: str, *, person_id: Optional[int] = None, performed_by: Optional[str] = None,
    old_equipment_id: Optional[int] = None, new_equipment_id: Optional[int] = None, category_id: Optional[int] = None,
    reason: Optional[str] = None, date: Optional[str] = None, comment: Optional[str] = None,
) -> dict[str, Any]:
    """Поставить на комплекс оборудование, указанное последними символами номера (взамен `old_equipment_id`, если оно снимается).
    Найдена одна подходящая единица (или выбрана `new_equipment_id`) — ставится она (mode='linked'); несколько — ошибка с просьбой ввести больше символов;
    в учёте нет вовсе — снимается старое и запоминается ожидающая привязка (mode='pending'). Снятое уходит на монтёра как дефектное, как при обычной замене."""
    key = _suffix_or_fail(suffix)
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        person = await _person(conn, person_id) if person_id else None
        old = None
        if old_equipment_id:
            old = await _require_item(conn, old_equipment_id)
            if old["place"] != eq.PLACE_POINT or old["point_id"] != pid:
                raise EquipmentError("Снимаемое оборудование не установлено на данном комплексе", 409)
            if _is_sim(old):
                raise EquipmentError("SIM-карты меняются отдельной кнопкой «Замена SIM-карты»", 409)
            category_id = category_id or old["category_id"]
        day = _clean(date) or repo.today()
        by = person["full_name"] if person else _clean(performed_by)
        new = None
        if new_equipment_id:
            new = await _require_item(conn, new_equipment_id)
        else:
            found = await repo.find_by_suffix_conn(conn, key, category_id)
            usable = [i for i in found if _suffix_state(i)[0]]
            if len(usable) > 1:
                raise EquipmentError("По этим символам найдено несколько единиц: " + ", ".join(f"№{i['serial_number']}" for i in usable[:6])
                                     + " — введите больше символов номера.", 409)
            if usable:
                new = usable[0]
            elif found:
                raise EquipmentError(f"По этим символам найдено №{found[0]['serial_number']}, но поставить его нельзя: {_suffix_state(found[0])[1]}", 409)
        if new is not None:
            if _is_sim(new):
                raise EquipmentError("SIM-карты ставятся отдельной кнопкой «Замена SIM-карты»", 409)
            if old and old["id"] == new["id"]:
                raise EquipmentError("Нельзя заменить оборудование на него же")
            other_hands = new["place"] == eq.PLACE_PERSON and (not person or new["person_id"] != person["id"])
            where_note = f"Числилось: {_where_is(new)} — фактически поставил {by}" if other_hands else ""
            # person=None: единицу с рук другого монтёра тоже можно поставить (он её уже поставил), а кто ставил — в installed_by
            await _attach_conn(
                conn, new, point_id=pid, person=None, installed_by=by, date=day, performed_by=performed_by, comment=comment,
                action=eq.ACT_REPLACE_IN if old else eq.ACT_ATTACH,
                extra=". ".join(p for p in ((f"Заменено взамен №{old['serial_number']}" if old else ""), f"Указано по последним символам номера …{key}", where_note) if p),
                sync_status=False,
            )
            if old:
                await _detach_conn(
                    conn, old, point_id=pid, to_person=person, date=day, performed_by=performed_by, comment=comment, status_id=None,
                    action=eq.ACT_REPLACE_OUT, extra=f"Заменено на №{new['serial_number']}", reason=reason,
                    sync_status=False,
                )
            await _sync_point_status_conn(conn, pid)
            await conn.commit()
            return {"mode": "linked", "installed": new["serial_number"], "removed": old["serial_number"] if old else "", "held_by_other": bool(other_hands)}
        # В учёте такого номера нет: старое снимаем, новое запоминаем — привяжется само при добавлении в систему.
        if old:
            await _detach_conn(
                conn, old, point_id=pid, to_person=person, date=day, performed_by=performed_by, comment=comment, status_id=None,
                action=eq.ACT_REPLACE_OUT, extra=f"Заменено на оборудование с номером …{key} (в учёте его пока нет)", reason=reason,
                sync_status=False,
            )
        await _sync_point_status_conn(conn, pid)
        cat_name = None
        if category_id:
            cur = await conn.execute("select name from equipment_categories where id = ?", (category_id,))
            row = await cur.fetchone()
            cat_name = row[0] if row else None
        cur = await conn.execute(
            "insert into equipment_pending (point_id, category_id, suffix, person_id, person_name, install_date, note, created_at) values (?,?,?,?,?,?,?,?)",
            (pid, category_id, key, person["id"] if person else None, by, day, _clean(comment), repo.now_iso()),
        )
        await repo.add_history_conn(
            conn, eq.ACT_NOTE, serial_number=f"…{key}", model_name=cat_name, person_id=person["id"] if person else None, person_name=by, point_id=pid,
            performed_by=performed_by, comment=f"Монтёр поставил на комплекс оборудование, указав последние символы номера …{key}; в учёте такого номера нет — "
                                               "оно привяжется к комплексу само, когда его добавят в систему", event_date=day,
        )
        await conn.commit()
        return {"mode": "pending", "suffix": key, "removed": old["serial_number"] if old else "", "pending_id": cur.lastrowid}


async def link_pending_conn(conn, item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Новая единица попала в учёт (приёмка, импорт): если её номер заканчивается на символы, которые монтёр раньше указывал при установке на комплекс
    (и категория сходится), единица ставится на тот комплекс. Подходящая ожидающая запись должна быть ровно одна — иначе ничего не делаем, чтобы не ошибиться."""
    if _is_sim(item) or item.get("place") != eq.PLACE_STOCK:
        return None
    key = repo.suffix_key(item["serial_number"])
    cur = await conn.execute("select id, point_id, category_id, suffix, person_name, install_date from equipment_pending where resolved_at is null")
    rows = [r for r in await cur.fetchall() if key.endswith(r[3]) and (r[2] is None or r[2] == item["category_id"])]
    if len(rows) != 1:
        return None
    pend_id, point_id, _cat, suffix, who, day = rows[0]
    cur = await conn.execute("select 1 from points where id = ?", (point_id,))
    if not await cur.fetchone():
        return None
    try:
        _check_usable(item)
    except EquipmentError:
        return None
    await _attach_conn(
        conn, item, point_id=point_id, person=None, installed_by=who, date=day or repo.today(), performed_by=None, comment=None, action=eq.ACT_ATTACH,
        extra=f"Привязано автоматически: монтёр{(' ' + who) if who else ''} при установке на комплекс указал последние символы номера …{suffix}",
    )
    await conn.execute("update equipment_pending set resolved_at = ?, resolved_equipment_id = ? where id = ?", (repo.now_iso(), item["id"], pend_id))
    return {"point_id": point_id, "suffix": suffix, "pending_id": pend_id}


async def pending_list(*, only_open: bool = True) -> list[dict[str, Any]]:
    """Ожидающие привязки (для склада): что и на какой комплекс монтёр поставил по последним символам номера, а в учёте этого ещё нет."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select p.id, p.point_id, p.suffix, p.person_name, p.install_date, p.created_at, p.resolved_at, c.name, e.serial_number "
            "from equipment_pending p left join equipment_categories c on c.id = p.category_id left join equipment e on e.id = p.resolved_equipment_id "
            + ("where p.resolved_at is null " if only_open else "") + "order by p.id desc limit 500"
        )
        keys = ("id", "point_id", "suffix", "person_name", "install_date", "created_at", "resolved_at", "category_name", "serial_number")
        return [dict(zip(keys, r)) for r in await cur.fetchall()]


# ------------------------------------------------------------------ состояния, правка, удаление

async def set_status(
    equipment_id: int, status_id: int, *, performed_by: Optional[str] = None, comment: Optional[str] = None,
) -> dict[str, Any]:
    async with get_connection() as conn:
        item = await _require_item(conn, equipment_id)
        status = await repo.get_status_conn(conn, status_id)
        if not status:
            raise EquipmentError("Состояние не найдено", 404)
        if item["place"] != eq.PLACE_STOCK:
            hint = ("Сначала отметьте, что оно нашлось." if item["place"] == eq.PLACE_UNKNOWN
                    else "Сначала снимите его с комплекса / примите от монтёра.")
            raise EquipmentError(
                f"Состояние меняется только у оборудования на складе — №{item['serial_number']} сейчас {_where_is(item)}. {hint}", 409,
            )
        if item["status_id"] == status_id:
            raise EquipmentError(f"№{item['serial_number']} уже имеет состояние «{status['name']}»")
        await conn.execute(
            "update equipment set status_id = ?, updated_at = ? where id = ?", (status_id, repo.now_iso(), equipment_id)
        )
        await repo.add_history_conn(
            conn, eq.ACT_STATUS, item=item, performed_by=performed_by,
            comment=f"{item['status'] or '—'} → {status['name']}" + (f". {_clean(comment)}" if _clean(comment) else ""),
        )
        await conn.commit()
    return {"id": equipment_id, "serial_number": item["serial_number"], "status": status["name"]}


async def bulk_set_status(
    equipment_ids: Iterable[int], status_id: int, *, performed_by: Optional[str] = None, comment: Optional[str] = None,
) -> dict[str, Any]:
    done: list[str] = []
    skipped: list[dict[str, Any]] = []
    for eid in dict.fromkeys(equipment_ids):
        try:
            res = await set_status(eid, status_id, performed_by=performed_by, comment=comment)
            done.append(res["serial_number"])
        except EquipmentError as exc:
            skipped.append({"id": eid, "reason": exc.detail})
    return {"changed": done, "skipped": skipped}


async def update_item(
    equipment_id: int, *, notes: Optional[str] = None, set_notes: bool = True, extra: Optional[dict[int, Optional[str]]] = None,
) -> None:
    """Примечание и/или значения дополнительных полей {field_id: значение}. `set_notes=False` — примечание не трогать."""
    async with get_connection() as conn:
        await _require_item(conn, equipment_id)
        if set_notes:
            await conn.execute("update equipment set notes = ?, updated_at = ? where id = ?", (_clean(notes), repo.now_iso(), equipment_id))
        if extra:
            for field_id in extra:
                cur = await conn.execute("select 1 from equipment_fields where id = ?", (field_id,))
                if not await cur.fetchone():
                    raise EquipmentError("Дополнительное поле не найдено", 404)
            if await repo.set_extra_conn(conn, equipment_id, extra):
                await conn.execute("update equipment set updated_at = ? where id = ?", (repo.now_iso(), equipment_id))
        await conn.commit()


async def _rename_conn(
    conn, item: dict[str, Any], new_serial: str, *, performed_by: Optional[str], comment: Optional[str] = None,
    note: str = "", sync_point: bool = True,
) -> str:
    """Исправление серийного номера (номер был записан неверно). История и привязки единицы сохраняются; у
    установленной на комплексе SIM-карты следом меняется поле «Номер SIM» комплекса."""
    new = _norm_serial(item, new_serial)
    if not new:
        raise EquipmentError("Укажите номер")
    if new == item["serial_number"]:
        raise EquipmentError("Номер не изменился")
    other = await repo.find_by_serial_conn(conn, new)
    if other and other["id"] != item["id"]:
        raise EquipmentError(f"№{new} уже есть в базе: {other['category_name']}, {_where_is(other)}", 409)
    await conn.execute("update equipment set serial_number = ?, updated_at = ? where id = ?", (new, repo.now_iso(), item["id"]))
    await repo.add_history_conn(
        conn, eq.ACT_RENAME, item={**item, "serial_number": new}, point_id=item.get("point_id"), performed_by=performed_by,
        comment=". ".join(p for p in (f"Было №{item['serial_number']}, стало №{new}", note, _clean(comment) or "") if p),
    )
    if sync_point and _is_sim(item) and item["place"] == eq.PLACE_POINT and item.get("point_id"):
        await _set_point_sim_conn(conn, item["point_id"], new)
    return new


async def rename_serial(equipment_id: int, new_serial: str, *, performed_by: Optional[str] = None, comment: Optional[str] = None) -> dict[str, Any]:
    async with get_connection() as conn:
        item = await _require_item(conn, equipment_id)
        new = await _rename_conn(conn, item, new_serial, performed_by=performed_by, comment=comment)
        await conn.commit()
    return {"id": equipment_id, "old": item["serial_number"], "serial_number": new}


async def delete_items(
    equipment_ids: Iterable[int], *, performed_by: Optional[str] = None, comment: Optional[str] = None
) -> dict[str, Any]:
    deleted: list[str] = []
    skipped: list[dict[str, Any]] = []
    async with get_connection() as conn:
        for eid in dict.fromkeys(equipment_ids):
            item = await repo.get_item_conn(conn, eid)
            if not item:
                skipped.append({"id": eid, "reason": "не найдено"})
                continue
            if item["place"] not in (eq.PLACE_STOCK, eq.PLACE_UNKNOWN):
                skipped.append({
                    "id": eid, "serial_number": item["serial_number"],
                    "reason": f"{_unavailable_reason(item)} — сначала верните его на склад",
                })
                continue
            await conn.execute("delete from equipment where id = ?", (eid,))
            await repo.add_history_conn(
                conn, eq.ACT_DELETE, serial_number=item["serial_number"], model_name=item["model_name"],
                performed_by=performed_by, comment=_clean(comment),
            )
            deleted.append(item["serial_number"])
        await conn.commit()
    return {"deleted": deleted, "skipped": skipped}


async def add_note(
    *, equipment_id: Optional[int] = None, point_id: Optional[str] = None, comment: str,
    date: Optional[str] = None, performed_by: Optional[str] = None,
) -> None:
    text = _clean(comment)
    if not text:
        raise EquipmentError("Опишите, что было сделано")
    async with get_connection() as conn:
        item = None
        if equipment_id:
            item = await _require_item(conn, equipment_id)
        elif point_id:
            point_id = await _require_point(conn, point_id)
        await repo.add_history_conn(
            conn, eq.ACT_NOTE, item=item, point_id=point_id or (item["point_id"] if item else None),
            performed_by=performed_by, comment=text, event_date=_clean(date),
        )
        await conn.commit()


# ------------------------------------------------------------------ для бота монтёра

async def point_equipment_by_category(point_id: str) -> list[dict[str, Any]]:
    """Что стоит на комплексе, по категориям: [{category_id, category_name, items: [...]}]."""
    async with get_connection() as conn:
        items = await repo.point_items_conn(conn, point_id)
        if not any(_is_sim(it) for it in items):
            # Комплекс в связке с другими на одной SIM: в учёте она одна запись, стоит на одном из комплексов связки, но
            # относится ко всем — монтёр видит её (и может заменить/исправить) на любом из них.
            items = items + [it for it in await _group_installed_sims_conn(conn, point_id) if it.get("point_id") != point_id]
    groups: dict[int, dict[str, Any]] = {}
    for it in items:
        g = groups.setdefault(it["category_id"], {"category_id": it["category_id"], "category_name": it["category_name"], "items": []})
        g["items"].append(it)
    return sorted(groups.values(), key=lambda g: g["category_name"].casefold())


async def release_point_equipment_conn(conn, point_id: str, *, performed_by: Optional[str], reason: str) -> None:
    """Комплекс удаляют из справочника — его оборудование снимается на склад (дефектным, с пометкой)."""
    items = await repo.point_items_conn(conn, point_id)
    if items:
        defect = await repo.role_status_id_conn(conn, eq.ROLE_DEFECT)
        day = repo.today()
        for item in items:
            await conn.execute(
                "update equipment set place = 'stock', person_id = NULL, held_reason = NULL, point_id = NULL, "
                "installed_date = NULL, installed_by = NULL, status_id = ?, removed_date = ?, removed_by = ?, "
                "removed_point_id = ?, accepted_date = ?, accepted_by = ?, updated_at = ? where id = ?",
                (defect, day, reason, point_id, day, reason, repo.now_iso(), item["id"]),
            )
            await repo.add_history_conn(
                conn, eq.ACT_DETACH, item=item, point_id=point_id, performed_by=performed_by, comment=reason, event_date=day,
            )
    await conn.execute("update solar_panels set point_id = NULL, updated_at = ? where point_id = ?", (repo.now_iso(), point_id))


async def rename_point_conn(conn, old_id: str, new_id: str) -> None:
    for table, col in (("equipment", "point_id"), ("equipment", "removed_point_id"), ("equipment_history", "point_id"), ("solar_panels", "point_id")):
        await conn.execute(f"update {table} set {col} = ? where {col} = ?", (new_id, old_id))
    await links.rename_point_conn(conn, old_id, new_id)


# ------------------------------------------------------------------ SIM-карты: связь с полем «Номер SIM» комплекса
# SIM-карта — обычная единица оборудования (служебная категория «SIM-карта»), серийный номер которой — те же последние
# 7 цифр, что хранятся в points.sim_number. Поле комплекса и учёт оборудования держатся согласованными: смена номера в
# карточке комплекса отражается на единице (sync_point_sim_conn), а установка/снятие/исправление SIM в учёте — на поле.

async def _group_installed_sims_conn(conn, point_id: str) -> list[dict[str, Any]]:
    """SIM, установленные на комплексе и на комплексах его связки (общая SIM в учёте — одна запись, стоит на одном из них)."""
    out: list[dict[str, Any]] = []
    for pid in await links.group_ids_conn(conn, point_id):
        out.extend(await repo.find_installed_sim_conn(conn, pid))
    return out


async def _put_on_point_conn(
    conn, item: dict[str, Any], *, point_id: str, date: str, installed_by: Optional[str], performed_by: Optional[str],
    action: str, comment: Optional[str] = None, sync_status: bool = True,
) -> None:
    """Поставить единицу на комплекс без проверок «годности» (для SIM, которую уже приняли решением человека)."""
    again = await _reuse_kept_conn(conn, item)
    comment = ". ".join(p for p in (_clean(comment) or "", again) if p) or None
    await conn.execute(
        "update equipment set place = 'point', point_id = ?, person_id = NULL, held_reason = NULL, issued_date = NULL, "
        "installed_date = ?, installed_by = ?, removed_date = NULL, removed_by = NULL, removed_point_id = NULL, "
        "accepted_date = NULL, accepted_by = NULL, removal_reason = NULL, updated_at = ? where id = ?",
        (point_id, date, _clean(installed_by), repo.now_iso(), item["id"]),
    )
    await repo.add_history_conn(
        conn, action, item=item, point_id=point_id, person_name=_clean(installed_by), performed_by=performed_by,
        comment=_clean(comment), event_date=date,
    )
    if sync_status:
        await _sync_point_status_conn(conn, point_id)


async def _new_sim_item_conn(
    conn, serial: str, *, point_id: Optional[str], performed_by: Optional[str], comment: str,
    installed_by: Optional[str] = None, model_name: Optional[str] = None, sync_status: bool = True,
) -> dict[str, Any]:
    """Новая SIM-карта в учёте: сразу на комплексе (point_id) либо на складе."""
    model_id = await repo.sim_model_id_conn(conn, model_name)
    ok = await repo.role_status_id_conn(conn, eq.ROLE_OK)
    now, day = repo.now_iso(), repo.today()
    cur = await conn.execute(
        "insert into equipment (model_id, serial_number, place, status_id, point_id, received_date, installed_date, installed_by, "
        "created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (model_id, serial, "point" if point_id else "stock", ok, point_id, day, day if point_id else None,
         _clean(installed_by) if point_id else None, now, now),
    )
    item = await repo.get_item_conn(conn, cur.lastrowid)
    await repo.add_history_conn(
        conn, eq.ACT_ATTACH if point_id else eq.ACT_RECEIVE, item=item, point_id=point_id, performed_by=performed_by,
        comment=comment, event_date=day,
    )
    if point_id and sync_status:
        await _sync_point_status_conn(conn, point_id)
    return item  # type: ignore[return-value]


async def sync_point_sim_conn(conn, point_id: str, old_sim: Optional[str], new_sim: Optional[str], *, performed_by: Optional[str] = None) -> None:
    """Номер SIM в карточке комплекса изменили (форма точки, импорт Excel, реестр SIM↔IP): приводим в соответствие
    учёт оборудования. Правка номера в карточке считается ИСПРАВЛЕНИЕМ записи — установленная SIM переименовывается;
    если новый номер — уже известная свободная SIM (склад, монтёр), она ставится на комплекс, а прежняя уходит на склад."""
    new_key, old_key = eq.sim_key(new_sim), eq.sim_key(old_sim)
    if new_key == old_key:
        return
    if new_key and not eq.sim_key_valid(new_key):
        return          # заглушка вместо номера («-», «нет») — учёт SIM не трогаем
    day = repo.today()
    installed = await _group_installed_sims_conn(conn, point_id)
    ok = await repo.role_status_id_conn(conn, eq.ROLE_OK)

    async def to_stock(items: list[dict[str, Any]], why: str) -> None:
        for it in items:
            await _detach_conn(
                conn, it, point_id=it.get("point_id") or point_id, to_person=None, date=day, performed_by=performed_by, comment=why,
                status_id=ok, action=eq.ACT_DETACH, sync_point=False,
            )

    if not new_key:
        await to_stock(installed, "Номер SIM удалён из карточки комплекса")
        return
    if any(it["serial_number"] == new_key for it in installed):
        await to_stock([it for it in installed if it["serial_number"] != new_key], "На комплексе остаётся другая SIM")
        return
    existing = await repo.find_by_serial_conn(conn, new_key)
    if existing is not None:
        if not _is_sim(existing):
            logger.warning("SIM %s комплекса %s совпадает с номером другого оборудования — в учёте не отражена", new_key, point_id)
            return
        if existing["place"] == eq.PLACE_POINT and existing["point_id"] != point_id:
            await repo.add_history_conn(
                conn, eq.ACT_NOTE, item=existing, point_id=point_id, performed_by=performed_by,
                comment=f"В карточке комплекса «{point_id}» указан номер этой SIM, но по учёту она стоит на «{existing['point_id']}»",
            )
            return
        await to_stock(installed, f"Заменена на SIM №{new_key} (номер изменён в карточке комплекса)")
        await _put_on_point_conn(
            conn, existing, point_id=point_id, date=day, installed_by=None, performed_by=performed_by,
            action=eq.ACT_REPLACE_IN if installed else eq.ACT_ATTACH, comment="Номер SIM указан в карточке комплекса",
        )
        return
    if installed:
        first, rest = installed[0], installed[1:]
        await _rename_conn(
            conn, first, new_key, performed_by=performed_by, note="Номер изменён в карточке комплекса", sync_point=False,
        )
        await to_stock(rest, "На комплексе остаётся другая SIM")
        return
    await _new_sim_item_conn(
        conn, new_key, point_id=point_id, performed_by=performed_by,
        comment="Учтена автоматически: номер SIM указан в карточке комплекса",
    )


async def backfill_point_sims() -> dict[str, int]:
    """При запуске: комплексам с номером SIM, но без SIM-карты в учёте оборудования, заводим запись. Уже существующие
    единицы не трогаем (SIM, чей номер есть в базе в другом месте, пропускается — это решается вручную)."""
    created = skipped = ignored = 0
    affected_points: set[str] = set()
    async with get_connection() as conn:
        # Убираем SIM-«единицы», которые эта же сверка завела по заглушкам вместо номера (нет цифр / слишком короткие):
        # у них одна запись в журнале — «Учтена автоматически».
        cur = await conn.execute(
            "select e.id, e.serial_number from equipment e join equipment_models m on m.id = e.model_id "
            "join equipment_categories c on c.id = m.category_id where c.kind = ?", (eq.CATEGORY_KIND_SIM,),
        )
        junk = [(i, s) for i, s in await cur.fetchall() if not eq.sim_key_valid(s or "")]
        removed = 0
        for item_id, _serial in junk:
            cur = await conn.execute("select count(*), min(comment) from equipment_history where equipment_id = ?", (item_id,))
            n, comment = await cur.fetchone()
            if n <= 1 and (comment or "").startswith("Учтена автоматически"):
                await conn.execute("delete from equipment_history where equipment_id = ?", (item_id,))
                await conn.execute("delete from equipment where id = ?", (item_id,))
                removed += 1
        cur = await conn.execute("select id, sim_number from points where trim(coalesce(sim_number, '')) != ''")
        rows = await cur.fetchall()
        cur = await conn.execute(
            "select e.point_id from equipment e join equipment_models m on m.id = e.model_id "
            "join equipment_categories c on c.id = m.category_id where e.place = 'point' and c.kind = ?",
            (eq.CATEGORY_KIND_SIM,),
        )
        have = {r[0] for r in await cur.fetchall()}
        cur = await conn.execute("select serial_number from equipment")
        serials = {(r[0] or "").casefold() for r in await cur.fetchall()}
        for pid, sim in rows:
            key = eq.sim_key(sim)
            if pid in have or not key:
                continue
            if not eq.sim_key_valid(key):
                ignored += 1
                continue
            if key.casefold() in serials:
                skipped += 1
                continue
            await _new_sim_item_conn(
                conn, key, point_id=pid, performed_by=None,
                comment="Учтена автоматически: номер SIM указан в карточке комплекса",
                sync_status=False,
            )
            affected_points.add(pid)
            serials.add(key.casefold())
            created += 1
        await _sync_point_status_conn(conn, *sorted(affected_points))
        await conn.commit()
    if created or skipped or ignored or removed:
        logger.info(
            "SIM-карты комплексов в учёте оборудования: добавлено %s, пропущено (тот же номер уже у другого комплекса) %s, "
            "не похоже на номер (заглушка) %s, убрано ошибочных записей %s", created, skipped, ignored, removed,
        )
    return {"created": created, "skipped": skipped, "ignored": ignored, "removed": removed}


async def _registry_ip_conn(conn, key: str) -> str:
    cur = await conn.execute("select ip_address from sim_ip_registry where sim_number = ?", (key,))
    row = await cur.fetchone()
    return row[0] if row and row[0] else ""


async def keep_sim_on_point(point_id: str, *, performed_by: Optional[str] = None) -> dict[str, Any]:
    """Разбор одинаковых SIM («Уведомления → Ошибки данных»): диспетчер выяснил, что SIM на самом деле стоит на этом комплексе.
    Ему остаются номер SIM и IP, а у всех остальных комплексов с тем же номером SIM и IP убираются (порт и прочее не трогаем),
    они выходят из связки. SIM в учёте оборудования становится на выбранный комплекс; прежние значения очищенных комплексов
    записываются в журнал этой SIM — чтобы их можно было вернуть вручную."""
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        cur = await conn.execute(
            "select id, sim_number, ip_address from points where trim(coalesce(sim_number, '')) != '' order by id"
        )
        rows = await cur.fetchall()
        mine = next((r for r in rows if r[0] == pid), None)
        key = eq.sim_key(mine[1]) if mine else ""
        if not eq.sim_key_valid(key):
            raise EquipmentError("У этого комплекса не указан номер SIM", 400)
        others = [r for r in rows if r[0] != pid and eq.sim_key(r[1]) == key]
        if not others:
            raise EquipmentError("Этот номер SIM записан только у этого комплекса — убирать нечего", 409)
        day = repo.today()

        # SIM в учёте оборудования — на выбранном комплексе (если она вообще числится на каком-то комплексе или её ещё нет).
        item = await repo.find_by_serial_conn(conn, key)
        why = "Номер SIM оставлен на этом комплексе (разбор одинаковых SIM)"
        if item is None:
            item = await _new_sim_item_conn(conn, key, point_id=pid, performed_by=performed_by, comment=why)
        elif _is_sim(item) and item["place"] == eq.PLACE_POINT and item["point_id"] != pid:
            await _put_on_point_conn(
                conn, item, point_id=pid, date=day, installed_by=None, performed_by=performed_by, action=eq.ACT_ATTACH,
                comment=f"{why}; раньше числилась на «{item['point_id']}»",
            )
        cleared = []
        for oid, osim, oip in others:
            await conn.execute("update points set sim_number = '', ip_address = '' where id = ?", (oid,))
            await links.unlink_conn(conn, oid)
            cleared.append({"id": oid, "sim_number": osim, "ip_address": oip or ""})
        if item is not None and _is_sim(item):
            await repo.add_history_conn(
                conn, eq.ACT_NOTE, item=item, point_id=pid, performed_by=performed_by, event_date=day,
                comment="Разбор одинаковых SIM: SIM и IP оставлены на этом комплексе, у остальных убраны — "
                + "; ".join(f"«{c['id']}» (SIM {c['sim_number']}, IP {c['ip_address'] or '—'})" for c in cleared),
            )
        # Группа разобрана — прежняя отметка «считать нормой» по этому номеру больше не нужна (иначе, если номер снова
        # повторится, группа с тем же составом «молча» ушла бы в отмеченные).
        await conn.execute("delete from data_issue_ack where kind = 'sim' and issue_key = ?", (key,))
        await conn.commit()
    return {"kept": pid, "sim": key, "cleared": cleared}


async def sim_replace(
    point_id: str, new_serial: str, *, person_id: Optional[int] = None, performed_by: Optional[str] = None,
    reason: Optional[str] = None, comment: Optional[str] = None, model_name: Optional[str] = None,
    removed_working: Optional[bool] = None,
) -> dict[str, Any]:
    """На комплексе заменили SIM-карту. Новая берётся из учёта (склад, выдана монтёру) либо заводится новой записью;
    старая снимается и — если указан монтёр — остаётся у него (сдавать на склад не обязательно, он может поставить её снова), с причиной снятия.
    `removed_working` — монтёр указал, рабочая ли снятая SIM: рабочая получает состояние «Исправно» (её можно выдать снова),
    нерабочая (и когда не указано) — «Дефектное»."""
    key = eq.sim_key(new_serial)
    if not key:
        raise EquipmentError("Укажите номер новой SIM-карты")
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        person = await _person(conn, person_id) if person_id else None
        installed = await _group_installed_sims_conn(conn, pid)
        if any(it["serial_number"] == key for it in installed):
            raise EquipmentError(f"SIM №{key} уже стоит на этом комплексе", 409)
        item = await repo.find_by_serial_conn(conn, key)
        day = repo.today()
        created = False
        if item is not None:
            if not _is_sim(item):
                raise EquipmentError(f"№{key} уже занят другим оборудованием ({item['category_name']}) — это не SIM-карта", 409)
            if item["place"] == eq.PLACE_POINT:
                raise EquipmentError(f"SIM №{key} числится на комплексе «{item['point_id']}» — сначала снимите её там", 409)
            if item["place"] == eq.PLACE_PERSON and person and item["person_id"] != person["id"]:
                raise EquipmentError(f"SIM №{key} числится на другом монтёре — {item['person_name']}", 409)
            if item["place"] == eq.PLACE_STOCK:
                _check_usable(item)
        else:
            item = await _new_sim_item_conn(
                conn, key, point_id=None, performed_by=performed_by, model_name=model_name,
                comment=f"Добавлена при замене SIM на комплексе «{pid}»: номера не было в базе",
            )
            created = True
        ok_status = await repo.role_status_id_conn(conn, eq.ROLE_OK) if removed_working else None
        state_note = "; снятая SIM рабочая" if removed_working else ("; снятая SIM нерабочая" if removed_working is False else "")
        for old in installed:
            await _detach_conn(
                conn, old, point_id=old.get("point_id") or pid, to_person=person, date=day, performed_by=performed_by, comment=comment,
                status_id=ok_status, action=eq.ACT_REPLACE_OUT, extra=f"Заменена на SIM №{key}{state_note}", reason=reason,
                sync_point=False, sync_status=False,
            )
        await _put_on_point_conn(
            conn, item, point_id=pid, date=day, installed_by=person["full_name"] if person else None, performed_by=performed_by,
            action=eq.ACT_REPLACE_IN if installed else eq.ACT_ATTACH,
            comment=". ".join(p for p in ((f"Заменена взамен №{installed[0]['serial_number']}" if installed else ""), _clean(comment) or "") if p) or None,
            sync_status=False,
        )
        ip = await _set_point_sim_conn(conn, pid, key)
        await _sync_point_status_conn(conn, pid, *(old.get("point_id") for old in installed))
        await conn.commit()
    return {
        "removed": [o["serial_number"] for o in installed], "installed": key, "created": created,
        "ip_known": bool(ip), "ip_address": ip, "removed_working": removed_working,
    }


async def sim_fix(
    point_id: str, serial: str, *, performed_by: Optional[str] = None, comment: Optional[str] = None,
    model_name: Optional[str] = None,
) -> dict[str, Any]:
    """Исправление номера SIM комплекса: раньше был записан не тот номер. Установленная SIM переименовывается (история
    остаётся), поле комплекса следует за ней; SIM на комплексе не было — заводится запись."""
    key = eq.sim_key(serial)
    if not key:
        raise EquipmentError("Укажите номер SIM-карты")
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        installed = await _group_installed_sims_conn(conn, pid)
        old_serial = ""
        if installed:
            current = installed[0]
            old_serial = current["serial_number"]
            if old_serial == key:
                raise EquipmentError("Этот номер уже записан на комплексе")
            await _rename_conn(
                conn, current, key, performed_by=performed_by, comment=comment,
                note=f"Исправлен номер SIM комплекса «{pid}» (раньше был указан неверно)",
            )
        else:
            existing = await repo.find_by_serial_conn(conn, key)
            if existing is not None:
                if not _is_sim(existing):
                    raise EquipmentError(f"№{key} уже занят другим оборудованием ({existing['category_name']}) — это не SIM-карта", 409)
                if existing["place"] == eq.PLACE_POINT:
                    raise EquipmentError(f"SIM №{key} числится на комплексе «{existing['point_id']}»", 409)
                await _put_on_point_conn(
                    conn, existing, point_id=pid, date=repo.today(), installed_by=None, performed_by=performed_by,
                    action=eq.ACT_ATTACH, comment=_clean(comment),
                )
            else:
                await _new_sim_item_conn(
                    conn, key, point_id=pid, performed_by=performed_by, model_name=model_name,
                    comment=_clean(comment) or "Номер SIM указан при работе на комплексе",
                )
            await _set_point_sim_conn(conn, pid, key)
        ip = await _registry_ip_conn(conn, key)
        if ip:
            await conn.execute("update points set ip_address = ? where id = ?", (ip, pid))
        await conn.commit()
    return {"old": old_serial, "installed": key, "ip_known": bool(ip), "ip_address": ip}


# ------------------------------------------------------------------ демонтаж: что именно снято

def item_label(item: dict[str, Any]) -> str:
    """Как оборудование называется в отчёте: «Камера №123 (модель)», у SIM — «SIM-карта 1234567»."""
    if _is_sim(item):
        return f"SIM-карта {item['serial_number']}"
    return f"{item['category_name']} №{item['serial_number']} ({item['model_name']})"


def describe_demontage(mode: str, taken: list[dict[str, Any]], left: list[dict[str, Any]], other: str = "") -> tuple[str, str]:
    """(перечень снятого, фраза для отчёта) — то же, что пишет бот при демонтаже."""
    labels = "; ".join(item_label(i) for i in taken)
    other = (other or "").strip()
    if mode == "whole":
        text = "Демонтирован весь комплекс" + (f": {labels}" if labels else " (оборудование в учёте не числилось)")
        if other:
            text += f"; ещё снято: {other}"
        return labels, text
    parts = [labels] if labels else []
    if other:
        parts.append(f"ещё: {other}")
    text = "Демонтированы отдельные элементы: " + ("; ".join(parts) or "—")
    text += ". Остались на комплексе: " + ("; ".join(item_label(i) for i in left) if left else "ничего (по учёту)") + "."
    return labels, text


async def point_equipment_list(point_id: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        return await repo.point_items_conn(conn, point_id)

async def demontage_remove(
    point_id: str, *, equipment_ids: Optional[Iterable[int]] = None, person_id: int, performed_by: Optional[str] = None,
    reason: Optional[str] = None, date: Optional[str] = None,
) -> dict[str, Any]:
    """Демонтаж на комплексе: всё оборудование (`equipment_ids=None` — весь комплекс) или выбранные элементы снимаются
    на монтёра и числятся на нём как снятые (ждут приёмки на склад), с причиной (по заданию на демонтаж)."""
    async with get_connection() as conn:
        pid = await _require_point(conn, point_id)
        person = await _person(conn, person_id)
        items = await repo.point_items_conn(conn, pid)
        whole = equipment_ids is None
        wanted = None if whole else set(equipment_ids or [])
        chosen = items if whole else [it for it in items if it["id"] in wanted]
        missing = [] if whole else sorted(wanted - {it["id"] for it in chosen})
        day = _clean(date) or repo.today()
        for it in chosen:
            await _detach_conn(
                conn, it, point_id=pid, to_person=person, date=day, performed_by=performed_by, comment=None,
                status_id=None, action=eq.ACT_DETACH,
                extra="Демонтаж комплекса" if whole else "Демонтаж: снят отдельный элемент", reason=reason,
            )
        await conn.commit()
    return {
        "removed": [
            {"id": it["id"], "serial_number": it["serial_number"], "category_name": it["category_name"], "model_name": it["model_name"]}
            for it in chosen
        ],
        "missing_ids": missing,
        "left": len(items) - len(chosen),
    }
