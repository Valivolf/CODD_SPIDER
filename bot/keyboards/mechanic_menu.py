"""Клавиатуры бота-монтёра. Единственная роль — Callback-кнопки с
{"action": ...} пейлоадом, как в исходном боте."""
from vkbottle import Keyboard, Callback

RESULT_STATUSES = ["Работает", "Не работает", "Демонтирован", "Установлен"]

# Причины снятия оборудования — кнопки; можно и написать свою (зеркалит app/core/equipment.py::REMOVAL_REASONS).
REMOVAL_REASONS = [
    "Неисправно", "Не включается", "Нет связи", "Механическое повреждение", "Плановая замена", "Причина неизвестна",
]


def get_unauthorized_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🔄 Проверить снова", {"action": "main_menu"}))
    keyboard.row()
    keyboard.add(Callback("📝 Отправить заявку", {"action": "request_join"}))
    return keyboard


def get_deactivated_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🔄 Проверить снова", {"action": "main_menu"}))
    return keyboard


def get_main_menu_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("📋 Мои задания на сегодня", {"action": "my_tasks"}))
    keyboard.row()
    keyboard.add(Callback("🎒 Моё оборудование", {"action": "my_equipment"}))
    keyboard.row()
    keyboard.add(Callback("✉️ Написать диспетчеру", {"action": "chat_open"}))
    keyboard.row()
    keyboard.add(Callback("👤 Профиль", {"action": "profile"}))
    return keyboard


def get_back_to_menu_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🏠 Главное меню", {"action": "main_menu"}))
    return keyboard


# VK принимает inline-клавиатуру не больше чем из 6 рядов и 10 кнопок
# суммарно — иначе messages.send падает с ошибкой 911 «Keyboard format is
# invalid: buttons contain too much rows», и список заданий не приходит
# вовсе. Поэтому страницу списка собираем под этот бюджет: каждое задание —
# своя кнопка в своём ряду (номер комплекса с типом работы помещается
# целиком), а всё, что осталось от 6 рядов, — под задания.
VK_INLINE_MAX_ROWS = 6
VK_INLINE_MAX_BUTTONS = 10


def _pager_in_controls_row(paged: bool, has_filter: bool) -> bool:
    """Листание ◀ ▶ делит ряд с «Поиск»/«Меню» (4 кнопки в ряду), чтобы
    освободить ряд под ещё одно задание. При активном поиске в ряду
    управления уже три кнопки («Поиск», «Сбросить», «Меню») — 5 в ряд VK
    не гарантирует, поэтому листание тогда идёт отдельным рядом."""
    return paged and not has_filter


def task_page_size(total: int, has_filter: bool) -> int:
    """Сколько заданий помещается на одну страницу списка — по одному в ряд.
    Без листания (все влезли) ряд остаётся только под кнопки управления;
    с листанием — под управление и (при активном поиске) под отдельный ряд
    ◀ ▶. Размер один и тот же на всех страницах, пока не меняется поиск."""
    if total <= VK_INLINE_MAX_ROWS - 1:
        return max(total, 1)
    return VK_INLINE_MAX_ROWS - (1 if _pager_in_controls_row(True, has_filter) else 2)


def task_button_label(t: dict) -> str:
    """Подпись кнопки задания в списке (VK — не больше 40 символов). Взятый в работу комплекс (по нему уже есть
    прогресс, фото или на нём кто-то работает) помечен 🔧 и, если помещается, числом фото и значком отчёта —
    список одинаково показывает это обоим монтёрам бригады."""
    if t.get("is_aux"):
        return f"ℹ️ {t.get('point') or '?'} · вспомогательная"[:40]
    base = f"{'🔧' if t.get('started') else '📍'} {t.get('point') or '?'} · {t.get('type') or '?'}"
    extras = ""
    if t.get("photo_count"):
        extras += f" 📷{t['photo_count']}"
    if t.get("has_report"):
        extras += " 📝"
    if t.get("in_work_by"):
        extras += " 👷"
    return (base + extras) if len(base + extras) <= 40 else base[:40]


