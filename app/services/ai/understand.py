"""Понимание вопроса ДО модели: что человек спросил, какие комплексы упомянул, какой период назвал, что ему нужно — число, список, объяснение, рейтинг, сравнение.

Небольшая модель на процессоре плохо угадывает номера комплексов, месяцы и то, ждёт ли человек число или рассказ, поэтому всё, что можно определить точно, определяется
программой по базе и тексту, а модели достаются проверенные подсказки (planner_hints). Заодно здесь решается, нужна ли модели вообще планировка запроса:
приветствие, благодарность, «что ты умеешь» — обычный разговор; «что с комплексом P-2», «почему не работает P-2» — досье комплекса (skills.py).
Всё здесь детерминированно и быстро (миллисекунды), без обращений к модели."""
from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.repositories import points_repository
from app.services.ai import plan_cache
from app.services.ai import query_engine as qe
from app.services.check_service import locate
from app.services.point_finder import PointFinder

# ---------------------------------------------------------------- справочник комплексов в памяти (на минуту): поиск по тексту вопроса должен быть мгновенным
_cache: dict[str, Any] = {"at": 0.0, "finder": None, "n": 0}


async def _finder() -> Optional[PointFinder]:
    now = time.monotonic()
    if _cache["finder"] is not None and now - _cache["at"] < 60:
        return _cache["finder"]
    try:
        points = await points_repository.list_points()
    except Exception:  # noqa: BLE001 — без справочника вопрос всё равно разбирается (модель получит меньше подсказок)
        return None
    _cache.update(at=now, finder=PointFinder(points), n=len(points))
    return _cache["finder"]


def reset_cache() -> None:
    _cache.update(at=0.0, finder=None)


# ---------------------------------------------------------------- слова вопроса
_SMALLTALK = re.compile(
    r"^(привет|приветствую|здравствуй\w*|добрый (день|вечер|утро)|доброе утро|хай|хэй|алло|салют|спасибо\w*|благодар\w*|пока|до свидания|ок|окей|понятно|ясно|отлично|супер|класс|"
    r"помощь|помоги|помогите|справка|что ты умеешь|что умеешь|что ты можешь|что можешь|ты кто|кто ты|как дела|как ты|ты здесь|ты живой|ты тут)\b")
_HELP = re.compile(r"(что ты (умеешь|можешь)|что умеешь|что можешь|чем (ты )?(можешь )?помочь|как (с тобой|тобой) (работать|пользоваться)|что ты такое|ты кто|кто ты|помощь|справка|что здесь можно)")
_THANKS = re.compile(r"^(спасибо|благодар|круто|супер|отлично|класс|ок|окей|понятно|ясно|ладно)")
_DOSSIER_WORDS = re.compile(
    r"(что с |что там с|как там|как дела с|расскажи|рассказать|информаци|инфо\b|сведени|статус|состояни|почему|причин|что нужно|что надо|что случилось|что делали|чем занимал|"
    r"что за |работает ли|история|досье|карточк|что известно|что по |что там дел|когда был|когда (в )?последни|последн\w+ (выезд|раз|ремонт)|что сдела)")
_AGG_WORDS = re.compile(r"(сколько|чаще|реже|больше всего|меньше всего|рейтинг|топ\b|сравни|сравнен|динамик|по месяц|по бригад|по исполнител|по категори|каждый|каждой|каждого|всего|в среднем|сумм)")
_RANKING = re.compile(r"(чаще всего|больше всего|реже всего|меньше всего|самый|самая|самое|самые|рейтинг|топ\b|лидер|чаще других|чаще остальных)")
_COMPARE = re.compile(r"(сравни\w*|сравнен\w*|динамик\w*|по сравнению|чем в прошл\w*|лучше или хуже)")
_LIST_WORDS = re.compile(r"(какие|какой|какая|какое|каких|кто\b|кого|на каких|у каких|по каким|где\b|покажи|показать|перечисли|назови|выведи|выгрузи|выгрузк|сформируй|список|дай )")
_WHY_WORDS = re.compile(r"(почему|причин|из-за чего|отчего|что нужно|что надо|что требуется|что необходимо|чтобы (починить|восстановить|исправить))")
_WHEN_WORDS = re.compile(r"(когда|в какой день|какого числа|давно ли|как давно)")
_COUNT_WORDS = re.compile(r"\bсколько\b")
_DATAISH = re.compile(r"(комплекс|апк|выезд|ремонт|задани|бригад|монтёр|монтер|отпуск|больничн|отсутств|оборудовани|склад|sim|сим|статус|поверк|питани|причин|адрес|ip\b|пароль|логин|инвентар)")
_ID_TOKEN = re.compile(r"(?<![\w.])([A-Za-zА-Яа-яЁё]{1,14}[-_]?\d[\w\-]*|\d{5,})(?![\w])")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_DATE_LIKE = re.compile(r"^\d{1,2}[.\-/]\d{1,2}([.\-/]\d{2,4})?$")
_PERIOD_WORDS = re.compile(r"(сегодня|вчера|завтра|послезавтра|на следующей неделе|на этой неделе|на прошлой неделе|этот месяц|прошлый месяц|в этом месяце|в прошлом месяце|за (неделю|месяц|год|квартал|сутки|день)|за последн|в этом году|в прошлом году)")


