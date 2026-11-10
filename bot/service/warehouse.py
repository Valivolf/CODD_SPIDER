"""Меню ответственного за склад (роль «склад» в «Персонале»): принять снятое от монтёра, выдать
оборудование монтёру, поставить состояние (отремонтировано, не подлежит восстановлению…), остатки.

Правила — на сервере (/api/bot/warehouse/*); он же проверяет, что звонит человек с ролью «склад».
Принятое снятое оборудование остаётся «дефектным» и помнит, кто и откуда его снял."""
from __future__ import annotations

import logging

from vkbottle.bot import Message

from bot import api_client
from bot.keyboards.equipment_menu import cut, get_back_to_warehouse_keyboard, get_warehouse_menu_keyboard, paged_keyboard, simple_keyboard
from bot.service import mechanic as base
from bot.service import views

logger = logging.getLogger(__name__)

WAREHOUSE_ROLE = "warehouse"


def is_warehouse(user: dict) -> bool:
    return user.get("role_type") == WAREHOUSE_ROLE


def _wh(session: dict) -> dict:
    return session.setdefault("wh", {})


async def _guard(peer_id: int, user: dict) -> bool:
    if is_warehouse(user):
        return True
    await base.send(peer_id, "⛔ Этот раздел доступен только ответственному за склад.", base.get_main_menu_keyboard())
    return False


async def _api(peer_id: int, coro):
    try:
        return await coro
    except api_client.ApiError as e:
        await base.send(peer_id, f"❌ {e.detail}", get_back_to_warehouse_keyboard())
    except Exception:  # noqa: BLE001
        logger.exception("Склад: сервер недоступен")
        await base.send(peer_id, "⚠️ Сервер сейчас недоступен, попробуйте через минуту.", get_back_to_warehouse_keyboard())
    return None


def _item_line(it: dict) -> str:
    base_txt = f"№{it['serial_number']} {it['category_name']} · {it['model_name']}"
    if it.get("held_reason") == "removed":
        src = f", снято с {it['removed_point_id']}" if it.get("removed_point_id") else ""
        why = f" (причина: {it['removal_reason']})" if it.get("removal_reason") else ""
        return f"🔧 {base_txt}{src} — {it.get('status') or 'дефектное'}{why}"
    if it.get("held_reason") == "kept":
        src = f", снята с {it['removed_point_id']}" if it.get("removed_point_id") else ""
        return f"💳 {base_txt}{src} — осталась у монтёра ({it.get('status') or '—'}), сдача не обязательна"
    if it.get("held_reason") == "issued":
        return f"📦 {base_txt} (выдано, не использовано)"
    return base_txt


# ------------------------------------------------------------------ приём от монтёра

