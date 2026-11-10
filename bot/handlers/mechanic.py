"""Регистрация обработчиков — вся бизнес-логика в service/mechanic.py.

Один labeler, одна роль (монтёр — любой активный человек, привязанный по
vk_id в lan_calendar, см. middlewares/auth_mw.py). Правила ниже — тот же
проверенный паттерн, что был в исходном боте для callback-кнопок
(GroupEventType.MESSAGE_EVENT + разбор payload вручную), только без
собственного обращения к БД — пользователя уже положил в контекст
AuthMiddleware."""
import functools
import json
import logging

from vkbottle.bot import BotLabeler, Message
from vkbottle.dispatch.rules import ABCRule
from vkbottle_types import GroupEventType

from bot.keyboards.mechanic_menu import get_power_source_keyboard
from bot.service import equipment as eq_svc
from bot.service import events
from bot.service import mechanic as svc
from bot.service import navigation
from bot.service import views
from bot.service import warehouse as wh_svc

logger = logging.getLogger(__name__)

labeler = BotLabeler()

# Фото и итог работ принимаются без кнопок: в меню комплекса (и пока бот спрашивает про питание) присланное фото сразу
# сохраняется, а присланный текст — это итог работ. Прежний режим «Готово/Отмена» работает, если монтёр в нём уже был.
# Во всех остальных шагах бот ждёт что-то другое — текст или ответ кнопкой.
_FREE_INPUT_STEPS = {"menu", "awaiting_power_source"}
_PHOTO_OK_STEPS = {"menu", "awaiting_photos", "awaiting_power_source"}

# Пока бот ждёт фото, текст или ответ диспетчера, работают только кнопки этого ожидания — остальные (из более
# старых сообщений) отвечают подсказкой и заново показывают текущее меню ожидания. Так нельзя нажать «дальше»,
# пока предыдущее не отправлено. «Меню»/«начать» текстом — запасной выход (прогресс сохраняется).
WAIT_ALLOWED = {
    "awaiting_photos": {"photos_done", "photos_cancel"},
    "awaiting_text": {"draft_save", "draft_cancel", "draft_edit", "draft_keep"},
    "awaiting_verification": {"leave_task", "dispatcher_phone", "chat_open"},
    "awaiting_demontage_approval": {"leave_task", "dispatcher_phone", "chat_open"},
    "awaiting_power_source": {"set_power_source", "leave_task"},
    "awaiting_result": {"set_result", "set_outcome", "repair_menu_back"},
    "awaiting_sim_confirm": {"sim_confirm_yes", "sim_confirm_no", "repair_sim_change", "repair_menu_back"},
    "awaiting_reason": {"rsn_pick", "rsn_back", "leave_task"},
    "awaiting_sim_state": {"sim_state_pick", "sim_state_back", "leave_task"},
    "chat": {"chat_exit"},
}
WAIT_HINTS = {
    "awaiting_photos": "Сначала нажмите «Готово» или «Отмена».",
    "awaiting_text": "Сначала сохраните или отмените ввод.",
    "awaiting_verification": "Ждём ответ диспетчера — он придёт в этот чат.",
    "awaiting_demontage_approval": "Ждём ответ диспетчера — он придёт в этот чат.",
    "awaiting_reason": "Сначала выберите причину снятия или напишите её.",
    "awaiting_sim_state": "Сначала ответьте кнопкой: снятая SIM рабочая или нет.",
    "chat": "Вы в чате с диспетчером — нажмите «Выйти из чата», чтобы продолжить работу.",
}
# Кнопки, которым нужен открытый комплекс. Если бота перезапускали и монтёр жмёт кнопку из старого сообщения,
# его возвращают на комплекс по открытой рабочей сессии на сервере (прогресс на месте).
TASK_ACTIONS = {
    "photo_rm", "report_rm", "report_edit", "repair_photo", "repair_report", "repair_equipment", "repair_equipment_other", "repair_shot", "repair_sim_change",
    "to_repair_mode", "from_repair_mode", "set_power_source", "set_outcome", "simple_finish", "repair_no_power", "repair_not_connected",
    "repair_check", "sim_confirm_yes", "sim_confirm_no", "repair_finish", "finish_anyway", "repair_release_request", "repair_menu_back",
    "set_result", "photos_done", "photos_cancel", "draft_save", "draft_cancel", "draft_edit", "draft_keep",
    "eq_catalog", "eq_cat", "eq_item", "eq_repl", "eq_rm", "eq_new", "eq_newc", "eq_pick", "eq_other",
    "eq_actpick", "eq_actnew", "eq_serial", "eq_suffix_ok", "eq_do", "eq_reason", "repair_sim_fix", "rsn_pick", "rsn_back", "sim_state_pick", "sim_state_back",
    "dem_scope", "dem_whole", "dem_whole_ok", "dem_parts", "dem_tog", "dem_other", "dem_parts_ok",
}