# Как люди называют состояния и признаки (слова вопроса → значение справочника). Небольшая модель такие соответствия угадывает плохо («неработающие» → статус «Не работает»,
# «солнечные панели» → питание «СЭС»), поэтому они определяются здесь и уходят модели подсказкой.
_STATUS_SYNONYMS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(не ?работа|неработ|не ?рабоч|нерабоч|упал|упавш|отвалил|отвалив|сломал|сломан|вышел? из строя|не ?отвеча|без связи|нет связи)"), "Не работает"),
    (re.compile(r"(нет доступа|недоступн|не ?доступ)"), "Нет доступа"),
    (re.compile(r"(на складе|складе)"), "На складе"),
    (re.compile(r"(нет питания|без питания)"), "Нет питания"),
]
_POWER_SYNONYMS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(солнечн|сэс|на панел|от панел|солнечных)"), "СЭС"),
    (re.compile(r"(электросет|от сети|сетев|городск\w* сет)"), "Электросеть"),
]
_TOMORROW = re.compile(r"(?<!после)завтра|завтрашн")
_DAY_AFTER = re.compile(r"послезавтра|послезавтрашн")
_NEXT_WEEK = re.compile(r"(следующ\w* недел|будущ\w* недел|недел\w* вперёд|недел\w* вперед)")
# Просьба что-то создать или изменить (составить план, назначить бригаду, удалить комплекс) — помощник только читает данные; «составь СПИСОК/отчёт» — это чтение
_WRITE_REQUEST = re.compile(r"^(составь|составить|создай|создать|добавь|добавить|измени|изменить|поставь|поставить|назначь|назначить|удали|удалить|перенеси|перенести|запланируй|запланировать|"
                            r"спланируй|спланировать)\b.*\b(план|расписани|задани|бригад|выезд|график|маршрут|ремонт|комплекс|точк|монтёр|монтер|оборудован)")
_READ_OBJECT = re.compile(r"(список|таблиц|отчёт|отчет|выгрузк|перечень|сводк|справк|документ|записк|реестр|ведомост)")

# Просьба отдать результат файлом: «выгрузи в Excel», «сделай справку в Word». Файл собирает программа из тех же данных, что и ответ (export_doc.py); модель в данные не вмешивается.
_WANT_XLSX = re.compile(r"(?<![a-z])(excel|xlsx|xls)(?![a-z])|эксел\w*|ексел\w*|\bэксель\b")
_WANT_DOCX = re.compile(r"(?<![a-z])(word|docx)(?![a-z])|\bворд\w*|\bдокумент\w*|\bсправк\w*|служебн\w+ записк\w*|\bзаписк\w+|\bдокладн\w+")
_FILE_WORDS = re.compile(r"\b(?:(?:в|во|как|виде|формате|файл\w*|документ\w*)\s+)+(?:excel\w*|эксел\w*|ексел\w*|word\w*|ворд\w*|xlsx|docx|xls)\b|\b(?:excel\w*|эксел\w*|ексел\w*|word\w*|ворд\w*|xlsx|docx|xls)\b", re.IGNORECASE)


