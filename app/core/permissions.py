"""Права доступа: каталог прав, права встроенных ролей, правила «какой адрес требует какое право».

Роль — это набор прав (app/repositories/roles_repository.py). Встроенные роли (администратор, оператор, склад, просмотр, отсутствия) — тоже наборы прав, их можно менять (кроме
администратора: у него «все права»), а новые роли администратор создаёт сам и сам выбирает, что им доступно. Право проверяется на сервере для каждого запроса (app/core/perm_guard.py) и
в самих обработчиках там, где от права зависит состав ответа (пароли и IP комплексов); интерфейс скрывает кнопки и разделы по тем же правам (static/js/ui.js: can(), data-perm).

Ключи прав — «раздел.действие». Права, которые подразумевают другие («изменять» → «видеть»), раскрываются автоматически (expand)."""
from __future__ import annotations

import re
from typing import Iterable, Optional

ALL = "*"          # особое «право»: всё (только у роли «администратор»)

# (раздел, [(ключ, название, пояснение)])
CATALOG: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Календарь и бригады", [
        ("calendar.view", "Видеть календарь, бригады и задания", "главная страница; без этого права разделы «Все АПК», «Отчёты» и др. работают, но календаря нет"),
        ("calendar.edit", "Создавать, менять, переносить и удалять задания и бригады", "в том числе командировки и перенос на другой день"),
        ("calendar.print", "Печатать план-наряд и выгружать план дня", "бланк план-наряда, Excel и KML для карт"),
        ("people.edit", "Менять персонал", "добавлять, править и удалять людей (монтёров, водителей и др.)"),
        ("plans.edit", "Планы выездов", "собирать планы, группировать, превращать в задания"),
    ]),
    ("Комплексы («Все АПК»)", [
        ("points.view", "Видеть раздел «Все АПК»", "список, карточки, карта, подгруппы"),
        ("points.edit", "Создавать, менять и удалять комплексы", "а также категории, статусы, типы выездов, подгруппы и объединение дублей"),
        ("points.secrets", "Видеть и копировать логины и пароли комплексов", "самые чувствительные сведения: давайте только тем, кому они нужны для работы"),
        ("points.network", "Видеть IP-адрес и порт, проверять связь", "в том числе выгрузка SIM с IP"),
        ("points.import", "Загружать комплексы и SIM из Excel и «Паутины»", ""),
        ("points.export", "Выгружать комплексы и SIM в Excel", ""),
        ("check.use", "Вкладка «Проверка»", "список номеров → сведения → Excel"),
    ]),
    ("Отчёты, мониторинг и фото", [
        ("reports.view", "Видеть раздел «Отчёты» и выгрузки", ""),
        ("reports.edit", "Вносить и править отчёты о выездах", "в том числе загрузка из Яндекс Форм и журнал выездов"),
        ("reports.builder", "Конструктор отчётов и шаблоны", ""),
        ("monitoring.view", "Видеть раздел «Мониторинг»", "итоги по комплексам, кто сейчас на комплексе"),
        ("monitoring.manage", "Убирать итоги и рабочие сессии, освобождать комплекс, отправлять на демонтаж", ""),
        ("photos.view", "Смотреть фото комплексов", ""),
        ("photos.upload", "Добавлять фото вручную", ""),
        ("photos.delete", "Удалять фото", ""),
        ("stats.view", "Видеть раздел «Статистика»", "в том числе вкладку «Счётчики» (числа по выбранным условиям)"),
        ("stats.counters", "Создавать и менять счётчики в «Статистике»", "название и условия счётчиков; видят счётчики все, кто видит «Статистику»"),
    ]),
    ("Работа с монтёрами", [
        ("chat.view", "Читать чат с монтёрами", ""),
        ("chat.use", "Писать монтёрам в чате", ""),
        ("chat.broadcast", "Массовая рассылка монтёрам в бота", "одно сообщение (текст и фото) всем, по условиям или по выбору; видно, кому дошло, а кому нет"),
        ("notifications.view", "Видеть раздел «Уведомления»", "проверки, ошибки данных, заявки монтёров"),
        ("notifications.resolve", "Решать заявки монтёров и ошибки данных", "проверка, демонтаж, доступ, связки SIM, заявки на вступление в бота"),
        ("maps.manage", "Карты бригад и «живые карты» (Яндекс)", ""),
        ("bot.toggle", "Включать и выключать бота VK", ""),
    ]),
    ("Склад", [
        ("warehouse.view", "Видеть раздел «Склад»", "оборудование, SIM, солнечные панели"),
        ("warehouse.edit", "Операции со складом", "приём, выдача, установка, снятие, статусы, импорт"),
        ("equipment.view", "Видеть оборудование на комплексах и историю его перемещений", ""),
        ("equipment.move", "Менять привязку оборудования к комплексам", ""),
    ]),
    ("Бухгалтерия", [
        ("accounting.view", "Видеть бухгалтерские поля в карточках АПК", ""),
        ("accounting.edit", "Менять бухгалтерские поля в карточках АПК", ""),
    ]),
    ("Отсутствия", [
        ("absences.view", "Видеть раздел «Отсутствия»", ""),
        ("absences.edit", "Вносить и править отсутствия", ""),
    ]),
    ("ИИ-помощник", [
        ("assistant.use", "Пользоваться ИИ-помощником", "помощник видит только те данные, на которые у роли есть права"),
    ]),
    ("Администрирование", [
        ("admin.users", "Пользователи и роли", "создание и удаление пользователей, пароли, создание и правка ролей. Высокий риск: позволяет раздавать права (не больше, чем есть у самого)"),
        ("admin.settings", "Настройки системы", "ИИ, погода, Яндекс-аккаунт и Формы, сетевой диск с фото"),
        ("admin.system", "«Состояние системы»", ""),
        ("admin.backup", "Копия базы: скачать и восстановить", "в копии — вся база целиком"),
        ("admin.keys", "Ключ шифрования", ""),
        ("admin.security", "Журнал безопасности", ""),
    ]),
]

