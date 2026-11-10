"""Каталог данных, которые видит помощник: наборы (datasets) и их поля, с правами по правам роли (app/core/permissions.py).

Помощник НЕ пишет SQL: модель выбирает набор и поля из этого каталога, а запрос собирает и выполняет код (query_engine.py) —
только по полям, разрешённым роли вошедшего человека. Поэтому то, чего роли видеть нельзя (пароли комплексов, IP, наборы данных без права на соответствующий раздел),
модель не получит и не покажет, как бы ни был сформулирован вопрос — граница на сервере.

Права зеркалят остальное приложение: пароли и логины комплексов — по праву «points.secrets», IP и порт — «points.network» (routes_points._strip_credentials);
набор данных виден тому, у кого есть право на соответствующий раздел (Dataset.needs — «любое из»). Роль передаётся ключом, права берутся из памяти (roles_repository)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from app.core import permissions as P
from app.repositories import roles_repository

# Типы полей: text — текст; enum — текст из ограниченного списка значений; date — дата «ГГГГ-ММ-ДД»; number; bool — «да»/«нет».
TEXT, ENUM, DATE, NUMBER, BOOL = "text", "enum", "date", "number", "bool"

MSK_DATE = "date(datetime({col}, '+3 hours'))"       # время отчётов хранится в UTC, люди считают по местному (МСК)


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    sql: str
    type: str = TEXT
    enum: Optional[str] = None            # ключ списка допустимых значений (см. query_engine.enum_values)
    groupable: bool = True
    # Право, без которого поле не показывается (в дополнение к правам набора); None — всем, кто видит набор
    perm: Optional[str] = None
    # SQL зависит от настроек (список ремонтных типов и т. п.) — функция возвращает (sql, params)
    dynamic: Optional[Callable[[], tuple[str, list]]] = None
    sensitive: bool = False               # пароли и подобное: не попадает в «Как посчитано», в историю и в подсказки модели


@dataclass(frozen=True)
class Dataset:
    name: str
    label: str
    description: str
    from_sql: str
    fields: tuple
    where_sql: str = ""
    date_field: Optional[str] = None                      # по какому полю считается «период»
    period_overlap: Optional[tuple[str, str]] = None      # если запись — промежуток: (поле начала, поле конца) — берутся пересекающиеся с периодом
    default_select: tuple = ()
    default_order: tuple = ()                             # ((поле, 'asc'|'desc'), …)
    needs: tuple = ("calendar.view",)                     # набор виден тому, у кого есть любое из этих прав
    row_label: str = "записей"

    def field(self, name: str) -> Optional[Field]:
        return next((f for f in self.fields if f.name == name), None)


# Права на наборы (любое из): выезды — отчёты или мониторинг; задания и бригады — календарь; комплексы и подгруппы — «Все АПК»; смены статуса — мониторинг или отчёты;
# комплексы — «Все АПК» или склад (складу нужен справочник комплексов); оборудование — склад или «Все АПК» (оборудование стоит на комплексах); отсутствия — отсутствия, календарь или склад.
NEEDS_VISITS = ("reports.view", "monitoring.view")
NEEDS_CALENDAR = ("calendar.view",)
NEEDS_POINTS = ("points.view", "warehouse.view")
NEEDS_APK = ("points.view",)
NEEDS_STATUS = ("monitoring.view", "reports.view")
NEEDS_EQUIPMENT = ("warehouse.view", "points.view")
NEEDS_ABSENCES = ("absences.view", "calendar.view", "warehouse.view")


def _repair_sql() -> tuple[str, list]:
    from app.core.work_types import COMBINED_SEPARATOR, repair_type_names

    names = repair_type_names()
    base = f"(case when instr(r.visit_type, '{COMBINED_SEPARATOR}') > 0 then substr(r.visit_type, 1, instr(r.visit_type, '{COMBINED_SEPARATOR}') - 1) else r.visit_type end)"
    marks = ",".join("?" for _ in names) or "''"
    return f"case when {base} in ({marks}) or r.visit_type like '%{COMBINED_SEPARATOR}Ремонт' then 'да' else 'нет' end", list(names)


_VISITS_FROM = (
    "task_visit_reports r left join tasks t on t.id = r.task_id left join crews c on c.id = t.team "
    "left join points p on p.id = r.point_id left join point_categories pc on pc.id = p.category_id "
    "left join people pe on pe.id = r.author_person_id"
)

VISITS = Dataset(
    name="visits", label="Выезды (отчёты о выездах на комплексы)",
    description="по одному на каждый отчёт: что делали на комплексе, итог, причина неисправности, что нужно для восстановления",
    from_sql=_VISITS_FROM, date_field="date", row_label="выездов",
    default_select=("date", "point_id", "address", "visit_type", "status", "executor", "comment", "fault", "needs"),
    default_order=(("date", "desc"),),
    needs=NEEDS_VISITS,
    fields=(
        Field("date", "Дата выезда", MSK_DATE.format(col="r.created_at"), DATE),
        Field("point_id", "Номер комплекса", "r.point_id"),
        Field("address", "Адрес комплекса", "coalesce(p.address, '')"),
        Field("category", "Категория комплекса", "coalesce(pc.name, '')", ENUM, "point_categories"),
        Field("visit_type", "Тип выезда (бывает «Инвентаризация + Ремонт» — ремонт при другом типе)", "r.visit_type", ENUM, "work_types"),
        Field("repair", "Был ремонт (да/нет)", "", BOOL, dynamic=_repair_sql),
        Field("status", "Итог выезда (Работает / Не работает / Демонтирован / Установлен или статус выезда типа)",
              "coalesce(nullif(r.result_status, ''), nullif(r.outcome, ''), '')", ENUM, "visit_statuses"),
        Field("executor", "Исполнитель (кто выезжал)", "coalesce(pe.full_name, r.created_by, '')"),
        Field("crew", "Бригада", "coalesce(c.name, '')"),
        Field("comment", "Что сделано (итог работ)", "coalesce(r.comment, '')"),
        Field("fault", "Причина неисправности", "coalesce(r.fault, '')"),
        Field("needs", "Что нужно для восстановления", "coalesce(r.needs, '')"),
        Field("equipment_removed", "Снято оборудование", "coalesce(r.equipment_removed, '')"),
        Field("equipment_installed", "Установлено оборудование", "coalesce(r.equipment_installed, '')"),
        Field("sim_removed", "SIM снята", "coalesce(r.sim_removed, '')"),
        Field("sim_installed", "SIM установлена", "coalesce(r.sim_installed, '')"),
        Field("photos", "Количество фото", "(select count(*) from visit_photos v where v.task_id = r.task_id)", NUMBER, groupable=False),
        Field("source", "Откуда отчёт (бот/диспетчер, Яндекс Форма, архив/журнал)",
              "case when r.source = 'forms' then 'Яндекс Форма' when r.imported = 1 then 'архив/журнал' else 'бот/диспетчер' end", ENUM, "visit_sources"),
    ),
)

TASKS = Dataset(
    name="tasks", label="Задания в календаре (план работ и их выполнение)",
    description="по одному на каждое задание: когда, какая бригада, какой комплекс, какой тип работ, выполнено ли",
    from_sql=("tasks t left join crews c on c.id = t.team left join points p on p.id = t.point "
              "left join point_categories pc on pc.id = p.category_id left join task_visit_reports r on r.task_id = t.id"),
    where_sql="t.type != 'Вспомогательная точка'", date_field="date", period_overlap=("date", "date_to"), row_label="заданий",
    default_select=("date", "crew", "point_id", "address", "type", "done"), default_order=(("date", "desc"),),
    needs=NEEDS_CALENDAR,
    fields=(
        Field("date", "Дата задания (начало)", "t.date", DATE),
        Field("date_to", "Дата окончания (командировка)", "coalesce(nullif(t.date_to, ''), t.date)", DATE),
        Field("crew", "Бригада", "coalesce(c.name, '')"),
        Field("crew_members", "Состав бригады",
              "coalesce((select group_concat(pe.full_name, ', ') from crew_members cm join people pe on pe.id = cm.person_id where cm.crew_id = t.team), '')"),
        Field("point_id", "Номер комплекса", "t.point"),
        Field("address", "Адрес комплекса", "coalesce(nullif(p.address, ''), t.address, '')"),
        Field("category", "Категория комплекса", "coalesce(pc.name, '')", ENUM, "point_categories"),
        Field("type", "Тип работы", "t.type", ENUM, "work_types"),
        Field("work", "Описание работы", "coalesce(t.work, '')"),
        Field("hours", "Часы на работу", "t.hours", NUMBER, groupable=False),
        Field("travel", "Часы дороги", "t.travel", NUMBER, groupable=False),
        Field("note", "Примечание", "coalesce(t.note, '')"),
        Field("done", "Выполнено (есть отчёт о выезде)", "case when r.id is not null then 'да' else 'нет' end", BOOL),
        Field("result", "Итог выезда", "coalesce(nullif(r.result_status, ''), nullif(r.outcome, ''), '')", ENUM, "visit_statuses"),
    ),
)

POINTS = Dataset(
    name="points", label="Комплексы (справочник «Все АПК»)",
    description="по одному на каждый комплекс: адрес, категория, статус, питание, SIM, IP, когда последний раз были",
    from_sql="points p left join point_categories pc on pc.id = p.category_id", row_label="комплексов",
    default_select=("point_id", "address", "category", "status", "power_source", "last_visit"), default_order=(("point_id", "asc"),),
    needs=NEEDS_POINTS,
    fields=(
        Field("point_id", "Номер комплекса", "p.id"),
        Field("address", "Адрес", "coalesce(p.address, '')"),
        Field("description", "Описание", "coalesce(p.description, '')"),
        Field("category", "Категория", "coalesce(pc.name, '')", ENUM, "point_categories"),
        Field("status", "Статус комплекса (Работает, Не работает и др.)", "coalesce(p.status, '')", ENUM, "point_statuses"),
        Field("power_source", "Питание (СЭС / Электросеть)", "coalesce(p.power_source, '')", ENUM, "power_sources"),
        Field("sim_number", "Номер SIM", "coalesce(p.sim_number, '')"),
        Field("inventory_number", "Инвентарный номер", "coalesce(p.inventory_number, '')"),
        Field("ip_address", "IP-адрес", "coalesce(p.ip_address, '')", perm="points.network"),
        Field("port", "Порт", "coalesce(p.port, '')", perm="points.network"),
        Field("verification_valid_until", "Поверка действует до", "coalesce(p.verification_valid_until, '')", DATE),
        Field("notes", "Заметки", "coalesce(p.notes, '')"),
        Field("fault_reason", "Почему не работает (из последнего отчёта выезда)", "coalesce(p.fault_reason, '')"),
        Field("fault_need", "Что нужно для восстановления (из последнего отчёта выезда)", "coalesce(p.fault_need, '')"),
        Field("down_since", "Не отвечает с (дата)", "coalesce(substr(p.port_down_since, 1, 10), '')", DATE),
        Field("last_visit", "Дата последнего выезда",
              f"coalesce((select max({MSK_DATE.format(col='rv.created_at')}) from task_visit_reports rv where rv.point_id = p.id), '')", DATE),
        Field("visits_count", "Сколько всего выездов",
              "(select count(*) from task_visit_reports rv where rv.point_id = p.id)", NUMBER, groupable=False),
        Field("login", "Логин для входа на комплекс", "coalesce(p.login, '')", perm="points.secrets", sensitive=True, groupable=False),
        Field("password", "Пароль для входа на комплекс", "coalesce(p.password, '')", perm="points.secrets", sensitive=True, groupable=False),
    ),
)

APK_GROUPS = Dataset(
    name="apk_groups", label="Подгруппы АПК (подборки комплексов: тип работы, описание и «что нужно на выезде» по каждому)",
    description="по одной записи на каждый комплекс в каждой подгруппе: название подгруппы, тип работы, описание комплекса в подгруппе, что нужно на выезде",
    from_sql="apk_group_points m join apk_groups g on g.id = m.group_id left join points p on p.id = m.point_id",
    row_label="комплексов в подгруппах", default_select=("group", "point_id", "address", "work_type", "note", "need"),
    default_order=(("group", "asc"), ("point_id", "asc")), needs=NEEDS_APK,
    fields=(
        Field("group", "Название подгруппы", "g.name"),
        Field("point_id", "Номер комплекса", "m.point_id"),
        Field("address", "Адрес комплекса", "coalesce(p.address, '')"),
        Field("status", "Статус комплекса сейчас", "coalesce(p.status, '')", ENUM, "point_statuses"),
        Field("work_type", "Тип работы в подгруппе", "m.work_type", ENUM, "work_types"),
        Field("note", "Описание комплекса в подгруппе", "coalesce(m.note, '')"),
        Field("need", "Что нужно на выезде (в подгруппе)", "coalesce(m.need, '')"),
    ),
)

STATUS_CHANGES = Dataset(
    name="status_changes", label="Смены статуса комплексов (когда комплекс перестал работать или заработал)",
    description="по одной на каждую смену статуса комплекса: когда, какой был и стал, откуда пришла (проверка связи, отчёт, диспетчер)",
    from_sql=("point_status_history h left join points p on p.id = h.point_id left join point_categories pc on pc.id = p.category_id"),
    date_field="date", row_label="смен статуса", default_select=("date", "point_id", "address", "old_status", "new_status", "source"),
    default_order=(("date", "desc"),), needs=NEEDS_STATUS,
    fields=(
        Field("date", "Дата смены", MSK_DATE.format(col="h.changed_at"), DATE),
        Field("point_id", "Номер комплекса", "h.point_id"),
        Field("address", "Адрес комплекса", "coalesce(p.address, '')"),
        Field("category", "Категория комплекса", "coalesce(pc.name, '')", ENUM, "point_categories"),
        Field("old_status", "Был статус", "h.old_status", ENUM, "point_statuses"),
        Field("new_status", "Стал статус", "h.new_status", ENUM, "point_statuses"),
        Field("source", "Откуда смена (проверка связи, отчёт о выезде, диспетчер…)", "h.source"),
    ),
)

_PLACE = "case e.place when 'stock' then 'на складе' when 'person' then 'у монтёра' when 'point' then 'на комплексе' else 'неизвестно' end"
_HELD = "case e.held_reason when 'issued' then 'выдано монтёру' when 'removed' then 'снято с комплекса, ждёт приёмки' when 'kept' then 'снятая SIM осталась у монтёра' else '' end"

EQUIPMENT = Dataset(
    name="equipment", label="Оборудование по серийным номерам (склад, у монтёров, на комплексах), в том числе SIM-карты",
    description="по одной единице оборудования: номер, категория, состояние, где находится, у кого",
    from_sql=("equipment e join equipment_models m on m.id = e.model_id join equipment_categories ec on ec.id = m.category_id "
              "left join equipment_statuses s on s.id = e.status_id left join people hp on hp.id = e.person_id"),
    row_label="единиц оборудования", default_select=("serial", "category", "model", "status", "place", "point_id", "holder"),
    default_order=(("category", "asc"), ("serial", "asc")), needs=NEEDS_EQUIPMENT,
    fields=(
        Field("serial", "Серийный номер (у SIM — последние 7 цифр)", "e.serial_number"),
        Field("category", "Категория оборудования", "ec.name", ENUM, "equipment_categories"),
        Field("model", "Модель", "m.name"),
        Field("status", "Состояние (Исправно, Дефектное…)", "coalesce(s.name, '')", ENUM, "equipment_statuses"),
        Field("place", "Где находится (на складе / у монтёра / на комплексе / неизвестно)", _PLACE, ENUM, "equipment_places"),
        Field("point_id", "На каком комплексе стоит", "coalesce(e.point_id, '')"),
        Field("holder", "У какого монтёра числится", "coalesce(hp.full_name, '')"),
        Field("held_reason", "Почему у монтёра", _HELD, ENUM, "equipment_held"),
        Field("received_date", "Дата поступления", "coalesce(e.received_date, '')", DATE),
        Field("issued_date", "Дата выдачи монтёру", "coalesce(e.issued_date, '')", DATE),
        Field("installed_date", "Дата установки на комплекс", "coalesce(e.installed_date, '')", DATE),
        Field("removed_date", "Дата снятия с комплекса", "coalesce(e.removed_date, '')", DATE),
        Field("removed_point", "С какого комплекса снято", "coalesce(e.removed_point_id, '')"),
        Field("removal_reason", "Причина снятия", "coalesce(e.removal_reason, '')"),
        Field("accepted_date", "Дата приёмки на склад", "coalesce(e.accepted_date, '')", DATE),
        Field("notes", "Примечание", "coalesce(e.notes, '')"),
    ),
)

ABSENCES = Dataset(
    name="absences", label="Отсутствия людей (отпуск, больничный и т. п.)",
    description="по одному на каждое отсутствие: кто, с какого по какое число, причина",
    from_sql="people_absences a join people pe on pe.id = a.person_id", date_field="date_from", period_overlap=("date_from", "date_to"),
    row_label="отсутствий", default_select=("person", "role", "date_from", "date_to", "reason"), default_order=(("date_from", "desc"),),
    needs=NEEDS_ABSENCES,
    fields=(
        Field("person", "Кто отсутствует", "pe.full_name"),
        Field("role", "Должность",
              "case pe.role_type when 'fitter' then 'монтёр' when 'driver' then 'водитель' when 'operator' then 'оператор' when 'warehouse' then 'склад' else pe.role_type end",
              ENUM, "people_roles"),
        Field("date_from", "С какого числа", "a.start_date", DATE),
        Field("date_to", "По какое число", "a.end_date", DATE),
        Field("days", "Сколько дней", "(cast(julianday(a.end_date) - julianday(a.start_date) as integer) + 1)", NUMBER, groupable=False),
        Field("reason", "Причина", "coalesce(a.reason, '')"),
    ),
)

CREWS = Dataset(
    name="crews", label="Бригады по дням (состав и оборудование)",
    description="по одной на каждую бригаду в каждый день: название, состав, лестница или автовышка, куда едут",
    from_sql="crews c", date_field="date", row_label="бригад", default_select=("date", "name", "members", "equip", "place", "tasks_count"),
    default_order=(("date", "desc"),), needs=NEEDS_CALENDAR,
    fields=(
        Field("date", "Дата", "c.date", DATE),
        Field("name", "Название бригады", "c.name"),
        Field("members", "Состав",
              "coalesce((select group_concat(pe.full_name, ', ') from crew_members cm join people pe on pe.id = cm.person_id where cm.crew_id = c.id), '')"),
        Field("equip", "Чем работают (Лестница / Автовышка)", "c.equip", ENUM, "crew_equip"),
        Field("place", "Куда едут", "coalesce(c.place, '')"),
        Field("tasks_count", "Сколько заданий", "(select count(*) from tasks t where t.team = c.id and t.type != 'Вспомогательная точка')", NUMBER, groupable=False),
        Field("contractor", "Подрядчик (да/нет)", "case when c.contractor = 1 then 'да' else 'нет' end", BOOL),
    ),
)

DATASETS: dict[str, Dataset] = {d.name: d for d in (VISITS, TASKS, POINTS, STATUS_CHANGES, EQUIPMENT, ABSENCES, CREWS, APK_GROUPS)}


def role_perms(role: str) -> frozenset:
    """Права роли (из памяти); роли нет — пусто, ничего не видно."""
    return roles_repository.perms_for_role(role)


def dataset_allowed(role: str, dataset: Dataset) -> bool:
    return P.has_any(role_perms(role), dataset.needs)


def role_datasets(role: str) -> list[Dataset]:
    return [d for d in DATASETS.values() if dataset_allowed(role, d)]


def role_fields(role: str, dataset: Dataset) -> list[Field]:
    """Поля набора, которые эта роль видит."""
    perms = role_perms(role)
    return [f for f in dataset.fields if f.perm is None or P.has(perms, f.perm)]


def role_can(role: str, dataset_name: str, field_name: Optional[str] = None) -> bool:
    ds = DATASETS.get(dataset_name)
    if ds is None or not dataset_allowed(role, ds):
        return False
    if field_name is None:
        return True
    return any(f.name == field_name for f in role_fields(role, ds))
