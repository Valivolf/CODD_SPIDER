"""Vocabulary for "what kind of work is this" — set on a task by the
dispatcher when it's created/scheduled (tasks.type), not chosen after the
fact in the visit report (task_visit_reports_repository derives its
visit_type from the task's own type — see routes_task_visit_reports.py).

The list is a managed dictionary now (table work_types, «Настройки → Типы
выездов»): types can be added and removed, and each one carries a
"связан с ремонтом" flag — repair-related types get the full repair menu in
the VK bot and may carry «что сняли / что поставили» equipment notes on the
visit report. The rows live in the database; this module only holds the
in-memory copy that the many synchronous call sites read (refreshed at
startup and after every edit by work_types_repository.load()).
"""
from __future__ import annotations

# Seeded into an empty work_types table on first start (and the fallback
# until the table has been read): (name, is_repair).
DEFAULT_WORK_TYPES: list[tuple[str, bool]] = [
    ("Опиловка", False),
    ("Ремонт", True),
    ("Юстировка", False),
    ("Проверка точки подключения", False),
    ("Инвентаризация", False),
    ("Подключение кабеля", True),
    ("Совместный выезд", False),
    ("Замер зон фиксации", False),
    ("Установка", True),
    ("Демонтаж", True),
    ("Вспомогательная точка", False),
]

# A visit that started as a non-repair type and was switched to repair mode
# in the bot is recorded as "<type> + Ремонт" (e.g. "Инвентаризация +
# Ремонт") in reports and exports.
REPAIR_TYPE_NAME = "Ремонт"
COMBINED_SEPARATOR = " + "

# Как вид работ называется на бланке план-задания: там пишут «Восстановление
# АПК», «Опиловка АПК», а не внутренние «Ремонт»/«Опиловка» (остальные типы
# — как есть). Зеркалится в static/js/app.js (COPY_TYPE_LABEL) для текста
# бригады, где те же слова короче: «Восстановление».
PLAN_TYPE_LABELS = {"Ремонт": "Восстановление АПК", "Опиловка": "Опиловка АПК"}

# Инвентаризация на бланке — одна строка на всю бригаду с числом комплексов
# («Кол-во АПК: 27шт»), а не отдельная строка на каждый комплекс.
INVENTORY_TYPE = "Инвентаризация"

# «Вспомогательная точка» — не работа, а метка с информацией на карте бригады
# («откуда подключать SIP до комплекса»). Живёт только в задании того дня, когда её
# добавили (координаты — в самом задании, tasks.aux_lat/aux_lon): в справочник «Все АПК»
# не попадает, монтёру в боте как точка работы не показывается, в планах, отчётах и
# статистике не учитывается; на карту бригады встаёт своим цветом.
AUX_TYPE_NAME = "Вспомогательная точка"


def is_aux_type(name: str | None) -> bool:
    return (name or "").strip() == AUX_TYPE_NAME


# Типы со встроенным поведением (по названию): их нельзя переименовать, иначе
# оно пропадёт — «Ремонт» (в «<тип> + Ремонт»), «Установка»/«Демонтаж» (итоговый
# статус комплекса в боте), «Инвентаризация» (одна строка в плане-задании),
# «Вспомогательная точка» (метка на карте, а не работа на комплексе).
LOCKED_TYPE_NAMES = {"Ремонт", "Установка", "Демонтаж", "Инвентаризация", AUX_TYPE_NAME}

# «Статусы выезда» (итоги работ) у не связанных с ремонтом типов: список меток-
# кнопок бота при завершении («Найден» / «Не найден»…). Это только запись в
# отчётах и статистике — на статус комплекса они не влияют.
MAX_OUTCOMES = 8
MAX_OUTCOME_LABEL = 30

_types: list[tuple[str, bool]] = list(DEFAULT_WORK_TYPES)
# Типы с включённым «Проверка комплекса и SIM в боте» (Настройки → Типы выездов): в простом меню бота у них кнопки «Проверка
# доступности» (результат только сообщается монтёру — диспетчеру на проверку не уходит) и «Замена SIM-карты». У типов, связанных
# с ремонтом, эти кнопки есть всегда, поэтому для них переключатель ничего не меняет.
_bot_tools: set[str] = set()


def set_work_types(rows: list[tuple[str, bool]]) -> None:
    """Replaces the in-memory copy — called by work_types_repository.load()."""
    _types[:] = [(str(name), bool(is_repair)) for name, is_repair in rows]


def set_bot_tools_types(names: list[str]) -> None:
    """Replaces the in-memory set of types with the bot tools switch on — called by work_types_repository.load()."""
    _bot_tools.clear()
    _bot_tools.update(str(n).strip() for n in names)


def has_bot_tools(name: str | None) -> bool:
    """Whether the bot's simple menu gets availability check + SIM replacement for this (non-repair) type."""
    return bool(name) and str(name).strip() in _bot_tools and not is_repair_type(name)


def work_type_names() -> list[str]:
    return [name for name, _ in _types]


def repair_type_names() -> list[str]:
    return [name for name, is_repair in _types if is_repair]


def is_repair_type(name: str | None) -> bool:
    """Whether a task/visit type is repair-related. Understands the combined
    "Инвентаризация + Ремонт" form: it counts as repair if any part does."""
    if not name:
        return False
    repair = set(repair_type_names())
    return any(part.strip() in repair for part in str(name).split(COMBINED_SEPARATOR))


def combined_visit_type(base: str) -> str:
    """"Инвентаризация" → "Инвентаризация + Ремонт" (unchanged if the type
    is already repair-related or already combined)."""
    base = (base or "").strip()
    if not base or is_repair_type(base):
        return base
    return f"{base}{COMBINED_SEPARATOR}{REPAIR_TYPE_NAME}"