LABELS: dict[str, str] = {k: label for _, items in CATALOG for k, label, _ in items}
ALL_KEYS: list[str] = [k for _, items in CATALOG for k, _, _ in items]

# Право → что оно подразумевает (раскрывается автоматически: нельзя «изменять», не умея «видеть»). Права «видеть пароли» и «видеть IP» (points.secrets, points.network) ничего не подразумевают:
# это права на сведения, где бы они ни показывались (у склада IP есть, а раздела «Все АПК» нет).
IMPLIES: dict[str, tuple[str, ...]] = {
    "calendar.edit": ("calendar.view",), "calendar.print": ("calendar.view",), "people.edit": ("calendar.view",), "plans.edit": ("calendar.view",),
    "points.edit": ("points.view",), "points.import": ("points.view",), "points.export": ("points.view",),
    "reports.edit": ("reports.view",), "reports.builder": ("reports.view",),
    "monitoring.manage": ("monitoring.view",),
    "photos.upload": ("photos.view",), "photos.delete": ("photos.view",),
    "stats.counters": ("stats.view",),
    "chat.use": ("chat.view",),
    "chat.broadcast": ("chat.use", "chat.view"),
    "notifications.resolve": ("notifications.view",),
    "warehouse.edit": ("warehouse.view",),
    "warehouse.view": ("equipment.view",),
    "equipment.move": ("equipment.view",),
    "monitoring.view": ("equipment.view",),
    "accounting.edit": ("accounting.view",),
    "absences.edit": ("absences.view",),
    # меню «Администрирование», настройки, копия базы, журнал и т. д. находятся в шапке главной страницы — без права на неё до них не добраться
    "admin.users": ("calendar.view",), "admin.settings": ("calendar.view",), "admin.system": ("calendar.view",), "admin.backup": ("calendar.view",),
    "admin.keys": ("calendar.view",), "admin.security": ("calendar.view",),
}

# Права, дающие чтение общих справочников (состояние календаря, бригады, персонал, список комплексов, категории, статусы, типы выездов…): без них у роли нет ни одного рабочего раздела.
CORE_READ: frozenset = frozenset({
    "calendar.view", "points.view", "reports.view", "monitoring.view", "notifications.view", "stats.view", "warehouse.view", "check.use", "photos.view", "chat.view", "maps.manage",
})

