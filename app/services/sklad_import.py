"""Импорт базы прежнего приложения «Склад» (warehouse.db) в общую базу.

Файл читается только на чтение, поддерживаются и старые версии схемы (нет каких-то колонок или таблиц).
Соответствия:
  комплексы «Склада»        → точки (комплексы) календаря: по инвентарному номеру, затем по названию
                              (= номер точки); не найден — создаётся новая точка
  монтёры                   → персонал (роль «монтёр») — по ФИО, нет — создаётся
  категории / модели        → категории / модели оборудования — по названию
  категории комплексов      → категории точек; статусы комплексов — только для новых точек
  оборудование              → по серийному номеру; статус «Установлено» → на комплексе, «Выдано» → у монтёра,
                              «На складе» → склад/исправно, «Снято», «В ремонте», «Списано» → склад с таким состоянием
  солнечные панели          → панели (привязка к комплексу сохраняется)
  журнал                    → журнал оборудования (повторы пропускаются)

Режимы: merge — добавить новое и дополнить пустые поля существующего, не меняя его место и состояние;
overwrite — у совпавших по серийному номеру применить место/состояние/даты из файла.
dry_run — посчитать, что будет, ничего не записывая.
"""
from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.core import equipment as eq
from app.core import field_crypto
from app.db.database import get_connection
from app.repositories import equipment_repository as repo
from app.repositories import point_categories_repository

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_SOURCE_BYTES = 300 * 1024 * 1024
_LIST_LIMIT = 30

_POINT_STATUS_MAP = {
    "работает": "Работает",
    "не работает": "Не работает",
    "на складе": "На складе",
    "частично на складе": "На складе",
}


