"""CRUD for the points directory ("комплексы" / "АПК") — a real per-row
resource, not part of the calendar's whole-document CAS state. Points are
edited from three independent places (the calendar's point form, the
dedicated /warehouse page, and the "Все АПК" directory page), so a
wipe-and-reinsert-everything write (like tasks still use) would let one
editor silently clobber the other's concurrent change — per-row updates
avoid that.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from app.core import equipment as eq
from app.core import fault as fault_rules
from app.core import field_crypto, map_events, stats_cache
from app.core.names import compact_name
from app.core.text import decode_entities
from app.db.database import get_connection, quiet_writes
from app.core.warehouse import WAREHOUSE_STATUS
from app.repositories import (
    point_categories_repository,
    point_components_repository,
    point_links_repository,
    sim_ip_registry_repository,
    warehouse_items_repository,
)
from app.services import equipment_service, point_rename

logger = logging.getLogger(__name__)


async def _sync_sim_conn(conn, point_id: str, old_sim: Optional[str], new_sim: Optional[str]) -> None:
    """Номер SIM в карточке комплекса изменился — приводим в соответствие SIM-карту в учёте оборудования
    (app/services/equipment_service.py::sync_point_sim_conn). Сбой учёта не должен ломать сохранение точки."""
    if eq.sim_key(old_sim) == eq.sim_key(new_sim):
        return
    try:
        await equipment_service.sync_point_sim_conn(conn, point_id, old_sim, new_sim)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отразить смену SIM комплекса %s в учёте оборудования", point_id)


async def _links_after_sim_edit_conn(conn, point_id: str, old_sim: Optional[str], new_sim: Optional[str], ip: str = "") -> None:
    """Комплекс в связке с общей SIM (point_links_repository): новый номер в карточке получают и остальные комплексы связки;
    если номер убрали (или вместо него заглушка) — комплекс выходит из связки, остальные не затрагиваются."""
    old_key, new_key = eq.sim_key(old_sim), eq.sim_key(new_sim)
    if old_key == new_key:
        return
    try:
        if eq.sim_key_valid(new_key):
            await point_links_repository.propagate_sim_conn(conn, point_id, str(new_sim), ip)
        else:
            await point_links_repository.unlink_conn(conn, point_id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось обновить связку комплекса %s после смены SIM", point_id)


async def _on_warehouse_transition_conn(conn, point_id: str, old_status: str, new_status: str) -> None:
    """Side effects of a status change on the warehouse data: leaving
    «На складе» clears the warehouse fields and un-stocks the components; a
    point moved to «На складе» by hand (the whole complex) puts every one of
    its components in stock."""
    if old_status == WAREHOUSE_STATUS and new_status != WAREHOUSE_STATUS:
        await warehouse_items_repository.delete_for_point_conn(conn, point_id)
    elif new_status == WAREHOUSE_STATUS and old_status != WAREHOUSE_STATUS:
        await point_components_repository.set_all_in_stock_conn(conn, point_id, True)


async def _record_status_change_conn(
    conn, point_id: str, old_status: str, new_status: str, source: str, task_id: Optional[str] = None
) -> None:
    """Appends a point_status_history row on an already-open connection/
    transaction — called from update_point/set_status below whenever a
    point's status actually changes, feeding the statistics page's
    "went down / recovered over period" metrics and general audit trail.
    `task_id` is only ever given for source='visit_report' — it's what lets
    delete_for_task() find and undo exactly the history row a deleted
    report caused."""
    if old_status == new_status:
        return
    await conn.execute(
        "insert into point_status_history (point_id, old_status, new_status, source, changed_at, task_id) "
        "values (?, ?, ?, ?, ?, ?)",
        (point_id, old_status, new_status, source, dt.datetime.now(dt.timezone.utc).isoformat(), task_id),
    )
    stats_cache.invalidate()      # сводка «Статистики» зависит от истории статусов

# Значение фильтра по статусу для «Не известно» (пустой статус) — то же
# служебное значение, что у «Без категории» в фильтре категорий.
NO_STATUS_FILTER = "__none__"

_KEYS = [
    "id", "address", "description", "lat", "lon",
    "inventory_number", "sim_number", "ip_address", "port", "notes", "status",
    "login", "password", "verification_valid_until", "power_source", "fault_reason", "fault_need", "fault_at", "fault_by", "category_id",
]
_COLUMNS = ", ".join(_KEYS)
_ENCRYPTED_KEYS = ("login", "password")        # в базе лежат зашифрованными (app/db/field_encryption.py): читаются через вычисляемые столбцы, пишутся в login_enc / password_enc


def _stored(key: str, value: str) -> str:
    return field_crypto.encrypt(f"points.{key}", value) if key in _ENCRYPTED_KEYS else value

# «Частично на складе» — подпись, а не статус (см. app/core/warehouse.py): точка
# «На складе», у которой есть компонент, не лежащий на складе.
_STOCK_PARTIAL_SQL = (
    f"(p.status = '{WAREHOUSE_STATUS}' and exists "
    "(select 1 from point_components c where c.point_id = p.id and c.in_stock = 0)) as stock_partial"
)

_READ_SELECT = f"""
    select {', '.join('p.' + k for k in _KEYS)}, pc.name as category_name, pc.color as category_color,
           pc.is_legacy as category_is_legacy, {_STOCK_PARTIAL_SQL}
    from points p
    left join point_categories pc on pc.id = p.category_id
