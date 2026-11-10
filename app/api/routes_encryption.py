"""Ключ шифрования полей (ФИО и телефоны персонала, логины и пароли комплексов, пароль сетевой папки): состояние, показ ключа для резервной
копии, смена ключа (после переноса копии базы с другого сервера). Только админ; показ ключа и смена — с повторным вводом пароля админа.
Что и как шифруется — app/core/field_crypto.py и app/db/field_encryption.py."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.core import field_crypto
from app.core.clientip import client_ip
from app.core.deps import require_keys
from app.core.security import verify_password
from app.db import database, field_encryption
from app.models.user import UserOut
from app.repositories import user_repository
from app.services import security_log

router = APIRouter(prefix="/api/encryption", tags=["encryption"])
log = logging.getLogger(__name__)


async def _check_password(user: UserOut, password: str) -> None:
    row = await user_repository.get_user_by_id(user.id)
    if not row or not verify_password(password or "", row["password_hash"]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Пароль администратора указан неверно")


async def _state() -> dict:
    async with database.get_connection() as conn:
        info = await field_encryption.status(conn)
    info["key_source"] = field_crypto.key_source()
    info["fingerprint"] = field_crypto.fingerprint()
    info["key_in_env"] = field_crypto.env_key_set()
    return info


@router.get("", dependencies=[Depends(require_keys)])
async def encryption_state():
    return await _state()


class PasswordIn(BaseModel):
    password: str = ""


@router.post("/key/show")
async def show_key(payload: PasswordIn, request: Request, user: UserOut = Depends(require_keys)):
    """Ключ в base64 — для резервной копии. Без пароля админа не отдаётся; факт показа пишется в журнал сервера (без самого ключа)."""
    await _check_password(user, payload.password)
    log.warning("Администратор %s запросил показ ключа шифрования", user.username)
    await security_log.record("key_view", username=user.username, role=user.role, ip=client_ip(request))
    return {"key": field_crypto.export_key(), "fingerprint": field_crypto.fingerprint()}


class KeyIn(BaseModel):
    key: str
    password: str = ""


@router.post("/key/apply")
async def apply_key(payload: KeyIn, request: Request, user: UserOut = Depends(require_keys)):
    """Поставить ключ другого сервера (когда сюда восстановлена его копия базы). Ключ принимается, только если расшифровывает хотя бы часть
    уже зашифрованных значений; прежний ключ сохраняется рядом (.field_key.old-…)."""
    await _check_password(user, payload.password)
    if field_crypto.env_key_set():
        raise HTTPException(status.HTTP_409_CONFLICT, "Ключ задан переменной FIELD_ENCRYPTION_KEY (файл .env) — меняйте его там и перезапустите приложение")
    text = (payload.key or "").strip()
    try:
        field_crypto._decode_key(text)  # noqa: SLF001 — проверка формата
    except (ValueError, TypeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Это не похоже на ключ: нужна строка из 43 знаков (как при показе ключа)")
    samples: list[tuple[str, str]] = []
    async with database.get_connection() as conn:
        for table, col, field in field_encryption.COLUMNS:
            cur = await conn.execute(f"select {col}_enc from {table} where {col}_enc like 'enc:v1:%' limit 20")
            samples += [(field, v) for (v,) in await cur.fetchall()]
    if samples and not any(field_crypto.can_decrypt_with(text, f, v) for f, v in samples):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Этот ключ не подходит к данным в базе — ни одно значение не расшифровалось. Прежний ключ оставлен.")
    field_crypto.apply_key(text)
    log.warning("Администратор %s сменил ключ шифрования полей", user.username)
    await security_log.record("key_change", username=user.username, role=user.role, ip=client_ip(request))
    return await _state()