def get_task_list_keyboard(tasks: list[dict], has_prev: bool, has_next: bool, has_filter: bool = False) -> Keyboard:
    """`tasks` is just the current page, sized by task_page_size();
    `has_prev`/`has_next` say whether to show the pager buttons (◀ / ▶ are
    the "slider" through the other pages of complexes)."""
    paged = has_prev or has_next
    inline_pager = _pager_in_controls_row(paged, has_filter)
    keyboard = Keyboard(inline=True)
    for t in tasks:
        keyboard.add(Callback(task_button_label(t), {"action": "open_task", "task_id": t["id"]}))
        keyboard.row()
    if paged:
        if has_prev:
            keyboard.add(Callback("◀" if inline_pager else "◀ Назад", {"action": "tasks_page", "dir": "prev"}))
        if has_next:
            keyboard.add(Callback("▶" if inline_pager else "Далее ▶", {"action": "tasks_page", "dir": "next"}))
        if not inline_pager:
            keyboard.row()
    keyboard.add(Callback("🔎 Поиск", {"action": "tasks_search"}))
    if has_filter:
        keyboard.add(Callback("❌ Сбросить", {"action": "tasks_search_clear"}))
    keyboard.add(Callback("🏠 Меню", {"action": "main_menu"}))
    return keyboard


def get_aux_task_keyboard(task_id: str) -> Keyboard:
    """Вспомогательная точка — метка с информацией, работы на ней нет: только «Принято» (точка выполнена,
    на карте бригады становится зелёной) и «Выйти» (назад к списку комплексов)."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Принято", {"action": "aux_accept", "task_id": task_id}))
    keyboard.add(Callback("🚪 Выйти", {"action": "my_tasks"}))
    return keyboard


def get_task_opened_keyboard(task_id: str, resume: bool = False) -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("▶️ Продолжить работу" if resume else "▶️ Начать работу", {"action": "start_task", "task_id": task_id}))
    keyboard.row()
    keyboard.add(Callback("🔙 К списку заданий", {"action": "my_tasks"}))
    return keyboard


def _photo_label(photo_count: int) -> str:
    return f"📷 Фото ({photo_count})" if photo_count else "📷 Фото"


def _report_label(has_report: bool) -> str:
    return "📝 Отчёт ✅" if has_report else "📝 Отчёт"


def get_simple_menu_keyboard(photo_count: int = 0, has_report: bool = False, tools: bool = False) -> Keyboard:
    """Меню задания НЕ связанного с ремонтом типа (инвентаризация, опиловка,
    юстировка…): фото, отчёт, завершить — и «Перейти в режим ремонта» на
    случай, если на точке обнаружилась неисправность или её решили
    восстановить (тогда визит в отчётах пишется «<тип> + Ремонт»).

    `tools` — у типа включено «Проверка комплекса и SIM в боте» (Настройки →
    Типы выездов): добавляются «Проверка доступности» (результат только
    сообщается монтёру, диспетчеру на проверку не уходит) и «Замена SIM-карты»."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback(_photo_label(photo_count), {"action": "repair_photo"}))
    keyboard.add(Callback(_report_label(has_report), {"action": "repair_report"}))
    keyboard.row()
    keyboard.add(Callback("🧴 Расходники", {"action": "consumables"}))
    keyboard.row()
    if tools:
        keyboard.add(Callback("🔍 Проверка доступности", {"action": "repair_check"}))
        keyboard.add(Callback("💳 Замена SIM-карты", {"action": "repair_sim_change"}))
        keyboard.row()
    keyboard.add(Callback("🔧 Перейти в режим ремонта", {"action": "to_repair_mode"}))
    keyboard.row()
    keyboard.add(Callback("📞 Диспетчер", {"action": "dispatcher_phone"}))
    keyboard.add(Callback("🚪 Выйти", {"action": "leave_task"}))
    keyboard.row()
    keyboard.add(Callback("✅ Завершить", {"action": "simple_finish"}))
    return keyboard


