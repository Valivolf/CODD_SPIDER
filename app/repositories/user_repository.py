"""CRUD for the users table."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from app.db.database import get_connection

_COLUMNS = "id, username, password_hash, role, must_change_password, token_version, created_at"


def _row_to_dict(row) -> dict[str, Any]:
    keys = ["id", "username", "password_hash", "role", "must_change_password", "token_version", "created_at"]
    d = dict(zip(keys, row))
    d["must_change_password"] = bool(d["must_change_password"])
    return d


async def count_users() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from users")
        (n,) = await cur.fetchone()
        return n


async def create_user(
    *, username: str, password_hash: str, role: str, must_change_password: bool = False
) -> dict[str, Any]:
    async with get_connection() as conn:
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cur = await conn.execute(
            "insert into users (username, password_hash, role, must_change_password, created_at) "
            "values (?, ?, ?, ?, ?)",
            (username, password_hash, role, int(must_change_password), created_at),
        )
        await conn.commit()
        user_id = cur.lastrowid
        return await get_user_by_id(user_id)  # type: ignore[return-value]


async def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from users where username = ?", (username,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from users where id = ?", (user_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_users() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from users order by id")
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def update_password(user_id: int, password_hash: str, *, must_change_password: bool = False) -> None:
    """Bumps token_version so any session issued before this change stops
    being accepted — see core/deps.get_current_user."""
    async with get_connection() as conn:
        await conn.execute(
            "update users set password_hash = ?, must_change_password = ?, token_version = token_version + 1 "
            "where id = ?",
            (password_hash, int(must_change_password), user_id),
        )
        await conn.commit()


async def update_role(user_id: int, role: str) -> None:
    async with get_connection() as conn:
        await conn.execute("update users set role = ? where id = ?", (role, user_id))
        await conn.commit()


async def delete_user(user_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from users where id = ?", (user_id,))
        await conn.commit()


async def count_admins() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from users where role = 'admin'")
        (n,) = await cur.fetchone()
        return n
