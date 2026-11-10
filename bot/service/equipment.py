"""Оборудование на комплексе — сценарий монтёра (меню «🔧 Замена оборудования» → «🧩 Оборудование комплекса»).

Монтёр видит категории оборудования, стоящего на комплексе, и что именно (номер) стоит. «Заменить» показывает
оборудование этой категории, ВЫДАННОЕ ему со склада; после подтверждения новое ставится на комплекс, а снятое
записывается на монтёра как дефектное — его он потом сдаёт на склад (принимает ответственный за склад).
Всё, что сделано, дописывается в «что сняли / что поставили» отчёта о выезде.
Данные и правила — на сервере (api_client → /api/bot/equipment/*); здесь только диалог."""
from __future__ import annotations

import logging

from vkbottle.bot import Message

from bot import api_client
from bot.keyboards.equipment_menu import paged_keyboard, simple_keyboard
from bot.service import mechanic as base
from bot.service import views

logger = logging.getLogger(__name__)


def _is_sim(item: dict | None) -> bool:
    return bool(item) and item.get("category_kind") == "sim"


def _label(item: dict) -> str:
    if _is_sim(item):
        return f"SIM {item['serial_number']}"
    return f"№{item['serial_number']} · {item['model_name']}"


def _report_text(item: dict) -> str:
    if _is_sim(item):
        return f"SIM-карта {item['serial_number']}"
    return f"{item['category_name']} №{item['serial_number']} ({item['model_name']})"


def _with_reason(text: str, reason: str | None) -> str:
    return f"{text} — причина: {reason}" if reason else text


def _append_report(session: dict, key: str, text: str) -> None:
    session[key] = "; ".join(p for p in (session.get(key), text) if p)


def _state(session: dict) -> dict:
    return session.setdefault("eq", {})


def _back_to_menu():
    return simple_keyboard([[("⬅ Назад в меню", {"action": "repair_menu_back"})]])


async def _api(peer_id: int, session: dict, coro):
    """Вызов API с показом понятной ошибки; None — не получилось (сообщение уже отправлено)."""
    try:
        return await coro
    except api_client.ApiError as e:
        await base.send(peer_id, f"❌ {e.detail}", base._menu(session))
    except Exception:  # noqa: BLE001 — сервер недоступен
        logger.exception("Оборудование: сервер недоступен")
        await base.send(peer_id, "⚠️ Сервер сейчас недоступен, попробуйте через минуту.", base._menu(session))
    return None


# ------------------------------------------------------------------ моё оборудование

async def show_my_equipment(peer_id: int, user: dict) -> None:
    """Что числится на монтёре: выданное со склада, снятое с комплексов (его нужно сдать на склад) и снятые SIM (остаются у монтёра)."""
    try:
        held = await api_client.get_held_equipment(user["vk_id"])
    except api_client.ApiError as e:
        await base.send(peer_id, f"❌ {e.detail}", base.get_back_to_menu_keyboard())
        return
    except Exception:  # noqa: BLE001
        logger.exception("Моё оборудование: сервер недоступен")
        await base.send(peer_id, "⚠️ Сервер сейчас недоступен, попробуйте через минуту.", base.get_back_to_menu_keyboard())
        return
    issued = [i for i in held if i.get("held_reason") == "issued"]
    removed = [i for i in held if i.get("held_reason") == "removed"]
    kept = [i for i in held if i.get("held_reason") == "kept"]
    if not held:
        await base.send(peer_id, "🎒 На вас сейчас не числится оборудование.", base.get_back_to_menu_keyboard())
        return
    lines = ["🎒 На вас числится:"]
    if issued:
        lines.append("\n📦 Выдано (для установки):")
        lines += [f"• {i['category_name']} №{i['serial_number']} ({i['model_name']})" for i in issued]
    if removed:
        lines.append("\n🔧 Снято с комплексов — сдайте на склад:")
        lines += [
            f"• {i['category_name']} №{i['serial_number']} ({i['model_name']})" + (f", с «{i['removed_point_id']}»" if i.get("removed_point_id") else "")
            for i in removed
        ]
    if kept:
        lines.append("\n💳 Снятые SIM — остались у вас (сдавать на склад не обязательно, можно ставить снова):")
        lines += [
            f"• SIM {i['serial_number']}" + (f", с «{i['removed_point_id']}»" if i.get("removed_point_id") else "")
            + (f" — {i['status'].lower()}" if i.get("status") else "")
            for i in kept
        ]
    await base.send(peer_id, "\n".join(lines), base.get_back_to_menu_keyboard())


# ------------------------------------------------------------------ каталог комплекса