"""


def _row(row) -> dict[str, Any]:
    d = dict(zip(_KEYS, row))
    d["category_name"] = row[len(_KEYS)] or ""
    d["category_color"] = row[len(_KEYS) + 1] or ""
    d["category_is_legacy"] = bool(row[len(_KEYS) + 2])
    d["stock_partial"] = bool(row[len(_KEYS) + 3])
    return d



_TEXT_FIELDS = ("address", "description", "notes", "inventory_number")


def _decoded(data: dict[str, Any], *, with_id: bool) -> dict[str, Any]:
    """Free-text fields can arrive entity-encoded ("&#1052;..." for "МЕ...",
    see app/core/text.py) from a pasted table or a Yandex Constructor file —
    decoded before saving so it doesn't get stored (and re-exported) that
    way. `id` is only decoded on create: an existing point's id is what
    tasks/reports/photos reference, so silently renaming it on an
    unrelated edit would orphan them — the one-time cleanup in
    database.py renames those properly, with every reference."""
    out = dict(data)
    for k in _TEXT_FIELDS:
        if k in out:
            out[k] = decode_entities(out[k])
    if with_id and "id" in out:
        out["id"] = decode_entities(out["id"]).strip()
    return out


async def _attach_stock_details(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Комплексам с подписью «Частично на складе» добавляет, какие компоненты лежат на складе (stock_in), а какие нет (stock_out) —
    чтобы в карточке было видно не только слова «частично», но и что именно. У остальных поля не добавляются (их сотни)."""
    ids = [p["id"] for p in items if p.get("stock_partial")]
    if not ids:
        return items
    comps = await point_components_repository.list_for_points(ids)
    for p in items:
        if p.get("stock_partial"):
            rows = comps.get(p["id"], [])
            p["stock_in"] = [c["name"] for c in rows if c["in_stock"]]
            p["stock_out"] = [c["name"] for c in rows if not c["in_stock"]]
    return items


async def list_points() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_READ_SELECT + " order by p.id")
        items = [_row(r) for r in await cur.fetchall()]
    return await _attach_stock_details(items)


async def get_point(point_id: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_READ_SELECT + " where p.id = ?", (point_id,))
        row = await cur.fetchone()
    if not row:
        return None
    point = _row(row)
    await _attach_stock_details([point])
    return point


async def mark_health_checked(point_id: str) -> None:
    with quiet_writes():        # отметка «проверено» пишется при каждой проверке — кэш списка комплексов из-за неё не сбрасываем
        await _mark_health_checked(point_id)


