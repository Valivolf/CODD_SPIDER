"""Управление пользователями: список, создание, смена роли, сброс пароля, удаление. Доступно тем, у кого есть право «Пользователи и роли» (admin.users).

Права раздаются «не выше своих»: нельзя назначить роль, права которой шире ваших, и нельзя менять, сбрасывать пароль или удалять пользователя, чья роль шире ваших прав
(иначе обладатель права «Пользователи и роли» мог бы стать администратором, сбросив пароль администратору)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core import permissions as P
from app.core import rate_limit
from app.core.clientip import client_ip
from app.core.deps import build_user, require_admin
from app.models.user import AdminPasswordReset, RoleChange, UserCreate, UserOut
from app.repositories import roles_repository, user_repository
from app.services import auth_service, security_log

router = APIRouter(prefix="/api/admin/users", tags=["admin"], dependencies=[Depends(require_admin)])


def _role_must_exist(role: str) -> None:
    if not roles_repository.role_exists(role):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Такой роли нет")


def _may_grant(actor: UserOut, role: str) -> None:
    if not P.is_subset(roles_repository.perms_for_role(role), actor.permissions):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нельзя назначить роль, права которой шире ваших")


def _may_touch(actor: UserOut, target: dict) -> None:
    if not P.is_subset(roles_repository.perms_for_role(target["role"]), actor.permissions):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нельзя менять пользователя, чьи права шире ваших")


@router.get("", response_model=list[UserOut])
async def list_users():
    await roles_repository.ensure_loaded()
    return [await build_user(u) for u in await user_repository.list_users()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, request: Request, admin: UserOut = Depends(require_admin)):
    await roles_repository.ensure_loaded()
    _role_must_exist(payload.role)
    _may_grant(admin, payload.role)
    try:
        u = await auth_service.create_user(payload.username, payload.password, payload.role)
    except auth_service.AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    await security_log.record("user_create", username=admin.username, role=admin.role, ip=client_ip(request), details={"user": u["username"], "role": u["role"]})
    return await build_user(u)


@router.patch("/{user_id}/role", response_model=UserOut)
async def change_role(user_id: int, payload: RoleChange, request: Request, admin: UserOut = Depends(require_admin)):
    await roles_repository.ensure_loaded()
    target = await user_repository.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    _role_must_exist(payload.role)
    _may_touch(admin, target)
    _may_grant(admin, payload.role)
    if target["role"] == "admin" and payload.role != "admin" and await user_repository.count_admins() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя разжаловать последнего администратора")
    await user_repository.update_role(user_id, payload.role)
    await security_log.record("user_role", username=admin.username, role=admin.role, ip=client_ip(request), details={"user": target["username"], "was": target["role"], "now": payload.role})
    return await build_user(await user_repository.get_user_by_id(user_id))


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: int, payload: AdminPasswordReset, request: Request, admin: UserOut = Depends(require_admin)):
    await roles_repository.ensure_loaded()
    target = await user_repository.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    _may_touch(admin, target)
    login = target["username"].strip().lower()
    if len(login) >= 3 and login in payload.new_password.lower():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ненадёжный пароль: пароль не должен содержать логин")
    await auth_service.admin_reset_password(user_id, payload.new_password)
    await security_log.record("password_reset", username=admin.username, role=admin.role, ip=client_ip(request), details={"user": target["username"]})
    # Also releases a login lockout — an admin resetting the password is
    # the account's real recovery path, since the lockout itself has no
    # separate unlock and a persistent attacker could otherwise keep it
    # locked indefinitely.
    rate_limit.clear(target["username"].strip().lower())
    return {"ok": True}


@router.delete("/{user_id}")
async def delete_user(user_id: int, request: Request, admin: UserOut = Depends(require_admin)):
    await roles_repository.ensure_loaded()
    target = await user_repository.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    _may_touch(admin, target)
    if target["id"] == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить собственную учётную запись")
    if target["role"] == "admin" and await user_repository.count_admins() <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нельзя удалить последнего администратора")
    await user_repository.delete_user(user_id)
    await security_log.record("user_delete", username=admin.username, role=admin.role, ip=client_ip(request), details={"user": target["username"], "role": target["role"]})
    return {"ok": True}