# Страницы: адрес → право для входа (в порядке предпочтения при выборе «стартовой» страницы)
PAGES: list[tuple[str, tuple]] = [
    ("/", ("calendar.view",)), ("/accounting", ("accounting.view",)), ("/apk", ("points.view", "check.use")), ("/warehouse", ("warehouse.view", "equipment.view")), ("/reports", ("reports.view",)), ("/monitoring", ("monitoring.view",)),
    ("/notifications", ("notifications.view",)), ("/stats", ("stats.view",)), ("/absences", ("absences.view",)), ("/assistant", ("assistant.use",)),
]


def expand(perms: Iterable[str]) -> frozenset:
    """Набор прав вместе с подразумеваемыми; неизвестные ключи отбрасываются; «*» остаётся как есть."""
    out: set[str] = set()
    for p in perms or ():
        if p == ALL:
            return frozenset({ALL})
        if p in LABELS:
            out.add(p)
            out.update(IMPLIES.get(p, ()))
    return frozenset(out)


# ---------------------------------------------------------------- права встроенных ролей (повторяют прежнее поведение ролей)
_OPERATOR = (
    "calendar.view", "calendar.edit", "calendar.print", "people.edit", "plans.edit",
    "points.view", "points.edit", "points.secrets", "points.network", "points.import", "points.export", "check.use",
    "reports.view", "reports.edit", "reports.builder", "monitoring.view", "monitoring.manage", "photos.view", "photos.upload", "photos.delete", "stats.view", "stats.counters",
    "chat.view", "chat.use", "chat.broadcast", "notifications.view", "notifications.resolve", "maps.manage", "bot.toggle",
    "absences.view", "absences.edit", "assistant.use",
)
BUILTIN: dict[str, dict] = {
    "admin": {"name": "Администратор", "description": "Всё: все разделы, настройки, пользователи и роли, копии базы, ключ шифрования, журнал безопасности.", "perms": (ALL,)},
    "user": {"name": "Оператор", "description": "Рабочая роль диспетчера: календарь, комплексы (с логинами и паролями), отчёты, мониторинг, уведомления, чат, статистика, отсутствия. Без администрирования и склада.", "perms": _OPERATOR},
    "warehouse": {"name": "Склад", "description": "Учёт оборудования: страница «Склад», операции со складом, смена статуса комплекса при приёме и выдаче; IP комплексов видит, пароли — нет.",
                  "perms": ("warehouse.view", "warehouse.edit", "points.network", "assistant.use")},
    "accounting": {"name": "Бухгалтерия", "description": "Список АПК, документы, бухгалтерские поля и состав оборудования с историей перемещений; без доступа к паролям и сетевым настройкам.",
                   "perms": ("points.view", "accounting.view", "accounting.edit", "warehouse.view", "equipment.move", "photos.view")},
    "viewer": {"name": "Только просмотр", "description": "Читает календарь, комплексы (без IP и паролей), отчёты, мониторинг, фото, уведомления и чат; ничего не меняет.",
               "perms": ("calendar.view", "points.view", "points.export", "check.use", "reports.view", "monitoring.view", "photos.view", "chat.view", "notifications.view", "absences.view", "assistant.use")},
    "absences": {"name": "Отсутствия", "description": "Только раздел «Отсутствия»: смотреть и вносить отсутствия сотрудников; остального не видит.", "perms": ("absences.view", "absences.edit", "assistant.use")},
}
BUILTIN_KEYS = tuple(BUILTIN)

# Права, появившиеся ПОСЛЕ первого выпуска ролей. Строки встроенных ролей в базе записаны при первом запуске и сами не обновляются, поэтому при запуске каждое такое право один раз
# добавляется тем встроенным ролям, у которых оно есть по умолчанию (roles_repository.ensure): иначе на уже работающем сервере «Оператор» его не получит. Новое право, которое нужно
# выдать встроенным ролям на действующих серверах, дописывается сюда в конец. Администратор потом может убрать его у роли — повторно оно не возвращается.
NEW_RIGHTS: tuple = ("stats.counters", "chat.broadcast", "equipment.view", "equipment.move", "warehouse.view", "photos.view")


