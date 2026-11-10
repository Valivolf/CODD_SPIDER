"""Клавиатуры сценариев с оборудованием (монтёр и склад).

Inline-клавиатура VK — не больше 6 рядов и 10 кнопок суммарно, иначе messages.send падает с ошибкой
911. Поэтому списки — постранично: сколько элементов помещается, считается от рядов/кнопок,
которые заняты «управлением» (внизу) и рядом листания ◀ ▶."""
from __future__ import annotations

from typing import Iterable

from vkbottle import Callback, Keyboard

MAX_ROWS = 6
MAX_BUTTONS = 10

Entry = tuple[str, dict]


def cut(text: str, limit: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def page_geometry(total: int, controls: list[list[Entry]]) -> tuple[int, int]:
    """(элементов на страницу, число страниц) при заданных рядах управления."""
    rows_budget = MAX_ROWS - len(controls)
    buttons_budget = MAX_BUTTONS - sum(len(r) for r in controls)
    if total <= max(min(rows_budget, buttons_budget), 1):
        return max(total, 1), 1
    size = max(min(rows_budget - 1, buttons_budget - 2), 1)
    return size, (total - 1) // size + 1


def paged_keyboard(entries: list[Entry], page: int, controls: list[list[Entry]] | None = None) -> tuple[Keyboard, int, int]:
    """Клавиатура-список: по одному элементу в ряд, ряд листания, затем ряды управления.
    Возвращает (клавиатура, номер страницы после ограничения, число страниц)."""
    controls = controls or []
    size, pages = page_geometry(len(entries), controls)
    page = max(0, min(page, pages - 1))
    keyboard = Keyboard(inline=True)
    first = True

    def new_row() -> None:
        nonlocal first
        if not first:
            keyboard.row()
        first = False

    for label, payload in entries[page * size:(page + 1) * size]:
        new_row()
        keyboard.add(Callback(cut(label), payload))
    if pages > 1:
        new_row()
        if page > 0:
            keyboard.add(Callback("◀", {"action": "pg", "n": page - 1}))
        if page < pages - 1:
            keyboard.add(Callback(f"▶ {page + 2}/{pages}" if page == 0 else "▶", {"action": "pg", "n": page + 1}))
    for row in controls:
        new_row()
        for label, payload in row:
            keyboard.add(Callback(cut(label), payload))
    return keyboard, page, pages


def simple_keyboard(rows: Iterable[list[Entry]]) -> Keyboard:
    keyboard = Keyboard(inline=True)
    first = True
    for row in rows:
        if not first:
            keyboard.row()
        first = False
        for label, payload in row:
            keyboard.add(Callback(cut(label), payload))
    return keyboard


# ---- меню ответственного за склад

def get_warehouse_menu_keyboard() -> Keyboard:
    return simple_keyboard([
        [("📥 Принять от монтёра", {"action": "wh_accept"})],
        [("📤 Выдать монтёру", {"action": "wh_issue"})],
        [("🏷 Состояние оборудования", {"action": "wh_status"})],
        [("📊 Остатки на складе", {"action": "wh_stock"})],
        [("👤 Профиль", {"action": "profile"})],
    ])


def get_back_to_warehouse_keyboard() -> Keyboard:
    return simple_keyboard([[("🏠 Меню склада", {"action": "main_menu"})]])
