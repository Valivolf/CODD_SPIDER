"""Общие правила доступа к сохранённым шаблонам отчётов."""
from __future__ import annotations

import json
from typing import Any


def can_view(item: dict[str, Any], user) -> bool:
    if item.get("created_by") == user.username:
        return True
    mode = item.get("visibility") or "public"
    if mode == "public":
        return True
    if mode == "users":
        try:
            selected = item.get("shared_users") or []
            if isinstance(selected, str):
                selected = json.loads(selected)
            return user.id in {int(i) for i in selected}
        except (TypeError, ValueError):
            return False
    return False


def can_edit(item: dict[str, Any], user) -> bool:
    if "*" in user.permissions or item.get("created_by") == user.username:
        return True
    # Публичные шаблоны по-прежнему может поддерживать любой обладатель
    # права конструктора; личные и адресные остаются под контролем владельца.
    return (item.get("visibility") or "public") == "public" and "reports.builder" in user.permissions


def can_manage_access(item: dict[str, Any], user) -> bool:
    return "*" in user.permissions or item.get("created_by") == user.username


def clean_access(visibility: str, user_ids: list[int] | None) -> tuple[str, list[int]]:
    if visibility not in {"private", "public", "users"}:
        raise ValueError("Выберите: только мне, всем или выбранным учётным записям")
    ids = list(dict.fromkeys(int(i) for i in (user_ids or []) if int(i) > 0))
    if visibility == "users" and not ids:
        raise ValueError("Выберите хотя бы одну учётную запись")
    return visibility, ids if visibility == "users" else []
