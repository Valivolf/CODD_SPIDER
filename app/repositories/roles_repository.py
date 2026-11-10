"""Роли и их права (таблица roles). Пользователь ссылается на роль ключом (users.role): у встроенных ролей это «admin», «user», «warehouse», «viewer», «absences», у созданных
администратором — «r_xxxxxx». Права каждой роли читаются в память один раз и обновляются при любом изменении ролей (приложение работает одним процессом).

Роль «admin» всегда имеет все права («*») — это поддерживается при каждом запуске, изменить их нельзя (иначе можно потерять доступ к системе). Остальные встроенные роли — обычные наборы прав
со значениями по умолчанию (app/core/permissions.py::BUILTIN), которые можно изменить и «сбросить к исходным»."""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from typing import Any, Optional

from app.core import permissions as P
from app.db.database import get_connection

log = logging.getLogger(__name__)
_cache: dict[str, dict[str, Any]] = {}
_loaded = False


class RoleError(ValueError):
    """Понятная человеку причина отказа (имя занято, роль используется и т. п.)."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


async def ensure(conn) -> None:
    """Создаёт таблицу ролей и записывает встроенные роли (при первом запуске — с правами по умолчанию; у «admin» права «все» восстанавливаются всегда)."""
    await conn.execute(
        """create table if not exists roles (
            key text primary key,
            name text not null,
            description text not null default '',
            permissions text not null default '[]',
            is_system integer not null default 0,
            created_at text not null,
            updated_at text not null
        )"""
    )
    now = _now()
    for key, spec in P.BUILTIN.items():
        await conn.execute(
            "insert or ignore into roles (key, name, description, permissions, is_system, created_at, updated_at) values (?, ?, ?, ?, 1, ?, ?)",
            (key, spec["name"], spec["description"], json.dumps(sorted(P.expand(spec["perms"]))), now, now),
        )
    await conn.execute("update roles set permissions = ?, is_system = 1 where key = 'admin'", (json.dumps([P.ALL]),))
    await _grant_new_rights(conn)
    await conn.commit()


async def _grant_new_rights(conn) -> None:
    """Право из P.NEW_RIGHTS, которого встроенная роль ещё не видела, добавляется ей (если оно есть у роли по умолчанию) — один раз: что администратор убрал потом, не возвращается."""
    await conn.execute("create table if not exists app_flags (key text primary key, value text not null)")
    cur = await conn.execute("select value from app_flags where key = 'roles_new_rights_done'")
    row = await cur.fetchone()
    try:
        done = set(json.loads(row[0])) if row else set()
    except ValueError:
        done = set()
    todo = [r for r in P.NEW_RIGHTS if r not in done]
    if not todo:
        return
    for key in P.BUILTIN_KEYS:
        if key == "admin":
            continue
        defaults = P.default_perms(key)
        cur = await conn.execute("select permissions from roles where key = ?", (key,))
        r = await cur.fetchone()
        if not r:
            continue
        try:
            current = set(json.loads(r[0] or "[]"))
        except ValueError:
            current = set()
        add = {right for right in todo if right in defaults}
        if add - current:
            await conn.execute("update roles set permissions = ?, updated_at = ? where key = ?", (json.dumps(sorted(P.expand(current | add))), _now(), key))
            log.info("Роль «%s»: добавлены новые права %s", key, ", ".join(sorted(add - current)))
    await conn.execute("insert into app_flags (key, value) values ('roles_new_rights_done', ?) on conflict(key) do update set value = excluded.value", (json.dumps(sorted(done | set(todo))),))


async def load() -> None:
    global _loaded
    async with get_connection() as conn:
        cur = await conn.execute("select key, name, description, permissions, is_system, created_at, updated_at from roles order by is_system desc, name collate nocase")
        rows = await cur.fetchall()
    fresh: dict[str, dict[str, Any]] = {}
    for key, name, description, perms, is_system, created_at, updated_at in rows:
        try:
            raw = json.loads(perms or "[]")
        except ValueError:
            raw = []
        fresh[key] = {"key": key, "name": name, "description": description, "permissions": P.expand(raw), "is_system": bool(is_system),
                      "created_at": created_at, "updated_at": updated_at}
    _cache.clear()
    _cache.update(fresh)
    _loaded = True


async def ensure_loaded() -> None:
    if not _loaded:
        await load()


def perms_for_role(key: str) -> frozenset:
    """Права роли из памяти. Нет такой роли (удалена) — пустой набор: доступа нет."""
    role = _cache.get(key)
    return role["permissions"] if role else frozenset()


def role_name(key: str) -> str:
    role = _cache.get(key)
    return role["name"] if role else key


def role_exists(key: str) -> bool:
    return key in _cache


def public(role: dict[str, Any], users: int = 0) -> dict[str, Any]:
    return {"key": role["key"], "name": role["name"], "description": role["description"], "permissions": sorted(role["permissions"]), "is_system": role["is_system"],
            "locked": role["key"] == "admin", "users": users, "default_permissions": sorted(P.default_perms(role["key"])) if role["key"] in P.BUILTIN else None}


async def list_roles() -> list[dict[str, Any]]:
    await ensure_loaded()
    async with get_connection() as conn:
        cur = await conn.execute("select role, count(*) from users group by role")
        counts = dict(await cur.fetchall())
    return [public(r, counts.get(r["key"], 0)) for r in _cache.values()]


async def get(key: str) -> Optional[dict[str, Any]]:
    await ensure_loaded()
    role = _cache.get(key)
    return public(role) if role else None


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 60:
        raise RoleError("Название роли — от 2 до 60 знаков")
    return name


def _name_taken(name: str, except_key: str = "") -> bool:
    low = name.casefold()
    return any(r["name"].casefold() == low and k != except_key for k, r in _cache.items())


def _clean_perms(permissions: list[str]) -> frozenset:
    unknown = [p for p in (permissions or []) if p != P.ALL and p not in P.LABELS]
    if unknown:
        raise RoleError("Неизвестные права: " + ", ".join(unknown[:5]))
    if P.ALL in (permissions or []):
        raise RoleError("Право «всё» есть только у администратора")
    return P.expand(permissions)


async def create(name: str, description: str, permissions: list[str]) -> dict[str, Any]:
    await ensure_loaded()
    name = _clean_name(name)
    if _name_taken(name):
        raise RoleError(f"Роль «{name}» уже есть")
    perms = _clean_perms(permissions)
    key = "r_" + secrets.token_hex(3)
    while key in _cache:
        key = "r_" + secrets.token_hex(3)
    now = _now()
    async with get_connection() as conn:
        await conn.execute("insert into roles (key, name, description, permissions, is_system, created_at, updated_at) values (?, ?, ?, ?, 0, ?, ?)",
                           (key, name, (description or "").strip()[:300], json.dumps(sorted(perms)), now, now))
        await conn.commit()
    await load()
    return (await get(key))  # type: ignore[return-value]


async def update(key: str, name: str, description: str, permissions: Optional[list[str]]) -> dict[str, Any]:
    await ensure_loaded()
    role = _cache.get(key)
    if role is None:
        raise RoleError("Такой роли нет")
    name = _clean_name(name)
    if _name_taken(name, key):
        raise RoleError(f"Роль «{name}» уже есть")
    perms = role["permissions"]
    if key == "admin":
        perms = frozenset({P.ALL})                          # права администратора менять нельзя
    elif permissions is not None:
        perms = _clean_perms(permissions)
    async with get_connection() as conn:
        await conn.execute("update roles set name = ?, description = ?, permissions = ?, updated_at = ? where key = ?",
                           (name, (description or "").strip()[:300], json.dumps(sorted(perms)), _now(), key))
        await conn.commit()
    await load()
    return (await get(key))  # type: ignore[return-value]


async def reset(key: str) -> dict[str, Any]:
    """Права встроенной роли — к исходным."""
    await ensure_loaded()
    if key not in P.BUILTIN:
        raise RoleError("Сбросить можно только встроенную роль")
    spec = P.BUILTIN[key]
    perms = frozenset({P.ALL}) if key == "admin" else P.expand(spec["perms"])
    async with get_connection() as conn:
        await conn.execute("update roles set name = ?, description = ?, permissions = ?, updated_at = ? where key = ?",
                           (spec["name"], spec["description"], json.dumps(sorted(perms)), _now(), key))
        await conn.commit()
    await load()
    return (await get(key))  # type: ignore[return-value]


async def delete(key: str) -> None:
    await ensure_loaded()
    role = _cache.get(key)
    if role is None:
        raise RoleError("Такой роли нет")
    if role["is_system"]:
        raise RoleError("Встроенную роль удалить нельзя (можно изменить её права)")
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from users where role = ?", (key,))
        (n,) = await cur.fetchone()
        if n:
            raise RoleError(f"Роль назначена пользователям ({n}) — сначала назначьте им другую роль")
        await conn.execute("delete from roles where key = ?", (key,))
        await conn.commit()
    await load()