async def _guard(action: str, user: dict, peer_id: int) -> str | None:
    """None — кнопка допустима. Иначе подсказка (всплывает у кнопки), а обработчик не вызывается."""
    session = navigation.get_session(user["vk_id"])
    step = session.get("step")
    if session.get("task_id") and step in WAIT_ALLOWED and action not in WAIT_ALLOWED[step]:
        await svc.reshow_wait(peer_id, session)
        return WAIT_HINTS.get(step, "Сначала ответьте на вопрос выше.")
    if action in TASK_ACTIONS and not session.get("task_id"):
        if await svc.restore_active_task(peer_id, user, session):
            return "Вернул вас на комплекс — нажмите нужную кнопку ещё раз."
        await svc.show_my_tasks(peer_id, user)
        return "Комплекс не открыт — выберите его в списке заданий."
    return None


class ActionPayloadRule(ABCRule):
    """Совпадение по {"action": ...} в payload callback-кнопки — и заодно
    достаёт user/payload/peer_id/conv_id из события одним местом, чтобы
    не повторять это в каждом обработчике."""

    def __init__(self, action: str):
        self.action = action

    async def check(self, event: dict):
        obj = event.get("object", {})
        payload = obj.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return False
        if not isinstance(payload, dict) or payload.get("action") != self.action:
            return False
        user = event.get("user")
        if not user:
            return False
        return {
            "user": user,
            "payload": payload,
            "peer_id": obj.get("peer_id"),
        }


class HasUserRule(ABCRule):
    """Для обычных текстовых/фото-сообщений — просто прокидывает user,
    которого уже положил AuthMiddleware."""

    async def check(self, event: Message):
        user = getattr(event, "user", None)
        if not user:
            return False
        return {"user": user}


labeler.auto_rules = [HasUserRule()]


def on_action(action: str):
    """Обработчик callback-кнопки. Пока бот не ответит на нажатие
    (events.ack_event), у кнопки в VK крутится «загрузка», — поэтому на каждое
    нажатие отвечаем, когда обработчик закончил (успешно или с ошибкой), а
    если он завис — всё равно через events.EventAck.limit секунд."""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(_event: dict, user: dict, payload: dict, peer_id: int):
            ack = events.EventAck(_event).start()
            note = None
            try:
                note = await _guard(action, user, peer_id)
                if note is None:
                    result = await func(_event, user, payload, peer_id)
                    await svc.after_action(user)   # что изменилось по комплексу — на сервер (общее для бригады)
                    return result
            finally:
                await ack.finish(note)

        return labeler.raw_event(GroupEventType.MESSAGE_EVENT, dict, ActionPayloadRule(action))(wrapper)

    return decorator


def get_mechanic_router() -> BotLabeler:
    return labeler


# ---------------------------------------------------------------- меню ----