async def show_accept(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    people = await _api(peer_id, api_client.wh_awaiting(user["vk_id"]))
    if people is None:
        return
    st = _wh(session)
    st.clear()
    st["people"] = people
    session["step"] = "menu"
    if not people:
        await base.send(peer_id, "Принимать нечего: ни на ком не числится оборудование.", get_back_to_warehouse_keyboard())
        return
    await views.show("wh_people", peer_id, user, session)


@views.register("wh_people")
async def _view_people(peer_id: int, user: dict, session: dict, page: int) -> None:
    people = _wh(session).get("people", [])
    entries = []
    for p in people:
        parts = []
        if p["removed"]:
            parts.append(f"снято {p['removed']}")
        if p["issued"]:
            parts.append(f"выдано {p['issued']}")
        if p.get("kept"):
            parts.append(f"SIM у монтёра {p['kept']}")
        entries.append((f"{p['full_name']} — {', '.join(parts)}", {"action": "wh_accp", "p": p["person_id"]}))
    keyboard, _, _ = paged_keyboard(entries, page, [[("🏠 Меню склада", {"action": "main_menu"})]])
    await base.send(peer_id, "📥 У кого принять оборудование? (снято с комплексов, выдано, снятые SIM — их можно не сдавать)", keyboard)


async def show_person_items(peer_id: int, user: dict, session: dict, person_id: int) -> None:
    if not await _guard(peer_id, user):
        return
    items = await _api(peer_id, api_client.wh_person_items(user["vk_id"], person_id))
    if items is None:
        return
    st = _wh(session)
    st["person_id"] = person_id
    st["items"] = items
    name = next((p["full_name"] for p in st.get("people", []) if p["person_id"] == person_id), "монтёр")
    st["person_name"] = name
    if not items:
        await base.send(peer_id, f"На «{name}» ничего не числится.", get_back_to_warehouse_keyboard())
        return
    await views.show("wh_person_items", peer_id, user, session)


@views.register("wh_person_items")
async def _view_person_items(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _wh(session)
    items = st.get("items", [])
    lines = [f"📥 {st.get('person_name')} — числится {len(items)}:"] + [_item_line(i) for i in items]
    entries = [(f"✅ №{i['serial_number']} {i['category_name']}", {"action": "wh_acc1", "i": i["id"]}) for i in items]
    controls = [
        [(f"✅ Принять всё ({len(items)})", {"action": "wh_accall"})],
        [("🔁 Сдан другой номер", {"action": "wh_other"})],
        [("⬅ Назад", {"action": "wh_accept"})],
    ]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, "\n".join(lines), keyboard)


async def accept_items(peer_id: int, user: dict, session: dict, ids: list[int] | None) -> None:
    if not await _guard(peer_id, user):
        return
    st = _wh(session)
    person_id = st.get("person_id")
    chosen = ids if ids is not None else [i["id"] for i in st.get("items", [])]
    if not chosen or not person_id:
        await show_accept(peer_id, user, session)
        return
    res = await _api(peer_id, api_client.wh_accept(user["vk_id"], chosen, person_id))
    if res is None:
        return
    lines = []
    if res["accepted"]:
        lines.append("✅ Принято на склад: " + ", ".join(f"№{a['serial_number']}" for a in res["accepted"]))
        if any(a["removed"] for a in res["accepted"]):
            lines.append("Снятое числится на складе как дефектное (снял: " + (st.get("person_name") or "монтёр") + "). Состояние можно изменить в «Состояние оборудования».")
    if res["skipped"]:
        lines.append("Не принято: " + "; ".join(f"№{s.get('serial_number', s['id'])} — {s['reason']}" for s in res["skipped"]))
    await base.send(peer_id, "\n".join(lines) or "Ничего не принято.", simple_keyboard([[("📥 Принять ещё", {"action": "wh_accept"})], [("🏠 Меню склада", {"action": "main_menu"})]]))


# ------------------------------------------------------------------ выдача монтёру

async def start_issue(peer_id: int, user: dict, session: dict, query: str = "") -> None:
    if not await _guard(peer_id, user):
        return
    fitters = await _api(peer_id, api_client.wh_fitters(user["vk_id"], query))
    if fitters is None:
        return
    st = _wh(session)
    st.clear()
    st["fitters"] = fitters
    st["query"] = query
    session["step"] = "menu"
    if not fitters:
        await base.send(
            peer_id, "Никого не нашёл. Введите часть фамилии ещё раз." if query else "В персонале нет монтёров.",
            simple_keyboard([[("🔎 Найти по фамилии", {"action": "wh_findp"})], [("🏠 Меню склада", {"action": "main_menu"})]]),
        )
        return
    await views.show("wh_fitters", peer_id, user, session)


@views.register("wh_fitters")
async def _view_fitters(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _wh(session)
    entries = [(f["full_name"], {"action": "wh_iss_p", "p": f["id"]}) for f in st.get("fitters", [])]
    controls = [[("🔎 Найти по фамилии", {"action": "wh_findp"})], [("🏠 Меню склада", {"action": "main_menu"})]]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, "📤 Кому выдаём оборудование?" + (f"\nПоиск: «{st['query']}»" if st.get("query") else ""), keyboard)


async def request_person_search(peer_id: int, session: dict) -> None:
    session["step"] = "wh_awaiting_person"
    await base.send(peer_id, "Введите часть фамилии монтёра.", get_back_to_warehouse_keyboard())


async def handle_person_search(message: Message, user: dict, session: dict) -> None:
    session["step"] = "menu"
    await start_issue(session.get("peer_id", message.peer_id), user, session, (message.text or "").strip())


async def issue_to_person(peer_id: int, user: dict, session: dict, person_id: int) -> None:
    st = _wh(session)
    person = next((f for f in st.get("fitters", []) if f["id"] == person_id), None)
    st["issue"] = {"person_id": person_id, "name": person["full_name"] if person else "монтёр", "ids": {}, "serials": []}
    await show_issue_screen(peer_id, user, session)


def _issue_state(session: dict) -> dict:
    return _wh(session).get("issue") or {}


def _chosen_count(iss: dict) -> int:
    return len(iss.get("ids", {})) + len(iss.get("serials", []))


async def show_issue_screen(peer_id: int, user: dict, session: dict) -> None:
    iss = _issue_state(session)
    if not iss:
        await start_issue(peer_id, user, session)
        return
    cats = await _api(peer_id, api_client.wh_categories(user["vk_id"]))
    if cats is None:
        return
    _wh(session)["cats"] = cats
    await views.show("wh_issue_cats", peer_id, user, session)


@views.register("wh_issue_cats")
async def _view_issue_cats(peer_id: int, user: dict, session: dict, page: int) -> None:
    session["step"] = "wh_awaiting_serials"   # пока открыт экран выдачи, набранный текст — номера оборудования
    st = _wh(session)
    iss = st.get("issue", {})
    chosen = [*(f"№{s}" for s in iss.get("ids", {}).values()), *(f"№{s}?" for s in iss.get("serials", []))]
    text = [f"📤 Выдача: {iss.get('name')}", f"Выбрано: {_chosen_count(iss)}" + (f" ({', '.join(chosen)})" if chosen else "")]
    text.append("\n🔎 Введите номер оборудования (можно часть номера) — найду на складе. Несколько номеров — через пробел. Или выберите категорию.")
    entries = [(f"{c['name']} ({c['count']})", {"action": "wh_iss_c", "c": c["id"]}) for c in st.get("cats", [])]
    controls = []
    if _chosen_count(iss):
        controls.append([(f"✅ Выдать ({_chosen_count(iss)})", {"action": "wh_iss_do"})])
    controls.append([("🔎 Найти по номеру", {"action": "wh_iss_find"})])
    controls.append([("❌ Отмена", {"action": "main_menu"})])
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, "\n".join(text), keyboard)


