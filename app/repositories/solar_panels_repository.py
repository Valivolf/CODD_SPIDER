"""Солнечные панели (из «Склада»): номер, где лежит/стоит, привязка к комплексу. У комплекса не
больше одной панели, у панели — не больше одного комплекса (уникальный индекс по point_id).
Привязка и отвязка пишутся в журнал оборудования."""
from __future__ import annotations

from typing import Any, Optional

from app.core import equipment as eq
from app.db.database import get_connection
from app.repositories import equipment_repository as repo

_SELECT = (
    "select sp.id, sp.panel_number, sp.point_id, sp.location, sp.notes, sp.at_warehouse, sp.created_at, sp.updated_at, "
    "p.address, p.inventory_number from solar_panels sp left join points p on p.id = sp.point_id"
)
_KEYS = ["id", "panel_number", "point_id", "location", "notes", "at_warehouse", "created_at", "updated_at", "point_address", "point_inventory"]


class PanelError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _row(r) -> dict[str, Any]:
    d = dict(zip(_KEYS, r))
    d["at_warehouse"] = bool(d["at_warehouse"])
    return d


def _clean(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip()
    return v or None


async def list_panels(search: str = "", at_warehouse: bool = False, linked: str = "") -> list[dict[str, Any]]:
    sql = _SELECT + " where 1 = 1"
    params: list[Any] = []
    if search:
        s = f"%{search.strip()}%"
        sql += " and (pylower(sp.panel_number) like pylower(?) or pylower(coalesce(sp.location, '')) like pylower(?) or pylower(coalesce(sp.point_id, '')) like pylower(?))"
        params += [s, s, s]
    if at_warehouse:
        sql += " and sp.at_warehouse = 1"
    if linked == "yes":
        sql += " and sp.point_id is not null"
    elif linked == "no":
        sql += " and sp.point_id is null"
    sql += " order by sp.panel_number collate nocase"
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]