async def _mark_health_checked(point_id: str) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "update points set health_checked_at = ? where id = ?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), point_id),
        )
        await conn.commit()


async def get_port_down_since(point_id: str) -> Optional[str]:
    async with get_connection() as conn:
        cur = await conn.execute("select port_down_since from points where id = ?", (point_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_port_down_since(point_id: str, value: Optional[str]) -> None:
    async with get_connection() as conn:
        await conn.execute("update points set port_down_since = ? where id = ?", (value, point_id))
        await conn.commit()


async def search_points(
    search: str = "",
    category_ids: Optional[list[int]] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    uncategorized: bool = False,
    statuses: Optional[list[str]] = None,
    sort: str = "",
    certificate: str = "",
    group_id: Optional[int] = None,
) -> tuple[list[dict[str, Any]], int]:
    """Server-side filtered/paginated variant of list_points(), for the
    "Все АПК" directory page — with a few thousand points, shipping the
    whole table to the browser on every open/search keystroke doesn't
    scale, so search and "load more" happen here instead of client-side.

    pylower(...) (see database.py) is used instead of a bare LIKE — SQLite's
    own LIKE only case-folds ASCII, and most of what's searched here
    (addresses, descriptions) is Cyrillic.

    category_ids/statuses are OR'd within themselves (any of the selected
    categories/statuses match; the status "__none__" means an empty status,
    shown as «Не известно») and AND'd with each other and search — the
    multi-select filter chips on the "Все АПК"/"Склад" pages. `uncategorized`
    combines with category_ids as an extra OR branch (so "Без категории" +
    a real category can be selected together).

`port_down_since` is when the point's configured port (plain ping if it
    has none) last STARTED failing — maintained by
    app/services/point_health_service.py, null while currently up. Used to
    show/sort by "down for N days" — deliberately NOT the same thing as
    the point's status string (which also covers non-network states like
    "Демонтирован"): a point that still answers ping but not its port
    counts as down here even though its status is "Есть только пинг", not
    "Не работает". sort="down_duration" orders currently-down points
    first, longest-down first (points that have never gone down sort last
    within that group). sort="recent_down" is the mirror image: currently-down
    points first, the most recently dropped on top.

    certificate ("valid"|"expired"|"missing"|"invalid" — нет или истёк) filters by verification_valid_until
    against today's date (plain string compare — the column is always
    "YYYY-MM-DD" or empty, so lexicographic order matches calendar order)."""
    where: list[str] = []
    params: list[Any] = []
    if search and search.strip():
        # Номер набирают по-разному: «ARENA 0901205», «arena0901205», «ТО-5» латиницей и т. п. — а в названии дефис, русские буквы и т. д. Поэтому, кроме поиска по подстроке,
        # сравнивается «название без разбора» (без регистра, пробелов, знаков; латинские двойники русских букв равны русским), а запрос из нескольких слов находит комплекс,
        # в котором есть ВСЕ слова (в номере, инвентарном номере, адресе или описании), в любом порядке.
        text = search.strip()
        s = f"%{text}%"
        branches = ["pylower(p.id) like pylower(?)", "pylower(p.description) like pylower(?)", "pylower(p.inventory_number) like pylower(?)"]
        params.extend([s, s, s])
        compact = compact_name(text)
        if compact:
            branches += ["pycompact(p.id) like ?", "pycompact(p.inventory_number) like ?"]
            params.extend([f"%{compact}%"] * 2)
        words = [w for w in text.split() if w]
        if len(words) > 1:
            cond, wparams = [], []
            for w in words:
                wc = compact_name(w)
                if not wc:
                    continue
                cond.append("pycompact(coalesce(p.id, '') || ' ' || coalesce(p.inventory_number, '') || ' ' || coalesce(p.address, '') || ' ' || coalesce(p.description, '')) like ?")
                wparams.append(f"%{wc}%")
            if len(cond) > 1:
                branches.append("(" + " and ".join(cond) + ")")
                params.extend(wparams)
        where.append("(" + " or ".join(branches) + ")")
    category_ids = [c for c in (category_ids or []) if c]
    if uncategorized or category_ids:
        branches = []
        if uncategorized:
            branches.append("p.category_id is null")
        if category_ids:
            branches.append(f"p.category_id in ({', '.join('?' for _ in category_ids)})")
            params.extend(category_ids)
        where.append("(" + " or ".join(branches) + ")")
    statuses = [s for s in (statuses or []) if s]
    if statuses:
        # NO_STATUS_FILTER — фильтр «Не известно»: точки с пустым статусом
        # (ни разу не проверялись: нет IP/SIM, проверку доступности не на что
        # направить) — OR'ится с обычными статусами, как «Без категории».
        named = [s for s in statuses if s != NO_STATUS_FILTER]
        branches = []
        if len(named) != len(statuses):
            branches.append("(p.status = '' or p.status is null)")
        if named:
            branches.append(f"p.status in ({', '.join('?' for _ in named)})")
            params.extend(named)
        where.append("(" + " or ".join(branches) + ")")
    if certificate == "missing":
        where.append("(p.verification_valid_until = '' or p.verification_valid_until is null)")
    elif certificate == "valid":
        where.append("p.verification_valid_until != '' and p.verification_valid_until >= ?")
        params.append(dt.date.today().isoformat())
    elif certificate == "expired":
        where.append("p.verification_valid_until != '' and p.verification_valid_until < ?")
        params.append(dt.date.today().isoformat())
    elif certificate == "invalid":
        # «без поверки»: сертификата нет вовсе или он уже истёк
        where.append("(p.verification_valid_until = '' or p.verification_valid_until is null or p.verification_valid_until < ?)")
        params.append(dt.date.today().isoformat())
    where_sql = (" where " + " and ".join(where)) if where else ""
    # Подгруппа АПК: только её комплексы, и к каждому — его СОБСТВЕННЫЕ описание, «что нужно на выезде» и тип работы в этой подгруппе (не связаны с карточкой).
    join_sql, join_params, group_cols = "", [], ""
    if group_id is not None:
        join_sql = " join apk_group_points g on g.point_id = p.id and g.group_id = ?"
        join_params = [group_id]
        group_cols = ", g.note, g.need, g.work_type, g.added_at"

    async with get_connection() as conn:
        cur = await conn.execute(f"select count(*) from points p{join_sql}{where_sql}", [*join_params, *params])
        (total,) = await cur.fetchone()

        select_cols = ', '.join('p.' + k for k in _KEYS)
        sql = (
            f"select {select_cols}, pc.name as category_name, pc.color as category_color, "
            f"pc.is_legacy as category_is_legacy, {_STOCK_PARTIAL_SQL}, p.port_down_since, p.health_checked_at{group_cols} "
            "from points p left join point_categories pc on pc.id = p.category_id"
            + join_sql + where_sql
        )
        if sort == "down_duration":
            sql += " order by p.port_down_since is null, p.port_down_since asc, p.id"
        elif sort == "recent_down":
            # Сначала те, что отвалились совсем недавно (самые свежие сверху), потом — по убыванию давности.
            sql += " order by p.port_down_since is null, p.port_down_since desc, p.id"
        else:
            sql += " order by p.id"
        query_params = [*join_params, *params]
        if limit is not None:
            sql += " limit ? offset ?"
            query_params += [limit, offset]
        cur = await conn.execute(sql, query_params)
        rows = await cur.fetchall()
        items = []
        for r in rows:
            d = _row(r[: len(_KEYS) + 4])
            d["port_down_since"] = r[len(_KEYS) + 4]
            d["health_checked_at"] = r[len(_KEYS) + 5]
            if group_id is not None:
                d["group_note"], d["group_need"], d["group_work_type"], d["group_added_at"] = (x or "" for x in r[len(_KEYS) + 6: len(_KEYS) + 10])
            items.append(d)
    await _attach_stock_details(items)
    return items, total


async def _resolve_category_id(conn, data: dict[str, Any]) -> Optional[int]:
    if "category_name" in data or "category_color" in data:
        return await point_categories_repository.get_or_create(
            conn, data.get("category_name"), data.get("category_color")
        )
    return data.get("category_id") or None


@stats_cache.invalidates
async def create_point(data: dict[str, Any]) -> dict[str, Any]:
    data = _decoded(data, with_id=True)
    async with get_connection() as conn:
        category_id = await _resolve_category_id(conn, data)
        if category_id is None:
            # No explicit category — try to auto-detect one from the point's
            # id/description matching a category's match_pattern (e.g.
            # "ARENA", "POST", "Метеор"). Only on create: an update that
            # explicitly clears the category must not get silently
            # re-assigned — see point_categories_repository.detect_category_id
            # and the bulk POST /api/point-categories/auto-assign for
            # retrofitting already-uncategorized points.
            category_id = await point_categories_repository.detect_category_id(
                conn, str(data.get("id") or ""), str(data.get("description") or "")
            )
        placeholders = ", ".join(["?"] * len(_KEYS))
        # логин и пароль комплекса хранятся только зашифрованными: пишем в столбцы *_enc (login/password — вычисляемые, только для чтения)
        columns = ", ".join(f"{k}_enc" if k in _ENCRYPTED_KEYS else k for k in _KEYS)
        await conn.execute(
            f"insert into points ({columns}) values ({placeholders})",
            tuple(_stored(k, str(data.get(k) or "")) for k in _KEYS[:-1]) + (category_id,),
        )
        await _sync_sim_conn(conn, str(data["id"]), "", str(data.get("sim_number") or ""))
        await conn.commit()
    map_events.kick()   # точка с координатами могла появиться на карте бригады
    return await get_point(str(data["id"]))  # type: ignore[return-value]


@stats_cache.invalidates
async def update_point(point_id: str, data: dict[str, Any]) -> None:
    data = _decoded(data, with_id=False)
    async with get_connection() as conn:
        cur = await conn.execute("select status, sim_number, power_source, fault_reason, fault_need, fault_at, fault_by from points where id = ?", (point_id,))
        row = await cur.fetchone()
        old_status = row[0] if row else ""
        old_sim = row[1] if row else ""
        # Не пришло поле питания (старый клиент, частичный словарь) — оставляем как было.
        power_source = data.get("power_source")
        power_source = (row[2] if row else "") if power_source is None else str(power_source)
        # Так же — «неисправность из отчёта»: не прислали — остаётся прежнее; прислали — записываем (правка вручную в карточке).
        def keep_fault(key: str, idx: int) -> str:
            value = data.get(key)
            return (row[idx] or "") if (value is None and row) else ("" if value is None else str(value).strip())
        fault_reason, fault_need = keep_fault("fault_reason", 3), keep_fault("fault_need", 4)
        fault_at, fault_by = keep_fault("fault_at", 5), keep_fault("fault_by", 6)

        new_sim = str(data.get("sim_number") or "")
        registry_ip = ""
        if new_sim and new_sim != old_sim:
            # Оператор/админ сам сменил (или добавил) номер SIM в форме
            # точки — тут же подставляем IP из реестра sim_ip_registry, как
            # уже делает бот в set_point_sim (routes_bot.py), безусловно
            # перезаписывая то, что было в поле IP раньше: reestr — источник
            # истины для связки SIM↔IP, а не то, что вручную стояло в форме.
            sim_normalized = "".join(c for c in new_sim if c.isdigit())[-7:] or new_sim
            known_ip = await sim_ip_registry_repository.get_ip_for_sim(sim_normalized)
            if known_ip:
                data = {**data, "ip_address": known_ip}
                registry_ip = known_ip

        category_id = await _resolve_category_id(conn, data)
        new_id = str(data.get("id") or point_id)
        new_status = str(data.get("status") or "")
        await conn.execute(
            "update points set id = ?, address = ?, description = ?, lat = ?, lon = ?, "
            "inventory_number = ?, sim_number = ?, ip_address = ?, port = ?, notes = ?, status = ?, "
            "login_enc = ?, password_enc = ?, verification_valid_until = ?, power_source = ?, "
            "fault_reason = ?, fault_need = ?, fault_at = ?, fault_by = ?, category_id = ? where id = ?",
            (
                new_id,
                str(data.get("address") or ""),
                str(data.get("description") or ""),
                str(data.get("lat") or ""),
                str(data.get("lon") or ""),
                str(data.get("inventory_number") or ""),
                str(data.get("sim_number") or ""),
                str(data.get("ip_address") or ""),
                str(data.get("port") or ""),
                str(data.get("notes") or ""),
                new_status,
                _stored("login", str(data.get("login") or "")),
                _stored("password", str(data.get("password") or "")),
                str(data.get("verification_valid_until") or ""),
                power_source,
                fault_reason, fault_need, fault_at, fault_by,
                category_id,
                point_id,
            ),
        )
        if new_id != point_id:
            # Номер комплекса — текстовая ссылка (не внешний ключ) в заданиях, отчётах, фото, истории статусов, запросах, сессиях бота, оборудовании, чате,
            # «Живых картах» и подгруппах: при переименовании всё это переезжает на новый номер (app/services/point_rename.py), иначе фото и отчёты «осиротеют».
            # warehouse_items и point_components — внешние ключи с каскадом, они следуют сами.
            await point_rename.rename_references_conn(conn, point_id, new_id)
            await equipment_service.rename_point_conn(conn, point_id, new_id)
        await _links_after_sim_edit_conn(conn, new_id, old_sim, new_sim, registry_ip)
        await _sync_sim_conn(conn, new_id, old_sim, new_sim)
        # Leaving the shelf — the warehouse-only data ("статус паутины",
        # receipt date, info) no longer applies and is cleared and the
        # components are un-stocked; arriving as «На складе» stocks them all.
        await _on_warehouse_transition_conn(conn, new_id, old_status, new_status)
        await _record_status_change_conn(conn, new_id, old_status, new_status, "manual")
        await conn.commit()
    map_events.kick()   # координаты, номер или описание точки — это подпись и положение метки на картах бригад


async def set_power_source(point_id: str, power_source: str) -> bool:
    """Only the power-source column — what the bot saves after asking the
    fitter «СЭС или Электросеть?», without touching anything else."""
    async with get_connection() as conn:
        cur = await conn.execute("update points set power_source = ? where id = ?", (power_source, point_id))
        await conn.commit()
        return cur.rowcount > 0


async def sync_ip_from_sim_registry(pairs: list[tuple[str, str]]) -> int:
    """After a SIM↔IP registry import (or update), reconcile every point
    against each (sim_number, ip_address) pair two ways — the IP is the
    org's stable per-connection identity (tied to the subscription/
    location), the SIM is what actually gets swapped out when one expires
    and a replacement takes over the same IP:

    1. Same SIM, IP changed (dynamic reassignment without a physical
       swap) — push the new IP onto whichever point still has that SIM.
    2. Same IP, SIM changed (the old SIM expired and got replaced) — push
       the new SIM onto whichever point still has that IP.

    Returns how many points changed (each point counted once even if both
    of its fields got corrected)."""
    if not pairs:
        return 0
    async with get_connection() as conn:
        touched: dict[str, dict] = {}
        for sim_number, ip_address in pairs:
            cur = await conn.execute(f"select {_COLUMNS} from points where sim_number = ?", (sim_number,))
            for row in await cur.fetchall():
                point = dict(zip(_KEYS, row))
                if point.get("ip_address") != ip_address:
                    point["ip_address"] = ip_address
                    touched[point["id"]] = point

            cur = await conn.execute(f"select {_COLUMNS} from points where ip_address = ?", (ip_address,))
            for row in await cur.fetchall():
                point = touched.get(row[0]) or dict(zip(_KEYS, row))
                if point.get("sim_number") != sim_number:
                    point["sim_number"] = sim_number
                    touched[point["id"]] = point

    for point in touched.values():
        await update_point(point["id"], point)
    return len(touched)


@stats_cache.invalidates
async def delete_point(point_id: str) -> None:
    async with get_connection() as conn:
        # Стоявшее на комплексе оборудование уходит на склад дефектным (с пометкой), панель отвязывается.
        await equipment_service.release_point_equipment_conn(
            conn, point_id, performed_by=None, reason="Комплекс удалён из справочника"
        )
        await conn.execute("delete from point_components where point_id = ?", (point_id,))
        await conn.execute("delete from apk_group_points where point_id = ?", (point_id,))
        await point_links_repository.unlink_conn(conn, point_id)
        await conn.execute("delete from points where id = ?", (point_id,))
        await conn.commit()
    from app.services import point_files  # noqa: PLC0415 — файлы карточки уходят вместе с комплексом
    await point_files.delete_for_point(point_id)
    map_events.kick()


async def set_status(point_id: str, status: str, source: str = "auto", task_id: Optional[str] = None) -> None:
    """Narrow update used by the automatic health check, the warehouse
    page's status-only endpoint, and the visit-report flow — touches only
    the status column so it can't clobber a concurrent edit to the point's
    other fields. `source` tags the point_status_history row (see
    _record_status_change_conn) — 'auto' (default) for the background/
    manual-ping-triggered checker, 'manual' for a dispatcher-driven change,
    'visit_report' for the task completion report flow (`task_id` given
    alongside it — see that param's own docstring on _record_status_change_conn)."""
    async with get_connection() as conn:
        cur = await conn.execute("select status from points where id = ?", (point_id,))
        row = await cur.fetchone()
        old_status = row[0] if row else ""
        await conn.execute("update points set status = ? where id = ?", (status, point_id))
        await _on_warehouse_transition_conn(conn, point_id, old_status, status)
        await _record_status_change_conn(conn, point_id, old_status, status, source, task_id)
        await conn.commit()


async def apply_report_fault(point_id: str, report: dict[str, Any]) -> bool:
    """Свежий отчёт выезда обновляет в карточке комплекса «почему не работает» / «что нужно для восстановления» (правила — app/core/fault.py).
    Возвращает, изменилось ли что-нибудь."""
    async with get_connection() as conn:
        cur = await conn.execute("select fault_reason, fault_need, fault_at, fault_by from points where id = ?", (point_id,))
        row = await cur.fetchone()
        if not row:
            return False
        nxt = fault_rules.next_state({"reason": row[0] or "", "need": row[1] or "", "at": row[2] or "", "by": row[3] or ""}, report)
        if nxt is None:
            return False
        await conn.execute("update points set fault_reason = ?, fault_need = ?, fault_at = ?, fault_by = ? where id = ?",
                           (nxt["reason"], nxt["need"], nxt["at"], nxt["by"], point_id))
        await conn.commit()
    return True


async def recompute_fault_if_source(point_id: str, report_created_at: str) -> None:
    """Отчёт удалён: если именно он был источником записанной неисправности — состояние пересчитывается по оставшимся отчётам комплекса."""
    async with get_connection() as conn:
        cur = await conn.execute("select fault_at from points where id = ?", (point_id,))
        row = await cur.fetchone()
        if not row or (row[0] or "") != (report_created_at or ""):
            return
        cur = await conn.execute(
            "select r.result_status, r.fault, r.needs, r.created_at, coalesce(pe.full_name, r.created_by, '') "
            "from task_visit_reports r left join people pe on pe.id = r.author_person_id where r.point_id = ? order by r.created_at asc, r.rowid asc", (point_id,))
        state = {"reason": "", "need": "", "at": "", "by": ""}
        for status, fault_text, needs, created_at, by in await cur.fetchall():
            nxt = fault_rules.next_state(state, {"result_status": status, "fault": fault_text, "needs": needs, "created_at": created_at, "by": by})
            if nxt is not None:
                state = nxt
        await conn.execute("update points set fault_reason = ?, fault_need = ?, fault_at = ?, fault_by = ? where id = ?",
                           (state["reason"], state["need"], state["at"], state["by"], point_id))
        await conn.commit()
