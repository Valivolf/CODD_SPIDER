"""Field schemas for editable report templates — what's on the form, in
what group, and whether the app fills it in (`computed`) or the operator
does (manual, stored in report_field_values — see
report_values_repository.py). Computed values are never stored; they're
recalculated fresh from app data on every request (see
reports_repository.daily_report_computed). `type` is just a display hint
for the form (number vs free text) — values are stored/transmitted as
strings either way.
"""
from __future__ import annotations

# "Отчёт о работе отдела технического обслуживания комплексов ФВФ" — the
# official daily form (see the "ежедневный отчет.xlsx" reference this
# mirrors). One saved value-set per calendar date (period = ISO date).
DAILY_REPORT_FIELDS = [
    {"key": "responsible_name", "label": "Ответственный сотрудник (ФИО)", "group": "Ответственный", "computed": False, "type": "text"},
    {"key": "responsible_phone", "label": "Телефон ответственного сотрудника", "group": "Ответственный", "computed": False, "type": "text"},

    {"key": "staff_total", "label": "1.1 Сотрудников по штату, всего", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_listed", "label": "Сотрудников по списку, всего", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_vacation", "label": "В отпуске, всего", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_sick", "label": "На больничном, всего", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_involved_total", "label": "1.2 Задействовано работников, чел.", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_involved_mgmt", "label": "из них управление, чел.", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "staff_involved_electricians", "label": "из них электромонтёры, чел. (по назначенным на сегодня бригадам)", "group": "1. Списочный состав", "computed": True, "type": "number"},
    {"key": "staff_involved_drivers", "label": "из них водители, чел. (по назначенным на сегодня бригадам)", "group": "1. Списочный состав", "computed": True, "type": "number"},
    {"key": "vehicles_total", "label": "1.3 Задействовано транспортных средств, ед.", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "vehicles_lifts", "label": "из них автогидроподъёмники", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "vehicles_cover", "label": "из них машины прикрытия", "group": "1. Списочный состав", "computed": False, "type": "number"},
    {"key": "vehicles_photo", "label": "из них машины фотовидеофиксации", "group": "1. Списочный состав", "computed": False, "type": "number"},

    {"key": "restored_onsite", "label": "2.1 Восстановлено АПК ФВФ на месте, шт", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "serviced_onsite", "label": "2.2 Обслужено АПК ФВФ на месте, шт", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "removed_for_repair", "label": "2.3 Снято на ремонт (АПК, комплектующие), шт", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "restored_after_removal", "label": "2.4 Восстановлено после снятия на ремонт, шт", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "tasks_total", "label": "2.5 Заданий выездных бригад за сутки, шт — всего", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "tasks_done", "label": "из них выполнено", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},
    {"key": "tasks_not_done", "label": "из них не выполнено", "group": "2. Обслуживание АПК ФВФ", "computed": True, "type": "number"},

    {"key": "solar_total", "label": "3. АПК с солнечными панелями, шт — всего", "group": "3. Солнечные панели", "computed": False, "type": "number"},
    {"key": "solar_working", "label": "из них работают", "group": "3. Солнечные панели", "computed": False, "type": "number"},
    {"key": "solar_planned_repair", "label": "планируются ремонтные работы", "group": "3. Солнечные панели", "computed": False, "type": "number"},

    {"key": "modems_new", "label": "4. Модемы, шт — новые, на складе", "group": "4. Модемы", "computed": False, "type": "number"},
    {"key": "modems_working", "label": "работающие, на складе", "group": "4. Модемы", "computed": False, "type": "number"},
    {"key": "modems_broken", "label": "не работающие, на складе", "group": "4. Модемы", "computed": False, "type": "number"},

    {"key": "crews_count", "label": "5. Кол-во бригад", "group": "5. Бригады", "computed": True, "type": "number"},
]

DAILY_REPORT_TYPE = "daily_report"

# План-наряд letterhead — currently the .env plan_* settings (see
# app/config.py and routes_plan_print.py); made editable/saveable from the
# UI instead of requiring a redeploy to change. Global, not per-date
# (period = '').
# Заголовок бланка «План-задание». В тексте можно использовать {число} {месяц} {год} {дата} — они подставятся из
# даты плана (например «от «{число}» {месяц} {год} г.» даст «от «07» октября 2026 г.»); пустые строки для
# ручного заполнения («№ _____») остаются как есть. По умолчанию — прежний текст бланка.
DEFAULT_PLAN_TITLE = "ПЛАН ЗАДАНИЕ № _____ от «____» _______________ {год} г."

PLAN_TEMPLATE_FIELDS = [
    {"key": "plan_title", "label": "Заголовок бланка. Можно {число} {месяц} {год} {дата} — подставятся из даты плана", "group": "", "computed": False, "type": "text"},
    {"key": "org_title", "label": "Заголовок организации (кем утверждён план-наряд)", "group": "", "computed": False, "type": "textarea"},
    {"key": "approver_name", "label": "Утверждающий (ФИО)", "group": "", "computed": False, "type": "text"},
    {"key": "route_label", "label": "Маршрут по умолчанию", "group": "", "computed": False, "type": "text"},
    {"key": "safety_officer", "label": "Проводивший инструктаж по ТБ", "group": "", "computed": False, "type": "text"},
    {"key": "second_approver_title", "label": "Согласующий — должность", "group": "", "computed": False, "type": "textarea"},
    {"key": "second_approver_name", "label": "Согласующий — ФИО", "group": "", "computed": False, "type": "text"},
    {"key": "plan_title_size_pt", "label": "Размер заголовка, пунктов (12–24)", "group": "Оформление", "computed": False, "type": "number"},
    {"key": "plan_header_shift_mm", "label": "Сдвиг шапки вверх/вниз, мм (от −15 до 15)", "group": "Оформление", "computed": False, "type": "number"},
    {"key": "plan_title_align", "label": "Выравнивание заголовка: left, center или right", "group": "Оформление", "computed": False, "type": "text"},
]

PLAN_TEMPLATE_TYPE = "plan_template"


def manual_keys(fields: list[dict]) -> list[str]:
    return [f["key"] for f in fields if not f["computed"]]


# The variable catalog for operator-uploaded .docx/.xlsx report templates
# (app/services/template_engine.py, report_templates_repository.py) — shown
# to the operator as a cheat-sheet when they create a template, and used to
# build the fill-in context in reports_repository.build_template_context.
#
# Scalar variables ({{key}}) fill in anywhere in the document. The
# fields used to configure the plan-наряд are managed from «Настройки» and
# are intentionally absent from the report-variable palette.
TEMPLATE_PERIOD_VARIABLES = [
    {"key": "date", "label": "Дата — только если период состоит из одного дня"},
    {"key": "date_from", "label": "Начало периода"},
    {"key": "date_to", "label": "Конец периода"},
    {"key": "period_label", "label": "Период одной строкой — «26-09-2026» или «20-09-2026 — 26-09-2026»"},
    {"key": "text_report", "label": "Готовый текстовый отчёт целиком, как в разделе «Текстовый отчёт» на этой странице"},
    {"key": "total_assigned", "label": "Заданий назначено за период, всего"},
    {"key": "total_reported", "label": "Заданий выполнено (сдан отчёт о выезде) за период, всего"},
    {"key": "total_not_done", "label": "Заданий не выполнено за период"},
    {"key": "total_restored", "label": "Восстановлено (статус «Работает») за период"},
    {"key": "total_dismantled", "label": "Демонтировано за период"},
    {"key": "total_installed", "label": "Установлено за период"},
    {"key": "total_not_working", "label": "Осталось неисправно за период"},
    {"key": "crews_active", "label": "Бригад работало за период, уникальных"},
]

# One row per (день, бригада) in the period — used to fill a repeating
# table row (see template_engine.py: the row of the table containing any
# {{row.*}} placeholder is cloned once per record).
TEMPLATE_ROW_VARIABLES = [
    {"key": "row.index", "label": "№ по порядку"},
    {"key": "row.date", "label": "Дата"},
    {"key": "row.crew", "label": "Бригада"},
    {"key": "row.members", "label": "Состав бригады"},
    {"key": "row.assigned", "label": "Назначено заданий"},
    {"key": "row.reported", "label": "Сдано отчётов (обследовано)"},
    {"key": "row.not_done", "label": "Не выполнено"},
    {"key": "row.restored", "label": "Восстановлено"},
    {"key": "row.dismantled", "label": "Демонтировано"},
    {"key": "row.installed", "label": "Установлено"},
    {"key": "row.not_working", "label": "Осталось неисправно"},
]


def template_variable_catalog() -> list[dict]:
    """Every variable key a template can use, with a group label for the
    cheat-sheet shown when creating a template."""
    catalog = [{**v, "group": "Период"} for v in TEMPLATE_PERIOD_VARIABLES]
    catalog += [{**v, "group": "Повторяющаяся строка таблицы"} for v in TEMPLATE_ROW_VARIABLES]
    return catalog