def get_power_source_keyboard() -> Keyboard:
    """Два варианта при взятии комплекса в работу, если питание на нём ещё не
    указано: СЭС или Электросеть."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("☀️ СЭС", {"action": "set_power_source", "value": "СЭС"}))
    keyboard.add(Callback("🔌 Электросеть", {"action": "set_power_source", "value": "Электросеть"}))
    keyboard.row()
    keyboard.add(Callback("🚪 Выйти", {"action": "leave_task"}))
    return keyboard


def get_outcome_keyboard(labels: list[str]) -> Keyboard:
    """Кнопки «статусов выезда» типа (Настройки → Типы выездов) при завершении:
    по две в ряд, в payload — номер кнопки (сами подписи берутся из сессии)."""
    keyboard = Keyboard(inline=True)
    for i, label in enumerate(labels):
        keyboard.add(Callback(label[:40], {"action": "set_outcome", "i": i}))
        if i % 2 == 1 and i + 1 < len(labels):
            keyboard.row()
    return keyboard


def get_equipment_menu_keyboard(shot: bool = False) -> Keyboard:
    """«🔧 Замена оборудования» — что именно меняли: оборудование комплекса по
    категориям и номерам (то, что выдано монтёру со склада), SIM-карту
    (отдельный сценарий: сохраняется новый номер, проверяется доступность)
    или любое другое оборудование текстом. «💥 Комплекс расстрелян» (как и «Нет питания» / «Не подведено питание»)
    здесь больше нет: эти отметки ставятся в сценарии «Завершить», когда комплекс недоступен
    (service/mechanic.py::repair_finish_anyway). Параметр `shot` оставлен для совместимости и не используется."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🧩 Оборудование комплекса", {"action": "eq_catalog"}))
    keyboard.row()
    keyboard.add(Callback("💳 Замена SIM-карты", {"action": "repair_sim_change"}))
    keyboard.row()
    keyboard.add(Callback("✏️ Исправить номер SIM", {"action": "repair_sim_fix"}))
    keyboard.row()
    keyboard.add(Callback("📝 Другое оборудование", {"action": "repair_equipment_other"}))
    keyboard.row()
    keyboard.add(Callback("⬅ Назад в меню", {"action": "repair_menu_back"}))
    return keyboard


def get_sim_confirm_keyboard() -> Keyboard:
    """Показывается, когда доступа нет, но SIM на точке уже была указана —
    вместо того чтобы сразу переспрашивать номер, сверяем то, что уже
    сохранено."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Да, верно", {"action": "sim_confirm_yes"}))
    keyboard.add(Callback("✏️ Нет, другой номер", {"action": "sim_confirm_no"}))
    keyboard.row()
    # Нет пинга после первой проверки — возможно, SIM как раз и неисправна:
    # можно сразу заменить её, не открывая «Замену оборудования».
    keyboard.add(Callback("💳 Заменили SIM-карту", {"action": "repair_sim_change"}))
    return keyboard


def get_no_access_decision_keyboard() -> Keyboard:
    """Показывается по «Завершить», если комплекс так и остался
    недоступен (нет пинга и порта) или есть только пинг без веб-интерфейса.
    «Всё равно завершить» — монтёр сам отмечает причину (нет питания / не подведено питание /
    расстрелян, см. get_unavailable_reason_keyboard) и отчёт уходит с итогом «Не работает»;
    «Уехать без ремонта» шлёт запрос диспетчеру (ответ придёт сюда). Демонтаж отсюда не
    запрашивается: его оформляет диспетчер в веб-панели."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Всё равно завершить", {"action": "finish_anyway"}))
    keyboard.row()
    keyboard.add(Callback("🔓 Уехать без ремонта", {"action": "repair_release_request"}))
    keyboard.row()
    keyboard.add(Callback("🔄 Проверить доступ ещё раз", {"action": "repair_check"}))
    keyboard.row()
    keyboard.add(Callback("⬅ Вернуться", {"action": "repair_menu_back"}))
    return keyboard