async def show_catalog(peer_id: int, user: dict, session: dict) -> None:
    point_id = session.get("point_id")
    if not point_id:
        await base.send(peer_id, "Сначала откройте задание.", simple_keyboard([[("🏠 Меню", {"action": "main_menu"})]]))
        return
    data = await _api(peer_id, session, api_client.get_point_equipment(point_id))
    if data is None:
        return
    st = _state(session)
    st.clear()
    st["groups"] = {g["category_id"]: g for g in data["groups"]}
    st["categories"] = data["categories"]
    session["step"] = "menu"
    await views.show("eq_catalog", peer_id, user, session)


@views.register("eq_catalog")
async def _view_catalog(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _state(session)
    groups = list(st.get("groups", {}).values())
    if groups:
        lines = [f"🧩 Оборудование на комплексе «{session.get('point_id')}»:"]
        for g in groups:
            lines.append(
                f"• {g['category_name']}: "
                + ", ".join(i["serial_number"] if _is_sim(i) else f"№{i['serial_number']} ({i['model_name']})" for i in g["items"])
            )
        lines.append("\nВыберите категорию, в которой нужно что-то заменить или снять:")
    else:
        lines = [f"На комплексе «{session.get('point_id')}» пока не записано оборудование.",
                 "Если вы что-то ставите — нажмите «Установить из выданного»."]
    entries = [(f"{g['category_name']} ({len(g['items'])})", {"action": "eq_cat", "c": g["category_id"]}) for g in groups]
    has_sim = any(any(_is_sim(i) for i in g["items"]) for g in groups)
    add_row = [("➕ Установить из выданного", {"action": "eq_new"})]
    if not has_sim:
        add_row.append(("💳 Указать SIM", {"action": "repair_sim_fix"}))   # SIM на комплексе в учёте ещё нет
    controls = [add_row, [("⬅ Назад", {"action": "repair_equipment"})]]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, "\n".join(lines), keyboard)


async def choose_category(peer_id: int, user: dict, session: dict, category_id: int) -> None:
    st = _state(session)
    group = st.get("groups", {}).get(category_id)
    if not group:
        await show_catalog(peer_id, user, session)
        return
    st["category"] = group
    if len(group["items"]) == 1:
        await choose_item(peer_id, user, session, group["items"][0]["id"])
        return
    await views.show("eq_items", peer_id, user, session)


@views.register("eq_items")
async def _view_items(peer_id: int, user: dict, session: dict, page: int) -> None:
    group = _state(session).get("category") or {}
    entries = [(_label(i), {"action": "eq_item", "i": i["id"]}) for i in group.get("items", [])]
    keyboard, _, _ = paged_keyboard(entries, page, [[("⬅ Назад", {"action": "eq_catalog"})]])
    await base.send(peer_id, f"{group.get('category_name', '')}: что именно?", keyboard)


