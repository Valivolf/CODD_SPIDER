"""Отчёты из таблицы Яндекс Форм: часть монтёров всё ещё сдаёт отчёты через форму, а не через бота.

Таблицу ответов формы (.xlsx) можно загрузить сюда. Для каждой строки программа:
  1. находит комплекс по НОМЕРУ — буквенная часть названия («POST-», «Пульсар-», «Сим Пульсар-»…) отбрасывается, сравниваются
     цифры (при совпадении цифр у нескольких комплексов помогают буквы; не помогли — строка помечается «неоднозначно»);
  2. если по комплексу за дату из формы отчёт уже есть (бот, диспетчер, прежняя загрузка, журнал выездов) — строку пропускает;
  3. иначе ищет задание на этот комплекс в календаре в пределах ±1 дня от даты отчёта (выезд в командировку учитывается),
     предпочитая бригаду, фамилии которой названы в строке, и задание без отчёта, и создаёт по нему отчёт, как будто его сдала
     бригада, — так задание считается выполненным. Если задания рядом нет, отчёт создаётся без задания (в истории комплекса,
     как загруженный из таблицы). Тип выезда — тип задания (без задания — «Ремонт»), итог работ — текст из формы, статус
     комплекса по умолчанию НЕ меняется (отчёт запоздалый — он не должен перебивать более свежую проверку), это включается галочкой.
Фото из столбца «Файл» (ссылки forms.yandex.ru) скачиваются сами, сразу после загрузки таблицы: файлы формы открываются лишь её
создателю, поэтому скачивание идёт через браузер робота карт (он под тем же Яндекс-аккаунтом). Если в папке комплекса за эту дату
фото уже есть (бот, прежний архив, прошлая загрузка), фото строки пропускаются.

Если в ячейке «Номер апк» перечислено НЕСКОЛЬКО комплексов («Пульсар-1025-26717, POST-1019771 и ARENA-1111040», «26717 26718») и целиком она комплекс не нашла, строка делится
на отдельные строки — по одной на комплекс (expand_multi): у каждой то же время, бригада, итог работ, статус, оборудование и те же фото; найденные комплексы получают свои
отчёты, ненайденные показываются отдельными строками «комплекс не найден». SIM и инвентарный номер при делении не берутся (они относились бы к одному из комплексов).

Строка узнаётся по отпечатку (row_hash): тот же файл можно загружать повторно — созданное не дублируется."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from openpyxl import load_workbook

from app.core.names import fold_name
from app.core.power_source import normalize_power_source
from app.core.text import decode_entities
from app.core.work_types import combined_visit_type, is_aux_type, is_repair_type
from app.db.database import get_connection
from app.repositories import (
    bot_task_progress_repository, bot_work_sessions_repository, crew_repository, people_repository, points_repository,
    task_visit_reports_repository, visit_photos_repository, work_types_repository,
)
from app.services import yandex_robot
from app.services.excel_service import _zip_bomb_error
from app.services.point_finder import PointLookup, digits_key, letters_key, split_designations
from app.services.visit_history_import import _local_day, default_type_name

MAX_ROWS = 20_000
MAX_FILE_SIZE = 15 * 1024 * 1024
MSK = dt.timezone(dt.timedelta(hours=3))        # время в формах — местное (Краснодар = Москва)
FORMS_AUTHOR = "Яндекс Форма"
DAY_WINDOW = 1                                    # задание ищется в пределах ±1 дня от даты отчёта
FETCH_CACHE_BYTES = 80 * 1024 * 1024               # сколько скачанных фото держим в памяти, пока их же нужно положить в папки нескольких комплексов

# Поле → заголовки столбцов (без регистра, «ё» = «е», без знаков). Подходит заголовок, равный названию или начинающийся с него.
_ALIASES: dict[str, list[str]] = {
    "time": ["время создания", "время отправки ответа", "время отправки", "дата создания", "время", "дата и время", "дата"],
    "sim": ["номер сим карты", "номер sim", "сим карта", "sim карта", "сим", "sim"],
    "point": ["номер апк", "номер комплекса", "апк", "комплекс", "номер"],
    "location": ["местоположение", "координаты", "адрес"],
    "inventory": ["инвентарный номер", "инв номер", "инвентарный"],
    "files": ["файл", "файлы", "фото", "вложения"],
    "result": ["результат выезда", "результат", "итог работ", "итог"],
    "status": ["статус"],
    "equipment": ["оборудование"],
    "crew": ["бригада", "исполнители", "исполнитель", "монтеры"],
    "power": ["тип питания", "питание"],
}


def _key(value: Any) -> str:
    s = decode_entities(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]|_", " ", s).split())


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return decode_entities(value).strip()


_URL_RE = re.compile(r"https?://[^\s,;]+")


def parse_form_time(value: Any) -> Optional[dt.datetime]:
    """Время строки (местное, без часового пояса): ячейка-дата или текст «2026-09-30 10:07:16» / «30.09.2026 10:07»."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    s = _text(value)
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def to_utc_iso(local: dt.datetime) -> str:
    return local.replace(tzinfo=MSK).astimezone(dt.timezone.utc).isoformat()


