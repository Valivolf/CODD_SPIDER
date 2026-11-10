"""Связки комплексов с общей SIM-картой.

Часть комплексов (в основном Пульсары) законно сидит на одной SIM в общей сети и отличается только портом. Диспетчер
может такие комплексы СВЯЗАТЬ (вкладка «Ошибки данных» в уведомлениях): пока они в связке, смена номера SIM на любом из
них — в карточке, ботом, из учёта оборудования — тут же меняет номер (и IP из реестра SIM↔IP, если он там есть) у остальных,
а общая SIM в учёте оборудования остаётся одной записью.

Правила:
  • связываются только комплексы с одним и тем же номером SIM, в связке всегда не меньше двух комплексов;
  • убрали номер SIM у комплекса (очистили поле, демонтаж, снятие SIM) — он выходит из связки, остальные не затрагиваются;
  • комплекс удалили или переименовали — связка следует за ним."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core import equipment as eq
from app.db.database import get_connection


class LinkError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def group_of_conn(conn, point_id: str) -> Optional[int]:
    cur = await conn.execute("select group_id from point_sim_links where point_id = ?", (point_id,))
    row = await cur.fetchone()
    return row[0] if row else None


async def others_conn(conn, point_id: str) -> list[str]:
    """Остальные комплексы связки этой точки (пусто, если она ни с кем не связана)."""
    cur = await conn.execute(
        "select b.point_id from point_sim_links a join point_sim_links b on b.group_id = a.group_id and b.point_id != a.point_id "
        "where a.point_id = ? order by b.point_id", (point_id,),
    )
    return [r[0] for r in await cur.fetchall()]


async def group_ids_conn(conn, point_id: str) -> list[str]:
    """Вся связка точки, сама точка первой; несвязанная точка → [point_id]."""
    return [point_id, *await others_conn(conn, point_id)]


async def groups_conn(conn) -> dict[int, list[str]]:
    cur = await conn.execute("select group_id, point_id from point_sim_links order by group_id, point_id")
    out: dict[int, list[str]] = {}
    for gid, pid in await cur.fetchall():
        out.setdefault(gid, []).append(pid)
    return out


async def link_conn(conn, point_ids: Iterable[str]) -> int:
    """Объединить комплексы в одну связку (уже существующие связки этих комплексов сливаются в одну). Возвращает id связки."""
    ids = list(dict.fromkeys(point_ids))
    marks = ",".join("?" * len(ids))
    cur = await conn.execute(f"select distinct group_id from point_sim_links where point_id in ({marks})", ids)
    existing = sorted(r[0] for r in await cur.fetchall())
    if existing:
        gid = existing[0]
        for other in existing[1:]:
            await conn.execute("update point_sim_links set group_id = ? where group_id = ?", (gid, other))
    else:
        cur = await conn.execute("select coalesce(max(group_id), 0) + 1 from point_sim_links")
        gid = (await cur.fetchone())[0]
    for pid in ids:
        await conn.execute("insert or replace into point_sim_links (point_id, group_id) values (?, ?)", (pid, gid))
    return gid


async def unlink_conn(conn, point_id: str) -> bool:
    """Вывести комплекс из связки; если в связке остался один — она распадается. False — комплекс ни с кем не был связан."""
    gid = await group_of_conn(conn, point_id)
    if gid is None:
        return False
    await conn.execute("delete from point_sim_links where point_id = ?", (point_id,))
    cur = await conn.execute("select count(*) from point_sim_links where group_id = ?", (gid,))
    if (await cur.fetchone())[0] < 2:
        await conn.execute("delete from point_sim_links where group_id = ?", (gid,))
    return True


async def rename_point_conn(conn, old_id: str, new_id: str) -> None:
    await conn.execute("update point_sim_links set point_id = ? where point_id = ?", (new_id, old_id))


async def propagate_sim_conn(conn, point_id: str, value: str, ip: str = "") -> list[str]:
    """Номер SIM комплекса сменили — тот же номер (и IP из реестра, если он известен) получают остальные комплексы связки."""
    others = await others_conn(conn, point_id)
    for pid in others:
        if ip:
            await conn.execute("update points set sim_number = ?, ip_address = ? where id = ?", (value, ip, pid))
        else:
            await conn.execute("update points set sim_number = ? where id = ?", (value, pid))
    return others


# ------------------------------------------------------------------ для API

async def link_points(point_ids: Iterable[str]) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(i).strip() for i in point_ids if str(i).strip()))
    if len(ids) < 2:
        raise LinkError("Выберите хотя бы два комплекса")
    async with get_connection() as conn:
        marks = ",".join("?" * len(ids))
        cur = await conn.execute(f"select id, sim_number from points where id in ({marks})", ids)
        sims = {r[0]: r[1] for r in await cur.fetchall()}
        missing = [i for i in ids if i not in sims]
        if missing:
            raise LinkError("Комплекс не найден: " + ", ".join(missing), 404)
        keys = {eq.sim_key(s) for s in sims.values()}
        if len(keys) != 1 or not eq.sim_key_valid(next(iter(keys))):
            raise LinkError("Связать можно только комплексы с одним и тем же номером SIM")
        gid = await link_conn(conn, ids)
        cur = await conn.execute(
            "select p.id, p.sim_number from point_sim_links l join points p on p.id = l.point_id where l.group_id = ? order by p.id", (gid,)
        )
        members = [(r[0], r[1]) for r in await cur.fetchall()]
        if len({eq.sim_key(s) for _, s in members}) != 1:
            await conn.rollback()
            raise LinkError("В одной из связок, к которой относятся эти комплексы, номера SIM расходятся — сначала разорвите её")
        await conn.commit()
    return {"group_id": gid, "members": [m for m, _ in members], "sim": next(iter(keys))}


async def unlink_point(point_id: str) -> bool:
    async with get_connection() as conn:
        done = await unlink_conn(conn, point_id)
        await conn.commit()
    return done


async def unlink_group(group_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from point_sim_links where group_id = ?", (group_id,))
        await conn.commit()
        return cur.rowcount > 0


async def linked_with(point_id: str) -> list[dict[str, str]]:
    """С кем связан комплекс общей SIM (для подсказки в карточке)."""
    async with get_connection() as conn:
        ids = await others_conn(conn, point_id)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        cur = await conn.execute(f"select id, address from points where id in ({marks}) order by id", ids)
        return [{"id": r[0], "address": r[1] or ""} for r in await cur.fetchall()]