async def choose_item(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _state(session)
    item = next((i for g in st.get("groups", {}).values() for i in g["items"] if i["id"] == item_id), None)
    if not item:
        await show_catalog(peer_id, user, session)
        return
    if not st.get("old") or st["old"]["id"] != item_id:
        st.pop("actual", None)          # выбрали другую единицу — прежняя «сверка» не относится к ней
    st["old"] = item
    st.pop("new", None)
    st.pop("reason", None)
    st["mode"] = None
    await show_item_card(peer_id, session)


def _actual_text(st: dict) -> str:
    """Что реально стояло, если монтёр указал другой номер (пустая строка — обычный случай)."""
    actual = st.get("actual")
    if not actual:
        return ""
    old = st["old"]
    if actual.get("item"):
        a = actual["item"]
        who = f"№{a['serial_number']} ({a['category_name']} {a['model_name']})"
    else:
        who = f"№{actual['serial']} (новый номер — запишу в базу)"
    return (f"\n🔁 Реально стояло: {who}.\nПо системе здесь должен быть №{old['serial_number']} — он будет помечен "
            "как «неизвестное местонахождение».")


async def show_item_card(peer_id: int, session: dict) -> None:
    st = _state(session)
    item = st["old"]
    since = f"\nУстановлено: {item.get('installed_date') or '—'}" if item.get("installed_date") else ""
    if _is_sim(item):
        # SIM: замена (новую можно ввести номером), исправление ошибочно записанного номера, снятие без замены.
        await base.send(
            peer_id,
            f"💳 SIM-карта {item['serial_number']}{since}\nЧто делаем?",
            simple_keyboard([
                [("🔄 Заменить SIM", {"action": "repair_sim_change"})],
                [("✏️ Исправить номер (ошибка в записи)", {"action": "repair_sim_fix"})],
                [("➖ Снять без замены", {"action": "eq_rm"})],
                [("⬅ Назад", {"action": "eq_catalog"})],
            ]),
        )
        return
    await base.send(
        peer_id,
        f"{item['category_name']} №{item['serial_number']} ({item['model_name']}){since}{_actual_text(st)}\nЧто делаем?",
        simple_keyboard([
            [("🔄 Заменить", {"action": "eq_repl"})],
            [("➖ Снять без замены", {"action": "eq_rm"})],
            [("🔁 Стоит другой номер", {"action": "eq_other"})],
            [("⬅ Назад", {"action": "eq_catalog"})],
        ]),
    )


# ------------------------------------------------------------------ «стоит другой номер»

async def request_actual(peer_id: int, session: dict) -> None:
    st = _state(session)
    if not st.get("old"):
        await base.send(peer_id, "Сначала выберите оборудование на комплексе.", base._menu(session))
        return
    session["step"] = "awaiting_actual_serial"
    await base.send(
        peer_id,
        f"Введите номер оборудования, которое реально стояло вместо №{st['old']['serial_number']} (можно часть номера).",
        simple_keyboard([[("⬅ Назад", {"action": "eq_item", "i": st["old"]["id"]})]]),
    )


async def handle_actual_text(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    st = _state(session)
    text = (message.text or "").strip()
    old = st.get("old")
    if not text or not old:
        await base.send(peer_id, "Введите номер.")
        return
    found = await _api(peer_id, session, api_client.find_equipment(user["vk_id"], text))
    if found is None:
        return
    session["step"] = "menu"
    st["actual_try"] = text
    st["actual_results"] = [i for i in found if i["id"] != old["id"]]
    await views.show("eq_actual", peer_id, user, session)


@views.register("eq_actual")
async def _view_actual(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _state(session)
    old = st.get("old") or {}
    results = st.get("actual_results", [])
    where = {"stock": "на складе", "person": "у монтёра", "point": "на комплексе", "unknown": "местонахождение неизвестно"}
    entries = [
        (f"№{i['serial_number']} · {i['model_name']} · {where.get(i['place'], '')}", {"action": "eq_actpick", "i": i["id"]})
        for i in results
    ]
    head = (f"Нашёл по «{st.get('actual_try')}». Какое реально стояло?" if results
            else f"Номера «{st.get('actual_try')}» в базе нет.")
    controls = [[(f"➕ Записать «{st.get('actual_try')}» как новое"[:40], {"action": "eq_actnew"})],
                [("🔍 Другой номер", {"action": "eq_other"}), ("⬅ Назад", {"action": "eq_item", "i": old.get("id", 0)})]]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, head, keyboard)


async def pick_actual(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _state(session)
    item = next((i for i in st.get("actual_results", []) if i["id"] == item_id), None)
    if not item or not st.get("old"):
        await show_catalog(peer_id, user, session)
        return
    st["actual"] = {"item": item}
    await show_item_card(peer_id, session)


async def new_actual(peer_id: int, user: dict, session: dict) -> None:
    st = _state(session)
    serial = st.get("actual_try")
    if not serial or not st.get("old"):
        await show_catalog(peer_id, user, session)
        return
    st["actual"] = {"serial": serial}
    await show_item_card(peer_id, session)


def _actual_payload(st: dict) -> dict | None:
    actual = st.get("actual")
    if not actual:
        return None
    if actual.get("item"):
        return {"actual_equipment_id": actual["item"]["id"]}
    return {"actual_serial": actual["serial"], "create_missing": True}


def _removed_for_report(st: dict) -> dict:
    """Что записать в отчёт как снятое: реально стоявшее (если указано), иначе то, что по системе."""
    old, actual = st["old"], st.get("actual")
    if actual and actual.get("item"):
        return actual["item"]
    if actual:
        return {**old, "serial_number": actual["serial"]}
    return old


# ------------------------------------------------------------------ замена / установка

async def _load_held(peer_id: int, user: dict, session: dict, category_id) -> list[dict] | None:
    held = await _api(peer_id, session, api_client.get_held_equipment(user["vk_id"], category_id))
    if held is None:
        return None
    return [i for i in held if i.get("held_reason") == "issued"]


async def start_replace(peer_id: int, user: dict, session: dict) -> None:
    st = _state(session)
    old = st.get("old")
    if not old:
        await show_catalog(peer_id, user, session)
        return
    if _is_sim(old):
        await base.request_sim_change(peer_id, session)   # новая SIM — по номеру: из выданных, из базы или новая запись
        return
    held = await _load_held(peer_id, user, session, old["category_id"])
    if held is None:
        return
    st["mode"] = "replace"
    st["held"] = held
    st.pop("via_suffix", None)
    st.pop("pending_suffix", None)
    if not held:
        await base.send(
            peer_id,
            f"У вас нет выданного оборудования категории «{old['category_name']}». Не страшно: введите последние {MIN_SUFFIX} цифр серийного номера того, что поставили, — "
            "найду его в учёте (даже если вам его не выдавали), а если его там ещё нет, запишу установку по номеру, и оно привяжется к комплексу, когда его добавят в систему.\n"
            "Если заменили что-то без номера, запишите это текстом — «Другое оборудование».",
            simple_keyboard([[("🔍 Ввести номер", {"action": "eq_serial"})], [("📝 Другое оборудование", {"action": "repair_equipment_other"})], [("⬅ Назад", {"action": "eq_catalog"})]]),
        )
        return
    await views.show("eq_held", peer_id, user, session)


@views.register("eq_held")
async def _view_held(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _state(session)
    old = st.get("old")
    held = st.get("held", [])
    head = (f"На вас выдано ({old['category_name']}):" if old else "Выдано вам:")
    entries = [(_label(i), {"action": "eq_pick", "i": i["id"]}) for i in held]
    controls = [[("🔍 Ввести номер", {"action": "eq_serial"})], [("⬅ Назад", {"action": "eq_catalog"})]]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, f"{head}\nВыберите, что ставите (или введите номер):", keyboard)


async def start_install(peer_id: int, user: dict, session: dict) -> None:
    """Установить на комплекс оборудование, которого там ещё нет в системе: выбор среди выданного монтёру."""
    held = await _load_held(peer_id, user, session, None)
    if held is None:
        return
    st = _state(session)
    st["mode"] = "install"
    st["old"] = None
    st.pop("reason", None)
    st.pop("via_suffix", None)
    st.pop("pending_suffix", None)
    st["held_all"] = held
    if not held:
        await base.send(
            peer_id,
            f"У вас нет выданного оборудования. Не страшно: введите последние {MIN_SUFFIX} цифр серийного номера того, что ставите, — найду его в учёте или запишу установку по номеру "
            "(привяжется к комплексу, когда оборудование добавят в систему).",
            simple_keyboard([[("🔍 Ввести номер", {"action": "eq_serial"})], [("📝 Другое оборудование", {"action": "repair_equipment_other"})], [("⬅ Назад", {"action": "eq_catalog"})]]),
        )
        return
    await views.show("eq_newcat", peer_id, user, session)


@views.register("eq_newcat")
async def _view_newcat(peer_id: int, user: dict, session: dict, page: int) -> None:
    held = _state(session).get("held_all", [])
    counts: dict[int, list] = {}
    for i in held:
        counts.setdefault(i["category_id"], []).append(i)
    entries = [(f"{items[0]['category_name']} ({len(items)})", {"action": "eq_newc", "c": cid}) for cid, items in counts.items()]
    keyboard, _, _ = paged_keyboard(entries, page, [[("🔍 Ввести номер", {"action": "eq_serial"})], [("⬅ Назад", {"action": "eq_catalog"})]])
    await base.send(peer_id, "Какое оборудование из выданного вам устанавливаете?", keyboard)


async def choose_install_category(peer_id: int, user: dict, session: dict, category_id: int) -> None:
    st = _state(session)
    st["held"] = [i for i in st.get("held_all", []) if i["category_id"] == category_id]
    await views.show("eq_held", peer_id, user, session)


async def pick_new(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _state(session)
    pool = st.get("held") or st.get("held_all") or []
    new = next((i for i in pool if i["id"] == item_id), None)
    if not new:
        await base.send(peer_id, "Этого оборудования нет среди выданного вам — откройте список заново.", base._menu(session))
        return
    st["new"] = new
    st.pop("reason", None)
    old = st.get("old")
    if old:
        gone = _removed_for_report(st)
        await base.ask_removal_reason(
            peer_id, session, {"op": "eq_replace"},
            title=f"Заменяем {old['category_name']} №{gone['serial_number']} → №{new['serial_number']}.",
        )
        return
    await _show_confirm(peer_id, session)


async def reason_given(peer_id: int, user: dict, session: dict, reason: str) -> None:
    """Причина снятия выбрана/написана (bot.service.mechanic.ask_removal_reason) — показываем подтверждение."""
    _state(session)["reason"] = reason
    session.pop("reason_ctx", None)
    await _show_confirm(peer_id, session)


async def change_reason(peer_id: int, session: dict) -> None:
    """«✏️ Причина» на экране подтверждения — спросить причину заново."""
    st = _state(session)
    if st.get("mode") not in ("replace", "remove"):
        return
    await base.ask_removal_reason(peer_id, session, {"op": "eq_replace" if st["mode"] == "replace" else "eq_remove"})


async def _show_confirm(peer_id: int, session: dict) -> None:
    st = _state(session)
    mode, old, new, reason = st.get("mode"), st.get("old"), st.get("new"), st.get("reason")
    why = f"\nПричина снятия: {reason}" if reason else ""
    rows = [[("✅ Подтвердить", {"action": "eq_do"}), ("❌ Отмена", {"action": "eq_catalog"})]]
    pend = st.get("pending_suffix")
    if pend and not new and mode in ("replace", "install"):
        # В учёте такого номера нет: запишем по последним цифрам, привяжется само, когда оборудование добавят в систему.
        if mode == "replace" and old:
            gone = _removed_for_report(st)
            text = (f"Заменить {old['category_name']} №{gone['serial_number']} → оборудование с номером …{pend} (в учёте его пока нет)?{why}\n"
                    f"Снятое (№{gone['serial_number']}) будет числиться на вас как дефектное — сдайте его на склад.\n"
                    "Новое оборудование привяжется к комплексу само, когда его добавят в систему." + _actual_text(st))
            rows.append([("✏️ Причина", {"action": "eq_reason"})])
        else:
            text = (f"Записать установку на комплекс «{session.get('point_id')}» оборудования с номером …{pend} (в учёте его пока нет)?\n"
                    "Оно привяжется к комплексу само, когда его добавят в систему.")
        await base.send(peer_id, text, simple_keyboard(rows))
        return
    if mode == "replace" and old and new:
        gone = _removed_for_report(st)
        text = (f"Заменить {old['category_name']} №{gone['serial_number']} → №{new['serial_number']} ({new['model_name']})?{why}\n"
                f"Снятое (№{gone['serial_number']}) будет числиться на вас как дефектное — сдайте его на склад."
                + _actual_text(st))
        rows.append([("✏️ Причина", {"action": "eq_reason"})])
    elif mode == "remove" and old:
        gone = _removed_for_report(st)
        text = (f"Снять {old['category_name']} №{gone['serial_number']} с комплекса без замены?{why}\n"
                "Оно будет числиться на вас как дефектное — сдайте его на склад." + _actual_text(st))
        rows.append([("✏️ Причина", {"action": "eq_reason"})])
    elif new:
        text = f"Установить {new['category_name']} №{new['serial_number']} ({new['model_name']}) на комплекс «{session.get('point_id')}»?"
    else:
        await base.send(peer_id, "Действие устарело — откройте оборудование комплекса заново.", base._menu(session))
        return
    await base.send(peer_id, text, simple_keyboard(rows))


MIN_SUFFIX = 6        # столько последних символов серийного номера достаточно, чтобы найти оборудование или записать установку «по номеру»


def _suffix_key(text: str) -> str:
    """Как сервер: без пробелов, дефисов и знаков, без регистра."""
    return "".join(ch for ch in (text or "") if ch.isalnum()).casefold()


async def request_serial(peer_id: int, session: dict) -> None:
    session["step"] = "awaiting_equipment_serial"
    await base.send(
        peer_id,
        f"Введите последние {MIN_SUFFIX} цифр серийного номера оборудования, которое ставите (можно и весь номер). Найду его в учёте — "
        "даже если вам его не выдавали. А если в учёте такого номера ещё нет, запишу установку по этим цифрам: оборудование само "
        "встанет на этот комплекс, когда его добавят в систему.",
        simple_keyboard([[("❌ Отмена", {"action": "eq_catalog"})]]),
    )


async def handle_serial_text(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    text = (message.text or "").strip().casefold()
    st = _state(session)
    st.pop("via_suffix", None)
    st.pop("pending_suffix", None)
    pool = st.get("held") or st.get("held_all") or []
    if not text:
        await base.send(peer_id, "Введите номер.")
        return
    exact = [i for i in pool if i["serial_number"].casefold() == text]
    found = exact or [i for i in pool if text in i["serial_number"].casefold()]
    session["step"] = "menu"
    if len(found) == 1:
        await pick_new(peer_id, user, session, found[0]["id"])
        return
    if found:
        st["held"] = found
        await views.show("eq_held", peer_id, user, session)
        return
    # Среди выданного монтёру нет — ищем по всему учёту по последним символам (склад, чужие руки), а если и там нет — предлагаем записать «по номеру».
    suffix = _suffix_key(message.text or "")
    again = simple_keyboard([[("🔍 Ввести другой", {"action": "eq_serial"})], [("⬅ Назад", {"action": "eq_catalog"})]])
    if len(suffix) < MIN_SUFFIX:
        await base.send(
            peer_id, f"Среди выданного вам «{message.text.strip()}» не нашлось. Чтобы найти по всему учёту или поставить по номеру, введите не меньше {MIN_SUFFIX} последних цифр серийного номера.",
            again,
        )
        return
    cat = (st.get("old") or {}).get("category_id")
    items = await _api(peer_id, session, api_client.find_equipment_by_suffix(user["vk_id"], suffix, cat))
    if items is None:
        return
    usable = [i for i in items if i.get("usable")]
    if usable:
        st["held"] = usable                       # дальше — как обычный выбор из списка; ставится через «по последним цифрам» (годится и оборудование с рук другого монтёра)
        st["via_suffix"] = suffix
        if len(usable) == 1:
            await pick_new(peer_id, user, session, usable[0]["id"])
        else:
            await views.show("eq_held", peer_id, user, session)
        return
    if items:
        await base.send(peer_id, f"Найдено №{items[0]['serial_number']}, но поставить его нельзя: {items[0].get('why') or 'недоступно'}.", again)
        return
    st["pending_suffix"] = suffix
    await base.send(
        peer_id,
        f"Оборудования с номером, заканчивающимся на «{suffix}», в учёте нет. Можно записать установку по этим цифрам: комплекс запомнит оборудование, "
        "а когда его добавят в систему, оно само привяжется к этому комплексу. Поставить по номеру?",
        simple_keyboard([[("✅ Да, поставить по номеру", {"action": "eq_suffix_ok"})], [("🔍 Ввести другой", {"action": "eq_serial"})], [("⬅ Назад", {"action": "eq_catalog"})]]),
    )


async def suffix_ok(peer_id: int, user: dict, session: dict) -> None:
    """«✅ Да, поставить по номеру»: в учёте такого номера нет — дальше обычная замена/установка (причина снятия старого → подтверждение)."""
    st = _state(session)
    suffix = st.get("pending_suffix")
    if not suffix or st.get("mode") not in ("replace", "install"):
        await base.send(peer_id, "Действие устарело — откройте оборудование комплекса заново.", base._menu(session))
        return
    st["new"] = None
    old = st.get("old")
    if old and st["mode"] == "replace":
        gone = _removed_for_report(st)
        await base.ask_removal_reason(
            peer_id, session, {"op": "eq_replace"},
            title=f"Заменяем {old['category_name']} №{gone['serial_number']} → оборудование с номером …{suffix} (в учёте пока нет).",
        )
        return
    await _show_confirm(peer_id, session)


async def start_remove(peer_id: int, user: dict, session: dict) -> None:
    st = _state(session)
    old = st.get("old")
    if not old:
        await show_catalog(peer_id, user, session)
        return
    st["mode"] = "remove"
    st["new"] = None
    st.pop("reason", None)
    st.pop("via_suffix", None)
    st.pop("pending_suffix", None)
    gone = _removed_for_report(st)
    await base.ask_removal_reason(
        peer_id, session, {"op": "eq_remove"}, title=f"Снимаем {old['category_name']} №{gone['serial_number']} без замены.",
    )


async def do_confirm(peer_id: int, user: dict, session: dict) -> None:
    st = _state(session)
    mode, old, new = st.get("mode"), st.get("old"), st.get("new")
    point_id, vk_id = session.get("point_id"), user["vk_id"]
    if not point_id or not mode:
        await base.send(peer_id, "Действие устарело — откройте оборудование комплекса заново.", base._menu(session))
        return
    reason = st.get("reason")
    suffix = st.get("pending_suffix") or st.get("via_suffix")
    if suffix and mode in ("replace", "install") and (new or st.get("pending_suffix")):
        # Оборудование указано по последним цифрам номера: найденное в учёте (в том числе не выданное этому монтёру) ставится, а если номера в учёте нет — запоминается
        # и привяжется к комплексу само, когда его добавят.
        gone = _removed_for_report(st) if (mode == "replace" and old) else None
        cat_id = (old or new or {}).get("category_id")
        res = await _api(peer_id, session, api_client.install_equipment_by_suffix(
            vk_id, point_id, suffix, old_id=old["id"] if gone else None, new_id=new["id"] if new else None, category_id=cat_id, reason=reason))
        if res is None:
            return
        if gone:
            _append_report(session, "equipment_removed", _with_reason(_report_text(gone), reason))
        if res.get("mode") == "linked" and new:
            _append_report(session, "equipment_installed", _report_text(new))
            text = f"✅ Установлено №{res.get('installed') or new['serial_number']} ({new['model_name']})."
            if res.get("held_by_other"):
                text += "\n⚠️ По учёту оно числилось на другом монтёре — в журнале склада это отмечено."
        else:
            cat = (old or {}).get("category_name") or "Оборудование"
            _append_report(session, "equipment_installed", f"{cat} с номером …{suffix} (указано по последним цифрам; в учёте пока нет)")
            text = (f"✅ Записано: на комплексе теперь оборудование с номером …{suffix}. В учёте его пока нет — оно привяжется к комплексу само, "
                    "когда его добавят в систему.")
        if res.get("removed"):
            text += f"\n№{res['removed']} снято и числится на вас как дефектное — сдайте на склад." + (f"\nПричина снятия: {reason}." if reason else "")
    elif mode == "replace" and old and new:
        gone = _removed_for_report(st)
        res = await _api(peer_id, session, api_client.replace_equipment(vk_id, point_id, old["id"], new["id"], _actual_payload(st), reason))
        if res is None:
            return
        _append_report(session, "equipment_removed", _with_reason(_report_text(gone), reason))
        _append_report(session, "equipment_installed", _report_text(new))
        text = (f"✅ Установлено №{new['serial_number']} ({new['model_name']}).\n"
                f"№{res['removed']} снято и числится на вас как дефектное — сдайте на склад."
                + (f"\nПричина снятия: {reason}." if reason else ""))
        if res.get("unknown"):
            text += f"\n⚠️ №{res['unknown']} (по системе стояло здесь) помечено как «неизвестное местонахождение» — сообщите складу."
    elif mode == "install" and new:
        res = await _api(peer_id, session, api_client.install_equipment(vk_id, point_id, new["id"]))
        if res is None:
            return
        _append_report(session, "equipment_installed", _report_text(new))
        text = f"✅ Установлено №{new['serial_number']} ({new['model_name']})."
    elif mode == "remove" and old:
        gone = _removed_for_report(st)
        res = await _api(peer_id, session, api_client.remove_equipment(vk_id, point_id, old["id"], _actual_payload(st), reason))
        if res is None:
            return
        _append_report(session, "equipment_removed", _with_reason(_report_text(gone), reason))
        text = (f"✅ №{res['removed']} снято с комплекса и числится на вас как дефектное — сдайте на склад."
                + (f"\nПричина снятия: {reason}." if reason else ""))
        if res.get("unknown"):
            text += f"\n⚠️ №{res['unknown']} (по системе стояло здесь) помечено как «неизвестное местонахождение» — сообщите складу."
    else:
        await base.send(peer_id, "Действие устарело — откройте оборудование комплекса заново.", base._menu(session))
        return
    st.clear()
    session["step"] = "menu"
    await base.send(
        peer_id, text + "\nЗамена попадёт в отчёт о выезде.",
        simple_keyboard([[("🧩 Ещё оборудование", {"action": "eq_catalog"})], [("⬅ Назад в меню", {"action": "repair_menu_back"})]]),
    )


# ------------------------------------------------------------------ демонтаж: что именно снято
# Для заданий на демонтаж («Демонтаж» в календаре или демонтаж, оформленный диспетчером) монтёр указывает, что именно он
# демонтировал: весь комплекс или отдельные элементы (с отметками по оборудованию из учёта + свободный текст для того,
# чего в учёте нет). Выбор сохраняется в прогрессе комплекса (общий для бригады), а при «Завершить» оборудование
# снимается на монтёра (как снятое — ждёт приёмки на склад) и сводка попадает в отчёт.

def _dem(session: dict) -> dict:
    return session.setdefault("dem", {})


def _dem_summary(mode: str, taken: list[dict], left: list[dict], other: str) -> tuple[str, str]:
    """(перечень снятого для «что снято» в отчёте, фраза для отчёта)."""
    labels = "; ".join(_report_text(i) for i in taken)
    if mode == "whole":
        text = "Демонтирован весь комплекс" + (f": {labels}" if labels else " (оборудование в учёте не числилось)")
        if other:
            text += f"; ещё снято: {other}"
        return labels, text
    parts = [labels] if labels else []
    if other:
        parts.append(f"ещё: {other}")
    text = "Демонтированы отдельные элементы: " + ("; ".join(parts) or "—")
    text += ". Остались на комплексе: " + ("; ".join(_report_text(i) for i in left) if left else "ничего (по учёту)") + "."
    return labels, text


async def start_demontage_scope(peer_id: int, user: dict, session: dict) -> None:
    point_id = session.get("point_id")
    if not point_id:
        await base.send(peer_id, "Сначала откройте задание.", simple_keyboard([[("🏠 Меню", {"action": "main_menu"})]]))
        return
    data = await _api(peer_id, session, api_client.get_point_equipment(point_id))
    if data is None:
        return
    items = [i for g in data["groups"] for i in g["items"]]
    scope = session.get("demontage_scope") or {}
    d = _dem(session)
    d.clear()
    d.update(items=items, selected=list(scope.get("ids") or []) if scope.get("mode") == "parts" else [], other=scope.get("other") or "")
    session["step"] = "menu"
    lines = [f"🧰 Что демонтировано на комплексе «{point_id}»?"]
    if items:
        lines.append("По учёту на комплексе: " + "; ".join(_report_text(i) for i in items) + ".")
    else:
        lines.append("По учёту на комплексе оборудования нет — можно отметить «весь комплекс» или описать снятое текстом.")
    if scope.get("summary"):
        lines.append("\nСейчас указано: " + scope["summary"])
    await base.send(
        peer_id, "\n".join(lines),
        simple_keyboard([
            [("🏗 Весь комплекс", {"action": "dem_whole"})],
            [("🧩 Отдельные элементы", {"action": "dem_parts"})],
            [("⬅ Назад в меню", {"action": "repair_menu_back"})],
        ]),
    )


async def dem_whole(peer_id: int, user: dict, session: dict) -> None:
    d = _dem(session)
    items = d.get("items")
    if items is None:
        await start_demontage_scope(peer_id, user, session)
        return
    _, text = _dem_summary("whole", items, [], "")
    await base.send(
        peer_id,
        f"{text}.\nВсё это оборудование спишется с комплекса и будет числиться на вас как снятое — сдайте его на склад. Верно?",
        simple_keyboard([[("✅ Да, весь комплекс", {"action": "dem_whole_ok"}), ("❌ Нет", {"action": "dem_scope"})]]),
    )


async def dem_whole_ok(peer_id: int, user: dict, session: dict) -> None:
    items = _dem(session).get("items") or []
    labels, text = _dem_summary("whole", items, [], "")
    session["demontage_scope"] = {"mode": "whole", "ids": [i["id"] for i in items], "labels": labels, "other": "", "summary": text}
    session["step"] = "menu"
    await base.send(peer_id, f"✅ Записал: {text}.\nЭто попадёт в отчёт; оборудование спишется при «Завершить».", base._menu(session))


async def dem_parts(peer_id: int, user: dict, session: dict) -> None:
    if _dem(session).get("items") is None:
        await start_demontage_scope(peer_id, user, session)
        return
    await views.show("dem_parts", peer_id, user, session)


@views.register("dem_parts")
async def _view_dem_parts(peer_id: int, user: dict, session: dict, page: int) -> None:
    d = _dem(session)
    items, selected, other = d.get("items", []), set(d.get("selected", [])), d.get("other") or ""
    head = ["🧩 Отметьте, что демонтировано (нажатие — отметить / снять отметку):"]
    if not items:
        head = ["По учёту на комплексе оборудования нет — опишите снятое текстом («✍️ Другое»)."]
    if other:
        head.append(f"✍️ Ещё снято: {other}")
    entries = [(f"{'☑' if i['id'] in selected else '☐'} {_label(i)}", {"action": "dem_tog", "i": i["id"]}) for i in items]
    controls = [
        [(f"✅ Готово ({len(selected)})", {"action": "dem_parts_ok"})],
        [("✍️ Другое", {"action": "dem_other"}), ("⬅ Назад", {"action": "dem_scope"})],
    ]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, "\n".join(head), keyboard)


async def dem_toggle(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    d = _dem(session)
    if item_id not in {i["id"] for i in d.get("items", [])}:
        await start_demontage_scope(peer_id, user, session)
        return
    selected = list(d.get("selected", []))
    if item_id in selected:
        selected.remove(item_id)
    else:
        selected.append(item_id)
    d["selected"] = selected
    await views.show("dem_parts", peer_id, user, session, session.get("view_page") or 0)


async def dem_other(peer_id: int, session: dict) -> None:
    d = _dem(session)
    await base.begin_draft(
        peer_id, session, "dem_other",
        "✍️ Напишите, что ещё снято, чего нет в списке оборудования (например: «кронштейн, кабель питания»). Если нечего — «-».",
        existing=d.get("other") or None, existing_title="✍️ Уже записано:",
    )


async def set_dem_other(peer_id: int, user: dict, session: dict, value: str) -> None:
    _dem(session)["other"] = "" if value.strip() in ("-", "—") else value.strip()
    session["step"] = "menu"
    await views.show("dem_parts", peer_id, user, session)


async def dem_parts_ok(peer_id: int, user: dict, session: dict) -> None:
    d = _dem(session)
    items = d.get("items", [])
    selected = [i for i in d.get("selected", []) if i in {x["id"] for x in items}]
    other = d.get("other") or ""
    if not selected and not other:
        await base.send(peer_id, "Отметьте хотя бы один элемент или опишите снятое текстом («✍️ Другое»).")
        await views.show("dem_parts", peer_id, user, session, session.get("view_page") or 0)
        return
    taken = [i for i in items if i["id"] in selected]
    left = [i for i in items if i["id"] not in selected]
    labels, text = _dem_summary("parts", taken, left, other)
    session["demontage_scope"] = {"mode": "parts", "ids": selected, "labels": labels, "other": other, "summary": text}
    session["step"] = "menu"
    await base.send(
        peer_id,
        f"✅ Записал: {text}\nЭто попадёт в отчёт; отмеченное оборудование спишется при «Завершить». "
        "Комплекс при этом целиком не демонтирован — на «Завершить» выберете его итоговый статус.",
        base._menu(session),
    )