class ImportError_(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def prepare_source(raw: bytes, workdir: Path, name: str) -> Path:
    """Сохраняет загруженный файл (распаковывая gzip) и проверяет, что это SQLite."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception as exc:  # noqa: BLE001
            raise ImportError_("Не удалось распаковать файл (.gz)") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise ImportError_("Файл слишком большой")
    if not raw.startswith(SQLITE_MAGIC):
        raise ImportError_("Это не файл базы SQLite. Нужен warehouse.db из приложения «Склад».")
    path = workdir / name
    path.write_bytes(raw)
    return path


def _read_table(con: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    try:
        cur = con.execute(f"select * from {table}")
    except sqlite3.OperationalError:
        return []
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        names = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    except sqlite3.DatabaseError as exc:
        raise ImportError_("Файл повреждён или это не база SQLite") from exc
    if not ({"equipment", "complexes"} & names):
        con.close()
        raise ImportError_("В файле нет таблиц «Склада» (equipment / complexes) — это не его база.")
    try:
        return {
            "categories": _read_table(con, "categories"),
            "models": _read_table(con, "models"),
            "installers": _read_table(con, "installers"),
            "complex_categories": _read_table(con, "complex_categories"),
            "complex_statuses": _read_table(con, "complex_statuses"),
            "complexes": _read_table(con, "complexes"),
            "solar_panels": _read_table(con, "solar_panels"),
            "equipment": _read_table(con, "equipment"),
            "history": _read_table(con, "history"),
        }
    finally:
        con.close()


def _s(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _n(v: Any) -> Optional[str]:
    return _s(v) or None


class _Report:
    def __init__(self, dry_run: bool, mode: str):
        self.data: dict[str, Any] = {
            "dry_run": dry_run, "mode": mode, "source": {},
            "equipment_categories": {"created": 0, "existing": 0},
            "models": {"created": 0, "existing": 0},
            "people": {"created": 0, "matched": 0, "created_names": []},
            "point_categories": {"created": 0, "existing": 0},
            "points": {"created": 0, "matched": 0, "filled": 0, "created_names": []},
            "panels": {"created": 0, "existing": 0, "linked": 0},
            "equipment": {"created": 0, "existing": 0, "updated": 0, "filled": 0, "conflicts": [], "conflict_count": 0},
            "history": {"added": 0, "duplicates": 0},
            "warnings": [],
        }

    def warn(self, text: str) -> None:
        w = self.data["warnings"]
        if len(w) < 100:
            w.append(text)
        elif len(w) == 100:
            w.append("…и ещё предупреждения (показаны первые 100)")


async def _point_status(conn, name: str, color: str, cache: dict[str, str]) -> str:
    key = name.strip().casefold()
    if key in cache:
        return cache[key]
    mapped = _POINT_STATUS_MAP.get(key)
    if mapped:
        cache[key] = mapped
        return mapped
    cur = await conn.execute("select name from point_statuses where pylower(name) = pylower(?)", (name.strip(),))
    row = await cur.fetchone()
    if row:
        cache[key] = row[0]
        return row[0]
    import datetime as dt

    await conn.execute(
        "insert into point_statuses (name, color, is_auto, protected, created_at) values (?, ?, 0, 0, ?)",
        (name.strip(), (color or "#64748b").strip() or "#64748b", dt.datetime.now(dt.timezone.utc).isoformat()),
    )
    cache[key] = name.strip()
    return cache[key]


async def run_import(
    src: dict[str, list[dict[str, Any]]], *, mode: str = "merge", dry_run: bool = True,
    import_history: bool = True, performed_by: Optional[str] = None, filename: str = "",
) -> dict[str, Any]:
    if mode not in ("merge", "overwrite"):
        raise ImportError_("Неизвестный режим импорта")
    rep = _Report(dry_run, mode)
    rep.data["source"] = {k: len(v) for k, v in src.items()}
    overwrite = mode == "overwrite"

    async with get_connection() as conn:
        ok_id = await repo.role_status_id_conn(conn, eq.ROLE_OK)

        # ---- категории и модели оборудования
        cat_map: dict[Any, int] = {}
        for c in src["categories"]:
            name = _s(c.get("name"))
            if not name:
                continue
            existed = await repo.category_by_name_conn(conn, name, create=False)
            cat_map[c["id"]] = existed or await repo.category_by_name_conn(conn, name)  # type: ignore[assignment]
            rep.data["equipment_categories"]["existing" if existed else "created"] += 1
        model_map: dict[Any, int] = {}
        for m in src["models"]:
            name = _s(m.get("name"))
            cid = cat_map.get(m.get("category_id"))
            if not name or not cid:
                continue
            existed = await repo.model_by_name_conn(conn, cid, name, create=False)
            model_map[m["id"]] = existed or await repo.model_by_name_conn(conn, cid, name)  # type: ignore[assignment]
            rep.data["models"]["existing" if existed else "created"] += 1

        # ---- монтёры → персонал
        person_map: dict[Any, int] = {}
        for i in src["installers"]:
            name = _s(i.get("full_name"))
            if not name:
                continue
            cur = await conn.execute("select id from people where pylower(trim(full_name)) = pylower(?) order by id limit 1", (name,))
            row = await cur.fetchone()
            if row:
                person_map[i["id"]] = row[0]
                rep.data["people"]["matched"] += 1
                continue
            cur = await conn.execute(
                "insert into people (full_name_enc, phone_enc, role_type, active, notes, created_at) values (?, ?, 'fitter', ?, ?, ?)",
                (field_crypto.encrypt("people.full_name", name), field_crypto.encrypt("people.phone", _n(i.get("phone"))),
                 1 if i.get("active", 1) else 0, "Добавлен при импорте из «Склада»", repo.now_iso()),
            )
            person_map[i["id"]] = cur.lastrowid
            rep.data["people"]["created"] += 1
            if len(rep.data["people"]["created_names"]) < _LIST_LIMIT:
                rep.data["people"]["created_names"].append(name)

        # ---- комплексы → точки
        cc_by_id = {c["id"]: c for c in src["complex_categories"]}
        cs_by_id = {s["id"]: s for s in src["complex_statuses"]}
        status_cache: dict[str, str] = {}
        pc_cache: dict[Any, Optional[int]] = {}
        point_map: dict[Any, str] = {}
        for cx in src["complexes"]:
            name, inv = _s(cx.get("name")), _s(cx.get("inventory_number"))
            if not name and not inv:
                continue
            point_id: Optional[str] = None
            if inv:
                cur = await conn.execute("select id from points where pylower(trim(inventory_number)) = pylower(?) limit 1", (inv,))
                row = await cur.fetchone()
                point_id = row[0] if row else None
            if point_id is None and name:
                cur = await conn.execute("select id, inventory_number from points where pylower(id) = pylower(?)", (name,))
                row = await cur.fetchone()
                if row and (not _s(row[1]) or not inv or _s(row[1]).casefold() == inv.casefold()):
                    point_id = row[0]
            cat = cc_by_id.get(cx.get("category_id"))
            category_id: Optional[int] = None
            if cat and _s(cat.get("name")):
                if cat["id"] in pc_cache:
                    category_id = pc_cache[cat["id"]]
                else:
                    cur = await conn.execute("select id from point_categories where pylower(name) = pylower(?)", (_s(cat["name"]),))
                    row = await cur.fetchone()
                    if row:
                        category_id = row[0]
                        rep.data["point_categories"]["existing"] += 1
                    else:
                        category_id = await point_categories_repository.get_or_create(conn, cat["name"], cat.get("color"))
                        rep.data["point_categories"]["created"] += 1
                    pc_cache[cat["id"]] = category_id
            if point_id is not None:
                rep.data["points"]["matched"] += 1
                filled = 0
                for column, value in (("address", _s(cx.get("location"))), ("notes", _s(cx.get("notes"))), ("inventory_number", inv)):
                    if value:
                        cur = await conn.execute(f"update points set {column} = ? where id = ? and coalesce({column}, '') = ''", (value, point_id))
                        filled += cur.rowcount
                if category_id:
                    cur = await conn.execute("update points set category_id = ? where id = ? and category_id is null", (category_id, point_id))
                    filled += cur.rowcount
                if filled:
                    rep.data["points"]["filled"] += 1
            else:
                new_id = name or inv
                cur = await conn.execute("select 1 from points where pylower(id) = pylower(?)", (new_id,))
                if await cur.fetchone():
                    new_id = f"{name} ({inv})" if inv else f"{name} (из Склада)"
                st = cs_by_id.get(cx.get("status_id")) or {}
                status_name = _s(st.get("name")) or _s(cx.get("status")) or "Работает"
                status = "На складе" if cx.get("at_warehouse") else await _point_status(conn, status_name, _s(st.get("color")), status_cache)
                if status == "Работает":      # SIM и IP при таком создании пусты — «Работает» из Склада ничем не подтверждено: статус «Не известно» (пустой), см. db/database.py: unknown_status_v1
                    status = ""
                await conn.execute(
                    "insert into points (id, address, description, lat, lon, inventory_number, sim_number, ip_address, port, notes, status, "
                    "login_enc, password_enc, verification_valid_until, power_source, category_id) "
                    "values (?, ?, '', '', '', ?, '', '', '', ?, ?, '', '', '', '', ?)",
                    (new_id, _s(cx.get("location")), inv, _s(cx.get("notes")), status, category_id),
                )
                point_id = new_id
                rep.data["points"]["created"] += 1
                if len(rep.data["points"]["created_names"]) < _LIST_LIMIT:
                    rep.data["points"]["created_names"].append(new_id)
            point_map[cx["id"]] = point_id

        # ---- солнечные панели
        panel_ids: dict[Any, int] = {}
        for sp in src["solar_panels"]:
            number = _s(sp.get("panel_number"))
            if not number:
                continue
            cur = await conn.execute("select id from solar_panels where pylower(panel_number) = pylower(?)", (number,))
            row = await cur.fetchone()
            if row:
                panel_ids[sp["id"]] = row[0]
                rep.data["panels"]["existing"] += 1
                for column, value in (("location", _s(sp.get("location"))), ("notes", _s(sp.get("notes")))):
                    if value:
                        await conn.execute(f"update solar_panels set {column} = ? where id = ? and coalesce({column}, '') = ''", (value, row[0]))
                continue
            now = repo.now_iso()
            cur = await conn.execute(
                "insert into solar_panels (panel_number, location, notes, at_warehouse, created_at, updated_at) values (?,?,?,?,?,?)",
                (number, _n(sp.get("location")), _n(sp.get("notes")), 1 if sp.get("at_warehouse") else 0, now, now),
            )
            panel_ids[sp["id"]] = cur.lastrowid
            rep.data["panels"]["created"] += 1
        for cx in src["complexes"]:
            pid_src, point_id = cx.get("panel_id"), point_map.get(cx["id"])
            if not pid_src or not point_id or pid_src not in panel_ids:
                continue
            panel_id = panel_ids[pid_src]
            cur = await conn.execute("select point_id, panel_number from solar_panels where id = ?", (panel_id,))
            cur_point, number = await cur.fetchone()
            if cur_point == point_id:
                continue
            if cur_point:
                rep.warn(f"Панель №{number} уже привязана к «{cur_point}» — привязка к «{point_id}» из файла пропущена")
                continue
            cur = await conn.execute("select panel_number from solar_panels where point_id = ?", (point_id,))
            other = await cur.fetchone()
            if other:
                rep.warn(f"У комплекса «{point_id}» уже стоит панель №{other[0]} — панель №{number} из файла не привязана")
                continue
            await conn.execute("update solar_panels set point_id = ?, updated_at = ? where id = ?", (point_id, repo.now_iso(), panel_id))
            rep.data["panels"]["linked"] += 1

        # ---- оборудование
        equip_map: dict[Any, int] = {}
        status_ids: dict[str, int] = {}

        async def _status(name: str) -> int:
            key = name.casefold()
            if key not in status_ids:
                color = {"снято": "#fb923c", "в ремонте": "#f59e0b", "списано": "#64748b"}.get(key, eq.DEFAULT_STATUS_COLOR)
                status_ids[key] = await repo.status_by_name_conn(conn, name, color=color, usable=0)  # type: ignore[assignment]
            return status_ids[key]

        for e in sorted(src["equipment"], key=lambda r: r.get("id") or 0):
            serial = _s(e.get("serial_number"))
            model_id = model_map.get(e.get("model_id"))
            if not serial:
                continue
            if not model_id:
                rep.warn(f"Оборудование №{serial}: не найдена модель — пропущено")
                continue
            old_status = _s(e.get("status")) or "На складе"
            place, status_id, point_id, person_id, held = eq.PLACE_STOCK, ok_id, None, None, None
            if old_status == "Установлено":
                point_id = point_map.get(e.get("complex_id"))
                if point_id:
                    place = eq.PLACE_POINT
                else:
                    rep.warn(f"Оборудование №{serial}: комплекс не найден — положено на склад")
            elif old_status == "Выдано":
                person_id = person_map.get(e.get("installer_id"))
                if person_id:
                    place, held = eq.PLACE_PERSON, eq.HELD_ISSUED
                else:
                    rep.warn(f"Оборудование №{serial}: монтёр не найден — положено на склад")
            elif old_status != "На складе":
                status_id = await _status(old_status)
            fields = dict(
                received_date=_n(e.get("received_date")), issued_date=_n(e.get("issued_date")) if place == eq.PLACE_PERSON else None,
                installed_date=_n(e.get("installed_date")) if place == eq.PLACE_POINT else None,
                installed_by=_n(e.get("installed_by")) if place == eq.PLACE_POINT else None, notes=_n(e.get("notes")),
            )
            existing = await repo.find_by_serial_conn(conn, serial)
            if existing is None:
                now = repo.now_iso()
                cur = await conn.execute(
                    "insert into equipment (model_id, serial_number, place, status_id, point_id, person_id, held_reason, "
                    "received_date, issued_date, installed_date, installed_by, notes, created_at, updated_at) "
                    "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        model_id, serial, place, status_id, point_id if place == eq.PLACE_POINT else None,
                        person_id if place == eq.PLACE_PERSON else None, held, fields["received_date"], fields["issued_date"],
                        fields["installed_date"], fields["installed_by"], fields["notes"], now, now,
                    ),
                )
                equip_map[e["id"]] = cur.lastrowid
                rep.data["equipment"]["created"] += 1
                continue
            equip_map[e["id"]] = existing["id"]
            rep.data["equipment"]["existing"] += 1
            same_state = existing["place"] == place and (
                (place == eq.PLACE_STOCK and existing["status_id"] == status_id)
                or (place == eq.PLACE_PERSON and existing["person_id"] == person_id)
                or (place == eq.PLACE_POINT and existing["point_id"] == point_id)
            )
            if not same_state:
                rep.data["equipment"]["conflict_count"] += 1
                if len(rep.data["equipment"]["conflicts"]) < _LIST_LIMIT:
                    rep.data["equipment"]["conflicts"].append({
                        "serial_number": serial,
                        "now": f"{existing['place_label']}" + (f" · {existing['status']}" if existing["place"] == eq.PLACE_STOCK else ""),
                        "in_file": eq.PLACE_LABEL[place] + (f" · {old_status}" if place == eq.PLACE_STOCK else ""),
                    })
            if overwrite and not same_state:
                await conn.execute(
                    "update equipment set place = ?, status_id = ?, point_id = ?, person_id = ?, held_reason = ?, "
                    "issued_date = ?, installed_date = ?, installed_by = ?, updated_at = ? where id = ?",
                    (
                        place, status_id if place == eq.PLACE_STOCK else existing["status_id"],
                        point_id if place == eq.PLACE_POINT else None, person_id if place == eq.PLACE_PERSON else None, held,
                        fields["issued_date"], fields["installed_date"], fields["installed_by"], repo.now_iso(), existing["id"],
                    ),
                )
                rep.data["equipment"]["updated"] += 1
            filled = 0
            for column in ("received_date", "notes"):
                if fields[column]:
                    cur = await conn.execute(f"update equipment set {column} = ? where id = ? and coalesce({column}, '') = ''", (fields[column], existing["id"]))
                    filled += cur.rowcount
            if filled:
                rep.data["equipment"]["filled"] += 1

        # ---- журнал
        if import_history:
            cur = await conn.execute(
                "select ts, action_type, coalesce(serial_number, ''), coalesce(person_name, ''), coalesce(comment, ''), coalesce(point_id, '') from equipment_history"
            )
            seen = {tuple(r) for r in await cur.fetchall()}
            serial_to_id: dict[str, int] = {}
            for h in sorted(src["history"], key=lambda r: ((r.get("ts") or ""), r.get("id") or 0)):
                ts = _s(h.get("ts"))
                action = _s(h.get("action_type"))
                if not ts or not action:
                    continue
                serial = _s(h.get("serial_number"))
                point = point_map.get(h.get("complex_id")) or _s(h.get("complex_name"))
                person_name = _s(h.get("installer_name"))
                comment = _s(h.get("comment"))
                key = (ts, action, serial, person_name, comment, point)
                if key in seen:
                    rep.data["history"]["duplicates"] += 1
                    continue
                seen.add(key)
                equipment_id = equip_map.get(h.get("equipment_id"))
                if equipment_id is None and serial:
                    if serial not in serial_to_id:
                        found = await repo.find_by_serial_conn(conn, serial)
                        serial_to_id[serial] = found["id"] if found else 0
                    equipment_id = serial_to_id[serial] or None
                await conn.execute(
                    "insert into equipment_history (ts, event_date, equipment_id, serial_number, model_name, action_type, "
                    "person_id, person_name, point_id, performed_by, comment) values (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ts, _s(h.get("event_date")) or ts[:10], equipment_id, _n(serial), _n(h.get("model_name")), action,
                        person_map.get(h.get("installer_id")), _n(person_name), _n(point), _n(h.get("performed_by")), _n(comment),
                    ),
                )
                rep.data["history"]["added"] += 1

        if not dry_run:
            await repo.add_history_conn(
                conn, eq.ACT_IMPORT, performed_by=performed_by,
                comment=f"Файл {filename or 'warehouse.db'}: оборудования +{rep.data['equipment']['created']}, "
                        f"комплексов +{rep.data['points']['created']}, режим «{'заменить' if overwrite else 'добавить'}»",
            )
            await conn.commit()
    return rep.data