async def request_issue_search(peer_id: int, session: dict) -> None:
    session["step"] = "wh_awaiting_serials"
    await base.send(
        peer_id,
        "🔎 Введите номер оборудования (или его часть). Несколько номеров — через пробел или с новой строки: "
        "добавлю те, что точно совпали с номером на складе.",
    )


async def _explain_missing(user: dict, token: str) -> str:
    """Почему номера нет среди годного на складе: он где-то ещё, не годен к выдаче или вообще не заведён."""
    try:
        found = await api_client.wh_find(user["vk_id"], token)
    except Exception:  # noqa: BLE001
        return f"№{token} — не нашёл"
    if not found:
        return f"№{token} — такого номера нет в базе"
    parts = []
    for it in found[:3]:
        where = _where(it)
        note = ""
        if it["place"] == "stock" and not it.get("status_usable", True):
            note = f", состояние «{it['status']}» — не выдаётся"
        parts.append(f"№{it['serial_number']} {where}{note}")
    return "; ".join(parts)


async def handle_serials_text(message: Message, user: dict, session: dict) -> None:
    """Набранное на экране выдачи — поиск по номеру среди годного на складе."""
    peer_id = session.get("peer_id", message.peer_id)
    iss = _issue_state(session)
    if not iss:
        await start_issue(peer_id, user, session)
        return
    tokens = [t for t in (message.text or "").replace(",", " ").replace(";", " ").split() if t]
    if not tokens:
        await base.send(peer_id, "Введите номер оборудования.")
        return
    st = _wh(session)
    if len(tokens) == 1:
        token = tokens[0]
        res = await _api(peer_id, api_client.wh_stock(user["vk_id"], None, token, 30))
        if res is None:
            return
        items = res["items"]
        exact = next((i for i in items if i["serial_number"].casefold() == token.casefold()), None)
        if exact:
            iss["ids"][exact["id"]] = exact["serial_number"]
            await base.send(peer_id, f"✅ Добавлено №{exact['serial_number']} ({exact['category_name']} · {exact['model_name']}).")
            await views.show("wh_issue_cats", peer_id, user, session, 0)
            return
        if items:
            st["stock"] = items
            st["stock_cat_name"] = f"Поиск «{token}»"
            await views.show("wh_issue_items", peer_id, user, session)
            return
        await base.send(peer_id, "Среди годного на складе нет: " + await _explain_missing(user, token))
        await views.show("wh_issue_cats", peer_id, user, session, 0)
        return
    added: list[str] = []
    problems: list[str] = []
    for token in tokens:
        res = await _api(peer_id, api_client.wh_stock(user["vk_id"], None, token, 5))
        if res is None:
            return
        exact = next((i for i in res["items"] if i["serial_number"].casefold() == token.casefold()), None)
        if exact:
            if exact["id"] not in iss["ids"]:
                iss["ids"][exact["id"]] = exact["serial_number"]
            added.append(f"№{exact['serial_number']}")
        else:
            problems.append(await _explain_missing(user, token))
    lines = []
    if added:
        lines.append("✅ Добавлено: " + ", ".join(added))
    if problems:
        lines.append("Не добавлено:\n" + "\n".join(f"• {p}" for p in problems))
    await base.send(peer_id, "\n".join(lines))
    await views.show("wh_issue_cats", peer_id, user, session, 0)