def get_unavailable_reason_keyboard() -> Keyboard:
    """«Всё равно завершить» при недоступном комплексе: почему он не заработал. Нажатие ставит отметку
    и сразу продолжает завершение (отчёт → «что неисправно» → «что нужно»)."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🔌 Нет питания", {"action": "repair_no_power"}))
    keyboard.row()
    keyboard.add(Callback("⚡ Не подведено питание", {"action": "repair_not_connected"}))
    keyboard.row()
    keyboard.add(Callback("💥 Комплекс расстрелян", {"action": "repair_shot"}))
    keyboard.row()
    keyboard.add(Callback("⬅ Вернуться", {"action": "repair_menu_back"}))
    return keyboard


def get_repair_menu_keyboard(
    show_power_buttons: bool = False, show_sim_button: bool = False, photo_count: int = 0, has_report: bool = False,
    can_undo_repair: bool = False, demontage: str | None = None,
) -> Keyboard:
    """Ремонт/Установка — свободный порядок действий, монтёр сам решает,
    что делать дальше (в отличие от остальных типов работ, где шаги идут
    строго по очереди). «Нет питания» / «Не подведено питание» / «Расстрелян» в этом меню больше нет: они
    предлагаются при «Завершить», когда комплекс недоступен (service/mechanic.py::repair_finish_anyway;
    `show_power_buttons` оставлен для совместимости, по умолчанию выключен). Когда проверка доступности
    показала, что доступа нет, в меню появляется «Замена SIM-карты»
    (нет пинга — частая причина в самой SIM). Запроса на демонтаж в меню нет
    — демонтаж оформляет диспетчер в веб-панели.

    `can_undo_repair` — режим ремонта включили кнопкой «Перейти в режим ремонта», но ничего ремонтного ещё не
    сделано: можно вернуться (иначе в отчёт попадёт «… + Ремонт»). `demontage` — задание на демонтаж: вместо
    проверки доступности кнопка «Что демонтировано» ('todo' — ещё не указано, 'done' — указано)."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback(_photo_label(photo_count), {"action": "repair_photo"}))
    keyboard.add(Callback(_report_label(has_report), {"action": "repair_report"}))
    keyboard.row()
    keyboard.add(Callback("🧴 Расходники", {"action": "consumables"}))
    keyboard.add(Callback("🔧 Замена оборудования", {"action": "repair_equipment"}))
    keyboard.row()
    if demontage:
        keyboard.add(Callback("🧰 Что демонтировано ✅" if demontage == "done" else "🧰 Что демонтировано", {"action": "dem_scope"}))
        keyboard.row()
        keyboard.add(Callback("📞 Диспетчер", {"action": "dispatcher_phone"}))
        keyboard.row()
    else:
        if show_power_buttons:
            keyboard.add(Callback("🔌 Нет питания", {"action": "repair_no_power"}))
            keyboard.add(Callback("⚡ Не подведено питание", {"action": "repair_not_connected"}))
            keyboard.row()
        elif show_sim_button:
            keyboard.add(Callback("💳 Замена SIM-карты", {"action": "repair_sim_change"}))
            keyboard.row()
        keyboard.add(Callback("🔍 Проверка доступности", {"action": "repair_check"}))
        keyboard.add(Callback("📞 Диспетчер", {"action": "dispatcher_phone"}))
        keyboard.row()
    if can_undo_repair:
        keyboard.add(Callback("↩ Это не ремонт", {"action": "from_repair_mode"}))
        keyboard.row()
    keyboard.add(Callback("✅ Завершить", {"action": "repair_finish"}))
    keyboard.add(Callback("🚪 Выйти", {"action": "leave_task"}))
    return keyboard


def get_result_status_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    for i, status in enumerate(RESULT_STATUSES):
        keyboard.add(Callback(status, {"action": "set_result", "status": status}))
        if i % 2 == 1:
            keyboard.row()
    keyboard.add(Callback("⬅ Назад в меню", {"action": "repair_menu_back"}))
    return keyboard


# ---------------------------------------------------------------- меню ожидания ввода ----
# Пока бот ждёт от монтёра фото или текст, меню задания не показывается — только кнопки этого ожидания
# (см. handlers/mechanic.py: остальные кнопки в такие моменты не срабатывают), чтобы нельзя было нажать
# «дальше», пока предыдущее не отправлено.

def get_photo_panel_keyboard(last_n: int, mine_n: int) -> Keyboard:
    """«📷 Фото» в меню комплекса: фото можно просто присылать (без кнопок), а здесь — убрать присланное по ошибке:
    последнюю пачку (то, что отправили подряд) или все свои фото по этому комплексу."""
    keyboard = Keyboard(inline=True)
    if last_n:
        keyboard.add(Callback(f"🗑 Убрать последние ({last_n})", {"action": "photo_rm", "scope": "last"}))
        keyboard.row()
    if mine_n and mine_n != last_n:
        keyboard.add(Callback(f"🗑 Убрать все мои ({mine_n})", {"action": "photo_rm", "scope": "all"}))
        keyboard.row()
    keyboard.add(Callback("⬅ Меню", {"action": "repair_menu_back"}))
    return keyboard