@dataclass
class FormRow:
    number: int                     # номер строки в файле
    form_time: Optional[dt.datetime]
    raw_point: str
    sim: str = ""
    location: str = ""
    inventory: str = ""
    urls: list[str] = field(default_factory=list)
    result: str = ""
    status: str = ""
    equipment: str = ""
    crew: str = ""
    power: str = ""
    part: str = ""                  # «2 из 3» — строка получена делением ячейки с несколькими комплексами
    origin_hash: str = ""           # отпечаток исходной (недлённой) строки: её прежняя запись «не найден» после загрузки убирается

    @property
    def row_hash(self) -> str:
        base = "|".join([
            self.form_time.isoformat() if self.form_time else "", digits_key(self.raw_point), letters_key(self.raw_point),
            " ".join(self.crew.split()).casefold(), " ".join(self.result.split()).casefold(), ",".join(self.urls),
        ])
        return hashlib.sha1(base.encode("utf-8"), usedforsecurity=False).hexdigest()


@dataclass
class Parsed:
    rows: list[FormRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse(content: bytes) -> Parsed:
    out = Parsed()
    bomb = _zip_bomb_error(content)
    if bomb:
        out.errors.append(bomb)
        return out
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return out
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True, max_row=MAX_ROWS + 20))
    cols: dict[str, int] = {}
    start = 0
    for idx, raw in enumerate(rows[:15]):
        keys = [_key(c) for c in raw]
        found: dict[str, int] = {}
        used: set[int] = set()
        for exact in (True, False):
            for name, aliases in _ALIASES.items():
                if name in found:
                    continue
                for alias in aliases:
                    hit = next((i for i, k in enumerate(keys) if i not in used and k and (k == alias if exact else k.startswith(alias + " "))), None)
                    if hit is not None:
                        found[name] = hit
                        used.add(hit)
                        break
        if "point" in found and "time" in found:
            cols, start = found, idx + 1
            break
    if not cols:
        out.errors.append("Не нашёл заголовки таблицы формы: нужны столбцы «Время создания» и «Номер апк» (остальные — по желанию)")
        return out

    def val(raw, name):
        i = cols.get(name)
        return raw[i] if i is not None and i < len(raw) else None

    for offset, raw in enumerate(rows[start:], start=start + 1):
        if all(c in (None, "") for c in raw):
            continue
        point = _text(val(raw, "point"))
        when = parse_form_time(val(raw, "time"))
        if not point:
            out.errors.append(f"Строка {offset}: не указан номер АПК — пропущена")
            continue
        if when is None:
            out.errors.append(f"Строка {offset}: не удалось прочитать время создания «{_text(val(raw, 'time'))}» — пропущена")
            continue
        out.rows.append(FormRow(
            number=offset, form_time=when, raw_point=point, sim=_text(val(raw, "sim")), location=_text(val(raw, "location")),
            inventory=_text(val(raw, "inventory")), urls=_URL_RE.findall(_text(val(raw, "files"))), result=_text(val(raw, "result")),
            status=_text(val(raw, "status")), equipment=_text(val(raw, "equipment")), crew=_text(val(raw, "crew")),
            power=_text(val(raw, "power")),
        ))
    if len(rows) - start > MAX_ROWS:
        out.errors.append(f"В файле больше {MAX_ROWS} строк — прочитаны первые {MAX_ROWS}")
    return out


# ---------------------------------------------------------------- поиск комплекса и задания

def _surnames(text: str) -> list[str]:
    parts = re.split(r"[,;/&]| и ", text or "")
    result = []
    for part in parts:
        words = re.findall(r"[^\W\d_]+", part, flags=re.UNICODE)
        if words:
            result.append(words[0].casefold().replace("ё", "е"))
    return result


def _same_surname(a: str, b: str) -> bool:
    return a == b or (len(a) >= 6 and len(b) >= 6 and a[:6] == b[:6])


async def _crew_names(crew_id: int, cache: dict[int, list[str]]) -> list[str]:
    if crew_id not in cache:
        crew = await crew_repository.get_crew(crew_id) if crew_id else None
        cache[crew_id] = [str(m.get("full_name") or "").split(" ")[0].casefold().replace("ё", "е") for m in ((crew or {}).get("members") or [])]
    return cache[crew_id]


