"""Данные для входа на комплекс в карточках запросов (проверка оператором, нет доступа, демонтаж): логин/пароль и «старый»
комплекс (вход по RDP) нужны кнопке «Веб-интерфейс», чтобы открыть вход и скопировать пароль. Как и в /api/points, пароль и
логин отдаются только тем, у кого есть право points.secrets, — остальным они пустые."""
from __future__ import annotations

from typing import Any

from app.models.user import UserOut
from app.core import permissions as P

POINT_EXTRA_KEYS = ["login", "password", "category_is_legacy"]


def finish_rows(keys: list[str], rows: list[tuple]) -> list[dict[str, Any]]:
    """Строки выборки со столбцами POINT_EXTRA_* в конце → словари (category_is_legacy — bool, пустые значения — '')."""
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["login"], d["password"] = d.get("login") or "", d.get("password") or ""
        d["category_is_legacy"] = bool(d.get("category_is_legacy"))
        out.append(d)
    return out


def strip_credentials(items: list[dict[str, Any]], user: UserOut) -> list[dict[str, Any]]:
    if P.secrets_ok(user.permissions):
        return items
    return [{**i, "login": "", "password": ""} for i in items]
