"""Password hashing and JWT helpers."""
from __future__ import annotations

import datetime as dt

import bcrypt
import jwt

from app.config import get_settings

JWT_ALGORITHM = "HS256"

# A bcrypt hash of an arbitrary password, computed once at import time.
# authenticate() checks against this when the username doesn't exist, so a
# login attempt takes the same ~100ms either way — without it, "unknown
# user" would return near-instantly while "wrong password" takes the full
# bcrypt round trip, letting an attacker enumerate valid usernames by
# timing alone.
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"not-a-real-account", bcrypt.gensalt()).decode("utf-8")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(*, user_id: int, username: str, role: str, token_version: int) -> str:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "tv": token_version,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
