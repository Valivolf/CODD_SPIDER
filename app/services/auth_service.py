from __future__ import annotations

from app.core.security import DUMMY_PASSWORD_HASH, create_access_token, hash_password, verify_password
from app.repositories import user_repository


class AuthError(Exception):
    pass


async def authenticate(username: str, password: str) -> tuple[dict, str]:
    user = await user_repository.get_user_by_username(username)
    # Always run the bcrypt check, even for an unknown username, so the
    # response time doesn't reveal whether the account exists.
    password_ok = verify_password(password, user["password_hash"] if user else DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        raise AuthError("Неверный логин или пароль")
    token = create_access_token(
        user_id=user["id"], username=user["username"], role=user["role"], token_version=user["token_version"]
    )
    return user, token


async def change_password(user_id: int, current_password: str, new_password: str) -> None:
    user = await user_repository.get_user_by_id(user_id)
    if not user or not verify_password(current_password, user["password_hash"]):
        raise AuthError("Текущий пароль указан неверно")
    await user_repository.update_password(user_id, hash_password(new_password), must_change_password=False)


async def create_user(username: str, password: str, role: str) -> dict:
    if await user_repository.get_user_by_username(username):
        raise AuthError("Такой логин уже занят")
    return await user_repository.create_user(
        username=username, password_hash=hash_password(password), role=role
    )


async def admin_reset_password(user_id: int, new_password: str) -> None:
    await user_repository.update_password(user_id, hash_password(new_password), must_change_password=True)