async def find_task(point_id: str, day: dt.date, crew_text: str, crew_cache: dict[int, list[str]], claimed: set[str]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Лучшее задание на комплекс в пределах ±1 дня от даты отчёта (и всё, что рассматривалось). У задания в командировке
    учитывается весь промежуток date…date_to."""
    lo, hi = (day - dt.timedelta(days=DAY_WINDOW)).isoformat(), (day + dt.timedelta(days=DAY_WINDOW)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "select t.id, t.date, coalesce(nullif(t.date_to, ''), t.date), t.team, t.type, c.name, "
            "(select count(*) from task_visit_reports r where r.task_id = t.id) "
            "from tasks t left join crews c on c.id = t.team "
            "where t.point = ? and t.type != 'Вспомогательная точка' and t.date <= ? and coalesce(nullif(t.date_to, ''), t.date) >= ?",
            (point_id, hi, lo),
        )
        rows = await cur.fetchall()
    wanted = _surnames(crew_text)
    cands = []
    for tid, tdate, tdate_to, team, ttype, cname, reports in rows:
        names = await _crew_names(team, crew_cache)
        crew_hit = bool(wanted) and any(_same_surname(w, n) for w in wanted for n in names if n)
        try:
            d0, d1 = dt.date.fromisoformat(tdate), dt.date.fromisoformat(tdate_to)
        except ValueError:
            continue
        dist = 0 if d0 <= day <= d1 else min(abs((day - d0).days), abs((day - d1).days))
        cands.append({"id": tid, "date": tdate, "date_to": tdate_to, "team": team, "type": ttype, "crew_name": cname or "",
                      "has_report": bool(reports) or tid in claimed, "crew_hit": crew_hit, "dist": dist})
    cands.sort(key=lambda c: (c["has_report"], not c["crew_hit"], c["dist"], c["date"]))
    return (cands[0] if cands else None), cands


# ---------------------------------------------------------------- статус из текста формы

_NEG_FIXED = ("не восстанов", "не работает", "не заработ", "не запуст")
_POS_FIXED = ("восстановлен", "работает", "заработал", "запущен")
_FOUND = ("обнаруж", "найден", "на месте", "присутств")
_NOT_FOUND = ("не обнаруж", "не найден", "отсутств", "нет на месте")


def _has(text: str, stems: tuple[str, ...]) -> bool:
    return any(s in text for s in stems)


def derive_status(task_type: str, status_text: str, result_text: str, outcomes: list[str]) -> dict[str, str]:
    """Статус отчёта из слов формы. Определённый — «Восстановлен»/«Не восстановлен» → «Работает»/«Не работает»; у не связанных
    с ремонтом типов — подходящий «статус выезда» из настроенных («Найден»…), иначе короткий текст результата как есть; неясное
    остаётся пустым (диспетчер поправит в отчёте). Возвращает {visit_type, result_status, outcome}."""
    status_key, result_key = fold_name(status_text), fold_name(result_text)
    both = f"{status_key} {result_key}"
    repair = is_repair_type(task_type)
    fixed_pos = _has(status_key, _POS_FIXED) and not _has(status_key, _NEG_FIXED)
    fixed_neg = _has(status_key, _NEG_FIXED) or (not fixed_pos and _has(result_key, _NEG_FIXED))
    if fixed_pos or fixed_neg:
        # итог про ремонт: у типа без ремонта визит записывается «<тип> + Ремонт» — как при переходе в режим ремонта в боте
        vt = task_type if repair else combined_visit_type(task_type)
        return {"visit_type": vt, "result_status": "Не работает" if fixed_neg else "Работает", "outcome": ""}
    if repair:
        return {"visit_type": task_type, "result_status": "", "outcome": ""}
    wanted = "notfound" if _has(both, _NOT_FOUND) else "found" if _has(both, _FOUND) else ""
    for label in outcomes:
        lk = fold_name(label)
        if wanted == "notfound" and _has(lk, _NOT_FOUND + ("не ",)):
            return {"visit_type": task_type, "result_status": "", "outcome": label}
    for label in outcomes:
        lk = fold_name(label)
        if wanted == "found" and _has(lk, _FOUND) and not _has(lk, _NOT_FOUND):
            return {"visit_type": task_type, "result_status": "", "outcome": label}
        if lk and (lk in result_key or lk in status_key):
            return {"visit_type": task_type, "result_status": "", "outcome": label}
    short = " ".join(result_text.split())
    outcome = short if 0 < len(short) <= 40 else ""
    return {"visit_type": task_type, "result_status": "", "outcome": outcome}


# ---------------------------------------------------------------- план (что будет сделано) и запись

ACTION_LABELS = {
    "create": "создать отчёт по заданию", "create_free": "создать отчёт без задания", "exists": "отчёт за эту дату уже есть",
    "done_before": "уже загружен из формы раньше", "duplicate": "повтор: за эту дату отчёт из другой строки",
    "no_point": "комплекс не найден", "ambiguous": "неоднозначно: несколько комплексов",
}
FREE_PREFIX = "frm-"                              # отчёт без задания: task_id = frm-<отпечаток строки>


def expand_multi(rows: list[FormRow], lookup: PointLookup) -> list[FormRow]:
    """Строки, где в ячейке «Номер апк» несколько комплексов, делятся по одной на комплекс (описание, время, бригада, фото — те же). Делится только если целиком ячейка комплекс не
    нашла, кусков не меньше двух и хотя бы один из них — известный комплекс (иначе остаётся как есть: «не найден» на всю ячейку). Два куска, указывающие на один комплекс, — один."""
    out: list[FormRow] = []
    for row in rows:
        if lookup.match(row.raw_point, row.inventory)[1] == "ok":
            out.append(row)
            continue
        parts = split_designations(row.raw_point)
        resolved = [lookup.match(p, "") for p in parts]
        if len(parts) == 1 and resolved[0][1] == "ok" and parts[0] != row.raw_point:      # один и тот же комплекс назван дважды («POST-1, POST-1») — это один комплекс
            out.append(replace(row, raw_point=parts[0]))
            continue
        if len(parts) < 2 or not any(r[1] == "ok" for r in resolved):
            out.append(row)
            continue
        seen_points: set[str] = set()
        keep: list[str] = []
        for part, (pid, how, _c) in zip(parts, resolved):
            if pid:
                if pid in seen_points:
                    continue
                seen_points.add(pid)
            keep.append(part)
        origin = row.row_hash
        for i, part in enumerate(keep, start=1):
            out.append(replace(row, raw_point=part, sim="", inventory="", part=f"{i} из {len(keep)}" if len(keep) > 1 else "", origin_hash=origin))
    return out


async def build_plan(parsed: Parsed) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select id, address, inventory_number, power_source from points")
        points = [{"id": r[0], "address": r[1] or "", "inventory_number": r[2] or "", "power_source": r[3] or ""} for r in await cur.fetchall()]
    lookup = PointLookup(points)
    parsed.rows = expand_multi(parsed.rows, lookup)           # ячейка с несколькими комплексами → по строке на комплекс (apply_plan видит те же строки)
    crew_cache: dict[int, list[str]] = {}
    claimed: set[str] = set()                                  # задания, на которые уже идёт строка этой таблицы
    claimed_days: dict[tuple[str, str], str] = {}              # (комплекс, день) → отчёт, который создаст строка этой таблицы
    plan: list[dict[str, Any]] = []
    outcomes_cache: dict[str, list[str]] = {}
    free_type = default_type_name(None)
    async with get_connection() as conn:
        cur = await conn.execute("select row_hash, task_id, outcome from forms_import_rows")
        known = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    # Какие отчёты уже есть в системе за период таблицы (с запасом: время отчётов хранится в UTC) — по комплексу и дню отчёта.
    by_day: dict[tuple[str, str], list[dict[str, Any]]] = {}
    report_ids: set[str] = set()
    if parsed.rows:
        lo = (min(r.form_time for r in parsed.rows).date() - dt.timedelta(days=2)).isoformat()
        hi = (max(r.form_time for r in parsed.rows).date() + dt.timedelta(days=3)).isoformat()
        async with get_connection() as conn:
            cur = await conn.execute(
                "select task_id, point_id, created_at, imported, coalesce(source, ''), coalesce(created_by, ''), "
                "coalesce(nullif(outcome, ''), result_status, '') from task_visit_reports where created_at >= ? and created_at < ?",
                (lo, hi),
            )
            for tid, pid, created, imported, source, by, shown in await cur.fetchall():
                report_ids.add(tid)
                by_day.setdefault((pid, _local_day(created, bool(imported))), []).append(
                    {"task_id": tid, "created_at": created, "imported": bool(imported), "source": source, "by": by, "status": shown or ""})
        for found in by_day.values():
            found.sort(key=lambda e: (e["imported"], e["created_at"]))      # настоящие отчёты — первыми

    for row in sorted(parsed.rows, key=lambda r: (r.form_time, r.number)):
        item: dict[str, Any] = {
            "hash": row.row_hash, "row": row.number, "time": row.form_time.strftime("%Y-%m-%d %H:%M"), "raw_point": row.raw_point,
            "crew_text": row.crew, "status_text": row.status, "result_text": row.result, "photos": len(row.urls), "urls": row.urls,
            "point_id": None, "point_address": "", "task": None, "action": "", "note": "", "existing": None, "derived": None,
            "candidates": [], "report_task_id": None, "part": row.part,
        }
        plan.append(item)
        point_id, how, cands = lookup.match(row.raw_point, row.inventory)
        if not point_id:
            item["action"] = "ambiguous" if how == "ambiguous" else "no_point"
            item["candidates"] = cands
            item["note"] = ("Подходят: " + ", ".join(cands)) if cands else "Такого номера нет в справочнике «Все АПК»"
            continue
        p = lookup.points[point_id]
        item["point_id"], item["point_address"] = point_id, p["address"]
        day = row.form_time.date()
        key = (point_id, day.isoformat())
        shown_day = day.strftime("%d.%m.%Y")

        prev = known.get(row.row_hash)
        if prev and prev[1] in ("created", "created_free") and prev[0] in report_ids:
            item["action"], item["report_task_id"] = "done_before", prev[0]
            item["note"] = "Эта строка уже была загружена, отчёт на месте"
            continue

        # 1) отчёт по комплексу за эту дату уже есть (бот, диспетчер, журнал, прошлая загрузка) — пропускаем
        existing_day = by_day.get(key)
        if existing_day:
            e = existing_day[0]
            item["existing"] = {"created_at": e["created_at"], "by": e["by"], "source": e["source"], "status": e["status"]}
            item["report_task_id"] = e["task_id"]
            item["action"] = "duplicate" if e["source"] == "forms" else "exists"
            item["note"] = f"По комплексу за {shown_day} отчёт уже есть" + (" (из таблицы форм)" if e["source"] == "forms" else "")
            continue
        if key in claimed_days:
            item["action"], item["report_task_id"] = "duplicate", claimed_days[key]
            item["note"] = f"За {shown_day} по этому комплексу в таблице уже есть другая строка — отчёт один; фото можно скачать"
            continue

        # 2) задание на комплекс ±1 день: без отчёта — закрываем его отчётом из формы; с отчётом — это тот же выезд
        task, others = await find_task(point_id, day, row.crew, crew_cache, claimed)
        if task:
            item["task"] = {"id": task["id"], "date": task["date"], "crew": task["crew_name"], "type": task["type"], "crew_hit": task["crew_hit"]}
            item["report_task_id"] = task["id"]
        if task and task["has_report"]:
            existing = await task_visit_reports_repository.get_for_task(task["id"])
            if existing:
                item["existing"] = {"created_at": existing["created_at"], "by": existing.get("created_by") or "", "source": existing.get("source") or "",
                                    "status": existing.get("outcome") or existing.get("result_status") or ""}
            item["action"] = "duplicate" if ((existing and existing.get("source") == "forms") or (not existing and task["id"] in claimed)) else "exists"
            item["note"] = {
                "exists": "Бригада (или диспетчер) уже сдали отчёт по ближайшему заданию на этот комплекс — это тот же выезд",
                "duplicate": "На это задание уже есть другая строка таблицы — отчёт один; фото можно скачать",
            }[item["action"]]
            continue
        if task:
            claimed.add(task["id"])
            if task["type"] not in outcomes_cache:
                outcomes_cache[task["type"]] = await work_types_repository.outcomes_for(task["type"])
            item["derived"] = derive_status(task["type"], row.status, row.result, outcomes_cache[task["type"]])
            item["action"] = "create"
            claimed_days[key] = task["id"]
            if not task["crew_hit"] and row.crew:
                item["note"] = "Бригада в строке не совпала с составом бригады задания — проверьте"
            continue
        # 3) задания нет — отчёт без задания (в истории комплекса); комплекс и дата те же, что в форме
        item["report_task_id"] = FREE_PREFIX + row.row_hash[:20]
        item["derived"] = derive_status(free_type, row.status, row.result, [])
        item["action"] = "create_free"
        claimed_days[key] = item["report_task_id"]
        item["note"] = f"Задания на {shown_day} (±1 день) не было — отчёт будет записан в историю комплекса без задания"
    for item in plan:                                         # строки, полученные делением: пояснение в начало
        if item["part"]:
            item["note"] = (f"Один отчёт на несколько комплексов ({item['part']}): описание и фото те же. " + (item["note"] or "")).strip()
    return plan


def _report_comment(row_plan: dict[str, Any], row: FormRow) -> str:
    lines = [row.result.strip()] if row.result.strip() else []
    extra = []
    if row.equipment.strip():
        extra.append(f"Оборудование (по форме): {row.equipment.strip()}")
    if row.location.strip():
        extra.append(f"Местоположение (по форме): {row.location.strip()}")
    if row.status.strip() and fold_name(row.status) not in fold_name((row_plan["task"] or {}).get("type") or ""):
        extra.append(f"Статус в форме: {row.status.strip()}")
    return "\n".join(lines + extra)


async def apply_plan(parsed: Parsed, plan: list[dict[str, Any]], *, apply_status: bool, only: Optional[set[str]], batch: str, user: str) -> dict[str, Any]:
    """Создаёт отчёты по строкам плана с действием «create» (по заданию) и «create_free» (без задания) — только по выбранным,
    если `only` задан, — и запоминает все строки: по ним скачиваются фото (к отчёту строки или к уже имеющемуся за эту дату)."""
    by_hash = {r.row_hash: r for r in parsed.rows}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    created = 0
    created_free = 0
    all_people = await people_repository.list_people(active_only=False)
    for item in plan:
        row = by_hash[item["hash"]]
        task = item["task"]
        if item["action"] in ("create", "create_free") and (only is None or item["hash"] in only):
            derived = item["derived"]
            data = {
                "result_status": derived["result_status"], "outcome": derived["outcome"], "comment": _report_comment(item, row),
                "equipment_removed": None, "equipment_installed": None,
                "sim_installed": (row.sim.strip() or None) if row.sim.strip() else None,
            }
            if derived["result_status"] == "Не работает" and row.result.strip():
                data["needs"] = row.result.strip()          # «Не восстановлен» + текст формы — это и есть «что нужно для исправления»
            names = _surnames(row.crew)
            matched = [p for p in all_people if p.get("full_name") and names and any(_same_surname(n, p["full_name"].split(" ")[0].casefold().replace("ё", "е")) for n in names[:1])]
            author = matched[0] if len(names) == 1 and len(matched) == 1 else None
            report_id = item["report_task_id"]
            try:
                await task_visit_reports_repository.upsert(
                    report_id, item["point_id"], derived["visit_type"], data, (row.crew.strip() or FORMS_AUTHOR)[:200],
                    author_person_id=author["id"] if author else None, edited_by=None,
                    # с заданием — настоящее время ответа; без задания — как у записей истории: полдень дня отчёта (день не «плывёт» по часовым поясам)
                    created_at=to_utc_iso(row.form_time) if task else f"{row.form_time.date().isoformat()}T12:00:00+00:00",
                    source="forms", apply_status=apply_status,
                    imported=not task, import_batch=f"forms-{batch}" if not task else None,
                )
                if task:
                    # как при сдаче отчёта ботом: прогресс бота по заданию и рабочие сессии больше не нужны
                    await bot_task_progress_repository.delete(task["id"])
                    await bot_work_sessions_repository.end_for_task(task["id"])
                if row.power.strip():
                    power = normalize_power_source(row.power)
                    if power:
                        async with get_connection() as conn:
                            cur = await conn.execute("select power_source from points where id = ?", (item["point_id"],))
                            current = await cur.fetchone()
                        if current is not None and not (current[0] or "").strip():
                            await points_repository.set_power_source(item["point_id"], power)
                if task:
                    created += 1
                    item["action"] = "created"
                else:
                    created_free += 1
                    item["action"] = "created_free"
                    item["note"] = f"Записан в историю комплекса без задания (за {row.form_time.strftime('%d.%m.%Y')})"
            except Exception as exc:  # noqa: BLE001 — сбой одной строки не должен останавливать остальные
                item["action"] = "error"
                item["note"] = f"Не удалось создать отчёт: {exc}"
        # запоминаем строку (для фото): у каждой строки с найденным комплексом есть отчёт, к которому фото относятся
        report_task_id = item["report_task_id"] if item["action"] not in ("error",) else None
        photo_state = [{"url": u, "state": "pending", "photo_id": None, "error": ""} for u in row.urls]
        async with get_connection() as conn:
            cur = await conn.execute("select photo_urls from forms_import_rows where row_hash = ?", (item["hash"],))
            old = await cur.fetchone()
            if old:
                try:
                    done = {x["url"]: x for x in json.loads(old[0] or "[]") if x.get("state") in ("done", "skipped")}
                except ValueError:
                    done = {}
                photo_state = [done.get(s["url"], s) for s in photo_state]
            await conn.execute(
                "insert into forms_import_rows (row_hash, batch, form_time, raw_point, point_id, task_id, task_date, crew_text, status_text, "
                "result_text, power_text, sim_text, location_text, inventory_text, equipment_text, photo_urls, photos_total, photos_done, "
                "photos_state, photos_error, outcome, outcome_note, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?) "
                "on conflict(row_hash) do update set batch = excluded.batch, point_id = excluded.point_id, task_id = excluded.task_id, "
                "task_date = excluded.task_date, photo_urls = excluded.photo_urls, photos_total = excluded.photos_total, "
                "photos_done = excluded.photos_done, outcome = excluded.outcome, outcome_note = excluded.outcome_note, updated_at = excluded.updated_at",
                (item["hash"], batch, row.form_time.isoformat(), row.raw_point, item["point_id"], report_task_id,
                 task["date"] if task else (row.form_time.date().isoformat() if report_task_id else None), row.crew, row.status, row.result, row.power,
                 row.sim, row.location, row.inventory, row.equipment, json.dumps(photo_state, ensure_ascii=False), len(photo_state),
                 sum(1 for s in photo_state if s["state"] in ("done", "skipped")), item["action"], item["note"], now, now),
            )
            await conn.commit()
    origins = {r.origin_hash for r in parsed.rows if r.origin_hash}
    if origins:                                               # исходная строка с несколькими комплексами раньше сохранялась как «не найден» — теперь есть строки по каждому
        async with get_connection() as conn:
            await conn.execute(f"delete from forms_import_rows where point_id is null and row_hash in ({','.join('?' for _ in origins)})", tuple(origins))
            await conn.commit()
    return {"created": created, "created_free": created_free}


def summarize(plan: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    counts["total"] = len(plan)
    counts["split"] = sum(1 for i in plan if i.get("part"))             # строк, полученных делением ячейки с несколькими комплексами
    counts["photos"] = sum(i["photos"] for i in plan if i["report_task_id"] and i["action"] not in ("no_point", "ambiguous", "error"))
    counts["to_create"] = counts.get("create", 0) + counts.get("create_free", 0)
    return counts


# ---------------------------------------------------------------- сохранённые строки и скачивание фото

async def list_rows(batch: Optional[str] = None, limit: int = 2000) -> list[dict[str, Any]]:
    where, params = ("where batch = ?", (batch,)) if batch else ("", ())
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select id, row_hash, batch, form_time, raw_point, point_id, task_id, task_date, crew_text, status_text, result_text, "
            f"photo_urls, photos_total, photos_done, photos_state, photos_error, outcome, outcome_note from forms_import_rows {where} "
            f"order by form_time desc limit ?", (*params, limit),
        )
        keys = ["id", "hash", "batch", "form_time", "raw_point", "point_id", "task_id", "task_date", "crew_text", "status_text", "result_text",
                "photo_urls", "photos_total", "photos_done", "photos_state", "photos_error", "action", "note"]
        rows = [dict(zip(keys, r)) for r in await cur.fetchall()]
    for r in rows:
        try:
            r["photo_urls"] = json.loads(r["photo_urls"] or "[]")
        except ValueError:
            r["photo_urls"] = []
        r["photos_skipped"] = sum(1 for it in r["photo_urls"] if it.get("state") == "skipped")     # в папке за эту дату уже были фото
        r["photos_done"] = r["photos_done"] - r["photos_skipped"]                                # …а скачано именно из таблицы
    return rows


_JOB: dict[str, Any] = {"running": False, "total": 0, "done": 0, "failed": 0, "skipped": 0, "current": "", "error": "", "started_at": 0.0, "finished_at": 0.0, "rows": []}
_JOB_LOCK = asyncio.Lock()
PHOTO_PAUSE_SECONDS = 0.5            # пауза между файлами: частые запросы Яндекс принимает за робота (проверка «Вы не робот»)
AUTH_FAILS_STOP = 5                  # столько отказов подряд («нет входа / нет доступа») — и скачивание останавливается


def job_status() -> dict[str, Any]:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _JOB.items()}


_EXT_BY_TYPE = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/heic": "heic", "image/heif": "heic", "image/bmp": "bmp"}


def _ext_for(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    m = re.search(r"\.([A-Za-z0-9]{3,4})(?:$|[?&#])", url.split("path=")[-1] if "path=" in url else url)
    return (m.group(1).lower() if m else "jpg")


async def start_download(row_ids: list[int]) -> dict[str, Any]:
    """Запускает скачивание фото выбранных строк в фоне (одно задание за раз). Строки, у которых в папке комплекса за дату отчёта
    уже есть фото (бот, прежний архив, прошлая загрузка), пропускаются — их фото считаются «уже есть»."""
    if _JOB["running"]:
        return {**job_status(), "started": False, "reason": "Скачивание уже идёт — дождитесь окончания"}
    async with get_connection() as conn:
        marks = ",".join("?" for _ in row_ids) or "''"
        cur = await conn.execute(
            f"select id, point_id, task_id, form_time, photo_urls, row_hash from forms_import_rows where id in ({marks}) and task_id is not null", row_ids)
        rows = await cur.fetchall()
    todo = []
    skipped = 0
    have_cache: dict[tuple[str, str], int] = {}                 # (комплекс, дата) → сколько фото уже в папке — снимок до начала скачивания
    for rid, point_id, task_id, form_time, urls_json, _hash in rows:
        try:
            items = json.loads(urls_json or "[]")
        except ValueError:
            items = []
        pending = [i for i, it in enumerate(items) if it.get("state") not in ("done", "skipped")]
        if not pending:
            continue
        taken = (form_time or "")[:10] or dt.date.today().isoformat()
        started = any(it.get("state") == "done" for it in items)       # строку уже качали (часть фото скачана) — докачиваем
        if not started:
            if (point_id, taken) not in have_cache:
                have_cache[(point_id, taken)] = len(await visit_photos_repository.list_for_point_date(point_id, taken))
            have = have_cache[(point_id, taken)]
            if have:
                for i in pending:
                    items[i].update(state="skipped", error=f"в папке комплекса за {taken} уже есть фото: {have}")
                await _save_row_photos(rid, items)
                skipped += len(pending)
                continue
        todo.append((rid, point_id, task_id, form_time, items, pending))
    total = sum(len(t[5]) for t in todo)
    if not total:
        reason = (f"Фото не качаю: в папках комплексов за эти даты уже есть фото ({skipped} шт. из таблицы пропущено)" if skipped
                  else "У выбранных строк нечего скачивать: фото уже загружены или ссылок нет (или комплекс не найден)")
        return {**job_status(), "started": False, "skipped": skipped, "reason": reason}
    _JOB.update(running=True, total=total, done=0, failed=0, skipped=skipped, current="", error="", started_at=time.time(), finished_at=0.0, rows=[t[0] for t in todo])
    asyncio.ensure_future(_run_download(todo))
    return {**job_status(), "started": True}


async def _run_download(todo: list[tuple]) -> None:
    from app.services import forms_auto  # noqa: PLC0415 — выбранный вход для Форм (тот же, что для карт, или отдельный)
    try:
        profile = (await forms_auto.load())["profile"]
    except Exception:  # noqa: BLE001
        profile = "maps"
    auth_fails = 0
    fetched: dict[str, tuple[bytes, str]] = {}        # ссылка → (файл, расширение): отчёт на несколько комплексов — те же фото у каждого, скачиваем один раз
    fetched_size = 0
    try:
        for rid, point_id, task_id, form_time, items, pending in todo:
            taken = (form_time or "")[:10] or dt.date.today().isoformat()
            for i in pending:
                it = items[i]
                _JOB["current"] = f"{point_id}: фото {pending.index(i) + 1} из {len(pending)}"
                try:
                    if it["url"] in fetched:
                        content, ext = fetched[it["url"]]
                    else:
                        resp = await yandex_robot.call("POST", "/download", json={"url": it["url"], "profile": profile}, timeout=150.0, raw=True)
                        content = resp.content
                        if not content:
                            raise RuntimeError("пустой файл")
                        ext = _ext_for(resp.headers.get("content-type", ""), it["url"])
                        fetched[it["url"]] = (content, ext)
                        fetched_size += len(content)
                        while fetched_size > FETCH_CACHE_BYTES and len(fetched) > 1:
                            _u, (old, _e) = next(iter(fetched.items()))
                            fetched.pop(_u)
                            fetched_size -= len(old)
                    photo = await visit_photos_repository.create(
                        task_id, point_id, taken, ext, content, FORMS_AUTHOR, source="forms",
                        file_name=re.sub(r"[^\w.\-]+", "_", it["url"].split("/")[-1].split("path=")[-1])[-80:] or None,
                    )
                    it.update(state="done", photo_id=photo["id"], error="")
                    _JOB["done"] += 1
                    auth_fails = 0
                    await asyncio.sleep(PHOTO_PAUSE_SECONDS)
                except yandex_robot.RobotUnavailable as exc:
                    it.update(state="error", error=str(exc))
                    _JOB["failed"] += 1
                    _JOB["error"] = str(exc)
                    await _save_row_photos(rid, items)
                    return                       # робот недоступен — остальные тоже не скачаются
                except yandex_robot.RobotRejected as exc:
                    it.update(state="error", error=exc.detail)
                    _JOB["failed"] += 1
                    _JOB["error"] = exc.detail if exc.status_code in (401, 403, 409) else _JOB["error"]
                    if exc.status_code in (401, 403, 409):
                        auth_fails += 1
                        if auth_fails >= AUTH_FAILS_STOP:        # подряд несколько отказов «нет входа / нет доступа» — остальные получат то же, не гоняем робота зря
                            await _save_row_photos(rid, items)
                            return
                    else:
                        auth_fails = 0
                except Exception as exc:  # noqa: BLE001
                    it.update(state="error", error=f"{type(exc).__name__}: {exc}"[:200])
                    _JOB["failed"] += 1
                await _save_row_photos(rid, items)
    finally:
        _JOB.update(running=False, finished_at=time.time(), current="")


async def _save_row_photos(row_id: int, items: list[dict[str, Any]]) -> None:
    done = sum(1 for it in items if it.get("state") in ("done", "skipped"))
    errors = [it["error"] for it in items if it.get("state") == "error" and it.get("error")]
    async with get_connection() as conn:
        await conn.execute(
            "update forms_import_rows set photo_urls = ?, photos_done = ?, photos_state = ?, photos_error = ?, updated_at = ? where id = ?",
            (json.dumps(items, ensure_ascii=False), done, "done" if done == len(items) else ("error" if errors else "partial"),
             (errors[0] if errors else "")[:300], dt.datetime.now(dt.timezone.utc).isoformat(), row_id),
        )
        await conn.commit()
