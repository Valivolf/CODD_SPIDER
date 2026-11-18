"""Клавиатуры для выбора расходников в задании."""
from vkbottle import Callback, Keyboard


PAGE_SIZE = 6
EDIT_PAGE_SIZE = 4


def _short(text: str) -> str:
    return text if len(text) <= 34 else text[:31] + "…"


def get_catalog_keyboard(items: list[dict], page: int, pages: int) -> Keyboard:
    keyboard = Keyboard(inline=True)
    start = page * PAGE_SIZE
    for offset, item in enumerate(items[start:start + PAGE_SIZE]):
        label = f"＋ {_short(str(item.get('name') or 'Материал'))} · {item.get('available', 0)}"
        keyboard.add(Callback(label[:40], {"action": "consumables_pick", "item_id": item["item_id"]}))
        if offset % 2:
            keyboard.row()
    if page > 0:
        keyboard.add(Callback("◀ Назад", {"action": "consumables_page", "page": page - 1}))
    if page + 1 < pages:
        keyboard.add(Callback("Далее ▶", {"action": "consumables_page", "page": page + 1}))
    if page > 0 or page + 1 < pages:
        keyboard.row()
    keyboard.add(Callback("✏️ Изменить / удалить", {"action": "consumables_edit_list"}))
    keyboard.row()
    keyboard.add(Callback("✅ Готово", {"action": "consumables_done"}))
    keyboard.add(Callback("⬅ Меню задания", {"action": "consumables_back"}))
    return keyboard


def get_edit_keyboard(items: list[dict], page: int, pages: int) -> Keyboard:
    keyboard = Keyboard(inline=True)
    start = page * EDIT_PAGE_SIZE
    for item in items[start:start + EDIT_PAGE_SIZE]:
        label = _short(str(item.get("name") or f"Материал №{item['item_id']}"))
        keyboard.add(Callback(f"✏️ {label}"[:40], {"action": "consumables_edit", "item_id": item["item_id"]}))
        keyboard.add(Callback(f"🗑 {label}"[:40], {"action": "consumables_remove", "item_id": item["item_id"]}))
        keyboard.row()
    if page > 0:
        keyboard.add(Callback("◀ Назад", {"action": "consumables_edit_page", "page": page - 1}))
    if page + 1 < pages:
        keyboard.add(Callback("Далее ▶", {"action": "consumables_edit_page", "page": page + 1}))
    if page > 0 or page + 1 < pages:
        keyboard.row()
    keyboard.add(Callback("⬅ К расходникам", {"action": "consumables"}))
    keyboard.add(Callback("⬅ Меню задания", {"action": "consumables_back"}))
    return keyboard


def get_quantity_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("❌ Отмена", {"action": "consumables_cancel_quantity"}))
    return keyboard