def default_perms(role_key: str) -> frozenset:
    return expand(BUILTIN[role_key]["perms"]) if role_key in BUILTIN else frozenset()


# ---------------------------------------------------------------- проверки
def has(perms: Iterable[str], key: str) -> bool:
    ps = perms if isinstance(perms, (set, frozenset)) else set(perms or ())
    return ALL in ps or key in ps


def has_any(perms: Iterable[str], keys: Iterable[str]) -> bool:
    ps = perms if isinstance(perms, (set, frozenset)) else set(perms or ())
    return ALL in ps or any(k in ps for k in keys)


def is_subset(wanted: Iterable[str], granted: Iterable[str]) -> bool:
    """Нельзя выдать права, которых нет у самого выдающего (кроме «всё» — оно у администратора)."""
    g = granted if isinstance(granted, (set, frozenset)) else set(granted or ())
    if ALL in g:
        return True
    w = set(wanted or ())
    return ALL not in w and w <= g


def secrets_ok(perms: Iterable[str]) -> bool:
    """Логины и пароли комплексов."""
    return has(perms, "points.secrets")


def network_ok(perms: Iterable[str]) -> bool:
    """IP-адрес и порт комплексов."""
    return has(perms, "points.network")


def profile(perms: Iterable[str]) -> str:
    """Короткая метка «что видит роль в данных комплексов» — ключ кэша ответов (одинаковые права — один и тот же ответ)."""
    return ("s" if secrets_ok(perms) else "") + ("n" if network_ok(perms) else "") or "-"


def core_read_allowed(perms: Iterable[str]) -> bool:
    return has_any(perms, CORE_READ)


def landing(perms: Iterable[str]) -> str:
    """Куда вести пользователя после входа: первая страница, на которую у него есть право."""
    for path, keys in PAGES:
        if has_any(perms, keys):
            return path
    return "/no-access"


# ---------------------------------------------------------------- правила адресов
# (методы или None = любые, регулярное выражение пути, права «любое из», пояснение). Первое совпавшее правило решает.
# Особые значения: CORE — «любое право на чтение общих справочников»; PUBLIC — достаточно быть вошедшим.
CORE = "core"
PUBLIC = "public"
_R = re.compile
G, W = {"GET", "HEAD"}, {"POST", "PUT", "PATCH", "DELETE"}

