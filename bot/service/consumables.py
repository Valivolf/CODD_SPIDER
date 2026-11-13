"""Выбор расходников для отчёта по текущему заданию."""
from __future__ import annotations

import logging

from vkbottle.bot import Message

from bot import api_client
from bot.keyboards import consumables_menu as kb
from bot.service import mechanic as base

logger = logging.getLogger(__name__)


def _catalog(raw: list[dict] | dict) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("items") or []
    result = []
    for row in raw:
        try:
            item_id = int(row.get("item_id", row.get("id")))
            available = int(row.get("available", row.get("balance", row.get("quantity", 0))))
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or available <= 0:
            continue
        result.append({
            "item_id": item_id,
            "name": str(row.get("name") or row.get("item_name") or f"Материал №{item_id}"),
            "unit": str(row.get("unit") or ""),
            "available": available,
        })
    return result


async def _load(peer_id: int, user: dict, session: dict) -> list[dict] | None:
    try:
        items = _catalog(await api_client.get_my_consumables(user["vk_id"]))
    except api_client.ApiError as exc:
        await base.send(peer_id, f"❌ Не удалось получить личный остаток: {exc.detail}", base._menu(session))
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось получить расходники монтёра")
        await base.send(peer_id, "⚠️ Не удалось получить личный остаток. Попробуйте ещё раз.", base._menu(session))
        return None
    session["_consumables_catalog"] = items
    return items


def _selected(session: dict) -> list[dict]:
    values = session.get("consumables")
    if not isinstance(values, list):
        return []
    result = []
    for row in values:
        if not isinstance(row, dict):
            continue
        try:
            result.append({"item_id": int(row.get("item_id")), "quantity": int(row.get("quantity"))})
        except (TypeError, ValueError):
            continue
    return result


def _selected_text(session: dict, catalog: list[dict]) -> str:
    chosen = _selected(session)
    if not chosen:
        return "Пока ничего не выбрано."
    by_id = {row["item_id"]: row for row in catalog}
    lines = ["Выбрано:"]
    for row in chosen:
        item = by_id.get(row.get("item_id"), {})
        label = item.get("name") or f"Материал №{row.get('item_id')}"
        unit = item.get("unit")
        lines.append(f"• {label} — {row.get('quantity')} {unit}".rstrip())
    return "\n".join(lines)


