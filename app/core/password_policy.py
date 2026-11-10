"""Требования к паролю пользователя (задаются при создании учётной записи, смене и сбросе пароля).

По умолчанию: не короче 8 знаков (PASSWORD_MIN_LENGTH в .env, не меньше 6), есть и буквы, и цифры, пароль не содержит логин и не из списка самых частых. Для администраторов рекомендуется 12 и больше
(регламент учётных записей). Пароль по умолчанию «admin123» при первом входе обязательно меняется на соответствующий этим требованиям."""
from __future__ import annotations

import os
import re

DEFAULT_MIN_LENGTH = 8
ABSOLUTE_MIN = 6
_COMMON = {
    "admin123", "admin1234", "password1", "password123", "qwerty123", "qwerty1234", "12345678", "123456789", "1234567890", "11111111", "00000000", "qwertyuiop", "1q2w3e4r",
    "1qaz2wsx", "q1w2e3r4", "pass1234", "abc12345", "welcome1", "letmein1", "iloveyou1", "пароль123", "йцукен123", "qwe12345", "zxcvbnm123", "admin2026", "admin12345", "user1234",
}


def min_length() -> int:
    try:
        return max(ABSOLUTE_MIN, int(os.environ.get("PASSWORD_MIN_LENGTH", DEFAULT_MIN_LENGTH)))
    except ValueError:
        return DEFAULT_MIN_LENGTH


def problems(password: str, username: str = "") -> list[str]:
    """Что не так с паролем (пустой список — пароль подходит)."""
    out: list[str] = []
    need = min_length()
    if len(password) < need:
        out.append(f"пароль должен быть не короче {need} знаков")
    if not re.search(r"[A-Za-zА-Яа-яЁё]", password) or not re.search(r"\d", password):
        out.append("в пароле должны быть и буквы, и цифры")
    login = (username or "").strip().lower()
    if len(login) >= 3 and login in password.lower():
        out.append("пароль не должен содержать логин")
    if password.lower() in _COMMON:
        out.append("слишком простой пароль (из списка самых частых)")
    return out


def check(password: str, username: str = "") -> str:
    """Возвращает пароль, если он подходит, иначе ValueError с понятным текстом (для проверки в моделях запросов)."""
    found = problems(password, username)
    if found:
        raise ValueError("Ненадёжный пароль: " + "; ".join(found))
    return password