RULES: list[tuple[Optional[set], re.Pattern, tuple, str]] = [
    # вход, свой пароль, политика, состояние здоровья контейнера
    (None, _R(r"^/api/auth(/|$)"), (PUBLIC,), "вход и смена своего пароля"),
    (None, _R(r"^/api/security/policy$"), (PUBLIC,), "политика паролей для форм"),
    (G, _R(r"^/api/weather$"), (PUBLIC,), "прогноз для фона"),
    (None, _R(r"^/api/games(/|$)"), (PUBLIC,), "игры: свои результаты и таблица рекордов (кому включены игры — решает администратор, проверка в самих маршрутах)"),
    # администрирование
    (None, _R(r"^/api/admin(/|$)"), ("admin.users",), "пользователи и роли"),
    (None, _R(r"^/api/backup(/|$)"), ("admin.backup",), "копия базы"),
    (None, _R(r"^/api/encryption(/|$)"), ("admin.keys",), "ключ шифрования"),
    (None, _R(r"^/api/security/"), ("admin.security",), "журнал безопасности"),
    (None, _R(r"^/api/system(/|$)"), ("admin.system",), "состояние системы"),
    (None, _R(r"^/api/(ai|ai-agents|forms-auto|photo-storage/(config|flush|index|marker|migrate|scan|test)|weather/(settings|test))(/|$)"), ("admin.settings",), "настройки интеграций, ИИ и компьютеры-агенты"),
    (None, _R(r"^/api/yandex-robot/(settings|view|audio|logout|netcheck|jobs/[^/]+/shot)"), ("admin.settings",), "аккаунт робота Яндекса"),
    (None, _R(r"^/api/yandex-robot(/|$)"), ("maps.manage",), "карты бригад"),
    (G, _R(r"^/api/photo-storage$"), (CORE,), "состояние сетевого диска"),
    (G, _R(r"^/api/bot-settings$"), (CORE,), "состояние бота"),
    (W, _R(r"^/api/bot-settings$"), ("bot.toggle",), "включить или выключить бота"),
    # календарь, бригады, персонал
    (G, _R(r"^/api/state$"), (CORE,), "состояние календаря"),
    (W, _R(r"^/api/state$"), ("calendar.edit",), "сохранение календаря"),
    (G, _R(r"^/api/crews(/|$)"), (CORE,), "бригады"),
    (W, _R(r"^/api/crews(/|$)"), ("calendar.edit",), "бригады: изменения"),
    (W, _R(r"^/api/people/\d+/absences(/|$)"), ("people.edit",), "отсутствия человека (карточка персонала)"),
    (G, _R(r"^/api/people/\d+/erase-preview$"), ("people.edit",), "что будет удалено и обезличено при удалении сотрудника"),
    (G, _R(r"^/api/people/\d+/tasks$"), ("people.edit",), "задания сотрудника"),
    (G, _R(r"^/api/people(/|$)"), (CORE,), "персонал"),
    (W, _R(r"^/api/people(/|$)"), ("people.edit",), "персонал: изменения"),
    (G, _R(r"^/api/absences(/|$)"), ("absences.view",), "отсутствия"),
    (W, _R(r"^/api/absences(/|$)"), ("absences.edit",), "отсутствия: изменения"),
    (None, _R(r"^/plan-print(/|$)"), ("calendar.print",), "печать план-наряда"),
    (G, _R(r"^/api/kml(/|$)"), ("calendar.print", "maps.manage", "points.export"), "KML для карт"),
    # комплексы
    (G, _R(r"^/api/points/[^/]+/(ping|check-port)$"), ("points.network",), "проверка связи"),
    (W, _R(r"^/api/points/resolve$"), ("calendar.edit", "points.import", "plans.edit", "maps.manage"), "разбор меток карты"),
    (W, _R(r"^/api/points/assign$"), ("calendar.edit", "plans.edit"), "комплексы → задания бригады"),
    (W, _R(r"^/api/points/[^/]+/status$"), ("points.edit", "warehouse.edit"), "статус комплекса"),
    (G, _R(r"^/api/points/duplicates$"), ("points.edit",), "дубли комплексов"),
    (G, _R(r"^/api/accounting/(points|point|candidates)$"), ("accounting.view",), "комплексы для бухгалтерии"),
    ({"POST"}, _R(r"^/api/accounting/export$"), ("accounting.view",), "выгрузка выбранных бухгалтерских АПК (данные не меняются)"),
    (G, _R(r"^/api/point-category-fields/point/"), ("points.view", "points.edit", "accounting.view", "accounting.edit"), "дополнительные поля комплекса: чтение"),
    (W, _R(r"^/api/point-category-fields/point/"), ("points.edit", "accounting.edit"), "дополнительные поля комплекса: изменения"),
    (G, _R(r"^/api/points(/|$)"), (CORE,), "справочник комплексов"),
    (W, _R(r"^/api/points(/|$)"), ("points.edit",), "комплексы: изменения"),
    (G, _R(r"^/api/(point-categories|point-statuses|work-types)(/|$)"), (CORE,), "справочники"),
    (W, _R(r"^/api/(point-categories|point-statuses|work-types)(/|$)"), ("points.edit",), "справочники: изменения"),
    (G, _R(r"^/api/point-plans(/|$)"), (CORE,), "планы выездов"),
    (W, _R(r"^/api/point-plans(/|$)"), ("plans.edit", "calendar.edit"), "планы выездов: изменения"),
    (G, _R(r"^/api/(point-card|point-files)(/|$)"), ("points.view", "calendar.view", "check.use"), "карточка комплекса и файлы при комплексе: чтение"),
    ({"DELETE"}, _R(r"^/api/point-files/\d+$"), ("points.edit",), "удаление файлов комплекса"),
    ({"POST"}, _R(r"^/api/point-files$"), ("points.edit", "accounting.edit"), "загрузка файла комплекса"),
    (W, _R(r"^/api/point-files(/|$)"), ("points.edit",), "изменение файлов при комплексе"),
    (G, _R(r"^/api/apk-groups(/|$)"), ("points.view",), "подгруппы"),
    (W, _R(r"^/api/apk-groups(/|$)"), ("points.edit",), "подгруппы: изменения"),
    (G, _R(r"^/api/excel/(plan|yandex)(/|-points|$)"), ("calendar.print", "maps.manage"), "выгрузки для календаря и карт"),
    (G, _R(r"^/api/excel/period-report$"), ("reports.view",), "отчёт за период"),
    (G, _R(r"^/api/excel/sim-ip-export$"), ("points.network",), "выгрузка SIM с IP"),
    (G, _R(r"^/api/excel/export$"), ("points.export",), "выгрузка комплексов"),
    (G, _R(r"^/api/excel/(template|sim-template|sim-ip-template|sim-ip-registry-template)$"), ("points.import",), "шаблоны загрузки"),
    (W, _R(r"^/api/excel/"), ("points.import",), "загрузка из Excel"),
    (W, _R(r"^/api/pautina(/|$)"), ("points.import",), "загрузка из «Паутины»"),
    (G, _R(r"^/api/pautina(/|$)"), ("points.import",), "итоги загрузки из «Паутины»"),
    (None, _R(r"^/api/check(/|$)"), ("check.use",), "вкладка «Проверка»"),
    # отчёты, мониторинг, фото, статистика
    (G, _R(r"^/api/task-visit-reports(/|$)"), ("reports.view", "monitoring.view", "calendar.view", "notifications.view"), "отчёты о выездах: чтение"),
    (W, _R(r"^/api/task-visit-reports(/|$)"), ("reports.edit",), "отчёты о выездах: изменения"),
    (G, _R(r"^/api/reports(/|$)"), ("reports.view",), "отчёты"),
    (W, _R(r"^/api/reports(/|$)"), ("reports.edit",), "отчёты: изменения"),
    (W, _R(r"^/api/report-designs/preview$"), ("reports.view",), "предпросмотр отчёта (данные не меняются)"),
    (G, _R(r"^/api/(report-designs|report-templates)(/|$)"), ("reports.view",), "конструктор отчётов: чтение"),
    (W, _R(r"^/api/(report-designs|report-templates)(/|$)"), ("reports.builder",), "конструктор отчётов: изменения"),
    (G, _R(r"^/api/visit-history(/|$)"), ("reports.edit",), "загрузка истории выездов: чтение"),
    (W, _R(r"^/api/visit-history(/|$)"), ("reports.edit",), "журнал выездов: изменения"),
    (G, _R(r"^/api/forms-import(/|$)"), ("reports.edit",), "загрузка из Яндекс Форм: чтение"),
    (W, _R(r"^/api/forms-import(/|$)"), ("reports.edit",), "загрузка из Яндекс Форм"),
    (G, _R(r"^/api/monitoring/photos"), ("photos.view",), "фото"),
    (W, _R(r"^/api/monitoring/photos/upload$"), ("photos.upload",), "добавить фото"),
    (W, _R(r"^/api/monitoring/photos"), ("photos.delete",), "удалить фото"),
    (G, _R(r"^/api/monitoring(/|$)"), ("monitoring.view",), "мониторинг"),
    (W, _R(r"^/api/monitoring(/|$)"), ("monitoring.manage",), "мониторинг: изменения"),
    (G, _R(r"^/api/stats(/|$)"), ("stats.view",), "статистика"),
    (G, _R(r"^/api/stat-counters(/|$)"), ("stats.view",), "счётчики статистики: чтение, список комплексов и выгрузка в Excel"),
    (W, _R(r"^/api/stat-counters/(preview|points)$"), ("stats.view",), "счётчики: предпросмотр и список комплексов (данные не меняются)"),
    (W, _R(r"^/api/stat-counters(/|$)"), ("stats.counters",), "счётчики статистики: создание, правка, удаление"),
    # уведомления, заявки, чат, карты
    (G, _R(r"^/api/verification-requests(/|$)"), ("notifications.view", "calendar.view", "monitoring.view"), "проверки комплексов оператором: чтение (и в «Мониторинге» — история проверок)"),
    (G, _R(r"^/api/(notifications|access-issues|demontage-requests|join-requests)(/|$)"), ("notifications.view", "calendar.view"), "уведомления и заявки: чтение"),
    (W, _R(r"^/api/(notifications|access-issues|verification-requests|demontage-requests|join-requests)(/|$)"), ("notifications.resolve",), "решения по заявкам"),
    (G, _R(r"^/api/chat/broadcast(/|$)"), ("chat.broadcast",), "массовая рассылка: получатели, история и итоги доставки"),
    (W, _R(r"^/api/chat/broadcast(/|$)"), ("chat.broadcast",), "массовая рассылка монтёрам в бота"),
    (G, _R(r"^/api/chat(/|$)"), ("chat.view",), "чат"),
    (W, _R(r"^/api/chat/\d+/read$"), ("chat.view",), "отметить переписку прочитанной"),
    (W, _R(r"^/api/chat(/|$)"), ("chat.use",), "чат: отправка"),
    (G, _R(r"^/api/account-chat(/|$)"), ("chat.view",), "личные сообщения: чтение"),
    (W, _R(r"^/api/account-chat(/|$)"), ("chat.use",), "личные сообщения: отправка и изменение своих сообщений"),
    (G, _R(r"^/api/live-maps(/|$)"), ("maps.manage", "points.view"), "живые карты: чтение"),
    (W, _R(r"^/api/live-maps(/|$)"), ("maps.manage",), "живые карты"),
    # склад
    (G, _R(r"^/api/consumables(/|$)"), ("warehouse.view",), "расходные материалы: чтение"),
    (W, _R(r"^/api/consumables(/|$)"), ("warehouse.edit",), "расходные материалы: изменения"),
    (G, _R(r"^/api/equipment(/|$)"), ("warehouse.view", "equipment.view"), "оборудование: чтение"),
    (G, _R(r"^/api/(warehouse|solar-panels)(/|$)"), ("warehouse.view",), "склад: чтение"),
    (W, _R(r"^/api/equipment/points/[^/]+/(attach|replace|detach/[^/]+|sim/(replace|fix))$"), ("warehouse.edit", "equipment.move"), "перемещение оборудования на комплексах"),
    (W, _R(r"^/api/equipment/export/"), ("warehouse.view",), "выгрузка склада в Excel (данные не меняются)"),
    (W, _R(r"^/api/(equipment|warehouse|solar-panels)(/|$)"), ("warehouse.edit",), "склад: операции"),
    # ИИ
    (None, _R(r"^/api/assistant(/|$)"), ("assistant.use",), "ИИ-помощник"),
]


def required(method: str, path: str) -> tuple[tuple, str]:
    """(права «любое из», пояснение) для запроса. Нет правила: чтение — CORE, изменение — «ALL» (только администратор)."""
    m = method.upper()
    for methods, rx, perms, note in RULES:
        if (methods is None or m in methods) and rx.search(path):
            return perms, note
    return ((CORE,) if m in G or m == "OPTIONS" else (ALL,)), "нет правила"


def allowed(method: str, path: str, perms: Iterable[str]) -> tuple[bool, tuple, str]:
    need, note = required(method, path)
    ps = perms if isinstance(perms, (set, frozenset)) else set(perms or ())
    if ALL in ps or PUBLIC in need:
        return True, need, note
    for n in need:
        if n == CORE:
            if core_read_allowed(ps):
                return True, need, note
        elif n in ps:
            return True, need, note
    return False, need, note


def need_text(need: tuple) -> str:
    names = []
    for n in need:
        names.append("любой рабочий раздел" if n == CORE else ("только администратор" if n == ALL else LABELS.get(n, n)))
    return " / ".join(names)