async def get_panel(panel_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        return await _get_conn(conn, panel_id)


async def _get_conn(conn, panel_id: int) -> Optional[dict[str, Any]]:
    cur = await conn.execute(_SELECT + " where sp.id = ?", (panel_id,))
    row = await cur.fetchone()
    return _row(row) if row else None


async def get_for_point(point_id: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " where sp.point_id = ?", (point_id,))
        row = await cur.fetchone()
        return _row(row) if row else None


async def _link_conn(conn, panel: dict[str, Any], point_id: Optional[str], performed_by: Optional[str], date: Optional[str] = None) -> None:
    if panel["point_id"] == point_id:
        return
    if panel["point_id"]:
        await conn.execute("update solar_panels set point_id = NULL, updated_at = ? where id = ?", (repo.now_iso(), panel["id"]))
        await repo.add_history_conn(
            conn, eq.ACT_PANEL_UNLINK, point_id=panel["point_id"], performed_by=performed_by,
            comment=f"Панель №{panel['panel_number']}", event_date=date,
        )
    if point_id:
        cur = await conn.execute("select id from points where id = ?", (point_id,))
        if not await cur.fetchone():
            raise PanelError("Комплекс не найден", 404)
        # У комплекса не больше одной панели: прежняя отвязывается.
        cur = await conn.execute("select id, panel_number from solar_panels where point_id = ? and id != ?", (point_id, panel["id"]))
        other = await cur.fetchone()
        if other:
            await conn.execute("update solar_panels set point_id = NULL, updated_at = ? where id = ?", (repo.now_iso(), other[0]))
            await repo.add_history_conn(
                conn, eq.ACT_PANEL_UNLINK, point_id=point_id, performed_by=performed_by,
                comment=f"Панель №{other[1]} (заменена на №{panel['panel_number']})", event_date=date,
            )
        await conn.execute("update solar_panels set point_id = ?, updated_at = ? where id = ?", (point_id, repo.now_iso(), panel["id"]))
        await repo.add_history_conn(
            conn, eq.ACT_PANEL_LINK, point_id=point_id, performed_by=performed_by,
            comment=f"Панель №{panel['panel_number']}", event_date=date,
        )


async def create_panel(
    panel_number: str, *, location: Optional[str] = None, notes: Optional[str] = None, at_warehouse: bool = False,
    point_id: Optional[str] = None, performed_by: Optional[str] = None,
) -> dict[str, Any]:
    number = (panel_number or "").strip()
    if not number:
        raise PanelError("Укажите номер панели")
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from solar_panels where pylower(panel_number) = pylower(?)", (number,))
        if await cur.fetchone():
            raise PanelError("Панель с таким номером уже существует", 409)
        now = repo.now_iso()
        cur = await conn.execute(
            "insert into solar_panels (panel_number, location, notes, at_warehouse, created_at, updated_at) values (?,?,?,?,?,?)",
            (number, _clean(location), _clean(notes), int(at_warehouse), now, now),
        )
        panel = await _get_conn(conn, cur.lastrowid)
        if point_id:
            await _link_conn(conn, panel, point_id, performed_by)
        await conn.commit()
        return await _get_conn(conn, panel["id"])  # type: ignore[return-value]


async def update_panel(
    panel_id: int, *, panel_number: str, location: Optional[str], notes: Optional[str], at_warehouse: bool,
    point_id: Optional[str], performed_by: Optional[str] = None,
) -> dict[str, Any]:
    number = (panel_number or "").strip()
    if not number:
        raise PanelError("Укажите номер панели")
    async with get_connection() as conn:
        panel = await _get_conn(conn, panel_id)
        if not panel:
            raise PanelError("Панель не найдена", 404)
        cur = await conn.execute(
            "select 1 from solar_panels where pylower(panel_number) = pylower(?) and id != ?", (number, panel_id)
        )
        if await cur.fetchone():
            raise PanelError("Панель с таким номером уже существует", 409)
        await conn.execute(
            "update solar_panels set panel_number = ?, location = ?, notes = ?, at_warehouse = ?, updated_at = ? where id = ?",
            (number, _clean(location), _clean(notes), int(at_warehouse), repo.now_iso(), panel_id),
        )
        panel = await _get_conn(conn, panel_id)
        await _link_conn(conn, panel, point_id or None, performed_by)  # type: ignore[arg-type]
        await conn.commit()
        return await _get_conn(conn, panel_id)  # type: ignore[return-value]


async def link_to_point(point_id: str, panel_id: Optional[int], performed_by: Optional[str] = None, date: Optional[str] = None) -> None:
    """Привязать панель к комплексу (panel_id=None — отвязать панель, стоящую на комплексе)."""
    async with get_connection() as conn:
        cur = await conn.execute("select id from points where id = ?", (point_id,))
        if not await cur.fetchone():
            raise PanelError("Комплекс не найден", 404)
        if panel_id is None:
            cur = await conn.execute(_SELECT + " where sp.point_id = ?", (point_id,))
            row = await cur.fetchone()
            if row:
                await _link_conn(conn, _row(row), None, performed_by, date)
        else:
            panel = await _get_conn(conn, panel_id)
            if not panel:
                raise PanelError("Панель не найдена", 404)
            await _link_conn(conn, panel, point_id, performed_by, date)
        await conn.commit()


async def delete_panel(panel_id: int, performed_by: Optional[str] = None) -> None:
    async with get_connection() as conn:
        panel = await _get_conn(conn, panel_id)
        if not panel:
            raise PanelError("Панель не найдена", 404)
        if panel["point_id"]:
            await repo.add_history_conn(
                conn, eq.ACT_PANEL_UNLINK, point_id=panel["point_id"], performed_by=performed_by,
                comment=f"Панель №{panel['panel_number']} удалена",
            )
        await conn.execute("delete from solar_panels where id = ?", (panel_id,))
        await conn.commit()