def get_report_panel_keyboard(has_report: bool, can_undo_last: bool) -> Keyboard:
    """«📝 Отчёт» в меню комплекса: текст итога работ можно просто присылать, а здесь — поправить уже записанное."""
    keyboard = Keyboard(inline=True)
    if has_report:
        if can_undo_last:
            keyboard.add(Callback("↩ Убрать последнее", {"action": "report_rm", "scope": "last"}))
            keyboard.row()
        keyboard.add(Callback("🗑 Убрать весь итог", {"action": "report_rm", "scope": "all"}))
        keyboard.add(Callback("✏️ Изменить текст", {"action": "report_edit"}))
        keyboard.row()
    keyboard.add(Callback("⬅ Меню", {"action": "repair_menu_back"}))
    return keyboard


def get_photos_keyboard() -> Keyboard:
    """Ожидание фото: «Готово» — оставить загруженное и вернуться в меню, «Отмена» — убрать фото, присланные
    в этот раз, и вернуться."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Готово", {"action": "photos_done"}))
    keyboard.add(Callback("❌ Отмена", {"action": "photos_cancel"}))
    return keyboard


def get_draft_input_keyboard() -> Keyboard:
    """Бот ждёт текст/номер, ничего ещё не введено."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("❌ Отмена", {"action": "draft_cancel"}))
    return keyboard


def get_draft_confirm_keyboard() -> Keyboard:
    """Введённое показано монтёру: сохранить, отменить или просто написать заново."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Сохранить", {"action": "draft_save"}))
    keyboard.add(Callback("❌ Отмена", {"action": "draft_cancel"}))
    return keyboard


def get_draft_keep_keyboard() -> Keyboard:
    """Такой текст уже сохранён (например, отчёт): оставить прежний или изменить."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✏️ Изменить", {"action": "draft_edit"}))
    keyboard.add(Callback("✅ Оставить прежний", {"action": "draft_keep"}))
    return keyboard


def get_waiting_keyboard() -> Keyboard:
    """Ждём ответ диспетчера/оператора: можно позвонить или выйти с комплекса — ответ придёт в этот чат."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("📞 Диспетчер", {"action": "dispatcher_phone"}))
    keyboard.add(Callback("🚪 Выйти", {"action": "leave_task"}))
    return keyboard


def get_sim_state_keyboard() -> Keyboard:
    """Снятая при замене SIM-карта рабочая или нет — рабочую можно выдать снова, нерабочую спишут."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✅ Рабочая", {"action": "sim_state_pick", "w": 1}))
    keyboard.add(Callback("❌ Нерабочая", {"action": "sim_state_pick", "w": 0}))
    keyboard.row()
    keyboard.add(Callback("↩ Отмена", {"action": "sim_state_back"}))
    return keyboard


def get_reason_keyboard() -> Keyboard:
    """Почему снимается оборудование: готовые причины одной кнопкой (по две в ряд) или своя — текстом."""
    keyboard = Keyboard(inline=True)
    for i, label in enumerate(REMOVAL_REASONS):
        keyboard.add(Callback(label[:40], {"action": "rsn_pick", "i": i}))
        if i % 2 == 1:
            keyboard.row()
    keyboard.add(Callback("❌ Отмена", {"action": "rsn_back"}))
    return keyboard


# ---------------------------------------------------------------- чат с диспетчером ----

def get_dispatcher_keyboard(in_task: bool) -> Keyboard:
    """«📞 Диспетчер»: номера телефонов и кнопка «Написать диспетчеру» (чат; история — в веб-панели диспетчера)."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✉️ Написать диспетчеру", {"action": "chat_open"}))
    keyboard.row()
    keyboard.add(Callback("⬅ Назад", {"action": "repair_menu_back" if in_task else "main_menu"}))
    return keyboard


def get_chat_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("🚪 Выйти из чата", {"action": "chat_exit"}))
    return keyboard


def get_chat_notice_keyboard() -> Keyboard:
    """Уведомление о новом сообщении диспетчера (монтёр не в чате): ответить — открыть чат."""
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("✉️ Ответить", {"action": "chat_open"}))
    return keyboard


def get_search_cancel_keyboard() -> Keyboard:
    keyboard = Keyboard(inline=True)
    keyboard.add(Callback("❌ Отмена", {"action": "tasks_search_cancel"}))
    return keyboard
