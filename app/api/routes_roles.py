"""Роли пользователей: список, создание, правка набора прав, копия, сброс встроенной роли, удаление. Доступно тем, у кого есть право «Пользователи и роли» (admin.users).

Права раздаются «не выше своих»: нельзя создать или изменить роль, дав ей право, которого нет у самого, и нельзя править роль, превосходящую ваши права (так обладатель права
«Пользователи и роли» не может выдать себе или другому больше, чем имеет сам; полный набор прав — у роли «администратор»)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core import permissions as P
from app.core.clientip import client_ip
from app.core.deps import require_perm
from app.models.user import UserOut
from app.repositories import roles_repository as repo
from app.services import security_log

router = APIRouter(prefix="/api/admin/roles", tags=["admin"], dependencies=[Depends(require_perm("admin.users"))])


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=300)
    permissions: Optional[list[str]] = None
    copy_from: Optional[str] = None


def _catalog() -> list[dict]:
    return [{"group": group, "items": [{"key": k, "label": label, "hint": hint, "implies": list(P.IMPLIES.get(k, ()))} for k, label, hint in items]} for group, items in P.CATALOG]


def _fail(exc: repo.RoleError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _must_cover(actor: UserOut, perms: set | frozenset | list) -> None:
    if not P.is_subset(perms, actor.permissions):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нельзя раздавать права, которых нет у вас самих")


@router.get("")
async def list_roles():
    return {"roles": await repo.list_roles(), "catalog": _catalog()}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_role(payload: RoleIn, request: Request, admin: UserOut = Depends(require_perm("admin.users"))):
    perms = payload.permissions
    if perms is None and payload.copy_from:
        src = await repo.get(payload.copy_from)
        if not src:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Роль для копирования не найдена")
        perms = [p for p in src["permissions"] if p != P.ALL] if not src["locked"] else list(P.ALL_KEYS)
    perms = perms or []
    _must_cover(admin, P.expand(perms))
    try:
        role = await repo.create(payload.name, payload.description, perms)
    except repo.RoleError as exc:
        raise _fail(exc)
    await security_log.record("role_create", username=admin.username, role=admin.role, ip=client_ip(request), details={"role": role["name"], "permissions": len(role["permissions"])})
    return role


@router.put("/{key}")
async def update_role(key: str, payload: RoleIn, request: Request, admin: UserOut = Depends(require_perm("admin.users"))):
    old = await repo.get(key)
    if not old:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой роли нет")
    _must_cover(admin, old["permissions"])                    # править можно только роль, не превосходящую ваши права
    if key != "admin" and payload.permissions is not None:
        _must_cover(admin, P.expand(payload.permissions))
    try:
        role = await repo.update(key, payload.name, payload.description, payload.permissions)
    except repo.RoleError as exc:
        raise _fail(exc)
    added = sorted(set(role["permissions"]) - set(old["permissions"]))
    removed = sorted(set(old["permissions"]) - set(role["permissions"]))
    await security_log.record("role_update", username=admin.username, role=admin.role, ip=client_ip(request),
                              details={"role": role["name"], "added": added[:12], "removed": removed[:12]})
    return role


@router.post("/{key}/reset")
async def reset_role(key: str, request: Request, admin: UserOut = Depends(require_perm("admin.users"))):
    old = await repo.get(key)
    if not old:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой роли нет")
    _must_cover(admin, old["permissions"])
    try:
        role = await repo.reset(key)
    except repo.RoleError as exc:
        raise _fail(exc)
    _must_cover(admin, role["permissions"])
    await security_log.record("role_update", username=admin.username, role=admin.role, ip=client_ip(request), details={"role": role["name"], "reset": True})
    return role


@router.delete("/{key}")
async def delete_role(key: str, request: Request, admin: UserOut = Depends(require_perm("admin.users"))):
    old = await repo.get(key)
    if not old:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой роли нет")
    _must_cover(admin, old["permissions"])
    try:
        await repo.delete(key)
    except repo.RoleError as exc:
        raise _fail(exc)
    await security_log.record("role_delete", username=admin.username, role=admin.role, ip=client_ip(request), details={"role": old["name"]})
    return {"ok": True}