@labeler.message()
async def catch_all_message(message: Message, user: dict):
    """Единственный обработчик текста/фото — маршрутизирует по текущему
    шагу диалога (navigation.get_session), т.к. это не набор команд, а
    пошаговый опрос (для Ремонт/Установка — свободное меню поверх той же
    механики)."""
    vk_id = user["vk_id"]
    session = navigation.get_session(vk_id)
    step = session.get("step")
    in_task = bool(session.get("task_id"))

    voices = svc.voice_attachments(message)
    if voices:
        if not in_task and await svc.restore_active_task(message.peer_id, user, session):
            in_task, step = True, session.get("step")
        if step == "chat":
            await svc.send(message.peer_id, "🎙 В чат с диспетчером голосовые не принимаются — напишите текстом.", svc.get_chat_keyboard())
        elif in_task and step not in _FREE_INPUT_STEPS:
            await svc.send(message.peer_id, "🎙 Сейчас голосовое не принимается — сначала завершите текущий шаг.")
            await svc.reshow_wait(message.peer_id, session)
        else:
            await svc.receive_voice(message, user, session, voices)
            await svc.after_action(user)
        return

    photos = await svc.collect_photo_attachments(message)
    if photos and step == "chat":
        await svc.chat_photos(message, user, session, photos)       # фото в чате уходят диспетчеру (а не в фото комплекса)
        return
    if photos:
        if not in_task:
            # бота перезапускали посреди работы — возвращаем монтёра на его комплекс по открытой рабочей сессии
            if await svc.restore_active_task(message.peer_id, user, session):
                in_task, step = True, session.get("step")
        if in_task and step in _FREE_INPUT_STEPS:
            await svc.receive_photos(message, user, session, photos)
            if (message.text or "").strip():                          # подпись к фото — тоже слова об итоге работ
                await svc.receive_report_text(message, user, session)
            await svc.after_action(user)
        elif in_task and step == "awaiting_photos":
            await svc.handle_photo_message(message, user, session, photos)
            await svc.after_action(user)
        elif in_task and step in WAIT_ALLOWED:
            await svc.send(message.peer_id, "Сейчас фото не принимаются — " + WAIT_HINTS.get(step, "сначала завершите текущий шаг.").lower())
            await svc.reshow_wait(message.peer_id, session)
        else:
            await svc.send(message.peer_id, "Сейчас фото не ожидается. Наберите «начать», чтобы открыть меню.")
        return

    text = (message.text or "").strip().lower()
    if text in ("начать", "start", "меню", "го", "go"):
        await svc.show_main_menu(message.peer_id, user)
        return

    if step == "chat":
        await svc.chat_message(message, user, session)
    elif step == "awaiting_text":
        await svc.handle_draft_text(message, user, session)
    elif step == "awaiting_photos":
        await svc.send(message.peer_id, "Жду фото — пришлите их или нажмите «Готово» / «Отмена».")
        await svc.reshow_wait(message.peer_id, session)
    elif step == "awaiting_equipment_serial":
        await eq_svc.handle_serial_text(message, user, session)
    elif step == "awaiting_actual_serial":
        await eq_svc.handle_actual_text(message, user, session)
    elif step == "awaiting_reason":
        await svc.handle_reason_text(message, user, session)
    elif step == "wh_awaiting_actual":
        await wh_svc.handle_actual_search(message, user, session)
    elif step == "wh_awaiting_person":
        await wh_svc.handle_person_search(message, user, session)
    elif step == "wh_awaiting_serials":
        await wh_svc.handle_serials_text(message, user, session)
    elif step == "wh_awaiting_status_serial":
        await wh_svc.handle_status_search(message, user, session)
    elif step == "awaiting_sim_state":
        await svc.send(message.peer_id, "Ответьте кнопкой: снятая SIM рабочая или нет.")
        await svc.reshow_wait(message.peer_id, session)
    elif step == "awaiting_result":
        await svc.send(message.peer_id, "Выберите итоговый статус кнопками выше.")
    elif step == "awaiting_sim_confirm":
        await svc.send(message.peer_id, "Пожалуйста, используйте кнопки выше — SIM указана верно или нет?")
    elif step == "awaiting_task_search":
        await svc.handle_task_search_text(message, session)
    elif step == "awaiting_verification":
        await svc.send(message.peer_id, "⏳ Ожидаем решение оператора — оно придёт в этот чат. Можно выйти с комплекса.", svc.get_waiting_keyboard())
    elif step == "awaiting_demontage_approval":
        await svc.send(message.peer_id, "⏳ Ожидаем решение диспетчера — оно придёт в этот чат. Можно выйти с комплекса.", svc.get_waiting_keyboard())
    elif in_task and step == "menu":
        await svc.receive_report_text(message, user, session)
    elif in_task and step == "awaiting_power_source":
        await svc.receive_report_text(message, user, session)
    else:
        await svc.send(message.peer_id, "Наберите «начать», чтобы открыть меню.")
    await svc.after_action(user)