# «Сделай справку по …», «составь служебную записку о …» — просьба-оболочка вокруг самого вопроса; остаётся сам вопрос («по неработающим комплексам» → «неработающим комплексам» разбирается так же, как «неработающие комплексы»)
_ASK_WRAPPER = re.compile(r"\b(?:сделай|составь|подготовь|сформируй|создай|оформи|напиши|выгрузи|скачай)(?:\s+мне)?\s+(?:(?:в\s+виде\s+)?(?:справк\w*|служебн\w+\s+записк\w*|записк\w+|докладн\w+|документ\w*|файл\w*|таблиц\w*))"
                          r"(?:\s+(?:по|о|об|про))?\b", re.IGNORECASE)
_DOC_NOUNS = re.compile(r"\b(?:справк\w*|служебн\w+\s+записк\w*|записк\w+|докладн\w+|документ\w*)\b", re.IGNORECASE)


def wanted_file(question: str) -> Optional[str]:
    """Что просят отдать файлом: 'xlsx' (Excel, «эксель», таблицей в файл), 'docx' (Word, документ, справка, служебная записка) или None. Если названы оба — Excel."""
    norm = plan_cache.normalize(question)
    if _WANT_XLSX.search(norm):
        return "xlsx"
    if _WANT_DOCX.search(norm) and len(norm.split()) > 2:            # «справка» одним словом — это «помощь», а не просьба о документе
        return "docx"
    return None


def strip_file_words(question: str, deep: bool = True) -> str:
    """Вопрос без слов про формат («в Excel», «в формате Word») — планировщику и памяти разборов они не нужны: «выгрузи неработающие комплексы в Excel» разбирается как просто «неработающие комплексы».
    deep=False — убрать только слова про формат, а просьбу («сделай справку по …») оставить: так вопрос записывается в сам файл, как его задал человек."""
    cleaned = _FILE_WORDS.sub(" ", question)
    if not deep:
        return " ".join(cleaned.split()).strip(" ,;:") or question
    wrapped = bool(_ASK_WRAPPER.search(cleaned))
    cleaned = _ASK_WRAPPER.sub(" ", cleaned)
    cleaned = _DOC_NOUNS.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ,;:")
    if wrapped and cleaned:
        norm = plan_cache.normalize(cleaned)
        if not (_LIST_WORDS.search(norm) or _COUNT_WORDS.search(norm) or _AGG_WORDS.search(norm) or _WHEN_WORDS.search(norm) or _WHY_WORDS.search(norm)):
            cleaned = "Покажи " + cleaned                           # «сделай справку по неработающим комплексам» → «Покажи неработающим комплексам»: у вопроса снова есть глагол, по которому видно, что нужен список
    return cleaned if len(cleaned.split()) >= 2 else question


def relative_period(norm: str, today: dt.date) -> Optional[tuple[str, str]]:
    """«завтра», «послезавтра», «на следующей неделе» → (с, по) датами ГГГГ-ММ-ДД. Готовых периодов для будущего в движке нет, поэтому даты считает программа."""
    days: list[dt.date] = []
    if _TOMORROW.search(norm):
        days.append(today + dt.timedelta(days=1))
    if _DAY_AFTER.search(norm):
        days.append(today + dt.timedelta(days=2))
    if days:
        return min(days).isoformat(), max(days).isoformat()
    if _NEXT_WEEK.search(norm):
        start = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7)
        return start.isoformat(), (start + dt.timedelta(days=6)).isoformat()
    return None


def named_months(norm: str, today: dt.date) -> list[str]:
    """Все названные по имени месяцы (ГГГГ-ММ) в порядке времени: «сравни сентябрь и октябрь» → [2026-09, 2026-10]. Год — ближайший прошедший или текущий месяц."""
    found = {_MONTHS[m.group(1)[:3]] for m in _MONTH_WORD.finditer(norm)}
    year_m = re.search(r"\b(20\d\d)\b", norm)
    year = int(year_m.group(1)) if year_m else None
    return sorted(f"{year or (today.year if mo <= today.month else today.year - 1)}-{mo:02d}" for mo in found)


def named_status(norm: str) -> Optional[str]:
    """Статус комплекса, который назван словами вопроса («неработающие» → «Не работает»), либо None."""
    for pattern, status in _STATUS_SYNONYMS:
        if pattern.search(norm):
            return status
    return None


def named_power(norm: str) -> Optional[str]:
    for pattern, power in _POWER_SYNONYMS:
        if pattern.search(norm):
            return power
    return None