async def show_issue_category(peer_id: int, user: dict, session: dict, category_id: int) -> None:
    res = await _api(peer_id, api_client.wh_stock(user["vk_id"], category_id))
    if res is None:
        return
    st = _wh(session)
    st["stock_cat"] = category_id
    st["stock"] = res["items"]
    cat = next((c for c in st.get("cats", []) if c["id"] == category_id), None)
    st["stock_cat_name"] = cat["name"] if cat else ""
    await views.show("wh_issue_items", peer_id, user, session)


@views.register("wh_issue_items")
async def _view_issue_items(peer_id: int, user: dict, session: dict, page: int) -> None:
    session["step"] = "wh_awaiting_serials"
    st = _wh(session)
    iss = st.get("issue", {})
    entries = []
    for it in st.get("stock", []):
        mark = "☑" if it["id"] in iss.get("ids", {}) else "☐"
        entries.append((f"{mark} №{it['serial_number']} · {it['model_name']}", {"action": "wh_iss_t", "i": it["id"]}))
    controls = []
    if _chosen_count(iss):
        controls.append([(f"✅ Выдать ({_chosen_count(iss)})", {"action": "wh_iss_do"})])
    controls.append([("🔎 Найти по номеру", {"action": "wh_iss_find"})])
    controls.append([("⬅ К категориям", {"action": "wh_iss_back"})])
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, f"{st.get('stock_cat_name')} — годное на складе. Отметьте, что выдаём ({_chosen_count(iss)} выбрано):", keyboard)