@on_action("main_menu")
async def handle_main_menu(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.show_main_menu(peer_id, user)


@on_action("profile")
async def handle_profile(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.show_profile(peer_id, user)


@on_action("my_tasks")
async def handle_my_tasks(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.show_my_tasks(peer_id, user)


@on_action("tasks_page")
async def handle_tasks_page(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.change_tasks_page(peer_id, session, payload.get("dir") or "next")


@on_action("tasks_search")
async def handle_tasks_search(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.request_task_search(peer_id, session)


@on_action("tasks_search_cancel")
async def handle_tasks_search_cancel(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.cancel_task_search(peer_id, navigation.get_session(user["vk_id"]))


@on_action("leave_task")
async def handle_leave_task(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.leave_task_action(peer_id, user, navigation.get_session(user["vk_id"]))


@on_action("photos_done")
async def handle_photos_done(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.photos_done(peer_id, navigation.get_session(user["vk_id"]))


@on_action("photos_cancel")
async def handle_photos_cancel(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.photos_cancel(peer_id, navigation.get_session(user["vk_id"]))


@on_action("draft_save")
async def handle_draft_save(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.draft_save(peer_id, user, navigation.get_session(user["vk_id"]))


@on_action("draft_cancel")
async def handle_draft_cancel(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.draft_cancel(peer_id, navigation.get_session(user["vk_id"]))


@on_action("draft_edit")
async def handle_draft_edit(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.draft_edit(peer_id, navigation.get_session(user["vk_id"]))


@on_action("draft_keep")
async def handle_draft_keep(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.draft_keep(peer_id, navigation.get_session(user["vk_id"]), user)


@on_action("tasks_search_clear")
async def handle_tasks_search_clear(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.clear_task_search(peer_id, session)


@on_action("open_task")
async def handle_open_task(_event: dict, user: dict, payload: dict, peer_id: int):
    task_id = payload.get("task_id")
    if task_id:
        await svc.show_task(peer_id, user, task_id)


@on_action("aux_accept")
async def handle_aux_accept(_event: dict, user: dict, payload: dict, peer_id: int):
    task_id = payload.get("task_id")
    if task_id:
        await svc.accept_aux_task(peer_id, user, task_id)


@on_action("start_task")
async def handle_start_task(_event: dict, user: dict, payload: dict, peer_id: int):
    task_id = payload.get("task_id")
    if task_id:
        await svc.start_task(peer_id, user, task_id)


@on_action("set_result")
async def handle_set_result(_event: dict, user: dict, payload: dict, peer_id: int):
    status = payload.get("status")
    session = navigation.get_session(user["vk_id"])
    if status:
        await svc.set_result(peer_id, user, session, status)


@on_action("dispatcher_phone")
async def handle_dispatcher_phone(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.show_dispatcher_phone(peer_id, navigation.get_session(user["vk_id"]))


@on_action("chat_open")
async def handle_chat_open(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    session.setdefault("peer_id", peer_id)
    await svc.chat_open(peer_id, user, session)


@on_action("chat_exit")
async def handle_chat_exit(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.chat_exit(peer_id, user, navigation.get_session(user["vk_id"]))


@on_action("repair_photo")
async def handle_repair_photo(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.repair_request_photo(peer_id, navigation.get_session(user["vk_id"]))


@on_action("photo_rm")
async def handle_photo_rm(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.photo_remove(peer_id, navigation.get_session(user["vk_id"]), "last" if payload.get("scope") == "last" else "all")


@on_action("report_rm")
async def handle_report_rm(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.report_remove(peer_id, navigation.get_session(user["vk_id"]), "last" if payload.get("scope") == "last" else "all")


@on_action("report_edit")
async def handle_report_edit(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.report_edit(peer_id, navigation.get_session(user["vk_id"]))


@on_action("repair_report")
async def handle_repair_report(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_request_report(peer_id, session)


@on_action("repair_equipment")
async def handle_repair_equipment(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_request_equipment(peer_id, session)


@on_action("repair_equipment_other")
async def handle_repair_equipment_other(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_request_equipment_other(peer_id, session)


@on_action("repair_shot")
async def handle_repair_shot(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_request_shot(peer_id, session)


@on_action("repair_sim_change")
async def handle_repair_sim_change(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.request_sim_change(peer_id, session)


@on_action("to_repair_mode")
async def handle_to_repair_mode(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.to_repair_mode(peer_id, session)


@on_action("from_repair_mode")
async def handle_from_repair_mode(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.from_repair_mode(peer_id, session)


@on_action("set_power_source")
async def handle_set_power_source(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.set_power_source(peer_id, session, str(payload.get("value") or ""))


@on_action("set_outcome")
async def handle_set_outcome(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    try:
        index = int(payload.get("i"))
    except (TypeError, ValueError):
        return
    await svc.set_outcome(peer_id, user, session, index)


@on_action("simple_finish")
async def handle_simple_finish(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.simple_finish(peer_id, user, session)


@on_action("repair_no_power")
async def handle_repair_no_power(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_set_power_status(peer_id, session, "no_power", user)


@on_action("repair_not_connected")
async def handle_repair_not_connected(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_set_power_status(peer_id, session, "not_connected", user)


@on_action("finish_anyway")
async def handle_finish_anyway(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_finish_anyway(peer_id, session)


@on_action("repair_check")
async def handle_repair_check(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_check_availability(peer_id, user, session)


@on_action("sim_confirm_yes")
async def handle_sim_confirm_yes(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.handle_sim_confirm(peer_id, session, True)


@on_action("sim_confirm_no")
async def handle_sim_confirm_no(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.handle_sim_confirm(peer_id, session, False)


@on_action("repair_finish")
async def handle_repair_finish(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_finish(peer_id, user, session)


@on_action("repair_release_request")
async def handle_repair_release_request(_event: dict, user: dict, payload: dict, peer_id: int):
    session = navigation.get_session(user["vk_id"])
    await svc.repair_release_request(peer_id, user, session)


@on_action("repair_menu_back")
async def handle_repair_menu_back(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.repair_menu_back(peer_id, navigation.get_session(user["vk_id"]))


# ---------------------------------------------------------------- оборудование комплекса (монтёр) ----

def _session(user: dict) -> dict:
    return navigation.get_session(user["vk_id"])


@on_action("my_equipment")
async def handle_my_equipment(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.show_my_equipment(peer_id, user)


@on_action("eq_catalog")
async def handle_eq_catalog(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.show_catalog(peer_id, user, _session(user))


@on_action("eq_cat")
async def handle_eq_cat(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.choose_category(peer_id, user, _session(user), int(payload.get("c") or 0))


@on_action("eq_item")
async def handle_eq_item(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.choose_item(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("eq_repl")
async def handle_eq_repl(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.start_replace(peer_id, user, _session(user))


@on_action("eq_rm")
async def handle_eq_rm(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.start_remove(peer_id, user, _session(user))


@on_action("eq_new")
async def handle_eq_new(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.start_install(peer_id, user, _session(user))


@on_action("eq_newc")
async def handle_eq_newc(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.choose_install_category(peer_id, user, _session(user), int(payload.get("c") or 0))


@on_action("eq_pick")
async def handle_eq_pick(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.pick_new(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("eq_other")
async def handle_eq_other(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.request_actual(peer_id, _session(user))


@on_action("eq_actpick")
async def handle_eq_actpick(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.pick_actual(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("eq_actnew")
async def handle_eq_actnew(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.new_actual(peer_id, user, _session(user))


@on_action("eq_serial")
async def handle_eq_serial(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.request_serial(peer_id, _session(user))


@on_action("eq_suffix_ok")
async def handle_eq_suffix_ok(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.suffix_ok(peer_id, user, _session(user))


@on_action("eq_do")
async def handle_eq_do(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.do_confirm(peer_id, user, _session(user))


@on_action("eq_reason")
async def handle_eq_reason(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.change_reason(peer_id, _session(user))


@on_action("rsn_pick")
async def handle_rsn_pick(_event: dict, user: dict, payload: dict, peer_id: int):
    try:
        index = int(payload.get("i"))
    except (TypeError, ValueError):
        return
    await svc.pick_reason(peer_id, user, _session(user), index)


@on_action("rsn_back")
async def handle_rsn_back(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.reason_cancel(peer_id, user, _session(user))


@on_action("sim_state_pick")
async def handle_sim_state_pick(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.pick_sim_state(peer_id, user, _session(user), bool(int(payload.get("w") or 0)))


@on_action("sim_state_back")
async def handle_sim_state_back(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.sim_state_cancel(peer_id, _session(user))


@on_action("repair_sim_fix")
async def handle_repair_sim_fix(_event: dict, user: dict, payload: dict, peer_id: int):
    await svc.request_sim_fix(peer_id, _session(user))


@on_action("dem_scope")
async def handle_dem_scope(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.start_demontage_scope(peer_id, user, _session(user))


@on_action("dem_whole")
async def handle_dem_whole(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_whole(peer_id, user, _session(user))


@on_action("dem_whole_ok")
async def handle_dem_whole_ok(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_whole_ok(peer_id, user, _session(user))


@on_action("dem_parts")
async def handle_dem_parts(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_parts(peer_id, user, _session(user))


@on_action("dem_tog")
async def handle_dem_tog(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_toggle(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("dem_other")
async def handle_dem_other(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_other(peer_id, _session(user))


@on_action("dem_parts_ok")
async def handle_dem_parts_ok(_event: dict, user: dict, payload: dict, peer_id: int):
    await eq_svc.dem_parts_ok(peer_id, user, _session(user))


@on_action("pg")
async def handle_page(_event: dict, user: dict, payload: dict, peer_id: int):
    await views.turn_page(peer_id, user, _session(user), int(payload.get("n") or 0))


# ---------------------------------------------------------------- склад (роль «склад») ----

@on_action("wh_accept")
async def handle_wh_accept(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_accept(peer_id, user, _session(user))


@on_action("wh_accp")
async def handle_wh_accp(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_person_items(peer_id, user, _session(user), int(payload.get("p") or 0))


@on_action("wh_acc1")
async def handle_wh_acc1(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.accept_items(peer_id, user, _session(user), [int(payload.get("i") or 0)])


@on_action("wh_accall")
async def handle_wh_accall(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.accept_items(peer_id, user, _session(user), None)


@on_action("wh_issue")
async def handle_wh_issue(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.start_issue(peer_id, user, _session(user))


@on_action("wh_findp")
async def handle_wh_findp(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.request_person_search(peer_id, _session(user))


@on_action("wh_iss_p")
async def handle_wh_iss_p(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.issue_to_person(peer_id, user, _session(user), int(payload.get("p") or 0))


@on_action("wh_iss_c")
async def handle_wh_iss_c(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_issue_category(peer_id, user, _session(user), int(payload.get("c") or 0))


@on_action("wh_iss_t")
async def handle_wh_iss_t(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.toggle_issue_item(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("wh_iss_back")
async def handle_wh_iss_back(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_issue_screen(peer_id, user, _session(user))


@on_action("wh_iss_do")
async def handle_wh_iss_do(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.do_issue(peer_id, user, _session(user))


@on_action("wh_status")
async def handle_wh_status(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.request_status_search(peer_id, user, _session(user))


@on_action("wh_st_i")
async def handle_wh_st_i(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_item(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("wh_st_s")
async def handle_wh_st_s(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.set_item_status(peer_id, user, _session(user), int(payload.get("s") or 0))


@on_action("wh_iss_find")
async def handle_wh_iss_find(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.request_issue_search(peer_id, _session(user))


@on_action("wh_other")
async def handle_wh_other(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.start_other(peer_id, user, _session(user))


@on_action("wh_oth_i")
async def handle_wh_oth_i(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.choose_other_item(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("wh_actpick")
async def handle_wh_actpick(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.pick_actual(peer_id, user, _session(user), int(payload.get("i") or 0))


@on_action("wh_actnew")
async def handle_wh_actnew(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.new_actual(peer_id, user, _session(user))


@on_action("wh_subdo")
async def handle_wh_subdo(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.do_substitute(peer_id, user, _session(user))


@on_action("wh_found")
async def handle_wh_found(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.mark_found(peer_id, user, _session(user))


@on_action("wh_stock")
async def handle_wh_stock(_event: dict, user: dict, payload: dict, peer_id: int):
    await wh_svc.show_stock(peer_id, user, _session(user))


@labeler.raw_event(GroupEventType.MESSAGE_EVENT, dict)
async def handle_unknown_button(_event: dict):
    """Последним: нажатие, не подошедшее ни под одну кнопку (клавиатура из
    старого сообщения, действие, которого больше нет). Без ответа кнопка
    крутилась бы бесконечно — отвечаем подсказкой."""
    await events.ack_event(_event, "Эта кнопка устарела. Наберите «начать» — откроется актуальное меню.")