# Слова, по которым человек вообще говорит о состоянии комплекса. Нет ни одного — значит, условие «статус = …» модель добавила от себя.
_ANY_STATUS_WORD = re.compile(r"(работа|рабоч|статус|демонтир|установлен|состояни|на складе|доступ)")


def mentions_status(norm: str) -> bool:
    return bool(_ANY_STATUS_WORD.search(norm)) or named_status(norm) is not None


_MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "мае": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_MONTH_WORD = re.compile(r"\b(январ\w*|феврал\w*|март\w*|апрел\w*|ма[яйе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)\b")


@dataclass
class Understanding:
    question: str
    norm: str = ""
    points: list[dict[str, Any]] = field(default_factory=list)           # найденные комплексы: {id, how, address, status}
    unknown_ids: list[dict[str, Any]] = field(default_factory=list)      # похожие на номер, но в справочнике нет: {token, hints}
    month: Optional[str] = None
    months: list[str] = field(default_factory=list)                     # все названные месяцы (ГГГГ-ММ), в порядке времени
    values: list[str] = field(default_factory=list)                     # упомянутые значения справочников: «статус комплекса = Не работает»
    intents: set[str] = field(default_factory=set)
    smalltalk: bool = False
    help_request: bool = False
    dossier: bool = False
    write_request: bool = False                                         # просят составить/изменить/назначить — помощник такое не умеет
    when: Optional[tuple[str, str]] = None                              # «завтра», «послезавтра», «на следующей неделе» → (с, по)

    @property
    def point_ids(self) -> list[str]:
        return [p["id"] for p in self.points]


def _looks_like_id(token: str) -> bool:
    if _YEAR.match(token) or _DATE_LIKE.match(token):
        return False
    return True


async def analyze(question: str, today: Optional[dt.date] = None) -> Understanding:
    """Что понятно из текста вопроса без модели."""
    from app.services.ai import qa  # noqa: PLC0415 — qa импортирует этот модуль в answer; здесь нужны её разборщики месяца

    today = today or dt.date.today()
    q = " ".join((question or "").split())
    norm = plan_cache.normalize(q)
    u = Understanding(question=q, norm=norm)
    u.month = qa.month_in_question(q, today)
    named = {_MONTHS[m.group(1)[:3]] for m in _MONTH_WORD.finditer(norm)}
    if len(named) >= 2:
        year = today.year
        u.months = sorted(f"{year if mo <= today.month else year - 1}-{mo:02d}" for mo in named)
        u.month = None                                                   # несколько месяцев — это период «с … по …», а не один месяц

    # --- комплексы
    finder = await _finder()
    if finder is not None:
        seen: set[str] = set()
        for m in _ID_TOKEN.finditer(q):
            token = m.group(1).strip(" .,;:!?()«»\"'")
            if not token or not _looks_like_id(token) or token.casefold() in seen:
                continue
            seen.add(token.casefold())
            pid, how, cands = locate(token, finder.index, finder)
            if pid:
                if pid not in {p["id"] for p in u.points}:
                    p = finder.points.get(pid) or {}
                    u.points.append({"id": pid, "how": how, "token": token, "address": p.get("address") or "", "status": p.get("status") or ""})
            elif ("-" in token or "_" in token) and re.search(r"\d", token):
                u.unknown_ids.append({"token": token, "hints": cands[:4]})
        u.points = u.points[:5]

    # --- значения справочников, названные в вопросе
    try:
        enums = await qe.enum_values()
    except Exception:  # noqa: BLE001
        enums = {}
    for pattern, status in _STATUS_SYNONYMS:
        if pattern.search(norm):
            u.values.append(f"статус комплекса = «{status}»")
            break
    for pattern, power in _POWER_SYNONYMS:
        if pattern.search(norm):
            u.values.append(f"питание = «{power}»")
            break
    for wt in enums.get("work_types", []):
        stem = plan_cache.normalize(wt)[:7]
        if len(stem) >= 5 and stem in norm:
            u.values.append(f"тип работ/выезда = «{wt}»")
            break
    for cname in enums.get("point_categories", []):
        if len(cname) >= 3 and plan_cache.normalize(cname) in norm:
            u.values.append(f"категория комплекса = «{cname}»")
            break
    if re.search(r"автовышк|вышк", norm):
        u.values.append("нужна автовышка: в «что нужно для восстановления» (needs) содержит «автовышк»")

    # --- что хочет человек
    if _COUNT_WORDS.search(norm):
        u.intents.add("count")
    if _LIST_WORDS.search(norm):
        u.intents.add("list")
    if _RANKING.search(norm):
        u.intents.add("ranking")
    if _COMPARE.search(norm):
        u.intents.add("compare")
    if _WHY_WORDS.search(norm):
        u.intents.add("why")
    if _WHEN_WORDS.search(norm):
        u.intents.add("when")
    if _AGG_WORDS.search(norm):
        u.intents.add("aggregate")

    words = norm.split()
    u.when = relative_period(norm, today)
    u.write_request = bool(_WRITE_REQUEST.search(norm)) and not _READ_OBJECT.search(norm)
    u.help_request = bool(_HELP.search(norm)) and not u.points and not _DATAISH.search(norm)      # «сделай справку по неработающим комплексам» — это просьба о данных, а не «справка/помощь»
    u.smalltalk = (not u.points and not _DATAISH.search(norm) and not _AGG_WORDS.search(norm)
                   and (bool(_SMALLTALK.match(norm)) or (len(words) <= 3 and not any(ch.isdigit() for ch in norm) and not _LIST_WORDS.search(norm))))
    if u.help_request:
        u.smalltalk = True
    # досье: названа ровно по смыслу «что с комплексом» (комплексов 1–3) и нет ни подсчёта, ни периода, ни рейтинга
    u.dossier = bool(u.points) and len(u.points) <= 3 and "aggregate" not in u.intents and "compare" not in u.intents and u.month is None \
        and not _PERIOD_WORDS.search(norm) and (bool(_DOSSIER_WORDS.search(norm)) or len(words) <= 4)
    return u


