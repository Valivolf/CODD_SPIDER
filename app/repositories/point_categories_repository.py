"""Free-form, colour-tagged categories for points ("АПК") — same
get-or-create-by-name pattern as the Sklad warehouse app's
complex_categories: typed freely on the point form, a new name gets its
own colour, an existing one (case-insensitive match) reuses its stored
colour.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.names import compact_name
from app.db.database import get_connection

DEFAULT_COLOR = "#6366f1"


async def list_categories() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select pc.id, pc.name, pc.color, pc.match_pattern, pc.is_legacy, count(p.id) as point_count
            from point_categories pc
            left join points p on p.category_id = pc.id
            group by pc.id
            order by pc.name collate nocase
            """
        )
        return [
            {
                "id": r[0], "name": r[1], "color": r[2], "match_pattern": r[3] or "",
                "is_legacy": bool(r[4]), "point_count": r[5],
            }
            for r in await cur.fetchall()
        ]


async def search_categories(q: str, limit: int = 10) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        if q:
            # pylower(...) (database.py) instead of a bare LIKE — SQLite's
            # own LIKE only case-folds ASCII, and category names here are
            # mostly Cyrillic.
            cur = await conn.execute(
                "select id, name, color from point_categories where pylower(name) like pylower(?) "
                "order by name collate nocase limit ?",
                (f"%{q.strip()}%", limit),
            )
        else:
            cur = await conn.execute(
                "select id, name, color from point_categories order by name collate nocase limit ?",
                (limit,),
            )
        return [{"id": r[0], "name": r[1], "color": r[2]} for r in await cur.fetchall()]


async def create_category(name: str, color: str, match_pattern: str = "", is_legacy: bool = False) -> dict[str, Any]:
    name = name.strip()
    color = (color or "").strip() or DEFAULT_COLOR
    match_pattern = (match_pattern or "").strip()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into point_categories (name, color, match_pattern, is_legacy) values (?, ?, ?, ?)",
            (name, color, match_pattern or None, int(is_legacy)),
        )
        await conn.commit()
        return {
            "id": cur.lastrowid, "name": name, "color": color, "match_pattern": match_pattern,
            "is_legacy": is_legacy, "point_count": 0,
        }


async def update_category(
    category_id: int, name: str, color: str, match_pattern: str = "", is_legacy: bool = False
) -> None:
    name = name.strip()
    color = (color or "").strip() or DEFAULT_COLOR
    match_pattern = (match_pattern or "").strip()
    async with get_connection() as conn:
        await conn.execute(
            "update point_categories set name = ?, color = ?, match_pattern = ?, is_legacy = ? where id = ?",
            (name, color, match_pattern or None, int(is_legacy), category_id),
        )
        await conn.commit()


async def delete_category(category_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select count(*) from points where category_id = ?", (category_id,)
        )
        (in_use,) = await cur.fetchone()
        if in_use:
            return False
        await conn.execute("delete from point_categories where id = ?", (category_id,))
        await conn.commit()
        return True


async def get_or_create(conn, name: Optional[str], color: Optional[str]) -> Optional[int]:
    """Runs on an already-open connection (called from points_repository's
    create/update, which manage their own transaction) — finds a category
    by case-insensitive name match, or creates one with the given colour."""
    if not name or not name.strip():
        return None
    name = name.strip()
    # pylower(...) (database.py) — SQLite's bare lower() only case-folds
    # ASCII, so "камера" wouldn't have matched an existing "Камера" and
    # would have silently created a near-duplicate category.
    cur = await conn.execute(
        "select id from point_categories where pylower(name) = pylower(?)", (name,)
    )
    row = await cur.fetchone()
    if row:
        return row[0]
    cur = await conn.execute(
        "insert into point_categories (name, color) values (?, ?)",
        (name, (color or "").strip() or DEFAULT_COLOR),
    )
    return cur.lastrowid


async def _match_patterns(conn) -> list[tuple[int, str]]:
    cur = await conn.execute(
        "select id, match_pattern from point_categories where match_pattern is not null and match_pattern != ''"
    )
    return [(r[0], r[1]) for r in await cur.fetchall()]


def _detect(patterns: list[tuple[int, str]], *texts: str) -> Optional[int]:
    """Longest pattern wins first (so a more specific pattern, e.g. "ARENA
    PRO", beats a shorter one, e.g. "ARENA", when a point's id/description
    contains both) — matched in Python (str.casefold), not SQL, since it
    needs to be Cyrillic-safe like the search fixes above."""
    haystacks = [t.casefold() for t in texts if t]
    if not haystacks:
        return None
    # Первый текст — номер комплекса: его сравниваем и «без разбора» (регистр, знаки, русские/латинские двойники, «АРЕНА-СМ-…» = «ARENA-…»), как везде в программе
    id_compact = compact_name(texts[0]) if texts and texts[0] else ""
    candidates = sorted(patterns, key=lambda p: len(p[1]), reverse=True)
    for category_id, pattern in candidates:
        needle = pattern.casefold()
        if any(needle in h for h in haystacks):
            return category_id
        needle_compact = compact_name(pattern)
        if id_compact and needle_compact and needle_compact in id_compact:
            return category_id
    return None


async def detect_category_id(conn, point_id: str, description: str = "") -> Optional[int]:
    """Runs on an already-open connection, same calling convention as
    get_or_create — used by points_repository.create_point/upsert_from_import
    when a point is saved without an explicit category, so it gets one
    automatically from its id/description matching a category's
    match_pattern (e.g. "ARENA", "POST", "Метеор")."""
    patterns = await _match_patterns(conn)
    return _detect(patterns, point_id, description)


async def auto_assign_uncategorized() -> int:
    """Bulk backfill for points that already exist without a category —
    goes through every currently-uncategorized point and assigns one
    wherever its id/description matches a category's match_pattern.
    Returns how many points got assigned."""
    async with get_connection() as conn:
        patterns = await _match_patterns(conn)
        if not patterns:
            return 0
        cur = await conn.execute("select id, description from points where category_id is null")
        rows = await cur.fetchall()
        assigned = 0
        for point_id, description in rows:
            category_id = _detect(patterns, point_id, description or "")
            if category_id is not None:
                await conn.execute("update points set category_id = ? where id = ?", (category_id, point_id))
                assigned += 1
        await conn.commit()
        return assigned