async def toggle_issue_item(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _wh(session)
    iss = st.get("issue")
    item = next((i for i in st.get("stock", []) if i["id"] == item_id), None)
    if not iss or not item:
        await start_issue(peer_id, user, session)
        return
    if item_id in iss["ids"]:
        del iss["ids"][item_id]
    else:
        iss["ids"][item_id] = item["serial_number"]
    await views.show("wh_issue_items", peer_id, user, session, session.get("view_page", 0))


async def do_issue(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    iss = _issue_state(session)
    if not iss or not _chosen_count(iss):
        await base.send(peer_id, "Ничего не выбрано.", get_back_to_warehouse_keyboard())
        return
    res = await _api(peer_id, api_client.wh_issue(user["vk_id"], iss["person_id"], list(iss["ids"]), iss["serials"]))
    if res is None:
        return
    lines = []
    if res["issued"]:
        lines.append(f"✅ Выдано {iss['name']}: " + ", ".join(f"№{s}" for s in res["issued"]))
    if res["skipped"]:
        lines.append("Не выдано: " + "; ".join(f"№{s.get('serial_number', s['id'])} — {s['reason']}" for s in res["skipped"]))
    if res["not_found"]:
        lines.append("Не найдено на складе: " + ", ".join(res["not_found"]))
    _wh(session).clear()
    session["step"] = "menu"
    await base.send(peer_id, "\n".join(lines) or "Ничего не выдано.", simple_keyboard([[("📤 Выдать ещё", {"action": "wh_issue"})], [("🏠 Меню склада", {"action": "main_menu"})]]))


# ------------------------------------------------------------------ состояние оборудования

async def request_status_search(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    _wh(session).clear()
    session["step"] = "wh_awaiting_status_serial"
    await base.send(peer_id, "🏷 Введите серийный номер оборудования (или его часть).", get_back_to_warehouse_keyboard())


async def handle_status_search(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    query = (message.text or "").strip()
    if not query:
        await base.send(peer_id, "Введите номер.")
        return
    found = await _api(peer_id, api_client.wh_find(user["vk_id"], query))
    if found is None:
        return
    session["step"] = "menu"
    if not found:
        session["step"] = "wh_awaiting_status_serial"
        await base.send(peer_id, f"«{query}» не найдено. Введите другой номер.", get_back_to_warehouse_keyboard())
        return
    st = _wh(session)
    st["found"] = found
    if len(found) == 1:
        await show_item(peer_id, user, session, found[0]["id"])
        return
    await views.show("wh_found", peer_id, user, session)


@views.register("wh_found")
async def _view_found(peer_id: int, user: dict, session: dict, page: int) -> None:
    entries = [(f"№{i['serial_number']} · {i['model_name']} · {i['status'] or '—'}", {"action": "wh_st_i", "i": i["id"]}) for i in _wh(session).get("found", [])]
    keyboard, _, _ = paged_keyboard(entries, page, [[("🔎 Искать другой", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]])
    await base.send(peer_id, "Нашёл несколько. Какое?", keyboard)


def _where(it: dict) -> str:
    if it["place"] == "unknown":
        return "местонахождение неизвестно" + (f" (числилось на «{it['removed_point_id']}»)" if it.get("removed_point_id") else "")
    if it["place"] == "person":
        return f"у {it['person_name']}" + {"removed": " (снято, ждёт приёмки)", "kept": " (снятая SIM осталась у монтёра)"}.get(it["held_reason"], " (выдано)")
    if it["place"] == "point":
        return f"на комплексе «{it['point_id']}»"
    return "на складе"


async def show_item(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _wh(session)
    item = next((i for i in st.get("found", []) if i["id"] == item_id), None)
    if not item:
        await request_status_search(peer_id, user, session)
        return
    st["item"] = item
    lines = [f"№{item['serial_number']} — {item['category_name']} · {item['model_name']}",
             f"Где: {_where(item)}", f"Состояние: {item['status'] or '—'}"]
    if item.get("removed_by") or item.get("removed_point_id"):
        lines.append(f"Снято: {item.get('removed_by') or '—'}" + (f", с «{item['removed_point_id']}»" if item.get("removed_point_id") else "") + (f", {item['removed_date']}" if item.get("removed_date") else ""))
    if item.get("removal_reason"):
        lines.append(f"Причина снятия: {item['removal_reason']}")
    if item.get("accepted_by"):
        lines.append(f"Принял на склад: {item['accepted_by']}")
    st["card"] = lines
    if item["place"] == "unknown":
        lines.append("\nОно числилось в другом месте, но там оказалось другое оборудование. Если нашлось — верните его на склад.")
        await base.send(
            peer_id, "\n".join(lines),
            simple_keyboard([[("📥 Нашлось — принять на склад", {"action": "wh_found"})], [("🔎 Искать другой", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]]),
        )
        return
    if item["place"] != "stock":
        lines.append("\nСостояние меняется у оборудования на складе." + (" Сначала примите его от монтёра («Принять от монтёра»)." if item["place"] == "person" else " Сначала его нужно снять с комплекса."))
        await base.send(peer_id, "\n".join(lines), simple_keyboard([[("🔎 Искать другой", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]]))
        return
    statuses = await _api(peer_id, api_client.wh_statuses(user["vk_id"]))
    if statuses is None:
        return
    st["statuses"] = statuses
    await views.show("wh_item_status", peer_id, user, session)


@views.register("wh_item_status")
async def _view_item_status(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _wh(session)
    item = st.get("item", {})
    lines = [*st.get("card", []), "", "Какое состояние поставить?"]
    entries = [(("✅ " if s["usable"] else "⛔ ") + s["name"], {"action": "wh_st_s", "s": s["id"]}) for s in st.get("statuses", []) if s["id"] != item.get("status_id")]
    keyboard, _, _ = paged_keyboard(entries, page, [[("🔎 Искать другой", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]])
    await base.send(peer_id, "\n".join(lines) + "\n(✅ годно к выдаче, ⛔ не выдаётся)", keyboard)


async def set_item_status(peer_id: int, user: dict, session: dict, status_id: int) -> None:
    if not await _guard(peer_id, user):
        return
    st = _wh(session)
    item = st.get("item")
    if not item:
        await request_status_search(peer_id, user, session)
        return
    res = await _api(peer_id, api_client.wh_set_status(user["vk_id"], item["id"], status_id))
    if res is None:
        return
    _wh(session).clear()
    await base.send(
        peer_id, f"✅ №{res['serial_number']}: состояние «{res['status']}».",
        simple_keyboard([[("🏷 Ещё одно", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]]),
    )


# ------------------------------------------------------------------ остатки

async def show_stock(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    data = await _api(peer_id, api_client.wh_summary(user["vk_id"]))
    if data is None:
        return
    lines = ["📊 Остатки на складе:"]
    for row in data["stock"]:
        lines.append(f"• {row['category']}: годных {row['usable']}" + (f", негодных {row['unusable']}" if row["unusable"] else ""))
    if not data["stock"]:
        lines.append("склад пуст")
    if data.get("unknown"):
        lines.append(f"\n❓ Неизвестное местонахождение: {data['unknown']} ед. (найдите по номеру в «Состояние оборудования»)")
    waiting = [p for p in data["awaiting"] if p["removed"] or p["issued"]]
    if waiting:
        lines.append("\nЧислится на монтёрах:")
        for p in waiting:
            parts = ([f"снято {p['removed']}"] if p["removed"] else []) + ([f"выдано {p['issued']}"] if p["issued"] else [])
            lines.append(f"• {p['full_name']}: {', '.join(parts)}")
    await base.send(peer_id, "\n".join(lines), get_back_to_warehouse_keyboard())


# ------------------------------------------------------------------ сдан другой номер

async def start_other(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    st = _wh(session)
    if not st.get("items"):
        await show_accept(peer_id, user, session)
        return
    await views.show("wh_other_pick", peer_id, user, session)


@views.register("wh_other_pick")
async def _view_other_pick(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _wh(session)
    entries = [(f"№{i['serial_number']} {i['category_name']}", {"action": "wh_oth_i", "i": i["id"]}) for i in st.get("items", [])]
    keyboard, _, _ = paged_keyboard(entries, page, [[("⬅ Назад", {"action": "wh_accp", "p": st.get("person_id", 0)})]])
    await base.send(peer_id, f"🔁 По системе на «{st.get('person_name')}» числится это. Какой номер он на самом деле сдал не тот?", keyboard)


async def choose_other_item(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _wh(session)
    item = next((i for i in st.get("items", []) if i["id"] == item_id), None)
    if not item:
        await show_accept(peer_id, user, session)
        return
    st["sub_expected"] = item
    st.pop("sub_actual", None)
    session["step"] = "wh_awaiting_actual"
    await base.send(
        peer_id,
        f"По системе на «{st.get('person_name')}»: №{item['serial_number']} ({item['category_name']} · {item['model_name']}).\n"
        "Введите номер, который он сдал на самом деле (можно часть номера).",
        simple_keyboard([[("⬅ Назад", {"action": "wh_accp", "p": st.get("person_id", 0)})]]),
    )


async def handle_actual_search(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    st = _wh(session)
    q = (message.text or "").strip()
    expected = st.get("sub_expected")
    if not q or not expected:
        await base.send(peer_id, "Введите номер.")
        return
    found = await _api(peer_id, api_client.wh_find(user["vk_id"], q))
    if found is None:
        return
    session["step"] = "menu"
    st["sub_try"] = q
    st["sub_results"] = [i for i in found if i["id"] != expected["id"]]
    await views.show("wh_actual", peer_id, user, session)


@views.register("wh_actual")
async def _view_actual(peer_id: int, user: dict, session: dict, page: int) -> None:
    st = _wh(session)
    expected = st.get("sub_expected") or {}
    results = st.get("sub_results", [])
    entries = [(f"№{i['serial_number']} · {i['model_name']} · {_where(i)}", {"action": "wh_actpick", "i": i["id"]}) for i in results]
    head = (f"Нашёл по «{st.get('sub_try')}». Какой номер сдан?" if results else f"Номера «{st.get('sub_try')}» в базе нет.")
    controls = [[(f"➕ Записать «{st.get('sub_try')}» как новое", {"action": "wh_actnew"})],
                [("🔍 Другой номер", {"action": "wh_oth_i", "i": expected.get("id", 0)}), ("⬅ Назад", {"action": "wh_accp", "p": st.get("person_id", 0)})]]
    keyboard, _, _ = paged_keyboard(entries, page, controls)
    await base.send(peer_id, head, keyboard)


async def _confirm_substitute(peer_id: int, session: dict) -> None:
    st = _wh(session)
    expected, actual = st["sub_expected"], st["sub_actual"]
    who = (f"№{actual['item']['serial_number']} ({actual['item']['category_name']} · {actual['item']['model_name']})"
           if actual.get("item") else f"№{actual['serial']} (новый номер — будет записан в базу)")
    await base.send(
        peer_id,
        f"Принять на склад {who} вместо №{expected['serial_number']}?\n"
        f"№{expected['serial_number']} получит пометку «неизвестное местонахождение».",
        simple_keyboard([[("✅ Подтвердить", {"action": "wh_subdo"}), ("❌ Отмена", {"action": "wh_accp", "p": st.get("person_id", 0)})]]),
    )


async def pick_actual(peer_id: int, user: dict, session: dict, item_id: int) -> None:
    st = _wh(session)
    item = next((i for i in st.get("sub_results", []) if i["id"] == item_id), None)
    if not item or not st.get("sub_expected"):
        await show_accept(peer_id, user, session)
        return
    st["sub_actual"] = {"item": item}
    await _confirm_substitute(peer_id, session)


async def new_actual(peer_id: int, user: dict, session: dict) -> None:
    st = _wh(session)
    if not st.get("sub_try") or not st.get("sub_expected"):
        await show_accept(peer_id, user, session)
        return
    st["sub_actual"] = {"serial": st["sub_try"]}
    await _confirm_substitute(peer_id, session)


async def do_substitute(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    st = _wh(session)
    expected, actual = st.get("sub_expected"), st.get("sub_actual")
    if not expected or not actual:
        await show_accept(peer_id, user, session)
        return
    payload = {"actual_equipment_id": actual["item"]["id"]} if actual.get("item") else {"actual_serial": actual["serial"], "create_missing": True}
    res = await _api(peer_id, api_client.wh_accept_substitute(user["vk_id"], expected["id"], st.get("person_id"), payload))
    if res is None:
        return
    person_id = st.get("person_id", 0)
    st.pop("sub_expected", None)
    st.pop("sub_actual", None)
    await base.send(
        peer_id,
        f"✅ Принято на склад №{res['accepted']}" + (" как дефектное (снято монтёром)." if res.get("removed") else ".")
        + f"\n❓ №{res['unknown']} помечен как «неизвестное местонахождение».",
        simple_keyboard([[("⬅ К списку монтёра", {"action": "wh_accp", "p": person_id})], [("🏠 Меню склада", {"action": "main_menu"})]]),
    )


async def mark_found(peer_id: int, user: dict, session: dict) -> None:
    if not await _guard(peer_id, user):
        return
    st = _wh(session)
    item = st.get("item")
    if not item:
        await request_status_search(peer_id, user, session)
        return
    res = await _api(peer_id, api_client.wh_found(user["vk_id"], item["id"]))
    if res is None:
        return
    st.clear()
    await base.send(
        peer_id, f"✅ №{res['serial_number']} найдено и принято на склад.",
        simple_keyboard([[("🏷 Ещё одно", {"action": "wh_status"})], [("🏠 Меню склада", {"action": "main_menu"})]]),
    )