def planner_hints(u: Understanding) -> list[str]:
    """Короткие проверенные подсказки для модели-планировщика (идут в конец сообщения, чтобы кэш подсказки модели не сбивался)."""
    hints: list[str] = []
    for p in u.points:
        hints.append(f"Комплекс «{p['token']}» есть в справочнике под номером «{p['id']}»: в условии используй ровно этот номер (point_id = {p['id']}).")
    for item in u.unknown_ids[:2]:
        similar = f" Похожие: {', '.join(item['hints'])}." if item["hints"] else ""
        hints.append(f"Номера «{item['token']}» в справочнике нет.{similar}")
    if u.month:
        hints.append(f"Месяц из вопроса — {u.month}: period.from и period.to = {u.month}, preset пустой.")
    if u.months:
        hints.append(f"Названы месяцы {', '.join(u.months)}: period.from = {u.months[0]}, period.to = {u.months[-1]}, group_by date:month, aggregates count.")
    if re.search(r"\b(сейчас|в данный момент|на данный момент|в настоящее время)\b", u.norm) and re.search(r"(отпуск|больничн|отсутств|выходн|отгул)", u.norm):
        hints.append("«Сейчас» для отсутствий — это сегодняшний день: period.preset = today (иначе попадут и давно закончившиеся отпуска).")
    if u.when:
        frm, to = u.when
        hints.append(f"Период из вопроса — {frm}" + (f" – {to}" if to != frm else "") + f": period.from = {frm}, period.to = {to}, preset пустой.")
    for value in u.values[:3]:
        hints.append(f"В вопросе названо: {value} — поставь это условием.")
    if "ranking" in u.intents:
        hints.append("Это рейтинг: group_by по нужному полю, aggregates count, order_by count desc, limit 5 (для «какой самый …» — limit 1).")
    if "compare" in u.intents:
        hints.append("Сравнение периодов: group_by date:month (или другой нужной группировки) за оба периода, aggregates count.")
    if "why" in u.intents:
        hints.append("Причины и что нужно для восстановления: набор visits, поля fault и needs (условие fault not_empty), либо points — поля fault_reason и fault_need.")
    if "list" in u.intents and "count" not in u.intents:
        hints.append("Нужен СПИСОК: select с понятными полями, без aggregates; limit не ставь, если не просят «первые N».")
    if "when" in u.intents:
        hints.append("Вопрос про дату: выведи поле даты (date у выездов, last_visit или down_since у комплексов).")
    return hints
