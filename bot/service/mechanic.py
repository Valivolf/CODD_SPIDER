"""Вся логика сценария монтёра — регистрация обработчиков в
handlers/mechanic.py, здесь только бизнес-логика.

После старта задания у каждого типа работ свободное меню — монтёр сам решает
порядок действий, а «Завершить» собирает итог из того, что накопилось в
сессии (демонтаж оформляет диспетчер в веб-панели, не бот):
- Тип СВЯЗАН С РЕМОНТОМ (Настройки → Типы выездов: ремонт, установка,
  подключение кабеля, демонтаж…): меню ремонта — фото / отчёт / замена
  оборудования (в т.ч. SIM-карты) / нет питания / проверка доступности /
  номер диспетчера / завершить.
- Остальные типы (инвентаризация, опиловка, юстировка…): фото / отчёт /
  завершить и «Перейти в режим ремонта» — если на точке нашлась
  неисправность или её решили восстановить; тогда меню становится меню
  ремонта, а визит в отчётах и выгрузках пишется «<тип> + Ремонт».
См. api_client.py — источник всех данных.

Что уже сделано по комплексу (текст отчёта, замена оборудования/SIM, итоги проверок) хранится на сервере и
общее у монтёров бригады: с комплекса можно выйти без завершения («🚪 Выйти») и зайти позже — работа продолжится,
фото и отчёт на месте; в списке заданий взятый комплекс помечен 🔧, в карточке видно, сколько фото загружено,
написан ли отчёт и кто ещё на комплексе. Пока бот ждёт фото или текст, показывается отдельное меню ожидания
(«Готово»/«Отмена», «Сохранить»/«Отмена») и остальные кнопки не работают — см. handlers/mechanic.py."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import random
import time
import uuid

import httpx
from vkbottle import API
from vkbottle.bot import Message

from bot import api_client
from bot.names import matches_query
from bot.keyboards.mechanic_menu import (
    RESULT_STATUSES,
    get_aux_task_keyboard,
    get_back_to_menu_keyboard,
    get_chat_keyboard,
    get_chat_notice_keyboard,
    get_dispatcher_keyboard,
    get_main_menu_keyboard,
    get_no_access_decision_keyboard,
    get_unavailable_reason_keyboard,
    get_draft_confirm_keyboard,
    get_draft_input_keyboard,
    get_draft_keep_keyboard,
    get_equipment_menu_keyboard,
    get_outcome_keyboard,
    REMOVAL_REASONS,
    get_photo_panel_keyboard,
    get_photos_keyboard,
    get_reason_keyboard,
    get_report_panel_keyboard,
    get_search_cancel_keyboard,
    get_sim_state_keyboard,
    get_waiting_keyboard,
    get_power_source_keyboard,
    get_repair_menu_keyboard,
    get_result_status_keyboard,
    get_sim_confirm_keyboard,
    get_simple_menu_keyboard,
    get_task_list_keyboard,
    get_task_opened_keyboard,
    task_page_size,
)
from bot.keyboards.equipment_menu import get_warehouse_menu_keyboard
from bot.service import navigation

logger = logging.getLogger(__name__)

# Итоговый статус комплекса по НАЗВАНИЮ типа задания, когда монтёр завершает в
# режиме ремонта (любой другой тип — «Работает» после подтверждённой проверки).
FINAL_STATUS_BY_TYPE = {"Установка": "Установлен", "Демонтаж": "Демонтирован"}
# Задание типа «Демонтаж» сразу считается демонтажом — проверять доступность
# комплекса, который снимают, незачем.
DEMONTAGE_TYPE = "Демонтаж"
POWER_STATUSES = {"no_power": "Нет питания", "not_connected": "Не подведено питание"}
# Зеркалит app/services/point_health_service.py — WORKING/PING_ONLY/NOT_WORKING.
STATUS_WORKING = "Работает"
STATUS_PING_ONLY = "Есть только пинг"

_api: API | None = None


def get_api() -> API:
    global _api
    if _api is None:
        _api = API(os.getenv("VK_TOKEN"))
    return _api


async def send(peer_id: int, text: str, keyboard=None, attachment: str | None = None) -> None:
    api = get_api()
    await api.messages.send(peer_id=peer_id, message=text, keyboard=keyboard, attachment=attachment, random_id=random.randint(1, 2**31))


def _menu(session: dict):
    """Меню текущего задания: у связанного с ремонтом типа (или после «Перейти
    в режим ремонта») — меню ремонта, иначе простое меню. Кнопок «Нет питания» / «Не подведено питание» /
    «Расстрелян» в меню ремонта нет: их предлагает «Завершить», когда комплекс недоступен (repair_finish_anyway).
    После «⛔ Доступа нет.» в меню появляется «Замена SIM-карты», а решение, что делать дальше (отпустить без ремонта /
    демонтаж), принимает диспетчер в веб-панели. На кнопках видно, сколько фото
    загружено и написан ли отчёт."""
    photo_count = int(session.get("photo_count") or 0)
    has_report = bool((session.get("report_text") or "").strip())
    if not session.get("repair_mode"):
        return get_simple_menu_keyboard(photo_count, has_report, bool(session.get("type_tools")))
    no_access = bool(session.get("no_access"))
    demontage = None
    if session.get("demontage_completed"):
        demontage = "done" if session.get("demontage_scope") else "todo"
    return get_repair_menu_keyboard(
        show_sim_button=no_access, photo_count=photo_count, has_report=has_report,
        can_undo_repair=_can_undo_repair(session) and not demontage, demontage=demontage,
    )


def _repair_footprint(session: dict) -> bool:
    """Монтёр уже сделал что-то, что имеет смысл только в ремонте (замена оборудования/SIM, статус питания)."""
    return bool(
        session.get("equipment_removed") or session.get("equipment_installed") or session.get("sim_change")
        or session.get("power_status") or session.get("shot_text")
    )


def _can_undo_repair(session: dict) -> bool:
    return bool(session.get("switched_to_repair")) and not _repair_footprint(session)


def _today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- прогресс по комплексу (общий, на сервере) ----
# См. app/repositories/bot_task_progress_repository.py. В сессии бота эти ключи лежат плоско
# (session["report_text"] …); на сервер уходит только то, что изменилось, а чужие изменения (второй монтёр
# бригады) подтягиваются обратно — persist_progress / _adopt_server.
STATE_DEFAULTS: dict = {
    "report_text": None, "equipment_installed": None, "equipment_removed": None, "power_status": None,
    "work_type": None, "point_id": None, "sim_change": None, "pending": None,
    "check_confirmed": False, "demontage_completed": False, "no_access": False, "repair_mode": False,
    "switched_to_repair": False, "sim_asked": False, "demontage_scope": None, "demontage_note": None,
    "fault_text": None, "needs_text": None, "shot_text": None, "consumables": [],
    # shot_text — что расстреляно (комплекс расстреляли: «💥 Комплекс расстрелян» в «Замене оборудования»)
}
# Ключи сессии, которые принадлежат открытому комплексу и очищаются при выходе с него.
TASK_SESSION_KEYS = (
    "task_id", "work_session_id", "photo_count", "comment", "pending_action", "draft", "photo_batch", "photo_batches", "report_chunks", "_photo_mut", "_photo_busy", "_saved",
    "watch_token", "type_outcomes", "_eq_installed_tmp", "_consumables_catalog", "reason_ctx", "dem", "after_fault", "chat_prev_step", "chat_acked",
    "finish_after_check", "type_tools", "finish_reason_pick", "consumables_item_id", "consumables_editing", "consumables_available",
    *STATE_DEFAULTS,
)
# Шаги, где бот ждёт ввод от монтёра: пока они идут, кнопки меню задания не срабатывают.
INPUT_STEPS = ("awaiting_text", "awaiting_photos", "awaiting_reason", "awaiting_sim_state", "chat")


def _norm_state_value(key: str, value):
    default = STATE_DEFAULTS[key]
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, list):
        return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    if key in ("sim_change", "pending", "demontage_scope"):
        return dict(value) if value else None
    return value if value not in (None, "") else None


def _state_of(session: dict) -> dict:
    return {k: _norm_state_value(k, session.get(k)) for k in STATE_DEFAULTS}


def _apply_state(session: dict, state: dict | None) -> None:
    """Заполняет сессию сохранённым состоянием комплекса (чего нет — значения по умолчанию)."""
    for key in STATE_DEFAULTS:
        session[key] = _norm_state_value(key, (state or {}).get(key))
    session["_saved"] = _state_of(session)


def _adopt_server(session: dict, prog: dict) -> None:
    """Подтягивает изменения коллеги: поле, которое на сервере отличается от того, что мы видели в последний раз."""
    saved = session.setdefault("_saved", {})
    server = prog.get("state") or {}
    for key in STATE_DEFAULTS:
        value = _norm_state_value(key, server.get(key))
        if value != _norm_state_value(key, saved.get(key)):
            session[key] = value
            saved[key] = value
    if "photo_count" in prog:
        session["photo_count"] = prog["photo_count"]


async def persist_progress(session: dict, force_pull: bool = False) -> dict | None:
    """Записывает на сервер то, что изменилось в состоянии комплекса, и забирает изменения коллеги. Без
    изменений ничего не отправляет (при force_pull — только читает). Ошибка связи не ломает диалог."""
    task_id, vk_id = session.get("task_id"), session.get("vk_id")
    if not task_id or not vk_id:
        return None
    current = _state_of(session)
    saved = session.get("_saved") or {}
    changes = {k: v for k, v in current.items() if _norm_state_value(k, saved.get(k)) != v}
    try:
        if changes:
            prog = await api_client.patch_task_progress(task_id, vk_id, changes)
        elif force_pull:
            prog = await api_client.get_task_progress(task_id, vk_id)
        else:
            return None
    except api_client.ApiError:
        logger.exception("Не удалось сохранить/получить прогресс по заданию %s", task_id)
        return None
    session["_saved"] = {**saved, **current}
    _adopt_server(session, prog)
    return prog


async def after_action(user: dict) -> None:
    """После каждой кнопки/сообщения: если монтёр на комплексе — записать изменения прогресса на сервер."""
    session = navigation.get_session(user["vk_id"])
    if session.get("task_id"):
        await persist_progress(session)


def _live_step(s: dict, step: str = "menu") -> None:
    """Ответ диспетчера пришёл, пока монтёр на комплексе: переводим в нужный шаг — но чат с диспетчером не прерываем."""
    if s.get("step") == "chat":
        s["chat_prev_step"] = step
    else:
        s["step"] = step


def _live_kb(s: dict):
    return get_chat_keyboard() if s.get("step") == "chat" else _menu(s)


def _live_session(vk_id: int, task_id: str) -> dict | None:
    """Сессия монтёра, если он сейчас на этом комплексе (иначе — он вышел или перешёл на другой)."""
    session = navigation.get_session(vk_id)
    return session if session.get("task_id") == task_id else None


async def _detached_session(user: dict, task_id: str) -> dict | None:
    """Состояние комплекса из прогресса на сервере — когда ответ диспетчера пришёл, а монтёр уже вышел с комплекса.
    Это тот же набор ключей, что и в живой сессии, поэтому с ним работают те же функции."""
    try:
        prog = await api_client.get_task_progress(task_id, user["vk_id"])
    except api_client.ApiError:
        return None
    if prog.get("finished"):
        return None
    detached = {"task_id": task_id, "vk_id": user["vk_id"], "detached": True, "photo_count": prog.get("photo_count", 0)}
    _apply_state(detached, prog.get("state"))
    return detached


_RUNNING_POLLERS: set = set()


def _spawn_poller(key: tuple, coro) -> None:
    """Фоновый опрос ответа диспетчера — один на запрос (повторный вход на комплекс не плодит дубли)."""
    if key in _RUNNING_POLLERS:
        coro.close()
        return
    _RUNNING_POLLERS.add(key)

    async def runner():
        try:
            await coro
        except Exception:  # noqa: BLE001
            logger.exception("Фоновый опрос запроса %s завершился с ошибкой", key)
        finally:
            _RUNNING_POLLERS.discard(key)

    asyncio.create_task(runner())


def _progress_line(photo_count, has_report: bool) -> str:
    return f"📷 Фото: {photo_count or 'нет'} · 📝 Отчёт: {'написан' if has_report else 'не написан'}"


async def show_main_menu(peer_id: int, user: dict) -> None:
    session = navigation.get_session(user["vk_id"])
    await leave_current_task(session)   # из меню — это выход с комплекса без завершения: прогресс сохраняется
    navigation.reset_session(user["vk_id"])
    if user.get("role_type") == "warehouse":
        # Ответственный за склад: у него нет заданий, вместо них — приём/выдача оборудования.
        text = f"👋 Привет, {user['full_name']}!\nВы — ответственный за склад. Выберите действие:"
        await send(peer_id, text, get_warehouse_menu_keyboard())
        return
    text = f"👋 Привет, {user['full_name']}!\nВыберите действие:"
    await send(peer_id, text, get_main_menu_keyboard())


async def show_profile(peer_id: int, user: dict) -> None:
    role_names = {"fitter": "Монтёр", "driver": "Водитель", "operator": "Оператор", "warehouse": "Склад"}
    text = (
        f"👤 {user['full_name']}\n"
        f"Роль: {role_names.get(user['role_type'], user['role_type'])}\n"
        f"Телефон: {user.get('phone') or '—'}\n"
        f"VK ID: {user['vk_id']}"
    )
    await send(peer_id, text, get_back_to_menu_keyboard())


async def show_my_tasks(peer_id: int, user: dict) -> None:
    vk_id = user["vk_id"]
    session = navigation.get_session(vk_id)
    await leave_current_task(session)   # «К списку» с открытого комплекса — выход без завершения
    tasks = await api_client.list_tasks(vk_id, _today())
    session.update(tasks={t["id"]: t for t in tasks}, tasks_page=0, tasks_filter="", peer_id=peer_id, vk_id=vk_id, step=None)
    await render_task_list(peer_id, session)


def _filtered_tasks(session: dict) -> list[dict]:
    tasks = list((session.get("tasks") or {}).values())
    q = (session.get("tasks_filter") or "").strip()
    if q:
        # Номер набирают по-разному («ARENA 0901205», «arena0901205», «то 5», русскими/латинскими буквами) — ищем «без разбора» по номеру и адресу комплекса.
        by_point = [t for t in tasks if matches_query(q, t.get("point"))]
        # по номеру ничего не нашлось — смотрим ещё и адрес («ленина 30»); так короткий номер не тянет за собой все адреса с такой цифрой
        tasks = by_point or [t for t in tasks if matches_query(q, t.get("point"), t.get("point_address") or t.get("address"))]
    return tasks


def _crew_map_links(session: dict) -> list[str]:
    """Уникальные непустые ссылки/заметки бригад (map_link) среди ВСЕХ
    заданий на сегодня (не только текущей страницы/фильтра поиска) — обычно
    монтёр состоит в одной бригаде на день, но на случай нескольких
    показываем с подписью, к какой бригаде относится какая ссылка."""
    seen: dict[str, str] = {}
    for t in (session.get("tasks") or {}).values():
        link = (t.get("map_link") or "").strip()
        if link and link not in seen:
            seen[link] = t.get("crew_name") or "бригада"
    return [f"🗺 {crew_name}: {link}" for link, crew_name in seen.items()]


async def render_task_list(peer_id: int, session: dict) -> None:
    """Показывает текущую страницу списка заданий (с учётом активного
    поиска по номеру, если есть) — общая точка выхода после открытия
    списка, листания страниц и поиска/сброса поиска."""
    tasks_filter = (session.get("tasks_filter") or "").strip()
    tasks = _filtered_tasks(session)
    map_links = _crew_map_links(session)
    map_lines = ("\n" + "\n".join(map_links) + "\n") if map_links else ""
    if not tasks:
        text = f"Ничего не найдено по номеру «{tasks_filter}»." if tasks_filter else "На сегодня заданий нет."
        await send(peer_id, map_lines + text, get_task_list_keyboard([], False, False, bool(tasks_filter)))
        return

    page_size = task_page_size(len(tasks), bool(tasks_filter))
    total_pages = (len(tasks) - 1) // page_size + 1
    page = max(0, min(session.get("tasks_page", 0), total_pages - 1))
    session["tasks_page"] = page
    page_tasks = tasks[page * page_size: (page + 1) * page_size]

    header = f"Задания на сегодня ({len(tasks)})"
    if tasks_filter:
        header += f" · поиск «{tasks_filter}»"
    if total_pages > 1:
        header += f"\nСтраница {page + 1} из {total_pages}"
    await send(
        peer_id, map_lines + header + ":",
        get_task_list_keyboard(page_tasks, page > 0, page < total_pages - 1, bool(tasks_filter)),
    )


async def change_tasks_page(peer_id: int, session: dict, direction: str) -> None:
    session["tasks_page"] = session.get("tasks_page", 0) + (1 if direction == "next" else -1)
    await render_task_list(peer_id, session)


async def request_task_search(peer_id: int, session: dict) -> None:
    session["step"] = "awaiting_task_search"
    await send(peer_id, "Введите номер комплекса (можно часть номера, с дефисом или без, любыми буквами; можно и адрес).", get_search_cancel_keyboard())


async def cancel_task_search(peer_id: int, session: dict) -> None:
    session["step"] = None
    await render_task_list(peer_id, session)


async def clear_task_search(peer_id: int, session: dict) -> None:
    session["tasks_filter"] = ""
    session["tasks_page"] = 0
    await render_task_list(peer_id, session)


async def handle_task_search_text(message: Message, session: dict) -> None:
    text = (message.text or "").strip()
    peer_id = session.get("peer_id", message.peer_id)
    session["tasks_filter"] = text
    session["tasks_page"] = 0
    session["step"] = None
    await render_task_list(peer_id, session)


def _aux_task_text(t: dict) -> str:
    lines = [f"ℹ️ Вспомогательная точка: {t.get('point') or '—'}"]
    if t.get("work"):
        lines.append(f"📝 {t['work']}")
    if t.get("aux_lat") and t.get("aux_lon"):
        lines.append(f"🧭 Координаты: {t['aux_lat']}, {t['aux_lon']}")
    lines.append("Это метка с информацией для бригады — работы здесь нет. Нажмите «Принято», когда ознакомились.")
    return "\n".join(lines)


def _task_text(t: dict) -> str:
    lines = [f"📍 {t.get('point') or '—'}", f"🏷 Тип: {t.get('type') or '—'}"]
    address = t.get("point_address") or t.get("address")
    if address:
        lines.append(f"🏠 {address}")
    lines.append(f"💳 SIM: {t.get('point_sim') or 'не указана'}")
    lines.append(f"⚡ Питание: {t.get('point_power_source') or 'не указано'}")
    if t.get("work"):
        lines.append(f"📝 {t['work']}")
    return "\n".join(lines)


async def show_task(peer_id: int, user: dict, task_id: str) -> None:
    session = navigation.get_session(user["vk_id"])
    task = (session.get("tasks") or {}).get(task_id)
    if not task:
        await send(peer_id, "Задание не найдено — откройте список заданий заново.", get_back_to_menu_keyboard())
        return
    if task.get("is_aux"):
        await send(peer_id, _aux_task_text(task), get_aux_task_keyboard(task_id))
        return
    prog = None
    try:
        prog = await api_client.get_task_progress(task_id, user["vk_id"])
    except api_client.ApiError:
        prog = None
    if prog and prog.get("finished"):
        (session.get("tasks") or {}).pop(task_id, None)
        await send(peer_id, f"✅ По комплексу «{task.get('point') or '—'}» отчёт уже сдан — задание закрыто.")
        await render_task_list(peer_id, session)
        return
    text = _task_text(task)
    resume = False
    if prog:
        others = [w["name"] for w in prog.get("workers", []) if not w.get("me")]
        resume = bool(prog.get("state") or prog.get("photo_count") or prog.get("workers"))
        if resume:
            text += "\n\n" + _progress_line(prog.get("photo_count"), prog.get("has_report"))
            if others:
                text += f"\n👷 Сейчас на комплексе: {', '.join(others)} — вы будете работать вместе, фото и отчёт общие."
            else:
                text += "\n⏸ Работа уже начата — можно продолжить с того же места."
    await send(peer_id, text, get_task_opened_keyboard(task_id, resume))


async def accept_aux_task(peer_id: int, user: dict, task_id: str) -> None:
    """«Принято» у вспомогательной точки: точка выполнена (зелёная на карте бригады), в статистику не идёт.
    После этого — снова список комплексов."""
    session = navigation.get_session(user["vk_id"])
    task = (session.get("tasks") or {}).get(task_id)
    if not task or not task.get("is_aux"):
        await send(peer_id, "Точка не найдена — откройте список заданий заново.", get_back_to_menu_keyboard())
        return
    try:
        await api_client.accept_aux_task(task_id, user["vk_id"])
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось отметить точку: {e.detail}", get_aux_task_keyboard(task_id))
        return
    (session.get("tasks") or {}).pop(task_id, None)
    await send(peer_id, f"✅ Принято: «{task.get('point') or 'точка'}». На карте бригады она станет зелёной.")
    await render_task_list(peer_id, session)


async def start_task(peer_id: int, user: dict, task_id: str) -> None:
    vk_id = user["vk_id"]
    session = navigation.get_session(vk_id)
    task = (session.get("tasks") or {}).get(task_id)
    if not task:
        await send(peer_id, "Задание не найдено — откройте список заданий заново.", get_back_to_menu_keyboard())
        return
    if session.get("task_id") and session.get("task_id") != task_id:
        await leave_current_task(session)   # перешли на другой комплекс — прежний остаётся в работе, прогресс сохранён
    await _enter_task(peer_id, user, session, task, restored=False)


async def _enter_task(peer_id: int, user: dict, session: dict, task: dict, restored: bool) -> None:
    """Вход на комплекс (первый или повторный): рабочая сессия, общий прогресс с сервера, фоновое наблюдение."""
    vk_id = user["vk_id"]
    task_id = task["id"]
    try:
        work_session = await api_client.start_work_session(task_id, vk_id)
        prog = await api_client.get_task_progress(task_id, vk_id)
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось начать работу: {e.detail}", get_back_to_menu_keyboard())
        return
    if prog.get("finished"):
        (session.get("tasks") or {}).pop(task_id, None)
        try:
            await api_client.end_work_session(work_session["id"])
        except api_client.ApiError:
            pass
        await send(peer_id, f"✅ По комплексу «{task.get('point') or '—'}» отчёт уже сдан — задание закрыто.")
        await render_task_list(peer_id, session)
        return

    work_type = task.get("type") or ""
    server_state = dict(prog.get("state") or {})
    resumed = bool(server_state or prog.get("photo_count") or work_session.get("resumed"))
    session.update(
        task_id=task_id, vk_id=vk_id, work_session_id=work_session["id"], peer_id=peer_id,
        photo_count=prog.get("photo_count", 0), comment=None, pending_action=None, draft=None, photo_batch=[],
        type_outcomes=list(task.get("type_outcomes") or []), type_tools=bool(task.get("type_tools")),
        watch_token=uuid.uuid4().hex, step="menu",
    )
    _apply_state(session, server_state)
    # Чего на сервере ещё нет — берём из задания; это же и «пометка, что комплекс взят» (🔧 в списке).
    session["point_id"] = session.get("point_id") or task["point"]
    session["work_type"] = session.get("work_type") or work_type
    if "repair_mode" not in server_state:
        session["repair_mode"] = bool(task.get("type_is_repair"))
    if "demontage_completed" not in server_state:
        session["demontage_completed"] = work_type == DEMONTAGE_TYPE
    await persist_progress(session)

    others = [w["name"] for w in prog.get("workers", []) if not w.get("me")]
    head = ("🔄 Продолжаем работу" if (restored or resumed) else "▶️ Начали работу") + f" по «{task.get('point')}»."
    lines = [head]
    if resumed:
        lines.append(_progress_line(session.get("photo_count"), bool((session.get("report_text") or "").strip())))
    if others:
        lines.append(f"👷 Также на комплексе: {', '.join(others)} — фото и отчёт общие.")
    intro = "\n".join(lines)
    asyncio.create_task(_task_watcher(peer_id, user, session, task_id, work_session["id"], session["watch_token"]))
    if session.get("pending"):
        await send(peer_id, intro)
        await _resume_pending(peer_id, user, session)
        return
    if not task.get("point_power_source"):
        # Чем запитан комплекс, ещё не указано — спрашиваем сразу, двумя кнопками,
        # прежде чем показать меню задания.
        session["step"] = "awaiting_power_source"
        await send(peer_id, intro + "\n⚡ Питание этого комплекса не указано. Чем он запитан?", get_power_source_keyboard())
        return
    await _show_task_menu(peer_id, session, task, intro)


async def _show_task_menu(peer_id: int, session: dict, task: dict, intro: str | None = None) -> None:
    session["step"] = "menu"
    head = intro or f"▶️ Работа по «{task.get('point')}»."
    await send(
        peer_id,
        f"{head}\n💡 Фото и итог работ можно просто присылать сюда — приму без кнопок "
        "(поправить: «📷 Фото» и «📝 Отчёт» в меню).\nВыберите действие:",
        _menu(session),
    )


async def leave_current_task(session: dict) -> dict | None:
    """Выход с комплекса БЕЗ завершения: прогресс сохраняется на сервере, рабочая сессия закрывается (в «Сейчас в
    работе» монтёра больше нет), а при следующем входе работа продолжается с того же места. Возвращает сводку
    для сообщения или None, если монтёр ни на каком комплексе не был."""
    task_id = session.get("task_id")
    if not task_id:
        return None
    info = {
        "point": session.get("point_id"), "photo_count": session.get("photo_count", 0),
        "has_report": bool((session.get("report_text") or "").strip()),
    }
    await persist_progress(session)
    work_session_id = session.get("work_session_id")
    if work_session_id:
        try:
            await api_client.end_work_session(work_session_id)
        except api_client.ApiError:
            logger.warning("Не удалось закрыть рабочую сессию %s при выходе с комплекса", work_session_id)
    for key in TASK_SESSION_KEYS:
        session.pop(key, None)
    session["step"] = None
    return info


async def leave_task_action(peer_id: int, user: dict, session: dict) -> None:
    """Кнопка «🚪 Выйти»."""
    info = await leave_current_task(session)
    if info:
        await send(
            peer_id,
            f"🚪 Вышли с комплекса «{info['point'] or '—'}» без завершения. Сохранено: "
            f"{_progress_line(info['photo_count'], info['has_report'])}.\n"
            "Зайдите в него снова из списка — работа продолжится с того же места.",
        )
    await show_my_tasks(peer_id, user)


async def restore_active_task(peer_id: int, user: dict, session: dict) -> bool:
    """Бот перезапускали, а монтёр нажал кнопку в старом сообщении: по его открытой рабочей сессии на сервере
    возвращаем его на тот же комплекс (прогресс на месте). Возвращает True, если вернули."""
    vk_id = user["vk_id"]
    try:
        active = (await api_client.get_active_session(vk_id)).get("session")
        if not active:
            return False
        tasks = await api_client.list_tasks(vk_id, _today())
    except api_client.ApiError:
        return False
    session["tasks"] = {t["id"]: t for t in tasks}
    task = session["tasks"].get(active["task_id"])
    if not task or task.get("is_aux"):
        return False
    await _enter_task(peer_id, user, session, task, restored=True)
    return bool(session.get("task_id"))


async def set_power_source(peer_id: int, session: dict, value: str) -> None:
    """Ответ на «СЭС или Электросеть?» — сохраняется на комплексе, дальше меню."""
    if session.get("step") != "awaiting_power_source" or not session.get("task_id"):
        return
    try:
        await api_client.set_point_power_source(session["point_id"], value)
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось сохранить питание: {e.detail}", get_power_source_keyboard())
        return
    task = (session.get("tasks") or {}).get(session["task_id"])
    if task:
        task["point_power_source"] = value
    await send(peer_id, f"⚡ Питание комплекса: {value}. Записал.")
    await _show_task_menu(peer_id, session, task or {"point": session.get("point_id")})


_DOC_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}


class _DocSize:
    def __init__(self, url: str):
        self.url, self.width, self.height = url, 0, 0


class _DocPhoto:
    """Снимок, присланный «файлом» (без сжатия): у документа VK нет набора размеров, как у фото, — делаем похожий объект, чтобы дальше всё шло тем же путём."""

    def __init__(self, doc):
        self.id = f"d{doc.id}"
        self.owner_id = doc.owner_id
        self.sizes = [_DocSize(doc.url)]
        self.file_name = f"photo_doc_{doc.owner_id}_{doc.id}.{(doc.ext or 'jpg').lower()}"


def _image_docs(obj) -> list:
    docs = []
    for a in (getattr(obj, "attachments", None) or []):
        doc = getattr(a, "doc", None)
        if doc and (getattr(doc, "ext", "") or "").lower() in _DOC_IMAGE_EXT and getattr(doc, "url", None):
            docs.append(_DocPhoto(doc))
    return docs


def _photos_in(obj) -> list:
    """Фото из вложений сообщения (и вложенных пересланных/ответа) — работает
    и с событием Long Poll, и с «сырым» сообщением из messages.getBy…
    Снимок, отправленный файлом («без сжатия»), тоже принимается — раньше он молча пропускался."""
    photos = [a.photo for a in (getattr(obj, "attachments", None) or []) if getattr(a, "photo", None)] + _image_docs(obj)
    for fwd in getattr(obj, "fwd_messages", None) or []:
        photos.extend(_photos_in(fwd))
    reply = getattr(obj, "reply_message", None)
    if reply:
        photos.extend(_photos_in(reply))
    return photos


async def collect_photo_attachments(message) -> list:
    """Все фото сообщения, а не только первое.

    Когда монтёр отправляет сразу несколько фото, Long Poll отдаёт боту
    УРЕЗАННОЕ сообщение (is_cropped): в нём одно-два вложения, остальные
    отрезаны — раньше из-за этого сохранялась только первая фотография из
    пачки. Поэтому, если в событии есть фото, полное сообщение запрашивается
    у VK заново (messages.getByConversationMessageId) и фото берутся из него;
    дубли отсеиваются по (владелец, id). Не получилось запросить — остаётся
    то, что пришло в событии."""
    photos = _photos_in(message)
    if photos or getattr(message, "is_cropped", False):
        try:
            found = await get_api().messages.get_by_conversation_message_id(
                peer_id=message.peer_id,
                conversation_message_ids=[message.conversation_message_id],
            )
            if found.items:
                photos = _photos_in(found.items[0]) + photos
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось запросить полное сообщение с фото — беру то, что пришло в событии")
    unique, seen = [], set()
    for photo in photos:
        key = (getattr(photo, "owner_id", None), getattr(photo, "id", None))
        if key in seen:
            continue
        seen.add(key)
        unique.append(photo)
    return unique


# ---------------------------------------------------------------- голосовой итог (ИИ) ----
# Монтёр присылает голосовое на комплексе: бот скачивает запись, отдаёт серверу (расшифровка речи + разбор моделью на «что сделано /
# причина / что нужно», это минуты на процессоре), а результат — отдельным сообщением, когда готов. Работа в боте при этом не блокируется.
# Включается и выключается администратором («Настройки → ИИ»). Подробности — app/services/ai/report_parser.py.

VOICE_POLL_SECONDS = 3.0
VOICE_POLL_LIMIT = 20 * 60
VOICE_TEXT_OFF = "🎙 Голосовые сообщения пока не принимаются — напишите итог работ обычным сообщением, оно записывается без кнопок."


def voice_attachments(message) -> list:
    """Голосовые сообщения (audio_message) из вложений."""
    voices = [a.audio_message for a in (getattr(message, "attachments", None) or []) if getattr(a, "audio_message", None)]
    for fwd in getattr(message, "fwd_messages", None) or []:
        voices.extend(voice_attachments(fwd))
    return voices


def _merge_text(current: str | None, new: str) -> str:
    """Дописывает новый текст к прежнему, не повторяя то, что уже записано."""
    current = (current or "").strip()
    new = (new or "").strip()
    if not new:
        return current
    if not current:
        return new
    if new.casefold() in current.casefold():
        return current
    return f"{current}; {new}"


async def receive_voice(message: Message, user: dict, session: dict, voices: list) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    try:
        cfg = await api_client.ai_config()
    except Exception:  # noqa: BLE001 — сервер недоступен
        await send(peer_id, "⚠️ Сервер сейчас недоступен — голосовое не принято. Напишите итог текстом.", _menu(session))
        return
    if not cfg.get("bot_voice"):
        await send(peer_id, VOICE_TEXT_OFF, _menu(session) if session.get("task_id") else None)
        return
    if not session.get("task_id"):
        await send(peer_id, "🎙 Голосовой итог принимается на комплексе: откройте задание (📋 Мои задания) и пришлите голосовое там.")
        return
    if not cfg.get("asr_ready"):
        await send(peer_id, "🎙 Распознавание речи сейчас не работает — напишите итог текстом, он записывается без кнопок.", _menu(session))
        return
    voice = voices[0]
    url = getattr(voice, "link_mp3", None) or getattr(voice, "link_ogg", None)
    if not url:
        await send(peer_id, "⚠️ Не удалось получить запись — пришлите голосовое ещё раз или напишите итог текстом.", _menu(session))
        return
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        audio = resp.content
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось скачать голосовое из VK")
        await send(peer_id, "⚠️ Не удалось скачать запись — пришлите голосовое ещё раз.", _menu(session))
        return
    task_id = session["task_id"]
    visit_type = session.get("work_type") or ""
    try:
        job = await api_client.ai_voice(user["vk_id"], task_id, session.get("point_id") or "", visit_type, "voice.mp3" if getattr(voice, "link_mp3", None) else "voice.ogg", audio)
    except api_client.ApiError as e:
        await send(peer_id, f"⚠️ Голосовое не принято: {e.detail}. Напишите итог текстом.", _menu(session))
        return
    seconds = int(getattr(voice, "duration", 0) or 0)
    await send(
        peer_id,
        f"🎙 Принял голосовое{f' ({seconds} с)' if seconds else ''}. Расшифровываю и раскладываю по полям отчёта — пришлю результат отдельным сообщением "
        "(это занимает от минуты). Пока можно продолжать работу: фото, текст, меню.",
        _menu(session),
    )
    _spawn_poller(("voice", job["job_id"]), _voice_watch(peer_id, user, task_id, job["job_id"]))


async def _voice_watch(peer_id: int, user: dict, task_id: str, job_id: str) -> None:
    waited = 0.0
    errors = 0
    while waited < VOICE_POLL_LIMIT:
        await asyncio.sleep(VOICE_POLL_SECONDS)
        waited += VOICE_POLL_SECONDS
        try:
            job = await api_client.ai_voice_job(job_id, user["vk_id"])
            errors = 0
        except api_client.ApiError:
            errors += 1
            if errors > 5:
                break
            continue
        except Exception:  # noqa: BLE001
            continue
        if job.get("status") == "done":
            await _apply_voice_result(peer_id, user, task_id, job["result"])
            return
        if job.get("status") == "error":
            await send(peer_id, f"⚠️ Не удалось обработать голосовое: {job.get('error') or 'ошибка'}\nНапишите итог текстом — он записывается без кнопок.")
            return
    await send(peer_id, "⏳ Голосовое обрабатывается слишком долго — напишите итог текстом.")


async def _apply_voice_result(peer_id: int, user: dict, task_id: str, result: dict) -> None:
    """Результат разбора → итог работ, причина и «что нужно» этого выезда: в открытую сессию, а если монтёр уже вышел с комплекса — в сохранённый прогресс."""
    s = _live_session(user["vk_id"], task_id) or await _detached_session(user, task_id)
    transcript = (result.get("transcript") or "").strip()
    if not s:
        await send(peer_id, f"🎙 Расшифровка (задание уже закрыто, в отчёт не записана): «{transcript}»")
        return
    done = (result.get("done") or transcript).strip()
    cause = (result.get("cause") or "").strip()
    needs = (result.get("needs") or "").strip()
    live = not s.get("detached")
    current = (s.get("report_text") or "").strip()
    if done:
        s["report_text"] = (current + "\n" + done) if current else done
        chunks = s.setdefault("report_chunks", [])
        if not current:
            chunks.clear()
        chunks.append(done)
    if cause:
        s["fault_text"] = _merge_text(s.get("fault_text"), cause)
    if needs:
        s["needs_text"] = _merge_text(s.get("needs_text"), needs)
    lines = [f"🎙 Расшифровка: «{transcript}»"]
    if result.get("used_ai"):
        lines.append(f"📝 Что сделано: {done}")
        lines.append(f"⚠️ Причина: {cause}" if cause else "⚠️ Причина: не названа")
        if needs:
            lines.append(f"📦 Что нужно для восстановления: {needs}")
        fixed = result.get("fixed")
        if fixed in ("да", "нет"):
            lines.append("По вашим словам неисправность " + ("устранена." if fixed == "да" else "не устранена."))
        lines.append("\n✅ Записано в итог работ" + (", причина и «что нужно» подставятся в отчёт — при завершении их можно изменить." if (cause or needs) else "."))
    else:
        lines.append("\n✅ Записал расшифровку как итог работ (разобрать по полям не получилось — модель сейчас недоступна).")
    lines.append("Поправить — кнопка «📝 Отчёт».")
    point = s.get("point_id") or "комплексу"
    prefix = "" if live else f"По комплексу «{point}»:\n"
    await persist_progress(s)             # сначала в общий прогресс (его видит и второй монтёр бригады), потом сообщение
    if live:
        await send(peer_id, prefix + "\n".join(lines), _live_kb(s))
    else:
        await send(peer_id, prefix + "\n".join(lines))


async def handle_photo_message(message: Message, user: dict, session: dict, photos: list | None = None) -> None:
    """Принимает фото. Прежний режим фото (кнопки «Готово/Отмена» из старых сообщений); фото, присланные в меню, теперь принимает receive_photos — без кнопок: бот показывает
    «Готово/Отмена» и ждёт остальные — пока монтёр не нажмёт одну из кнопок, меню задания не появляется."""
    if photos is None:
        photos = await collect_photo_attachments(message)
    if not photos:
        return
    vk_id = user["vk_id"]
    peer_id = session.get("peer_id", message.peer_id)
    if session.get("step") != "awaiting_photos":
        session["photo_batch"] = []
    session["step"] = "awaiting_photos"
    saved_ids, failed, duplicates = await _upload_photos(session, vk_id, photos)
    session.setdefault("photo_batch", []).extend(saved_ids)
    uploaded = len(saved_ids)
    if uploaded:
        session["photo_count"] = session.get("photo_count", 0) + uploaded
    text = f"📷 Принято фото: {uploaded}" + (f" из {len(photos)}" if len(photos) > 1 else "") + "."
    text += f" В этот раз: {len(session.get('photo_batch') or [])} · всего по комплексу: {session.get('photo_count', 0)}."
    if duplicates:
        text += f"\nℹ️ Уже было принято раньше: {duplicates}."
    if failed:
        text += f"\n⚠️ Не удалось сохранить: {failed} — отправьте их ещё раз."
    text += "\nПришлите ещё или нажмите «Готово»."
    await send(peer_id, text, get_photos_keyboard())


def _photo_touch(session: dict) -> None:
    """Отмечает, что число фото в сессии менялось: ответ фонового опроса сервера, начатый раньше, число фото уже не перезапишет."""
    session["_photo_mut"] = session.get("_photo_mut", 0) + 1


def _photo_file_name(photo) -> str:
    """Имя файла по самому вложению VK (владелец + номер): по нему сервер узнаёт повторную отправку того же фото. Номер фото уникален только у одного владельца."""
    named = getattr(photo, "file_name", None)
    if named:
        return str(named)
    owner = getattr(photo, "owner_id", None)
    return f"photo_{owner}_{photo.id}.jpg" if owner is not None else f"photo_{photo.id}.jpg"


UPLOAD_ATTEMPTS = 3              # столько раз пробуем отправить фото на сервер, если связь оборвалась или сервер не успел ответить
UPLOAD_RETRY_PAUSE = 2.0         # пауза перед повтором (умножается на номер попытки), с


async def _send_photo_to_server(vk_id: int, task_id: str, filename: str, content: bytes) -> tuple[str, int | None]:
    """Отправляет одно фото на сервер с повторами: ('saved', id) | ('duplicate', id) — такое фото по заданию уже есть | ('failed', None).
    Раньше ожидание ответа было 15 секунд, а запись на сетевой диск бывает медленнее — фото терялось «молча»; теперь ждём долго, при обрыве повторяем,
    а повтор не создаёт копию (сервер узнаёт то же фото по имени файла). Любая ошибка связи — не повод ронять обработку всех остальных фото из пачки."""
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        try:
            saved = await api_client.upload_photo(vk_id, task_id, filename, content)
        except api_client.ApiError as exc:
            if exc.status_code < 500 and exc.status_code != 408:        # отказ по существу (нет задания, пустой файл и т. п.) — повтор не поможет
                logger.warning("Фото не принято сервером: %s", exc)
                return "failed", None
            logger.warning("Фото: сервер ответил ошибкой (попытка %d из %d): %s", attempt, UPLOAD_ATTEMPTS, exc)
        except Exception as exc:  # noqa: BLE001 — обрыв связи, таймаут, перезапуск сервера
            logger.warning("Фото: не удалось отправить на сервер (попытка %d из %d): %r", attempt, UPLOAD_ATTEMPTS, exc)
        else:
            if saved.get("duplicate") and attempt == 1:
                return "duplicate", saved.get("id")         # это фото уже принимали раньше (монтёр прислал его второй раз)
            return "saved", saved.get("id")                 # (повтор после обрыва: прежняя попытка могла дойти до сервера — тогда сервер вернёт то же фото)
        if attempt < UPLOAD_ATTEMPTS:
            await asyncio.sleep(UPLOAD_RETRY_PAUSE * attempt)
    return "failed", None


async def _upload_photos(session: dict, vk_id: int, photos: list) -> tuple[list[int], int, int]:
    """Скачивает фото из VK и кладёт на сервер: (id сохранённых, сколько не удалось, сколько уже было принято раньше)."""
    saved_ids: list[int] = []
    failed = duplicates = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for photo in photos:
            sizes = photo.sizes or []
            if not sizes:
                failed += 1
                continue
            best = max(sizes, key=lambda s: (s.width or 0) * (s.height or 0))
            content = None
            for attempt in (1, 2):                      # скачивание из VK тоже иногда обрывается — одна повторная попытка
                try:
                    resp = await client.get(best.url)
                    resp.raise_for_status()
                    content = resp.content
                    break
                except Exception:  # noqa: BLE001
                    logger.exception("Не удалось скачать фото из VK (попытка %d из 2)", attempt)
                    await asyncio.sleep(1.5)
            if not content:
                failed += 1
                continue
            outcome, photo_id = await _send_photo_to_server(vk_id, session["task_id"], _photo_file_name(photo), content)
            if outcome == "saved":
                saved_ids.append(photo_id)
            elif outcome == "duplicate":
                duplicates += 1
            else:
                failed += 1
    return saved_ids, failed, duplicates


# Фото приходят то одним сообщением, то по одному подряд: ответ «принято» шлём один раз после паузы, а не на каждое.
_PHOTO_ACK: dict[int, dict] = {}
PHOTO_ACK_DELAY = 2.5


def _wait_keyboard_for(session: dict):
    step = session.get("step")
    if step == "awaiting_power_source":
        return get_power_source_keyboard()
    return _menu(session) if session.get("task_id") else None


async def receive_photos(message: Message, user: dict, session: dict, photos: list) -> None:
    """Фото, присланные без нажатия кнопок (в меню комплекса): сразу сохраняются, а ответ «принято» приходит один раз —
    когда монтёр закончил присылать. Убрать присланное по ошибке — «📷 Фото» в меню."""
    vk_id = user["vk_id"]
    peer_id = session.get("peer_id", message.peer_id)
    _photo_touch(session)
    before = session.get("photo_count", 0)
    session["_photo_busy"] = True                # пока фото уходят на сервер, фоновый опрос число фото не трогает
    try:
        saved_ids, failed, duplicates = await _upload_photos(session, vk_id, photos)
    finally:
        session["_photo_busy"] = False
    if saved_ids:
        session["photo_count"] = before + len(saved_ids)     # от числа ДО загрузки: промежуточные значения с сервера не складываем
    _photo_touch(session)
    ack = _PHOTO_ACK.setdefault(vk_id, {"new": 0, "failed": 0, "dup": 0, "task": None, "ids": []})
    ack["new"] += len(saved_ids)
    ack["failed"] += failed
    ack["dup"] = ack.get("dup", 0) + duplicates
    ack["ids"].extend(i for i in saved_ids if i)
    if ack["task"] is not None:
        ack["task"].cancel()
    ack["task"] = asyncio.create_task(_photo_ack_later(vk_id, peer_id, session, ack))


async def _photo_ack_later(vk_id: int, peer_id: int, session: dict, ack: dict) -> None:
    try:
        await asyncio.sleep(PHOTO_ACK_DELAY)
    except asyncio.CancelledError:
        return
    new, failed, dup, ids = ack["new"], ack["failed"], ack.get("dup", 0), list(ack["ids"])
    ack.update(new=0, failed=0, dup=0, task=None, ids=[])
    if not new and not failed and not dup:
        return
    if ids:                      # одна «пачка» = то, что подтверждено одним ответом «Принято фото» — её и убирает «Убрать последние»
        session.setdefault("photo_batches", []).append(ids)
    lines = []
    if new:
        lines.append(f"📷 Принято фото: {new}. Всего по комплексу: {session.get('photo_count', 0)}.")
        lines.append("Можно присылать ещё. Убрать присланное по ошибке — кнопка «📷 Фото».")
    if dup:
        lines.append(f"ℹ️ Это фото уже было принято раньше: {dup}.")
    if failed:
        lines.append(f"⚠️ Не удалось сохранить: {failed} — отправьте их ещё раз.")
    if session.get("step") == "awaiting_power_source":
        lines.append("⚡ А пока — чем запитан комплекс?")
    try:
        await send(peer_id, "\n".join(lines), _wait_keyboard_for(session))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось отправить подтверждение о принятых фото")


REPORT_MIN_CHARS = 3


async def receive_report_text(message: Message, user: dict, session: dict) -> None:
    """Текст, присланный в меню комплекса без нажатия кнопок, — это итог работ: он записывается (к уже написанному
    добавляется новым абзацем). Поправить — «📝 Отчёт» в меню: убрать последнее добавленное, весь итог или изменить."""
    peer_id = session.get("peer_id", message.peer_id)
    text = (message.text or "").strip()
    if len(text) < REPORT_MIN_CHARS:
        await send(peer_id, "Слишком коротко для итога работ — напишите подробнее. Если нужна кнопка — выберите её в меню.", _wait_keyboard_for(session))
        return
    current = (session.get("report_text") or "").strip()
    session["report_text"] = (current + "\n" + text) if current else text
    chunks = session.setdefault("report_chunks", [])
    if not current:
        chunks.clear()
    chunks.append(text)
    shown = session["report_text"]
    shown = shown if len(shown) <= 700 else shown[:700] + "…"
    head = "📝 Добавил к итогу работ." if current else "📝 Итог работ записан."
    tail = "\n⚡ А пока — чем запитан комплекс?" if session.get("step") == "awaiting_power_source" else ""
    await send(
        peer_id,
        f"{head}\n«{shown}»\n\nМожно дописать ещё сообщением. Поправить — кнопка «📝 Отчёт».{tail}",
        _wait_keyboard_for(session),
    )


def _last_batch(mine: list[dict], session: dict | None = None) -> list[dict]:
    """Последняя пачка фото: то, что подтверждено последним ответом «Принято фото»; если бот перезапускался и пачки не
    помнит — присланные подряд (пауза между соседними не больше 90 секунд), с конца."""
    by_id = {ph["id"]: ph for ph in mine}
    for ids in reversed((session or {}).get("photo_batches") or []):
        alive = [by_id[i] for i in ids if i in by_id]
        if alive:
            return alive
    batch: list[dict] = []
    prev = None
    for ph in reversed(mine):
        try:
            when = dt.datetime.fromisoformat(str(ph.get("uploaded_at")))
        except ValueError:
            when = None
        if prev is not None and when is not None and (prev - when).total_seconds() > 90:
            break
        batch.append(ph)
        prev = when if when is not None else prev
    return list(reversed(batch))


async def _my_photos(session: dict) -> list[dict]:
    try:
        return await api_client.list_my_photos(session["vk_id"], session["task_id"])
    except api_client.ApiError:
        return []


def _photos_prompt(session: dict) -> str:
    return (
        f"📷 Отправляйте фото (можно сразу несколько). По комплексу уже загружено: {session.get('photo_count', 0)}.\n"
        "Закончите — «Готово». «Отмена» уберёт фото, присланные в этот раз."
    )


async def repair_request_photo(peer_id: int, session: dict) -> None:
    """«📷 Фото»: фото принимаются и так, без кнопок — просто присылайте. Здесь можно убрать присланное по ошибке."""
    session["step"] = "menu"
    await persist_progress(session, force_pull=True)
    mine = await _my_photos(session)
    last = _last_batch(mine, session)
    total = session.get("photo_count", 0)
    lines = [f"📷 Фото по комплексу: {total or 'нет'}."]
    if mine:
        lines.append(f"Ваших фото: {len(mine)}, последняя пачка — {len(last)}.")
    lines.append("Просто присылайте фото сюда — приму без кнопок. Убрать присланное по ошибке — кнопки ниже.")
    await send(peer_id, "\n".join(lines), get_photo_panel_keyboard(len(last), len(mine)))


async def photo_remove(peer_id: int, session: dict, scope: str) -> None:
    """Убрать свои фото по комплексу: «last» — последнюю пачку, «all» — все свои (фото коллеги не трогаем)."""
    session["step"] = "menu"
    _photo_touch(session)
    mine = await _my_photos(session)
    targets = _last_batch(mine, session) if scope == "last" else mine
    removed = 0
    for ph in targets:
        try:
            await api_client.delete_photo(ph["id"], session["vk_id"], session["task_id"])
            removed += 1
        except api_client.ApiError:
            logger.warning("Не удалось убрать фото %s", ph.get("id"))
    gone = {ph["id"] for ph in targets}
    session["photo_batches"] = [b for b in ([[i for i in ids if i not in gone] for ids in (session.get("photo_batches") or [])]) if b]
    await persist_progress(session, force_pull=True)   # число фото — по серверу
    _photo_touch(session)
    head = f"🗑 Убрано фото: {removed}." if removed else "Убирать было нечего."
    await send(peer_id, f"{head} Всего по комплексу: {session.get('photo_count', 0)}.", _menu(session))


async def photos_done(peer_id: int, session: dict) -> None:
    got = len(session.get("photo_batch") or [])
    session["step"] = "menu"
    session["photo_batch"] = []
    head = f"✅ Фото сохранены (в этот раз: {got})." if got else "Новых фото не было."
    await send(peer_id, f"{head} Всего по комплексу: {session.get('photo_count', 0)}.", _menu(session))


async def photos_cancel(peer_id: int, session: dict) -> None:
    batch = [i for i in (session.get("photo_batch") or []) if i]
    removed = 0
    for photo_id in batch:
        try:
            await api_client.delete_photo(photo_id, session["vk_id"], session["task_id"])
            removed += 1
        except api_client.ApiError:
            logger.warning("Не удалось убрать фото %s при отмене", photo_id)
    session["step"] = "menu"
    session["photo_batch"] = []
    await persist_progress(session, force_pull=True)   # число фото — по серверу (коллега мог добавить свои)
    head = f"❌ Отменено: убрано фото — {removed}." if batch else "❌ Отменено — присланных в этот раз фото не было."
    await send(peer_id, f"{head} Всего по комплексу: {session.get('photo_count', 0)}.", _menu(session))


async def set_result(peer_id: int, user: dict, session: dict, status: str) -> None:
    """Итоговый статус комплекса — последний шаг «Завершить» для не связанных
    с ремонтом типов (отчёт и фото к этому моменту уже есть)."""
    if status not in RESULT_STATUSES:
        return
    if status == "Не работает" and not await _ensure_fault_info(
        peer_id, session, {"op": "finish", "status": status, "comment": session.get("report_text") or ""},
    ):
        return
    await _submit_and_finish(peer_id, user, session, status, session.get("report_text") or "")


async def simple_finish(peer_id: int, user: dict, session: dict) -> None:
    """«Завершить» в простом меню: нужны хотя бы одно фото и текст отчёта,
    потом выбор итогового статуса."""
    await persist_progress(session, force_pull=True)   # фото и отчёт второго монтёра бригады тоже считаются
    if not session.get("photo_count"):
        await send(peer_id, "Пришлите хотя бы одно фото (просто отправьте его сюда), прежде чем завершить.", _menu(session))
        return
    if not (session.get("report_text") or "").strip():
        await send(peer_id, "Сначала напишите итог работ (просто сообщением сюда).", _menu(session))
        return
    session["step"] = "awaiting_result"
    outcomes = session.get("type_outcomes") or []
    if outcomes:
        # У типа настроены свои «статусы выезда» (Настройки → Типы выездов) —
        # итог выбирается из них; на статус комплекса они не влияют.
        await send(peer_id, "Выберите статус выезда:", get_outcome_keyboard(outcomes))
        return
    await send(peer_id, "Выберите итоговый статус комплекса:", get_result_status_keyboard())


async def set_outcome(peer_id: int, user: dict, session: dict, index: int) -> None:
    """Выбран «статус выезда» типа: пишется в отчёт и статистику как есть,
    состояние комплекса не меняется."""
    outcomes = session.get("type_outcomes") or []
    if session.get("step") != "awaiting_result" or not (0 <= index < len(outcomes)):
        return
    await _submit_and_finish(
        peer_id, user, session, "", session.get("report_text") or "", outcome=outcomes[index],
    )


async def to_repair_mode(peer_id: int, session: dict) -> None:
    """«Перейти в режим ремонта» — на точке нашлась неисправность или её решили
    восстановить: дальше меню ремонта (проверка доступности, замена
    оборудования, SIM…), а визит в отчётах и выгрузках записывается как
    «<тип> + Ремонт»."""
    if session.get("repair_mode"):
        await send(peer_id, "Режим ремонта уже включён.", _menu(session))
        return
    session.update(repair_mode=True, switched_to_repair=True, step="menu")
    base = session.get("work_type") or "выезд"
    await send(
        peer_id,
        f"🔧 Режим ремонта включён. В отчётах и выгрузках визит будет записан как «{base} + Ремонт».\n"
        "Выполните ремонт, при необходимости замените оборудование, запустите «Проверка доступности» "
        "и нажмите «Завершить».",
        _menu(session),
    )


async def from_repair_mode(peer_id: int, session: dict) -> None:
    """«↩ Это не ремонт» — режим ремонта включили по ошибке (на комплексе были, например, только замеры): возвращаем
    простое меню, и визит в отчётах пишется под своим типом, без «+ Ремонт». Нельзя, если уже записано что-то ремонтное."""
    if not session.get("switched_to_repair"):
        await send(peer_id, "Режим ремонта включён самим заданием — отключить его нельзя.", _menu(session))
        return
    if _repair_footprint(session):
        await send(
            peer_id,
            "Вернуться нельзя: уже записана замена оборудования/SIM или статус питания — это ремонт. "
            "Если визит всё-таки был без ремонта, попросите диспетчера поменять тип выезда в отчёте.",
            _menu(session),
        )
        return
    session.update(repair_mode=False, switched_to_repair=False, no_access=False, step="menu")
    base = session.get("work_type") or "выезд"
    await send(peer_id, f"↩ Вернул обычный режим. В отчётах визит будет записан как «{base}».", _menu(session))


def _equipment_for_report(session: dict) -> tuple[str | None, str | None]:
    """Что сняли / что поставили — свободный текст из «Замены оборудования» и демонтаж. SIM-карта сюда не входит:
    это не обычное оборудование, она идёт в отчёт отдельными полями (см. `_sim_for_report`)."""
    removed = [session.get("equipment_removed")]
    installed = [session.get("equipment_installed")]
    scope = session.get("demontage_scope") or {}
    if scope.get("labels") or scope.get("other"):
        removed.append("; ".join(p for p in (scope.get("labels"), scope.get("other")) if p))
    join = lambda parts: "; ".join(p for p in parts if p) or None  # noqa: E731
    return join(removed), join(installed)


def _sim_for_report(session: dict) -> tuple[str | None, str | None, str | None]:
    """(SIM снята, SIM установлена, состояние снятой: working / defective) — если SIM меняли за этот выезд."""
    change = session.get("sim_change") or {}
    if not change.get("new"):
        return None, None, None
    old = change.get("old") or None
    state = change.get("old_state") if old and change.get("old_state") in ("working", "defective") else None
    return old, change["new"], state


async def _submit_and_finish(
    peer_id: int, user: dict, session: dict, status: str, comment: str,
    final_text: str = "✅ Отчёт сохранён. Хорошего дня!", outcome: str | None = None,
) -> None:
    vk_id = user["vk_id"]
    task_id = session.get("task_id")
    if not task_id:
        await send(peer_id, "Сессия потеряна — начните задание заново.", get_back_to_menu_keyboard())
        return
    scope = session.get("demontage_scope") if session.get("demontage_completed") else None
    if scope:
        # Демонтаж: выбранное оборудование (или всё на комплексе) снимается на монтёра и ждёт приёмки на склад.
        note = (session.get("demontage_note") or "").strip()
        try:
            await api_client.demontage_equipment(
                vk_id, session.get("point_id") or "", whole=scope.get("mode") == "whole",
                equipment_ids=scope.get("ids") or [], reason="Демонтаж" + (f": {note}" if note else ""),
            )
        except api_client.ApiError as e:
            await send(peer_id, f"❌ Не удалось списать оборудование с комплекса: {e.detail}\nНажмите «Завершить» ещё раз.", _menu(session))
            return
        line = scope.get("summary") or ""
        if line and line not in comment:
            comment = (comment + "\n" if comment else "") + line
    try:
        removed, installed = _equipment_for_report(session)
        sim_removed, sim_installed, sim_state = _sim_for_report(session)
        await api_client.submit_visit_report(
            task_id=task_id, vk_id=vk_id, result_status=status, comment=comment,
            equipment_removed=removed, equipment_installed=installed,
            sim_removed=sim_removed, sim_installed=sim_installed, sim_removed_state=sim_state,
            repair_mode=bool(session.get("switched_to_repair")),
            outcome=outcome,
            fault=_fault_for_report(session),
            needs=(session.get("needs_text") or "").strip() or None if status == "Не работает" else None,
            consumables=[dict(row) for row in session.get("consumables", []) if isinstance(row, dict)],
        )
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось сохранить отчёт: {e.detail}\nИсправьте расходники или попробуйте отправить отчёт ещё раз.", _menu(session))
        return
    work_session_id = session.get("work_session_id")
    if work_session_id:
        try:
            await api_client.end_work_session(work_session_id)
        except api_client.ApiError:
            logger.exception("Не удалось завершить рабочую сессию %s", work_session_id)
    if not session.get("detached"):
        navigation.reset_session(vk_id)   # у «отсоединённого» (ответ пришёл после выхода) своей сессии в чате нет
    await send(peer_id, final_text, get_main_menu_keyboard())


# ---------------------------------------------------------------- меню задания: фото / отчёт / оборудование ----

async def show_dispatcher_phone(peer_id: int, session: dict | None = None) -> None:
    """«📞 Диспетчер»: телефоны и кнопка «Написать диспетчеру» (чат прямо здесь, ответ придёт в бота)."""
    try:
        phones = await api_client.get_dispatcher_phones()
    except api_client.ApiError:
        phones = []
    keyboard = get_dispatcher_keyboard(bool(session and session.get("task_id")))
    if not phones:
        await send(peer_id, "Не удалось получить номер диспетчера. Можно написать ему сообщением — кнопка ниже.", keyboard)
        return
    lines = [f"{p['full_name']}: {p['phone']}" for p in phones]
    await send(peer_id, "📞 Диспетчер:\n" + "\n".join(lines) + "\n\nИли напишите ему здесь — ответ придёт в этот чат.", keyboard)


# ---------------------------------------------------------------- чат с диспетчером ----
# «✉️ Написать диспетчеру» включает режим чата: всё, что монтёр пишет, уходит диспетчеру (он читает и отвечает в
# веб-панели, окно «Чаты»), ответы диспетчера приходят сюда. «🚪 Выйти из чата» — закончить. Если диспетчер напишет, когда
# монтёр уже вышел, бот пришлёт уведомление с кнопкой «Ответить». История переписки в боте не показывается — только в
# веб-панели. Режим чата не трогает комплекс, на котором работает монтёр: после выхода он возвращается туда, где был.

CHAT_TEXT_MAX = 1500
CHAT_WAIT_STEPS = ("awaiting_verification", "awaiting_demontage_approval")
CHAT_HELLO = (
    "✉️ Чат с диспетчером. Пишите сообщения и присылайте фото — они уйдут диспетчеру, его ответы придут сюда.\n"
    "Закончить — «Выйти из чата». Если диспетчер ответит позже, бот пришлёт уведомление. Переписка хранится 7 дней."
)


async def chat_open(peer_id: int, user: dict, session: dict) -> None:
    if session.get("step") == "chat":
        await send(peer_id, "Вы уже в чате с диспетчером — просто пишите сообщение.", get_chat_keyboard())
        return
    session["chat_prev_step"] = session.get("step")
    session["chat_acked"] = False
    session["step"] = "chat"
    await send(peer_id, CHAT_HELLO, get_chat_keyboard())


async def chat_photos(message: Message, user: dict, session: dict, photos: list) -> None:
    """Фото в чат диспетчеру (до 10 в одном сообщении) — вместе с подписью, если она есть."""
    peer_id = session.get("peer_id", message.peer_id)
    caption = (message.text or "").strip()[:CHAT_TEXT_MAX]
    files: list[tuple[str, bytes]] = []
    failed = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for photo in photos[:10]:
            sizes = photo.sizes or []
            if not sizes:
                failed += 1
                continue
            best = max(sizes, key=lambda s: (s.width or 0) * (s.height or 0))
            try:
                resp = await client.get(best.url)
                resp.raise_for_status()
                files.append((f"photo_{photo.id}.jpg", resp.content))
            except Exception:  # noqa: BLE001
                logger.exception("Чат: не удалось скачать фото из VK")
                failed += 1
    failed += max(0, len(photos) - 10)
    if not files:
        await send(peer_id, "⚠️ Не удалось получить фото — отправьте ещё раз.", get_chat_keyboard())
        return
    try:
        await api_client.chat_send_photos(user["vk_id"], caption, files, session.get("task_id"), session.get("point_id"))
    except api_client.ApiError as e:
        await send(peer_id, f"⚠️ Фото не отправлено: {e.detail}. Попробуйте ещё раз.", get_chat_keyboard())
        return
    except Exception:  # noqa: BLE001 — сервер недоступен
        logger.exception("Чат: сервер недоступен")
        await send(peer_id, "⚠️ Сервер сейчас недоступен — фото не отправлено. Попробуйте через минуту.", get_chat_keyboard())
        return
    note = f"✓ Отправил диспетчеру: фото {len(files)}." + (f" Не удалось получить: {failed}." if failed else "")
    if not session.get("chat_acked"):
        session["chat_acked"] = True
        note += " Ответ придёт сюда."
    await send(peer_id, note, get_chat_keyboard())


async def _vk_photo_attachment(peer_id: int, content: bytes) -> str:
    """Загружает фото в VK для личного сообщения; возвращает строку вложения («photo<владелец>_<id>_<ключ>»)."""
    from vkbottle import PhotoMessageUploader

    return await PhotoMessageUploader(get_api()).upload(file_source=content, peer_id=peer_id)


# Фото массовой рассылки одно на всех получателей: в VK его загружаем один раз и дальше шлём готовую строку вложения (иначе сотня
# получателей — сотня загрузок). Не принял VK — сбрасываем и загружаем заново (см. _send_chat_message).
_BROADCAST_PHOTOS: dict[int, tuple[float, str]] = {}
BROADCAST_PHOTO_TTL = 3600.0
BROADCAST_PHOTO_CACHE_MAX = 40


async def _chat_attachment(m: dict, photos: list[dict], *, fresh: bool = False) -> tuple[str | None, bool]:
    """Вложения сообщения чата для VK: (строка вложений, использован ли кэш рассылки)."""
    if not photos:
        return None, False
    cacheable = bool(m.get("broadcast"))
    used_cache = False
    parts = []
    now = time.monotonic()
    for a in photos[:10]:
        key = a.get("file_id") or a["id"]
        hit = _BROADCAST_PHOTOS.get(key) if cacheable and not fresh else None
        if hit and now - hit[0] < BROADCAST_PHOTO_TTL:
            parts.append(hit[1])
            used_cache = True
            continue
        part = await _vk_photo_attachment(m["vk_id"], await api_client.chat_file(a["id"]))
        if cacheable:
            if len(_BROADCAST_PHOTOS) >= BROADCAST_PHOTO_CACHE_MAX:
                _BROADCAST_PHOTOS.pop(next(iter(_BROADCAST_PHOTOS)))
            _BROADCAST_PHOTOS[key] = (now, part)
        parts.append(part)
    return ",".join(parts), used_cache


async def _send_chat_message(m: dict, text: str, keyboard, photos: list[dict]) -> None:
    attachment, used_cache = await _chat_attachment(m, photos)
    try:
        await send(m["vk_id"], text, keyboard, attachment=attachment)
    except Exception:  # noqa: BLE001
        if not used_cache:
            raise
        for a in photos[:10]:
            _BROADCAST_PHOTOS.pop(a.get("file_id") or a["id"], None)
        attachment, _ = await _chat_attachment(m, photos, fresh=True)
        await send(m["vk_id"], text, keyboard, attachment=attachment)


async def chat_message(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    text = (message.text or "").strip()
    if not text:
        await send(peer_id, "В чат можно писать текст и присылать фото.", get_chat_keyboard())
        return
    try:
        await api_client.chat_send(user["vk_id"], text[:CHAT_TEXT_MAX], session.get("task_id"), session.get("point_id"))
    except api_client.ApiError as e:
        await send(peer_id, f"⚠️ Сообщение не отправлено: {e.detail}. Напишите ещё раз.", get_chat_keyboard())
        return
    except Exception:  # noqa: BLE001 — сервер недоступен
        logger.exception("Чат: сервер недоступен")
        await send(peer_id, "⚠️ Сервер сейчас недоступен — сообщение не отправлено. Попробуйте через минуту.", get_chat_keyboard())
        return
    if not session.get("chat_acked"):
        session["chat_acked"] = True
        await send(peer_id, "✓ Отправил диспетчеру. Ответ придёт сюда.", get_chat_keyboard())


async def chat_exit(peer_id: int, user: dict, session: dict) -> None:
    prev = session.pop("chat_prev_step", None)
    session.pop("chat_acked", None)
    if session.get("step") == "chat":
        session["step"] = None
    if not session.get("task_id"):
        session["step"] = None
        await send(peer_id, "Вышли из чата. Если диспетчер ответит, бот пришлёт уведомление.", get_main_menu_keyboard())
        return
    # Вернуться туда, где был: ждали ответ диспетчера — снова ждём (если решение уже пришло, в меню), иначе — меню задания.
    if prev in CHAT_WAIT_STEPS and session.get("pending"):
        session["step"] = prev
        await send(peer_id, "Вышли из чата. Если диспетчер ответит, бот пришлёт уведомление.")
        await reshow_wait(peer_id, session)
        return
    session["step"] = "menu"
    await send(peer_id, "Вышли из чата. Если диспетчер ответит, бот пришлёт уведомление.", _menu(session))


async def deliver_chat_messages() -> int:
    """Раз в несколько секунд (bot/main.py): забирает новые ответы диспетчера из веб-панели и шлёт монтёрам в VK. В чате
    — просто сообщение, вне чата — уведомление с кнопкой «Ответить». Не получилось отправить — сервер повторит позже."""
    try:
        items = await api_client.chat_claim()
    except Exception:  # noqa: BLE001 — сервер недоступен, попробуем в следующий проход
        return 0
    delivered: list[int] = []
    for m in items:
        in_chat = navigation.get_session(m["vk_id"]).get("step") == "chat"
        head = "💬 Диспетчер:" if in_chat else "💬 Сообщение от диспетчера:"
        if m.get("broadcast"):
            head = "📢 Объявление от диспетчера:"
        photos = m.get("attachments") or []
        text = f"{head}\n{m['text']}" if m["text"] else (f"{head}\n📷 Фото: {len(photos)}" if photos else head)
        try:
            # фото ответа: забираем файлы с сервера и загружаем в VK — до 10 вложений в одном сообщении
            await _send_chat_message(m, text, get_chat_keyboard() if in_chat else get_chat_notice_keyboard(), photos)
            delivered.append(m["id"])
        except Exception as exc:  # noqa: BLE001 — VK не принял (нет разрешения на сообщения и т. п.)
            logger.warning("Чат: не удалось отправить сообщение %s монтёру %s: %s", m["id"], m["vk_id"], exc)
            try:
                await api_client.chat_failed(m["id"], str(exc)[:200])
            except Exception:  # noqa: BLE001
                pass
    try:
        await api_client.chat_delivered(delivered)
    except Exception:  # noqa: BLE001
        logger.exception("Чат: не удалось отметить доставленные")
    return len(delivered)


async def repair_request_report(peer_id: int, session: dict) -> None:
    """«📝 Отчёт»: итог работ принимается и так, без кнопок — просто пишите. Здесь его можно поправить."""
    session["step"] = "menu"
    current = (session.get("report_text") or "").strip()
    chunks = session.get("report_chunks") or []
    can_undo_last = bool(current and chunks and chunks[-1] != current and current.endswith(chunks[-1]))
    if not current:
        await send(
            peer_id,
            "📝 Итога работ пока нет. Просто напишите его обычным сообщением — приму без кнопок "
            "(можно в несколько сообщений: они дописываются друг за другом).",
            get_report_panel_keyboard(False, False),
        )
        return
    shown = current if len(current) <= 900 else current[:900] + "…"
    await send(
        peer_id,
        f"📝 Итог работ:\n«{shown}»\n\nДописать — просто пришлите ещё текст. Поправить — кнопки ниже.",
        get_report_panel_keyboard(True, can_undo_last),
    )


async def report_remove(peer_id: int, session: dict, scope: str) -> None:
    """Убрать итог работ: «last» — последнее добавленное сообщение, «all» — весь текст."""
    current = (session.get("report_text") or "").strip()
    chunks = session.get("report_chunks") or []
    session["step"] = "menu"
    if scope == "last" and chunks and current.endswith(chunks[-1]) and chunks[-1] != current:
        session["report_text"] = current[: len(current) - len(chunks[-1])].rstrip("\n").strip() or None
        chunks.pop()
        text = "↩ Убрал последнее добавленное. Итог работ теперь:\n«" + (session["report_text"] or "") + "»"
    else:
        session["report_text"] = None
        session["report_chunks"] = []
        text = "🗑 Итог работ убран. Напишите новый — просто сообщением."
    await send(peer_id, text, _menu(session))


async def report_edit(peer_id: int, session: dict) -> None:
    """«✏️ Изменить текст»: прежний сценарий с подтверждением — показывает итог и просит новый текст целиком."""
    current = (session.get("report_text") or "").strip()
    await begin_draft(
        peer_id, session, "report", "📝 Напишите текст итога работ целиком — он заменит прежний.",
        existing=current or None, existing_title="📝 Итог работ сейчас такой:",
    )


# ---------------------------------------------------------------- ввод текста с подтверждением ----
# Любой текст/номер, который монтёр вводит в задании (отчёт, номер SIM, что поставили/сняли), идёт через
# «черновик»: бот показывает введённое и спрашивает «Сохранить / Отмена» (или можно написать заново). Пока черновик
# открыт, кнопки меню задания не работают — нельзя нажать «дальше», не сохранив и не отменив ввод.
# session["draft"] = {kind, prompt, stage: choose|input|confirm, value, existing, payload}.

async def begin_draft(
    peer_id: int, session: dict, kind: str, prompt: str, existing: str | None = None, existing_title: str = "",
    payload: dict | None = None,
) -> None:
    draft = {"kind": kind, "prompt": prompt, "value": None, "existing": existing, "title": existing_title, "payload": payload or {}}
    session["draft"] = draft
    session["step"] = "awaiting_text"
    if existing:
        draft["stage"] = "choose"
        await send(peer_id, f"{existing_title}\n«{existing}»\n\nОставить прежний или изменить?", get_draft_keep_keyboard())
        return
    draft["stage"] = "input"
    await send(peer_id, prompt, get_draft_input_keyboard())


def _normalize_draft(kind: str, raw: str) -> str:
    raw = (raw or "").strip()
    return "".join(raw.split()) if kind in ("sim", "new_sim", "sim_fix") else raw


def _draft_empty_hint(kind: str) -> str:
    return {
        "report": "Отчёт не может быть пустым.", "sim": "Номер SIM не может быть пустым.",
        "new_sim": "Номер SIM не может быть пустым.", "sim_fix": "Номер SIM не может быть пустым.",
        "dem_other": "Напишите, что ещё снято.",
        "fault": "Опишите, что именно неисправно.", "needs": "Напишите, что нужно для исправления.",
        "shot": "Опишите, что расстреляно (например: камера, радар, шкаф, антенна). Если отметили по ошибке — отправьте «-».",
    }.get(kind, "Отправьте «-», если ничего.")


def _draft_preview(draft: dict) -> str:
    kind, value = draft["kind"], draft["value"]
    if kind == "report":
        return f"📝 Текст отчёта:\n«{value}»\n\nСохранить? Или просто напишите текст заново."
    if kind == "new_sim":
        current = (draft.get("payload") or {}).get("current") or "не указана"
        return f"💳 Новая SIM: {value}\n(сейчас на комплексе: {current})\n\nСохранить? Или введите другой номер."
    if kind == "sim":
        return f"💳 Номер SIM комплекса: {value}\n\nСохранить? Или введите другой номер."
    if kind == "sim_fix":
        current = (draft.get("payload") or {}).get("current") or "не указана"
        return (f"✏️ Исправление номера SIM: было {current} → станет {value}\n"
                "Это исправление ошибки в записи (SIM не меняли), в отчёт как замена оборудования не попадёт.\n\n"
                "Сохранить? Или введите другой номер.")
    if kind == "dem_other":
        return f"🧰 Ещё снято (нет в списке оборудования): «{value}»\n\nСохранить? Или напишите заново."
    if kind == "fault":
        return f"⚠️ Что неисправно:\n«{value}»\n\nСохранить? Или просто напишите заново."
    if kind == "needs":
        return f"📦 Что нужно для исправления:\n«{value}»\n\nСохранить? Или просто напишите заново."
    if kind == "shot":
        return ("💥 Снять отметку «комплекс расстрелян»?\n\nСохранить? Или напишите, что расстреляно." if value == "-"
                else f"💥 Комплекс расстрелян. Что расстреляно:\n«{value}»\n\nСохранить? Или просто напишите заново.")
    label = "Установили" if kind == "eq_installed" else "Сняли"
    shown = "ничего" if value == "-" else f"«{value}»"
    return f"🔧 {label}: {shown}\n\nСохранить? Или напишите заново."


async def handle_draft_text(message: Message, user: dict, session: dict) -> None:
    draft = session.get("draft")
    peer_id = session.get("peer_id", message.peer_id)
    if not draft:
        session["step"] = "menu" if session.get("task_id") else None
        await send(peer_id, "Сейчас ничего вводить не нужно.", _menu(session) if session.get("task_id") else None)
        return
    value = _normalize_draft(draft["kind"], message.text or "")
    if not value:
        await send(peer_id, _draft_empty_hint(draft["kind"]), get_draft_input_keyboard() if draft.get("stage") != "confirm" else get_draft_confirm_keyboard())
        return
    draft["value"] = value
    draft["stage"] = "confirm"
    await send(peer_id, _draft_preview(draft), get_draft_confirm_keyboard())


async def draft_edit(peer_id: int, session: dict) -> None:
    draft = session.get("draft")
    if not draft:
        return
    draft["stage"] = "input"
    await send(peer_id, draft["prompt"], get_draft_input_keyboard())


async def draft_keep(peer_id: int, session: dict, user: dict | None = None) -> None:
    """«Оставить прежний» — введённого нет, прежнее значение не меняется. Если текст подтверждали перед запросом
    «отпустить без ремонта», запрос уходит диспетчеру сразу."""
    kind = (session.get("draft") or {}).get("kind")
    session["draft"] = None
    session["step"] = "menu"
    if kind == "fault" and user is not None:
        await _ask_needs(peer_id, session)
        return
    if kind == "needs" and user is not None:
        await _after_fault(peer_id, user, session)
        return
    if session.pop("pending_action", None) == "release_request" and user is not None:
        await _release_after_report(peer_id, user, session)
        return
    await send(peer_id, "👌 Оставил прежний текст.", _menu(session))


async def draft_cancel(peer_id: int, session: dict) -> None:
    draft = session.get("draft") or {}
    kind = draft.get("kind")
    session["draft"] = None
    session["step"] = "menu"
    session.pop("_eq_installed_tmp", None)
    pending_release = session.pop("pending_action", None) == "release_request"
    if kind == "report":
        text = "❌ Отменено — отчёт остался как был." if (session.get("report_text") or "").strip() else "❌ Отменено — отчёт не сохранён."
        if pending_release:
            text += " Запрос диспетчеру не отправлен."
    elif kind in ("sim", "new_sim", "sim_fix"):
        session.pop("finish_after_check", None)
        text = "❌ Отменено — номер SIM не менялся."
    elif kind == "dem_other":
        text = "❌ Отменено."
    elif kind == "shot":
        text = "❌ Отменено — отметка «расстрелян» осталась как была."
    elif kind in ("fault", "needs"):
        release = (session.pop("after_fault", None) or {}).get("op") == "release"
        text = ("❌ Отменено — запрос диспетчеру не отправлен." if release
                else "❌ Отменено — отчёт пока не отправлен. Когда будете готовы, нажмите «Завершить» ещё раз.")
    else:
        text = "❌ Отменено — замена оборудования не сохранена."
    await send(peer_id, text, _menu(session))


async def draft_save(peer_id: int, user: dict, session: dict) -> None:
    draft = session.get("draft")
    if not draft or draft.get("value") is None:
        await send(peer_id, "Сначала введите значение — или нажмите «Отмена».", get_draft_input_keyboard())
        return
    session["draft"] = None
    commit = {
        "report": _commit_report, "sim": _commit_sim, "new_sim": _commit_new_sim, "sim_fix": _commit_sim_fix,
        "eq_installed": _commit_eq_installed, "eq_removed": _commit_eq_removed, "dem_other": _commit_dem_other,
        "fault": _commit_fault, "needs": _commit_needs, "shot": _commit_shot,
    }[draft["kind"]]
    await commit(peer_id, user, session, draft["value"], draft)


async def _retry_draft(peer_id: int, session: dict, draft: dict, error: str) -> None:
    """Сохранить не получилось — черновик остаётся: «Сохранить» повторит, «Отмена» отменит."""
    session["draft"] = draft
    session["step"] = "awaiting_text"
    await send(peer_id, f"❌ {error}\nНажмите «Сохранить», чтобы повторить, или «Отмена».", get_draft_confirm_keyboard())


async def _commit_report(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["report_text"] = value
    session["report_chunks"] = []
    session["step"] = "menu"
    # Пришли сюда не из обычного ввода итога, а потому что «Отпустить
    # без ремонта» потребовал сначала описать, что предпринято — теперь,
    # когда текст есть, можно наконец отправить запрос диспетчеру.
    if session.pop("pending_action", None) == "release_request":
        await _release_after_report(peer_id, user, session)
        return
    await send(peer_id, "✅ Отчёт сохранён.", _menu(session))


# ---------------------------------------------------------------- «Не работает»: что неисправно и что нужно ----
# Если комплекс так и не заработал (итог «Не работает» — выбран монтёром, нет питания, «отпустить без ремонта»), бот просит
# два отдельных текста помимо отчёта: что именно неисправно и — следующим сообщением — что нужно для исправления.
# Они попадают в итог по комплексу, выгрузки и «Уведомления» диспетчера. Монтёру ничего про «повторный выезд» не пишем.

FAULT_PROMPT = (
    "⚠️ Что именно неисправно на комплексе? Опишите отдельно от отчёта — конкретно "
    "(например: «не работает камера, сгорел блок питания, нет связи по SIM»)."
)
NEEDS_PROMPT = "📦 Что нужно для исправления? Запчасти, оборудование, инструмент, доступ — одним сообщением."


async def _ask_fault(peer_id: int, session: dict) -> None:
    current = (session.get("fault_text") or "").strip()
    await begin_draft(
        peer_id, session, "fault", FAULT_PROMPT, existing=current or None,
        existing_title="⚠️ Уже записано, что неисправно:",
    )


async def _ask_needs(peer_id: int, session: dict) -> None:
    current = (session.get("needs_text") or "").strip()
    await begin_draft(
        peer_id, session, "needs", NEEDS_PROMPT, existing=current or None,
        existing_title="📦 Уже записано, что нужно для исправления:",
    )


async def _ensure_fault_info(peer_id: int, session: dict, cont: dict) -> bool:
    """Запускает два вопроса (что неисправно → что нужно для исправления) и запоминает `cont` — что сделать после
    ответов. Уже записанные раньше тексты показываются с «Оставить / Изменить», чтобы в итог не ушла устаревшая запись.
    Всегда возвращает False: вызывающий код на этом останавливается и продолжит из _after_fault."""
    session["after_fault"] = cont
    if _shot_covers_fault(session):                   # комплекс расстреляли и это описано — «что неисправно» отдельно не спрашиваем, только «что нужно»
        await _ask_needs(peer_id, session)
    else:
        await _ask_fault(peer_id, session)
    return False


async def _commit_fault(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["fault_text"] = value
    await _ask_needs(peer_id, session)


async def _commit_needs(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["needs_text"] = value
    await _after_fault(peer_id, user, session)


async def _after_fault(peer_id: int, user: dict, session: dict) -> None:
    cont = session.get("after_fault") or {}
    if cont.get("op") in ("finish", "release"):
        # Без обоих текстов итог «Не работает» не отправляем — спрашиваем недостающий.
        if not (session.get("fault_text") or "").strip() and not _shot_covers_fault(session):
            await _ask_fault(peer_id, session)
            return
        if not (session.get("needs_text") or "").strip():
            await _ask_needs(peer_id, session)
            return
    session.pop("after_fault", None)
    session["step"] = "menu"
    op = cont.get("op")
    if op == "finish":
        await _submit_and_finish(
            peer_id, user, session, cont.get("status") or "Не работает", cont.get("comment") or session.get("report_text") or "",
            final_text=cont.get("final_text") or "✅ Отчёт сохранён. Хорошего дня!", outcome=cont.get("outcome"),
        )
    elif op == "release":
        await _request_no_access_decision(peer_id, user, session, "release")
    else:
        await send(peer_id, "✅ Записано.", _menu(session))


async def _release_after_report(peer_id: int, user: dict, session: dict) -> None:
    """Отчёт для «отпустить без ремонта» готов — до запроса диспетчеру спрашиваем, что неисправно и что нужно."""
    if await _ensure_fault_info(peer_id, session, {"op": "release"}):
        await _request_no_access_decision(peer_id, user, session, "release")


async def _commit_eq_installed(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["_eq_installed_tmp"] = None if value == "-" else value
    await begin_draft(peer_id, session, "eq_removed", "Что сняли? Если ничего — отправьте «-».")


async def _commit_eq_removed(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["equipment_installed"] = session.pop("_eq_installed_tmp", None)
    session["equipment_removed"] = None if value == "-" else value
    session["step"] = "menu"
    await send(peer_id, "✅ Замена оборудования сохранена.", _menu(session))


async def _commit_sim(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    """Номер SIM комплекса (его не было, а проверка доступности его требует)."""
    try:
        await api_client.set_point_sim(session["point_id"], value)
    except api_client.ApiError as e:
        await _retry_draft(peer_id, session, draft, f"Не удалось сохранить номер SIM: {e.detail}")
        return
    session["sim_asked"] = True
    session["step"] = "menu"
    await send(peer_id, "Номер SIM сохранён, проверяю доступность…")
    await run_availability_check(peer_id, user, session)


async def _commit_new_sim(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    """Замена SIM-карты. Если на комплексе уже была SIM — сначала спрашиваем, рабочая ли снятая (рабочую можно выдать
    снова, нерабочую спишут), а для нерабочей — ещё и причину (она уйдёт в учёт оборудования и в отчёт); иначе заменяем сразу."""
    task = (session.get("tasks") or {}).get(session.get("task_id")) or {}
    old = (draft.get("payload") or {}).get("current") or task.get("point_sim") or ""
    if old:
        session["reason_ctx"] = {"op": "sim_replace", "new": value, "old": old}
        session["step"] = "awaiting_sim_state"
        await send(peer_id, f"💳 Заменяем SIM {old} → {value}.\nСнятая SIM (№{old}) рабочая?", get_sim_state_keyboard())
        return
    await _do_sim_replace(peer_id, user, session, value, old, None, None)


async def pick_sim_state(peer_id: int, user: dict, session: dict, working: bool) -> None:
    """Ответ «снятая SIM рабочая / нерабочая». Рабочая — заменяем сразу; нерабочая — спрашиваем, что с ней не так."""
    ctx = session.get("reason_ctx") or {}
    if session.get("step") != "awaiting_sim_state" or ctx.get("op") != "sim_replace":
        return
    ctx = {**ctx, "working": bool(working)}
    if working:
        session.pop("reason_ctx", None)
        session["step"] = "menu"
        await _do_sim_replace(peer_id, user, session, ctx.get("new") or "", ctx.get("old") or "", None, True)
        return
    await ask_removal_reason(peer_id, session, ctx, title=f"💳 Снятая SIM №{ctx.get('old')} нерабочая.")


async def sim_state_cancel(peer_id: int, session: dict) -> None:
    session.pop("reason_ctx", None)
    session["step"] = "menu"
    await send(peer_id, "❌ Отменено — SIM не менялась.", _menu(session))


async def _do_sim_replace(
    peer_id: int, user: dict, session: dict, value: str, old: str, reason: str | None, working: bool | None = None,
) -> None:
    """Замена SIM в учёте: новая встаёт на комплекс (поле «Номер SIM» и IP из реестра обновляются), старая числится на
    монтёре как снятая — с причиной. В отчёт («что сняли / что поставили») идёт замена SIM."""
    task = (session.get("tasks") or {}).get(session.get("task_id")) or {}
    try:
        res = await api_client.sim_replace(user["vk_id"], session["point_id"], value, reason, removed_working=working)
    except api_client.ApiError as e:
        session["step"] = "menu"
        await send(peer_id, f"❌ Не удалось заменить SIM: {e.detail}", _menu(session))
        return
    new = res.get("installed") or value
    removed = (res.get("removed") or [None])[0] or old
    # Если меняли несколько раз — старый номер берём от первой замены.
    previous = session.get("sim_change") or {}
    old_state = ("working" if working else "defective" if working is False else "") if not previous.get("old") else (previous.get("old_state") or "")
    session["sim_change"] = {"old": previous.get("old") or removed or "", "new": new, "reason": reason or previous.get("reason") or "", "old_state": old_state}
    if task:
        task["point_sim"] = new
    session["sim_asked"] = True
    session["step"] = "menu"
    lines = [f"💳 SIM заменена: {removed or 'не была указана'} → {new}."]
    if removed and working:
        lines.append("Снятая SIM рабочая — осталась у вас: сдавать на склад не обязательно, можно поставить её снова.")
    elif removed:
        lines.append(
            (f"Причина снятия: {reason}. " if reason else "") + "Снятая SIM осталась у вас (в списке «Моё оборудование») — "
            "сдавать на склад не обязательно."
        )
    if res.get("created"):
        lines.append("Новой SIM не было в базе — записал её.")
    if res.get("ip_known"):
        lines.append(f"IP комплекса обновлён из реестра SIM↔IP: {res.get('ip_address')}.")
    else:
        lines.append(
            "Новой SIM пока нет в реестре SIM↔IP — IP комплекса остался прежним; "
            "его обновит загрузка реестра (сообщите диспетчеру)."
        )
    lines.append("Проверяю доступность…")
    await send(peer_id, "\n".join(lines))
    await run_availability_check(peer_id, user, session)


async def _commit_sim_fix(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    """Исправление номера SIM: на комплексе был записан не тот номер. Это не замена — ничего не снимается."""
    task = (session.get("tasks") or {}).get(session.get("task_id")) or {}
    try:
        res = await api_client.sim_fix(user["vk_id"], session["point_id"], value)
    except api_client.ApiError as e:
        session["step"] = "menu"
        await send(peer_id, f"❌ Не удалось исправить номер SIM: {e.detail}", _menu(session))
        return
    new = res.get("installed") or value
    # Исправили номер, который сам же ставили заменой в этот выезд, — в отчёте должен быть верный.
    change = session.get("sim_change")
    if change and change.get("new") == res.get("old"):
        session["sim_change"] = {**change, "new": new}
    if task:
        task["point_sim"] = new
    session["sim_asked"] = True
    session["step"] = "menu"
    lines = [f"✏️ Номер SIM исправлен: {res.get('old') or 'не был указан'} → {new}."]
    lines.append(
        f"IP комплекса обновлён из реестра SIM↔IP: {res.get('ip_address')}." if res.get("ip_known")
        else "Этой SIM пока нет в реестре SIM↔IP — IP комплекса остался прежним."
    )
    lines.append("Проверяю доступность…")
    await send(peer_id, "\n".join(lines))
    await run_availability_check(peer_id, user, session)


async def request_sim_fix(peer_id: int, session: dict) -> None:
    """«✏️ Исправить номер SIM» — номер на комплексе был указан неверно (SIM не меняли)."""
    current = ""
    task = (session.get("tasks") or {}).get(session.get("task_id"))
    if task:
        current = task.get("point_sim") or ""
    if not current:
        try:
            current = (await api_client.get_point(session["point_id"])).get("sim_number") or ""
        except api_client.ApiError:
            current = ""
    await begin_draft(
        peer_id, session, "sim_fix",
        f"✏️ Сейчас на комплексе указана SIM: {current or 'не указана'}.\n"
        "Введите правильный номер (без пробелов) — это исправление ошибки, а не замена.",
        payload={"current": current},
    )


# ---------------------------------------------------------------- причина снятия оборудования ----
# Прежде чем снять/заменить оборудование (в т. ч. SIM), бот спрашивает, ПОЧЕМУ: готовая причина одной кнопкой или своя
# фраза. Причина пишется в учёт оборудования (её видит склад при приёмке) и в отчёт. session["reason_ctx"] = {op, ...}.

async def ask_removal_reason(peer_id: int, session: dict, ctx: dict, title: str = "") -> None:
    session["reason_ctx"] = dict(ctx)
    session["step"] = "awaiting_reason"
    head = f"{title}\n" if title else ""
    await send(peer_id, f"{head}Почему снимаете? Выберите причину кнопкой или напишите, какая проблема:", get_reason_keyboard())


async def pick_reason(peer_id: int, user: dict, session: dict, index: int) -> None:
    if session.get("step") != "awaiting_reason" or not (0 <= index < len(REMOVAL_REASONS)):
        return
    await _reason_given(peer_id, user, session, REMOVAL_REASONS[index])


async def handle_reason_text(message: Message, user: dict, session: dict) -> None:
    peer_id = session.get("peer_id", message.peer_id)
    reason = " ".join((message.text or "").split())[:200]
    if not reason:
        await send(peer_id, "Напишите причину словами или выберите кнопкой.", get_reason_keyboard())
        return
    await _reason_given(peer_id, user, session, reason)


async def _reason_given(peer_id: int, user: dict, session: dict, reason: str) -> None:
    ctx = session.get("reason_ctx") or {}
    session["reason_ctx"] = {**ctx, "reason": reason}
    session["step"] = "menu"
    op = ctx.get("op")
    if op == "sim_replace":
        session.pop("reason_ctx", None)
        await _do_sim_replace(peer_id, user, session, ctx.get("new") or "", ctx.get("old") or "", reason, ctx.get("working"))
    elif op in ("eq_replace", "eq_remove"):
        from bot.service import equipment as eq_svc   # equipment импортирует этот модуль — поэтому здесь, а не вверху
        await eq_svc.reason_given(peer_id, user, session, reason)
    else:
        session.pop("reason_ctx", None)
        await send(peer_id, "Причина записана.", _menu(session))


async def reason_cancel(peer_id: int, user: dict, session: dict) -> None:
    ctx = session.pop("reason_ctx", None) or {}
    session["step"] = "menu"
    if ctx.get("op") in ("eq_replace", "eq_remove"):
        from bot.service import equipment as eq_svc
        await eq_svc.show_catalog(peer_id, user, session)
        return
    await send(peer_id, "❌ Отменено — ничего не изменено.", _menu(session))


async def _commit_dem_other(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    from bot.service import equipment as eq_svc
    await eq_svc.set_dem_other(peer_id, user, session, value)


async def reshow_wait(peer_id: int, session: dict) -> None:
    """Повторно показывает текущее «ожидание» (кнопки и подсказку) — когда монтёр нажал что-то постороннее."""
    step = session.get("step")
    if step == "awaiting_photos":
        await send(peer_id, _photos_prompt(session), get_photos_keyboard())
    elif step == "awaiting_text":
        draft = session.get("draft") or {}
        stage = draft.get("stage")
        if stage == "choose":
            await send(peer_id, f"{draft.get('title')}\n«{draft.get('existing')}»\n\nОставить прежний или изменить?", get_draft_keep_keyboard())
        elif stage == "confirm":
            await send(peer_id, _draft_preview(draft), get_draft_confirm_keyboard())
        else:
            await send(peer_id, draft.get("prompt") or "Введите значение.", get_draft_input_keyboard())
    elif step in ("awaiting_verification", "awaiting_demontage_approval"):
        await send(peer_id, "⏳ Ждём ответ диспетчера — он придёт сюда. Можно позвонить или выйти с комплекса.", get_waiting_keyboard())
    elif step == "awaiting_power_source":
        await send(peer_id, "⚡ Чем запитан комплекс?", get_power_source_keyboard())
    elif step == "awaiting_result":
        outcomes = session.get("type_outcomes") or []
        if outcomes:
            await send(peer_id, "Выберите статус выезда:", get_outcome_keyboard(outcomes))
        else:
            await send(peer_id, "Выберите итоговый статус комплекса:", get_result_status_keyboard())
    elif step == "awaiting_sim_confirm":
        await send(peer_id, "Это правильный номер SIM? Ответьте кнопками.", get_sim_confirm_keyboard())
    elif step == "awaiting_reason":
        await send(peer_id, "Почему снимаете? Выберите причину кнопкой или напишите, какая проблема:", get_reason_keyboard())
    elif step == "awaiting_sim_state":
        ctx = session.get("reason_ctx") or {}
        await send(peer_id, f"Снятая SIM (№{ctx.get('old') or '—'}) рабочая?", get_sim_state_keyboard())
    elif step == "chat":
        await send(peer_id, "Вы в чате с диспетчером — пишите сообщение или нажмите «Выйти из чата».", get_chat_keyboard())


async def repair_request_equipment(peer_id: int, session: dict) -> None:
    """«🔧 Замена оборудования» — что меняли: оборудование комплекса, SIM-карту или другое. Отметка «комплекс расстрелян» ставится не здесь, а при «Завершить» (repair_finish_anyway)."""
    session["step"] = "menu"
    shot = (session.get("shot_text") or "").strip()
    await send(peer_id, "Что заменили?" + (f"\n\n💥 Отмечено: комплекс расстрелян — {shot}" if shot else ""),
               get_equipment_menu_keyboard(bool(shot)))


async def repair_request_shot(peer_id: int, session: dict) -> None:
    """«💥 Комплекс расстрелян» — описание, что расстреляно (камера, радар, шкаф…). Попадает в отчёт как «что неисправно» (в карточку комплекса и «Уведомления»)."""
    await begin_draft(
        peer_id, session, "shot", "💥 Опишите, что расстреляно (например: камера, радар, шкаф, антенна). Если отметили по ошибке — отправьте «-».",
        existing=(session.get("shot_text") or "").strip() or None, existing_title="💥 Уже отмечено: комплекс расстрелян. Что расстреляно:",
    )


async def _commit_shot(peer_id: int, user: dict, session: dict, value: str, draft: dict) -> None:
    session["step"] = "menu"
    if value.strip() == "-":
        session["shot_text"] = None
        await send(peer_id, "✅ Отметка «расстрелян» снята.", _menu(session))
        return
    session["shot_text"] = value
    if session.pop("finish_reason_pick", None):
        # Причину («расстрелян») выбрали в сценарии «Всё равно завершить» — продолжаем завершение.
        await send(peer_id, f"✅ Записано: комплекс расстрелян — {value}\nЗавершаю с итогом «Не работает».")
        await repair_finish(peer_id, user, session)
        return
    await send(peer_id, f"✅ Записано: комплекс расстрелян — {value}\nЭто попадёт в отчёт («что неисправно») и в карточку комплекса — диспетчер увидит.", _menu(session))


def _shot_covers_fault(session: dict) -> bool:
    """Отмечено «комплекс расстрелян» с описанием, а отдельного «что неисправно» нет: описание расстрела и есть причина."""
    return bool((session.get("shot_text") or "").strip()) and not (session.get("fault_text") or "").strip()


def _fault_for_report(session: dict) -> str | None:
    """«Что неисправно» для отчёта: если комплекс расстреляли — в начале «💥 Расстрелян: <что>», дальше обычное описание неисправности."""
    fault = (session.get("fault_text") or "").strip()
    shot = (session.get("shot_text") or "").strip().rstrip(" .;,")
    if shot:
        line = f"💥 Расстрелян: {shot}"
        return f"{line}. {fault}" if fault else line
    return fault or None


async def repair_request_equipment_other(peer_id: int, session: dict) -> None:
    await begin_draft(peer_id, session, "eq_installed", "Что установили? Если ничего — отправьте «-».")


async def request_sim_change(peer_id: int, session: dict) -> None:
    """Замена SIM-карты: показываем текущий номер, просим новый — и сначала подтверждаем введённый."""
    current = ""
    task = (session.get("tasks") or {}).get(session.get("task_id"))
    if task:
        current = task.get("point_sim") or ""
    if not current:
        try:
            current = (await api_client.get_point(session["point_id"])).get("sim_number") or ""
        except api_client.ApiError:
            current = ""
    await begin_draft(
        peer_id, session, "new_sim",
        f"💳 Сейчас на комплексе указана SIM: {current or 'не указана'}.\nВведите номер новой SIM-карты (без пробелов). "
        "Если ставите SIM, которая числится на вас, — введите её номер.",
        payload={"current": current},
    )


async def repair_set_power_status(peer_id: int, session: dict, kind: str, user: dict | None = None) -> None:
    status_name = POWER_STATUSES[kind]
    try:
        await api_client.set_point_power_status(session["point_id"], status_name)
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось сохранить статус: {e.detail}", _menu(session))
        return
    session["power_status"] = status_name
    if session.pop("finish_reason_pick", None) and user is not None:
        # Причину выбрали в сценарии «Всё равно завершить» — сразу ведём дальше (отчёт → «что неисправно» → «что нужно» → итог «Не работает»).
        await send(peer_id, f"⚡ Причина отмечена: «{status_name}». Завершаю с итогом «Не работает».")
        await repair_finish(peer_id, user, session)
        return
    await send(
        peer_id,
        f"⚡ Статус комплекса: «{status_name}». Система продолжит проверять связь — если появится, статус обновится сам."
        if kind == "no_power" else
        f"⚡ Статус комплекса: «{status_name}».",
        _menu(session),
    )


async def repair_check_availability(peer_id: int, user: dict, session: dict, *, from_finish: bool = False) -> None:
    """Проверка доступности. `from_finish` — её запустило «Завершить» (доступ ещё не проверялся): тогда по итогу проверки
    бот сам ведёт дальше (нет доступа → предложить уехать без ремонта; только пинг → завершить нельзя). Ручная проверка
    кнопкой этот режим сбрасывает."""
    if from_finish:
        session["finish_after_check"] = True
    else:
        session.pop("finish_after_check", None)
    point_id = session["point_id"]
    try:
        point = await api_client.get_point(point_id)
    except api_client.ApiError:
        point = {}
    if not (point.get("sim_number") or "").strip():
        await begin_draft(peer_id, session, "sim", "Укажите номер SIM-карты комплекса (без пробелов).")
        return
    await run_availability_check(peer_id, user, session)


async def run_availability_check(peer_id: int, user: dict, session: dict) -> None:
    point_id = session["point_id"]
    try:
        result = await api_client.recheck_point(point_id)
    except api_client.ApiError as e:
        # Сбой связи с сервером — это не «нет доступа»: не помечаем комплекс недоступным и не предлагаем уехать.
        session.pop("finish_after_check", None)
        session["step"] = "menu"
        await send(peer_id, f"⚠️ Не удалось проверить доступность: {e.detail}. Попробуйте ещё раз через минуту.", _menu(session))
        return
    status = result.get("status") or ""
    finishing = bool(session.get("finish_after_check"))

    if status == STATUS_WORKING:
        session.pop("finish_after_check", None)
        session["no_access"] = False
        if not session.get("repair_mode"):
            # Выезд не по ремонту (проверка включена переключателем типа): результат только сообщаем монтёру — на финальную
            # проверку диспетчеру/оператору не отправляем, это нужно лишь после ремонта.
            session["step"] = "menu"
            await send(peer_id, "✅ Комплекс доступен: пинг и порт отвечают. Проверка диспетчером не требуется.", _menu(session))
            return
        await _request_operator_check(peer_id, user, session)
        return

    if status == STATUS_PING_ONLY:
        # IP отвечает (пинг есть) — значит SIM/IP уже верные, переспрашивать
        # их незачем: дело не в связи, а в самом оборудовании на точке.
        session.pop("finish_after_check", None)
        session["sim_asked"] = True
        session["no_access"] = False
        session["step"] = "menu"
        if finishing:
            # Раньше здесь завершить было нельзя. Теперь монтёр решает сам: починить и проверить снова либо завершить как есть с причиной.
            await send(
                peer_id,
                "📡 Есть пинг, но веб-интерфейс не отвечает — комплекс в сети, значит неисправность на месте. "
                "Можно проверить оборудование, кабели и питание и запустить проверку снова — либо всё равно завершить работу "
                "(отметите причину, напишете итоги и что неисправно; итог будет «Не работает»).",
                get_no_access_decision_keyboard(),
            )
            return
        else:
            text = (
                "📡 Есть только пинг — комплекс в сети, но веб-интерфейс не отвечает. "
                "Проверьте оборудование/кабель на месте. Если не разберётесь — позвоните диспетчеру."
            )
        await send(peer_id, text, _menu(session))
        return

    if not session.get("sim_asked"):
        try:
            point = await api_client.get_point(point_id)
        except api_client.ApiError:
            point = {}
        current_sim = (point.get("sim_number") or "").strip()
        if current_sim:
            session["step"] = "awaiting_sim_confirm"
            await send(
                peer_id,
                f"⛔ Доступа нет. Сейчас на комплексе указана SIM: {current_sim}. Это правильный номер?",
                get_sim_confirm_keyboard(),
            )
        else:
            await begin_draft(peer_id, session, "sim", "⛔ Доступа нет. Укажите номер SIM-карты комплекса (без пробелов).")
        return

    await _mark_no_access(peer_id, session)


async def _mark_no_access(peer_id: int, session: dict) -> None:
    """Доступа нет, а SIM уже введена/подтверждена (не важно, была она
    указана заново или просто сверена) — просто сообщаем монтёру и
    возвращаем в меню, БЕЗ номера диспетчера и БЕЗ автоматического
    уведомления диспетчеру: и то, и другое раньше срабатывало прямо тут,
    хотя должно происходить только по явному решению монтёра — либо кнопка
    «📞 Номер диспетчера» в общем меню (когда он сам захочет позвонить),
    либо кнопка «Отпустить без ремонта», которая появляется при «Завершить»
    (см. repair_finish/repair_offer_release) — там нажатие кнопки и есть то
    самое явное решение, отправляющее запрос диспетчеру. Демонтаж бригаде
    из бота не запрашивается вообще: его оформляет диспетчер в веб-панели
    («Мониторинг» → «Сейчас в работе»), а бот об этом узнаёт сам — см.
    _task_watcher."""
    session["step"] = "menu"
    session["no_access"] = True
    if session.pop("finish_after_check", None):
        # Проверку запустило «Завершить» — сразу предлагаем уехать без ремонта (с итогами, неисправностью и тем, что нужно).
        await send(peer_id, "⛔ Доступа нет: комплекс не отвечает ни по пингу, ни по порту.")
        await repair_offer_release(peer_id, session)
        return
    await send(peer_id, "⛔ Доступа нет.", _menu(session))


async def handle_sim_confirm(peer_id: int, session: dict, correct: bool) -> None:
    if session.get("step") != "awaiting_sim_confirm":
        return
    if correct:
        session["sim_asked"] = True
        await _mark_no_access(peer_id, session)
    else:
        await begin_draft(peer_id, session, "sim", "Введите номер SIM без пробелов.")


async def _request_operator_check(peer_id: int, user: dict, session: dict) -> None:
    try:
        request = await api_client.create_verification_request(session["task_id"])
    except api_client.ApiError as e:
        await send(peer_id, f"⚠️ Не удалось отправить на проверку оператору: {e.detail}. Попробуйте ещё раз позже.", _menu(session))
        session["step"] = "menu"
        return
    session["step"] = "awaiting_verification"
    session["pending"] = {"kind": "verification", "id": request["id"]}   # ответ придёт и если монтёр выйдет с комплекса
    await send(
        peer_id,
        "📡 Пинг/порт отвечают. Отправил на финальную проверку оператору — подождите, ответ придёт сюда. "
        "Можно и выйти с комплекса: ответ всё равно придёт.",
        get_waiting_keyboard(),
    )
    _spawn_poller(("v", request["id"]), _poll_verification_request(peer_id, user, session["task_id"], request["id"]))


async def _poll_verification_request(peer_id: int, user: dict, task_id: str, request_id: int) -> None:
    for _ in range(120):  # ~10 минут при паузе в 5с
        await asyncio.sleep(5)
        try:
            request = await api_client.get_verification_request(request_id)
        except api_client.ApiError:
            continue
        if request["status"] == "pending":
            continue
        await _apply_verification_result(peer_id, user, task_id, request)
        return
    await _pending_timeout(peer_id, user, task_id, request_id, "verification",
                           "⏳ Проверка оператором затянулась. Статус можно будет увидеть позже — напишите диспетчеру, если срочно.")


async def _pending_timeout(peer_id: int, user: dict, task_id: str, request_id: int, kind: str, text: str) -> None:
    s = _live_session(user["vk_id"], task_id) or await _detached_session(user, task_id)
    if not s or (s.get("pending") or {}).get("id") != request_id:
        return
    s["pending"] = None
    if not s.get("detached") and (s.get("step") in ("awaiting_verification", "awaiting_demontage_approval") or s.get("chat_prev_step") in ("awaiting_verification", "awaiting_demontage_approval")):
        _live_step(s)
        await send(peer_id, text, _live_kb(s))
    else:
        await send(peer_id, f"ℹ️ По комплексу «{s.get('point_id') or '—'}»: {text}")
    await persist_progress(s)


async def _apply_verification_result(peer_id: int, user: dict, task_id: str, request: dict) -> None:
    """Ответ оператора: применяем к состоянию комплекса — к открытой сессии монтёра, а если он уже вышел с
    комплекса, то к сохранённому прогрессу (и сообщаем, что делать дальше)."""
    s = _live_session(user["vk_id"], task_id) or await _detached_session(user, task_id)
    if not s or (s.get("pending") or {}).get("id") != request["id"]:
        return   # комплекс уже закрыт или этот ответ уже обработан
    live = not s.get("detached")
    point = s.get("point_id") or "комплексу"
    s["pending"] = None
    if request["status"] == "confirmed":
        s["check_confirmed"] = True
        if live:
            _live_step(s)
            await send(peer_id, "✅ Оператор подтвердил — доступ есть. Напишите отчёт (если ещё не писали) и нажмите «Завершить».", _live_kb(s))
        else:
            await send(peer_id, f"✅ Оператор подтвердил комплекс «{point}» — доступ есть. Зайдите в него из списка заданий, напишите отчёт и нажмите «Завершить».")
    elif request["status"] == "remark":
        # «Работает, но с замечанием — отпустить»: диспетчер принял комплекс и отпускает бригаду —
        # задание закрывается сразу (как при «отпустить без ремонта»), замечание уходит в отчёт.
        remark = (request.get("issue_comment") or "").strip() or "не указано"
        report_text = (s.get("report_text") or "").strip()
        full_report = (f"{report_text}\n" if report_text else "") + f"Замечание диспетчера: {remark}"
        await _submit_and_finish(
            peer_id, user, s, FINAL_STATUS_BY_TYPE.get(s.get("work_type") or "", "Работает"), full_report,
            final_text=f"✅ Диспетчер принял комплекс «{point}»: работает, но есть замечание — {remark}\nМожно уезжать. Спасибо!",
        )
        return   # прогресс удалён вместе с закрытием задания — заново его записывать нельзя
    else:
        # «Продолжить работу»: диспетчер не принял комплекс — комментарий может быть пустым.
        comment = (request.get("issue_comment") or "").strip()
        s["check_confirmed"] = False
        note = "🔧 Диспетчер просит продолжить работу" + (f": {comment}" if comment else ".")
        if live:
            _live_step(s)
            await send(peer_id, note + "\nДоделайте и запустите «Проверка доступности» ещё раз, когда будете готовы.", _live_kb(s))
        else:
            await send(peer_id, f"{note}\nКомплекс «{point}» — зайдите в него из списка заданий и доделайте.")
    await persist_progress(s)


# Кнопки при «Завершить», когда комплекс так и остался недоступен (см.
# repair_finish) — причина уже известна, поэтому нажатие сразу создаёт
# запрос и уходит на опрос _poll_demontage_request.
async def repair_offer_release(peer_id: int, session: dict) -> None:
    await send(
        peer_id,
        "⛔ Комплекс недоступен. Можно всё равно завершить работу: отметите причину (нет питания, не подведено питание или "
        "комплекс расстрелян), напишете итоги и что неисправно — отчёт уйдёт с итогом «Не работает». "
        "Или уехать без ремонта — запрос уйдёт диспетчеру, ответ придёт сюда. "
        "Если на месте что-то поправили — проверьте доступ ещё раз.",
        get_no_access_decision_keyboard(),
    )


async def repair_finish_anyway(peer_id: int, session: dict) -> None:
    """«✅ Всё равно завершить» при недоступном комплексе: спрашиваем причину — нет питания / не подведено питание / расстрелян.
    Нажатие на причину ставит отметку и продолжает завершение (repair_set_power_status / _commit_shot → repair_finish)."""
    session["step"] = "menu"
    session["finish_reason_pick"] = True
    await send(peer_id, "Почему комплекс не работает? Выберите причину — после неё допишем итоги и завершим работу.", get_unavailable_reason_keyboard())


async def _request_no_access_decision(peer_id: int, user: dict, session: dict, kind: str) -> None:
    # Defensive: clears a stale "release" gate left behind if the fitter
    # picked something else instead of finishing that prompt — otherwise a
    # later, unrelated "напишите отчёт" could misfire this flow.
    session.pop("pending_action", None)
    try:
        request = await api_client.create_demontage_request(session["task_id"], "Нет доступа к комплексу", kind=kind)
    except api_client.ApiError as e:
        await send(peer_id, f"❌ Не удалось отправить запрос: {e.detail}", _menu(session))
        return
    session["step"] = "awaiting_demontage_approval"
    session["pending"] = {"kind": "decision", "id": request["id"], "decision": kind}
    label = "отпуск без ремонта" if kind == "release" else "демонтаж"
    await send(peer_id, f"📨 Запрос на {label} отправлен диспетчеру — ожидайте решения, оно придёт сюда. Можно и выйти с комплекса.", get_waiting_keyboard())
    _spawn_poller(("d", request["id"]), _poll_demontage_request(peer_id, user, session["task_id"], request["id"]))


async def repair_release_request(peer_id: int, user: dict, session: dict) -> None:
    # «Отпустить без ремонта» закрывает задание сразу по одобрению диспетчера (см. _apply_decision_result), и в итог по
    # комплексу уходит именно ЭТОТ отчёт монтёра — а не отметка «диспетчер отпустил». Поэтому перед запросом монтёр
    # видит свой отчёт и подтверждает его (или правит): в нём должно быть, что сделано и что нужно для следующего выезда.
    session["pending_action"] = "release_request"
    current = (session.get("report_text") or "").strip()
    await begin_draft(
        peer_id, session, "report",
        "Перед запросом диспетчеру напишите отчёт по комплексу: что сделано (какие действия и попытки). "
        "Именно он попадёт в итог по комплексу; следом спрошу, что неисправно и что нужно для исправления.",
        existing=current or None,
        existing_title="📝 Этот отчёт уйдёт в итог по комплексу. Он верный?",
    )


async def repair_menu_back(peer_id: int, session: dict) -> None:
    session.pop("finish_after_check", None)
    session.pop("finish_reason_pick", None)
    if session.get("step") in ("awaiting_result", "awaiting_sim_confirm"):
        session["step"] = "menu"
    await send(peer_id, "Меню:", _menu(session))


async def _poll_demontage_request(peer_id: int, user: dict, task_id: str, request_id: int) -> None:
    for _ in range(360):  # ~30 минут
        await asyncio.sleep(5)
        try:
            request = await api_client.get_demontage_request(request_id)
        except api_client.ApiError:
            continue
        if request["status"] == "pending":
            continue
        await _apply_decision_result(peer_id, user, task_id, request)
        return
    await _pending_timeout(peer_id, user, task_id, request_id, "decision",
                           "⏳ Решение диспетчера ещё не поступило. Уточните напрямую, если срочно.")


async def _apply_decision_result(peer_id: int, user: dict, task_id: str, request: dict) -> None:
    s = _live_session(user["vk_id"], task_id) or await _detached_session(user, task_id)
    if not s or (s.get("pending") or {}).get("id") != request["id"]:
        return
    live = not s.get("detached")
    point = s.get("point_id") or "комплексу"
    kind = request.get("kind") or "demontage"
    s["pending"] = None
    if request["status"] == "approved":
        if kind == "release":
            # Отпуск без ремонта — решение уже принято, дальше монтёру
            # ничего доделывать не нужно (в отличие от демонтажа, где
            # ещё нужны фото/отчёт) — сразу закрываем задание.
            await _submit_and_finish(
                peer_id, user, s, "Не работает",
                s.get("report_text") or "Диспетчер отпустил бригаду без ремонта — доступа к комплексу нет.",
                final_text=f"✅ Диспетчер согласился по комплексу «{point}» — можно уезжать. Спасибо!",
            )
            return
        s["demontage_completed"] = True
        if live:
            _live_step(s)
            await send(peer_id, "✅ Демонтаж одобрен. Выполните его, пришлите фото, напишите отчёт и нажмите «Завершить».", _live_kb(s))
        else:
            await send(peer_id, f"✅ Демонтаж по комплексу «{point}» одобрен. Зайдите в него из списка заданий, пришлите фото, напишите отчёт и нажмите «Завершить».")
    else:
        comment = request.get("decline_comment") or "не указано"
        label = "отпуск без ремонта" if kind == "release" else "демонтаж"
        text = f"⛔ Диспетчер отклонил {label}: {comment}. Продолжайте работу."
        if live:
            _live_step(s)
            await send(peer_id, text, _live_kb(s))
        else:
            await send(peer_id, f"{text} (комплекс «{point}»)")
    await persist_progress(s)


async def _resume_pending(peer_id: int, user: dict, session: dict) -> None:
    """Монтёр вернулся на комплекс, по которому ещё ждём ответ диспетчера: снова показываем «ожидание» и следим за ответом."""
    pending = session.get("pending") or {}
    task_id = session["task_id"]
    if pending.get("kind") == "verification":
        session["step"] = "awaiting_verification"
        await send(peer_id, "📡 Запрос оператору по этому комплексу ещё в работе — ответ придёт сюда.", get_waiting_keyboard())
        _spawn_poller(("v", pending["id"]), _poll_verification_request(peer_id, user, task_id, pending["id"]))
    elif pending.get("kind") == "decision":
        session["step"] = "awaiting_demontage_approval"
        label = "отпуск без ремонта" if pending.get("decision") == "release" else "демонтаж"
        await send(peer_id, f"📨 Запрос на {label} по этому комплексу ещё не решён — ответ придёт сюда.", get_waiting_keyboard())
        _spawn_poller(("d", pending["id"]), _poll_demontage_request(peer_id, user, task_id, pending["id"]))
    else:
        session["pending"] = None
        await _show_task_menu(peer_id, session, {"point": session.get("point_id")})


async def repair_finish(peer_id: int, user: dict, session: dict) -> None:
    await persist_progress(session, force_pull=True)   # отчёт/проверки второго монтёра бригады тоже считаются
    work_type = session.get("work_type") or ""
    if session.get("demontage_completed"):
        scope = session.get("demontage_scope")
        if not scope:
            await send(
                peer_id,
                "Сначала укажите, что именно демонтировано: кнопка «🧰 Что демонтировано» — весь комплекс или отдельные элементы.",
                _menu(session),
            )
            return
        if not (session.get("report_text") or "").strip():
            await send(peer_id, "Сначала напишите итог работ (просто сообщением сюда).", _menu(session))
            return
        if scope.get("mode") == "parts":
            # Снята только часть оборудования — комплекс «Демонтирован» целиком не считается: итоговый статус выбирает монтёр.
            session["step"] = "awaiting_result"
            await send(
                peer_id, "Демонтированы только отдельные элементы. Выберите итоговый статус комплекса:",
                get_result_status_keyboard(),
            )
            return
        status = "Демонтирован"
        default_final = "✅ Демонтаж оформлен, можно уезжать. Спасибо!"
    elif session.get("check_confirmed"):
        status = FINAL_STATUS_BY_TYPE.get(work_type, "Работает")
        default_final = "✅ Отчёт сохранён, можно уезжать. Спасибо!"
    elif session.get("power_status"):
        status = "Не работает"
        default_final = "✅ Отчёт сохранён. Как только питание появится — статус обновится сам."
    elif (session.get("shot_text") or "").strip():
        # Комплекс расстреляли (отметка из «Всё равно завершить»): итог «Не работает», что расстреляно — в «что неисправно».
        status = "Не работает"
        default_final = "✅ Отчёт сохранён: комплекс расстрелян, диспетчер это увидит. Спасибо!"
    elif session.get("no_access"):
        if session.get("step") == "awaiting_demontage_approval":
            await send(peer_id, "⏳ Уже ждём решения диспетчера по этому запросу.", _menu(session))
        else:
            await repair_offer_release(peer_id, session)
        return
    else:
        # Доступ не проверен (или проверка показала только пинг и это ещё не исправили) — не просим монтёра нажимать
        # «Проверка доступности», а проверяем сами; дальше по итогу: доступ есть → проверка оператором, нет → предложение
        # уехать без ремонта, только пинг → завершить нельзя.
        await send(peer_id, "🔎 Доступ к комплексу ещё не подтверждён — проверяю сам, это займёт немного времени…")
        await repair_check_availability(peer_id, user, session, from_finish=True)
        return
    report_text = (session.get("report_text") or "").strip()
    if not report_text:
        await send(peer_id, "Сначала напишите итог работ (просто сообщением сюда).", _menu(session))
        return
    if status == "Не работает" and not await _ensure_fault_info(
        peer_id, session, {"op": "finish", "status": status, "comment": report_text, "final_text": default_final},
    ):
        return
    await _submit_and_finish(peer_id, user, session, status, report_text, final_text=default_final)


async def _task_watcher(peer_id: int, user: dict, session: dict, task_id: str, work_session_id: int, token: str) -> None:
    """Пока монтёр на комплексе (раз в 10 с, до ~2 часов): узнаёт о том, чего бот сам не видит, — lan_calendar не
    может ничего отправить боту, её только опрашивают. Решение диспетчера из /monitoring («Без ремонта» / «Демонтаж»),
    отчёт, сданный вторым монтёром бригады, и его правки отчёта/оборудования/фото (подтягиваются в эту сессию)."""
    vk_id = user["vk_id"]
    for _ in range(720):
        await asyncio.sleep(10)
        current = navigation.get_session(vk_id)
        if current.get("task_id") != task_id or current.get("watch_token") != token:
            return   # вышли с комплекса (или зашли заново — тогда наблюдает уже другая задача)
        mut_before = current.get("_photo_mut", 0)
        try:
            status = await api_client.watch_task(task_id, work_session_id, vk_id)
        except api_client.ApiError:
            continue
        # Пока ждали ответ сервера, монтёр мог выйти с комплекса: рабочая сессия при выходе закрывается, и ответ «закрыто извне»
        # относится уже к ней. Сессию (список заданий!) в таком случае трогать нельзя — раньше она сбрасывалась, и кнопки списка
        # заданий отвечали «Задание не найдено».
        current = navigation.get_session(vk_id)
        if current.get("task_id") != task_id or current.get("watch_token") != token:
            return
        point = current.get("point_id") or "комплекс"
        if status.get("finished"):
            navigation.reset_session(vk_id)
            await send(peer_id, f"✅ По комплексу «{point}» отчёт уже сдан (вторым монтёром бригады или диспетчером) — работа закрыта.", get_main_menu_keyboard())
            return
        if status.get("closed_externally"):
            navigation.reset_session(vk_id)
            await send(peer_id, "ℹ️ Диспетчер закрыл эту работу со своей стороны.", get_main_menu_keyboard())
            return
        progress = status.get("progress")
        if progress and current.get("step") not in INPUT_STEPS:
            if current.get("_photo_mut", 0) != mut_before or current.get("_photo_busy"):     # монтёр сам присылает/убирает фото — число фото в ответе устарело
                progress = {k: v for k, v in progress.items() if k != "photo_count"}
            _adopt_server(current, progress)   # правки коллеги (отчёт, оборудование, фото) — без шума
        demontage = status.get("demontage")
        if demontage and current.get("step") not in ("awaiting_demontage_approval",):
            session_now = current
            session_now["demontage_completed"] = True
            session_now["repair_mode"] = True
            session_now["step"] = "menu"
            await persist_progress(session_now)
            note = demontage.get("reason") or "не указано"
            session_now["demontage_note"] = note
            await persist_progress(session_now)
            await send(
                peer_id,
                f"🔧 Диспетчер оформил демонтаж: {note}\n"
                "Выполните демонтаж, пришлите фото, напишите отчёт и нажмите «Завершить».",
                _menu(session_now),
            )
            return