def _page(total: int, requested: int) -> tuple[int, int]:
    pages = max(1, (total + kb.PAGE_SIZE - 1) // kb.PAGE_SIZE)
    return max(0, min(requested, pages - 1)), pages


async def show(peer_id: int, user: dict, session: dict, page: int = 0) -> None:
    if session.get("step") not in ("menu", "consumables_edit"):
        await base.send(peer_id, "Сначала завершите текущий шаг.")
        return
    session["step"] = "menu"
    catalog = await _load(peer_id, user, session)
    if catalog is None:
        return
    page, pages = _page(len(catalog), page)
    if catalog:
        intro = "Выберите расходник из личного остатка. Списание будет только вместе с отчётом."
    else:
        intro = "Личный остаток расходников пуст. Ранее выбранные строки можно изменить или удалить."
    await base.send(
        peer_id,
        f"🧴 Расходники\n{_selected_text(session, catalog)}\n\n{intro}",
        kb.get_catalog_keyboard(catalog, page, pages),
    )


async def show_editor(peer_id: int, user: dict, session: dict, page: int = 0) -> None:
    catalog = await _load(peer_id, user, session)
    if catalog is None:
        return
    session["step"] = "menu"
    chosen = _selected(session)
    if not chosen:
        await base.send(peer_id, "Пока нечего изменять или удалять.", kb.get_catalog_keyboard(catalog, 0, _page(len(catalog), 0)[1]))
        return
    names = {row["item_id"]: row for row in catalog}
    lines = ["Что изменить или удалить?"]
    for row in chosen:
        item = names.get(row.get("item_id"), {})
        label = item.get("name") or f"Материал №{row.get('item_id')}"
        unit = f" {item['unit']}" if item.get("unit") else ""
        lines.append(f"• {label} — {row.get('quantity')}{unit}")
    pages = max(1, (len(chosen) + kb.EDIT_PAGE_SIZE - 1) // kb.EDIT_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    session["step"] = "consumables_edit"
    await base.send(peer_id, "\n".join(lines), kb.get_edit_keyboard(chosen, page, pages))


async def pick(peer_id: int, user: dict, session: dict, item_id: int, editing: bool = False) -> None:
    catalog = await _load(peer_id, user, session)
    if catalog is None:
        return
    item = next((row for row in catalog if row["item_id"] == item_id), None)
    if item is None:
        await base.send(peer_id, "Этого материала уже нет в личном остатке.", kb.get_catalog_keyboard(catalog, 0, _page(len(catalog), 0)[1]))
        return
    chosen = _selected(session)
    current = next((row for row in chosen if row.get("item_id") == item_id), None)
    if current and not editing:
        editing = True
    if editing and current is None:
        await base.send(peer_id, "Эта строка уже удалена.", kb.get_catalog_keyboard(catalog, 0, _page(len(catalog), 0)[1]))
        return
    other_quantity = sum(
        int(row.get("quantity") or 0) for row in chosen if row.get("item_id") == item_id and row is not current
    )
    remaining = item["available"] - other_quantity
    if remaining <= 0:
        await base.send(peer_id, f"Нет свободного остатка материала «{item['name']}».", kb.get_catalog_keyboard(catalog, 0, _page(len(catalog), 0)[1]))
        return
    session["step"] = "awaiting_consumable_quantity"
    session["consumables_item_id"] = item_id
    session["consumables_editing"] = bool(editing)
    session["consumables_available"] = remaining
    unit = f" {item['unit']}" if item.get("unit") else ""
    await base.send(
        peer_id,
        f"Введите количество «{item['name']}» (целое число от 1 до {remaining}{unit}).",
        kb.get_quantity_keyboard(),
    )


async def receive_quantity(message: Message, user: dict, session: dict) -> None:
    peer_id = message.peer_id
    raw = (message.text or "").strip()
    if not raw.isascii() or not raw.isdecimal():
        await base.send(peer_id, "Введите целое положительное число.", kb.get_quantity_keyboard())
        return
    quantity = int(raw)
    available = int(session.get("consumables_available") or 0)
    if quantity <= 0 or quantity > available:
        await base.send(peer_id, f"Количество должно быть от 1 до {available}.", kb.get_quantity_keyboard())
        return
    item_id = int(session.get("consumables_item_id") or 0)
    if not item_id:
        session["step"] = "menu"
        await show(peer_id, user, session)
        return
    chosen = _selected(session)
    row = next((item for item in chosen if item.get("item_id") == item_id), None)
    if row:
        row["quantity"] = quantity
    else:
        chosen.append({"item_id": item_id, "quantity": quantity})
    session["consumables"] = chosen
    for key in ("consumables_item_id", "consumables_editing", "consumables_available"):
        session.pop(key, None)
    session["step"] = "menu"
    await base.persist_progress(session)
    await show(peer_id, user, session)


async def remove(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    chosen = [row for row in _selected(session) if row.get("item_id") != item_id]
    session["consumables"] = chosen
    await base.persist_progress(session)
    await show_editor(peer_id, user, session)


async def cancel_quantity(peer_id: int, user: dict, session: dict) -> None:
    for key in ("consumables_item_id", "consumables_editing", "consumables_available"):
        session.pop(key, None)
    session["step"] = "menu"
    await show(peer_id, user, session)


async def back_to_task(peer_id: int, session: dict) -> None:
    for key in ("consumables_item_id", "consumables_editing", "consumables_available"):
        session.pop(key, None)
    session["step"] = "menu"
    await base.send(peer_id, "Меню задания:", base._menu(session))
