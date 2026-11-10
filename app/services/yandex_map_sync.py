"""Карты бригад в Яндекс.Конструкторе ведёт само приложение.

Что происходит:
* у каждой бригады (у командировки — одна карта на все дни) есть «владелец карты». Для него считается
  список точек карты (задания с координатами; приоритетные — жёлтые, с отчётом о выезде — зелёные) и
  его «подпись»; в таблице yandex_maps запомнено, что лежало на карте при последней синхронизации;
* когда точки меняются (добавили, убрали, сдан отчёт…) и изменения «устоялись» (несколько десятков
  секунд без новых правок), фоновый цикл сам правит карту ТОЧЕЧНО (убрать / перекрасить / добавить через поиск по
  координатам). Ссылка на карту остаётся той же ВСЕГДА: карты не пересоздаются и не удаляются при правках. Правок много —
  они делаются порциями (PATCH_CHUNK за задание), остальное следующим проходом. Карта, чьё содержимое приложение не помнит
  (ссылку вставили вручную), сначала читается роботом, потом правится так же;
* если точек не осталось или бригаду удалили — карта остаётся как есть;
* карты удаляются ТОЛЬКО по сроку хранения (настройка в окне «Яндекс-аккаунт для карт»: через N дней после даты выезда).

Ту же логику использует кнопка «Создать / обновить карту» в окне бригады. Всё, что делает браузер
(Конструктор), выполняет отдельный контейнер-робот (robot/), сюда доходят только его результаты.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import get_settings
from app.core import map_events
from app.core.dates import format_date
from app.repositories import crew_repository, points_repository, state_repository
from app.repositories import yandex_map_repository as repo
from app.services import excel_service, yandex_robot

log = logging.getLogger(__name__)

# Карту только правим (ссылка не меняется), но за одно задание робота — не больше PATCH_CHUNK правок: каждая новая точка — поиск по координатам, ~7 секунд, а
# задание ограничено по времени. Остальное доделывается следующими проходами (в учёте запоминается то, что реально лежит на карте).
PATCH_CHUNK = 40
FATAL_CODES = {"not_logged_in", "captcha", "unavailable"}   # дальше пробовать бессмысленно
MAX_JOBS_PER_TICK = 4
_MAP_ID_RE = re.compile(r"um=constructor(?:%3A|:)([0-9a-fA-F]{16,})")
_YANDEX_MAP_LINK_RE = re.compile(r"^https://yandex\.[a-z]+/maps/")


def map_id_from_link(link: str) -> str:
    m = _MAP_ID_RE.search(link or "")
    return m.group(1).lower() if m else ""


def is_yandex_map_link(link: str) -> bool:
    return bool(_YANDEX_MAP_LINK_RE.match((link or "").strip()))


def _hex_for_preset(preset: str) -> str:
    return excel_service._YANDEX_PRESET_COLORS.get(preset, "1E98FF")


# ------------------------------------------------------------------ владелец карты и «что изменилось»

@dataclass
class Owner:
    key: str                  # crew:<id> | trip:<trip_id>
    crew: dict                # бригада-представитель (для названия, места, даты)
    crew_ids: list[int]
    name: str
    rows: list[list]          # строки импорта: [широта, долгота, описание, подпись, "", цвет-preset]
    skipped: int              # заданий без координат
    work_date: str            # последний день выезда (по нему работает автоудаление)
    link: str                 # что сейчас в поле «Ссылка на карту» бригады

    @property
    def signature(self) -> str:
        return hashlib.sha256(json.dumps([self.name, self.rows], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def map_name(crew: dict) -> str:
    if crew.get("trip_id") and crew.get("trip_from"):
        when = f"{format_date(crew['trip_from'])} – {format_date(crew['trip_to'])}"
    else:
        when = format_date(crew["date"])
    place = (crew.get("place") or "").strip()
    return " · ".join(part for part in (crew.get("name") or "Бригада", place, when) if part)


def owner_key_of(crew: dict) -> str:
    return f"trip:{crew['trip_id']}" if crew.get("trip_id") else f"crew:{crew['id']}"


def build_owners(crews: list[dict], tasks: list[dict], points: list[dict]) -> dict[str, Owner]:
    pts = {p["id"]: p for p in points if "id" in p}
    owners: dict[str, Owner] = {}
    for crew in crews:
        key = owner_key_of(crew)
        if key in owners:
            continue
        rows, skipped = excel_service._yandex_crew_rows(crew["date"], crew, tasks, pts)
        owners[key] = Owner(
            key=key, crew=crew, crew_ids=list(crew.get("trip_crew_ids") or [crew["id"]]), name=map_name(crew),
            rows=rows, skipped=skipped, work_date=crew.get("trip_to") or crew["date"], link=(crew.get("map_link") or "").strip(),
        )
    return owners


def _point(row: list) -> dict:
    return {"lat": str(row[0]), "lon": str(row[1]), "caption": row[3], "desc": row[2], "color": _hex_for_preset(row[5])}


def diff_rows(old: list[list], new: list[list]) -> tuple[list[list], list[list], list[tuple[list, list]]]:
    """(убрать, добавить, изменить). Точка опознаётся по (широта, долгота, подпись); если у неё
    изменилось только описание или цвет — это «изменить», а не «убрать + добавить»."""
    old_by, new_by = defaultdict(list), defaultdict(list)
    for r in old:
        old_by[(str(r[0]), str(r[1]), r[3])].append(r)
    for r in new:
        new_by[(str(r[0]), str(r[1]), r[3])].append(r)
    removed: list[list] = []
    added: list[list] = []
    changed: list[tuple[list, list]] = []
    for key in set(old_by) | set(new_by):
        o_left = list(old_by.get(key, []))
        n_left: list[list] = []
        for r in new_by.get(key, []):
            if r in o_left:
                o_left.remove(r)       # такая же строка уже на карте
            else:
                n_left.append(r)
        while o_left and n_left:
            changed.append((o_left.pop(0), n_left.pop(0)))
        removed += o_left
        added += n_left
    return removed, added, changed


# ------------------------------------------------------------------ план действия

@dataclass
class Plan:
    kind: str                                   # none | create | patch | adopt (прочитать карту и поправить)
    owner: Optional[Owner] = None
    registry: Optional[dict] = None
    map_id: str = ""
    body: dict = field(default_factory=dict)    # тело задания для робота
    summary: str = ""
    note: str = ""
    rows_after: Optional[list] = None           # patch: что будет лежать на карте после ЭТОЙ порции правок
    partial: bool = False                       # patch: правок больше одной порции — остальное следующим проходом


def chunk_ops(removed: list, changed: list, added: list, limit: int = PATCH_CHUNK) -> tuple[list, list, list, bool]:
    """Первая порция правок: сначала убрать, потом изменить, потом добавить — всего не больше `limit`. Последний элемент — остались ли ещё правки."""
    rem = removed[:limit]
    chg = changed[: max(limit - len(rem), 0)]
    add = added[: max(limit - len(rem) - len(chg), 0)]
    return rem, chg, add, (len(rem) + len(chg) + len(add)) < (len(removed) + len(changed) + len(added))


def rows_after(old_rows: list[list], rem: list, chg: list, add: list) -> list[list]:
    """Содержимое карты после применения правок к известному прежнему."""
    rows = [list(r) for r in old_rows]
    for r in rem:
        try:
            rows.remove(list(r))
        except ValueError:
            pass
    for o, n in chg:
        try:
            rows[rows.index(list(o))] = list(n)
        except ValueError:
            rows.append(list(n))
    rows.extend(list(r) for r in add)
    return rows


def patch_body(map_id: str, name: str, rem: list, chg: list, add: list, expected_total: int, meta: dict) -> dict:
    return {
        "kind": "patch", "map_id": map_id, "name": name,
        "add": [_point(r) for r in add], "remove": [_point(r) for r in rem],
        "change": [
            {"caption": o[3], "old_desc": o[2], "desc": n[2] if n[2] != o[2] else None, "color": _hex_for_preset(n[5]) if n[5] != o[5] else None}
            for o, n in chg
        ],
        "expected_total": expected_total, "meta": meta,
    }


def ops_summary(rem: list, chg: list, add: list, name_changed: bool) -> str:
    parts = [p for p in (f"+{len(add)}" if add else "", f"−{len(rem)}" if rem else "", f"изменено {len(chg)}" if chg else "", "название" if name_changed else "") if p]
    return ", ".join(parts)


_OBJECT_RE = re.compile(r"^\((?P<caption>.*?)\)\s*(?P<desc>.*)$", re.S)


def split_object_text(text: str) -> tuple[str, str]:
    """«(ПОДПИСЬ) описание» из списка точек Конструктора → (подпись, описание)."""
    m = _OBJECT_RE.match((text or "").strip())
    return (m.group("caption").strip(), m.group("desc").strip()) if m else ("", (text or "").strip())


def _desc_key(desc: Any) -> str:
    return " ".join(str(desc or "").split())[:25].lower()


def diff_by_text(texts: list[str], new_rows: list[list]) -> tuple[list[list], list[list], list[tuple[list, list]]]:
    """Что убрать / добавить / изменить, когда о карте известны только тексты точек (координат и цветов список не показывает): точка «та же», если совпала подпись и
    начало описания; та же подпись с другим описанием — «изменить»; остальное — «убрать» и «добавить». Цвет у «тех же» не трогаем (прочитать его нельзя)."""
    objs = [split_object_text(t) for t in texts]
    used = [False] * len(objs)
    unmatched: list[list] = []
    for r in new_rows:
        key = _desc_key(r[2])
        idx = next((i for i, (cap, desc) in enumerate(objs) if not used[i] and cap == r[3] and (not key or key in " ".join(desc.split()).lower())), None)
        if idx is None:
            unmatched.append(r)
        else:
            used[idx] = True
    left = [i for i in range(len(objs)) if not used[i]]
    changed: list[tuple[list, list]] = []
    added: list[list] = []
    for r in unmatched:
        idx = next((i for i in left if objs[i][0] == r[3]), None)
        if idx is None:
            added.append(r)
            continue
        left.remove(idx)
        changed.append((["", "", objs[idx][1], objs[idx][0], "", r[5]], r))
    removed = [["0", "0", objs[i][1], objs[i][0], "", ""] for i in left]
    return removed, added, changed


def _xlsx_b64(owner: Owner) -> str:
    return base64.b64encode(excel_service._yandex_workbook_bytes(owner.rows)).decode("ascii")


def _create_body(owner: Owner, old_map_id: str = "") -> dict:
    return {
        "kind": "create", "name": owner.name, "filename": f"crew-{owner.crew['id']}.xlsx",
        "xlsx_b64": _xlsx_b64(owner), "map_id": old_map_id,
        "meta": {"owner": owner.key, "points": len(owner.rows), "skipped": owner.skipped},
    }


def make_plan(owner: Owner, registry: Optional[dict], *, force: bool = False) -> Plan:
    """Что сделать с картой владельца. force=True — вызов кнопкой: ссылку в поле можно заменить."""
    link_id = map_id_from_link(owner.link)
    reg = registry
    if reg is None and link_id:
        # Карта создана до учёта (или ссылку вставили вручную) — берём под своё ведение.
        reg = {"owner_key": owner.key, "map_id": link_id, "url": owner.link, "rows": None, "signature": "", "name": ""}

    if not owner.rows:
        # Карту не удаляем и не очищаем: она остаётся как есть (ссылка рабочая), удалится по сроку хранения из настроек.
        if registry is not None:
            return Plan("none", owner, registry, note="точек не осталось — карту не трогаю (удалится по сроку хранения)")
        return Plan("none", owner, note="нет заданий с координатами")

    if reg is None:
        if owner.link and not is_yandex_map_link(owner.link) and not force:
            return Plan("none", owner, note="в поле ссылки заметка — карту не создаю, чтобы её не затереть")
        return Plan("create", owner, None, body=_create_body(owner), summary=f"создаётся карта ({len(owner.rows)} точек)")

    # Карта ведётся. Если ссылку в бригаде поменяли руками — автомат не вмешивается, кнопка создаёт новую.
    if reg.get("url") and owner.link and owner.link != reg["url"] and map_id_from_link(owner.link) != reg["map_id"]:
        if not force:
            return Plan("none", owner, reg, note="ссылку в бригаде изменили вручную — карту не трогаю")
        return Plan("create", owner, reg, body=_create_body(owner), summary="создаётся новая карта (ссылка была другой)")
    if not owner.link and not force and reg.get("signature") is not None and registry is not None:
        return Plan("none", owner, reg, note="ссылку в бригаде очистили вручную — карту не трогаю")

    if reg.get("signature") == owner.signature and owner.link:
        return Plan("none", owner, reg, note="карта актуальна")

    old_rows = reg.get("rows")
    if old_rows is None:
        # Что на карте — не знаем (ссылку вставили вручную): робот прочитает точки, и карта правится ТОЧЕЧНО, как всегда — ссылка остаётся.
        return Plan("adopt", owner, reg, reg["map_id"], summary="читаю точки на карте и правлю её точечно (ссылка остаётся прежней)")
    removed, added, changed = diff_rows(old_rows, owner.rows)
    name_changed = reg.get("name") != owner.name
    ops = len(removed) + len(added) + len(changed)
    if ops == 0 and not name_changed:
        # Содержимое то же, а подпись отличается — только если поменялся порядок строк: на карте это не важно.
        return Plan("none", owner, reg, note="карта актуальна")
    rem, chg, add, partial = chunk_ops(removed, changed, added)
    after = rows_after(old_rows, rem, chg, add)
    meta = {"owner": owner.key, "points": len(after), "skipped": owner.skipped}
    summary = "правлю карту точечно: " + ops_summary(rem, chg, add, name_changed) + (f" (первая порция, всего правок {ops})" if partial else "")
    return Plan("patch", owner, reg, reg["map_id"], patch_body(reg["map_id"], owner.name, rem, chg, add, len(after), meta), summary, rows_after=after, partial=partial)


# ------------------------------------------------------------------ задания приложения (то, что видит окно)

@dataclass
class AppJob:
    id: str
    owner_key: str
    kind: str
    created: float
    state: str = "queued"
    message: str = "В очереди"
    url: Optional[str] = None
    error_code: Optional[str] = None
    finished: Optional[float] = None
    robot_job_id: str = ""
    has_shot: bool = False
    points: int = 0
    skipped: int = 0
    auto: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "message": self.message, "url": self.url,
            "error_code": self.error_code, "created": self.created, "finished": self.finished, "has_shot": self.has_shot,
            "robot_job_id": self.robot_job_id, "points": self.points, "skipped": self.skipped,
        }


_jobs: dict[str, AppJob] = {}
_active: dict[str, str] = {}          # owner_key -> id выполняющегося задания
_locks: dict[str, asyncio.Lock] = {}
_tasks: set[asyncio.Task] = set()
STATUS: dict[str, Any] = {"last_tick": None, "last_message": "", "last_error": "", "paused_until": 0.0}


def get_job(job_id: str) -> Optional[AppJob]:
    return _jobs.get(job_id)


def _trim_jobs() -> None:
    if len(_jobs) <= 80:
        return
    for job in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - 80]:
        if job.state in ("done", "error"):
            _jobs.pop(job.id, None)


async def _robot_job(job: Optional[AppJob], body: dict, *, timeout: float = 900) -> dict:
    """Ставит задание роботу и ждёт его конца (ход работы — в job.message)."""
    started = await yandex_robot.call("POST", "/jobs", json=body, timeout=90.0)
    rid = started["id"]
    if job is not None:
        job.robot_job_id = rid
    t0 = time.monotonic()
    failures = 0
    while True:
        await asyncio.sleep(1.5)
        try:
            rj = await yandex_robot.call("GET", f"/jobs/{rid}")
            failures = 0
        except yandex_robot.RobotRejected as exc:   # робот перезапустился — задание потеряно
            return {"state": "error", "error_code": "unknown", "message": f"Робот потерял задание: {exc.detail}", "has_shot": False, "id": rid}
        except yandex_robot.RobotUnavailable:
            failures += 1
            if failures >= 6:
                raise
            continue
        if job is not None:
            job.message = rj.get("message") or job.message
            job.has_shot = bool(rj.get("has_shot"))
        if rj["state"] in ("done", "error"):
            return rj
        if time.monotonic() - t0 > timeout:
            return {"state": "error", "error_code": "timeout", "message": "Робот не ответил вовремя", "has_shot": False, "id": rid}


def _lock_for(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


async def _save_registry(owner: Owner, url: str, rows: Optional[list] = None, signature: Optional[str] = None) -> None:
    """Что лежит на карте после задания. Порция правок: rows — то, что реально на карте, signature пустая (карта ещё не догнала бригаду — следующий проход продолжит)."""
    await repo.upsert_map(
        owner.key, crew_id=owner.crew["id"], map_id=map_id_from_link(url), url=url, name=owner.name,
        rows=owner.rows if rows is None else rows, signature=owner.signature if signature is None else signature, work_date=owner.work_date,
    )


async def _set_link(owner: Owner, url: str) -> None:
    # update_crew для бригады в командировке применяется ко всем её дням.
    await crew_repository.update_crew(owner.crew["id"], name=None, equip=None, person_ids=None, map_link=url)


async def _adopt(job: AppJob, owner: Owner, plan: Plan) -> Optional[str]:
    """Карта, чьё содержимое неизвестно: прочитать точки, посчитать правки по текстам и применить порциями. Возвращает ссылку, "missing" (карты нет в аккаунте)
    или None (задание уже помечено ошибкой). Ничего не удаляется и не создаётся заново."""
    rj = await _robot_job(job, {"kind": "read", "map_id": plan.map_id, "meta": {"owner": owner.key}})
    if rj["state"] != "done":
        if rj.get("error_code") == "map_missing":
            return "missing"
        _fail(job, rj)
        return None
    texts = rj.get("objects") or []
    removed, added, changed = diff_by_text(texts, owner.rows)
    current = len(texts)
    url = owner.link
    total_ops = len(removed) + len(added) + len(changed)
    done_ops = 0
    while removed or added or changed:
        rem, chg, add, _ = chunk_ops(removed, changed, added)
        removed, changed, added = removed[len(rem):], changed[len(chg):], added[len(add):]
        current = current - len(rem) + len(add)
        done_ops += len(rem) + len(chg) + len(add)
        job.message = f"Правлю карту точечно ({done_ops} из {total_ops})…"
        rj = await _robot_job(job, patch_body(plan.map_id, owner.name, rem, chg, add, current, {"owner": owner.key, "points": current, "skipped": owner.skipped}))
        if rj["state"] != "done":
            _fail(job, rj)
            return None
        url = rj["url"]
    return url


async def _run(job: AppJob, plan: Plan) -> None:
    owner = plan.owner
    assert owner is not None
    async with _lock_for(owner.key):
        try:
            job.state = "running"
            job.message = plan.summary or "Запускаю…"
            kind = plan.kind
            body = plan.body
            if kind == "patch":
                rj = await _robot_job(job, body)
                if rj["state"] == "done":
                    await _save_registry(owner, rj["url"], rows=plan.rows_after, signature="" if plan.partial else None)
                    await _set_link(owner, rj["url"])
                    _finish(job, url=rj["url"], message="Карта обновлена" + (" (часть правок — остальные следом)" if plan.partial else ""))
                    return
                if rj.get("error_code") != "map_missing":
                    _fail(job, rj)             # правка не вышла — карту НЕ пересоздаём (ссылка остаётся), следующий проход попробует снова
                    return
                kind, body = "create", _create_body(owner)
                job.message = "Карты нет в аккаунте Яндекса (удалена вручную) — создаю новую…"
            elif kind == "adopt":
                got = await _adopt(job, owner, plan)
                if got is None:
                    return
                if got == "missing":
                    kind, body = "create", _create_body(owner)
                    job.message = "Карты нет в аккаунте Яндекса (удалена вручную) — создаю новую…"
                else:
                    await _save_registry(owner, got)
                    await _set_link(owner, got)
                    _finish(job, url=got, message="Карта приведена в порядок (ссылка прежняя)")
                    return
            if kind == "create":
                rj = await _robot_job(job, body)
                if rj["state"] != "done":
                    _fail(job, rj)
                    return
                url = rj["url"]
                await _save_registry(owner, url)
                await _set_link(owner, url)
                _finish(job, url=url, message="Карта создана")
        except yandex_robot.RobotUnavailable as exc:
            job.state, job.error_code, job.message = "error", "unavailable", str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой при работе с картой %s", owner.key)
            job.state, job.error_code, job.message = "error", "unknown", f"Сбой: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            job.finished = time.time()
            if _active.get(owner.key) == job.id:
                _active.pop(owner.key, None)


def _finish(job: AppJob, *, url: Optional[str], message: str) -> None:
    job.state, job.url, job.message = "done", url, message


def _fail(job: AppJob, rj: dict) -> None:
    job.state, job.error_code, job.message = "error", rj.get("error_code") or "unknown", rj.get("message") or "Робот не смог выполнить задание"


def start(owner: Owner, plan: Plan, *, auto: bool = False) -> AppJob:
    """Запускает выполнение плана в фоне. Для владельца одновременно идёт одно задание."""
    active = _jobs.get(_active.get(owner.key, ""))
    if active is not None and active.state in ("queued", "running"):
        return active
    job = AppJob(id=uuid.uuid4().hex[:12], owner_key=owner.key, kind=plan.kind, created=time.time(), points=len(owner.rows), skipped=owner.skipped, auto=auto)
    _jobs[job.id] = job
    _active[owner.key] = job.id
    task = asyncio.create_task(_run(job, plan))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    _trim_jobs()
    return job


# ------------------------------------------------------------------ удаление карт

async def _delete_by_id(map_id: str) -> tuple[bool, str]:
    """Удаляет карту в Конструкторе напрямую (корзина, автоудаление). (успех, сообщение)."""
    rj = await _robot_job(None, {"kind": "delete", "map_id": map_id})
    return rj["state"] == "done", rj.get("message") or ""


async def _delete_registered(reg: dict, *, reason: str) -> bool:
    ok, message = await _delete_by_id(reg["map_id"])
    if not ok:
        log.warning("Не удалось удалить карту %s (%s): %s", reg["owner_key"], reason, message)
        return False
    await repo.delete_map_row(reg["owner_key"])
    crew = await crew_repository.get_crew(reg["crew_id"])
    if crew and map_id_from_link(crew.get("map_link") or "") == reg["map_id"]:
        await crew_repository.update_crew(crew["id"], name=None, equip=None, person_ids=None, map_link="")
    log.info("Карта %s удалена (%s)", reg["owner_key"], reason)
    return True


async def _owner_exists(owner_key: str) -> bool:
    kind, _, value = owner_key.partition(":")
    from app.db.database import get_connection

    async with get_connection() as conn:
        cur = await conn.execute(
            "select 1 from crews where trip_id = ? limit 1" if kind == "trip" else "select 1 from crews where id = ? limit 1", (value,)
        )
        return (await cur.fetchone()) is not None


# ------------------------------------------------------------------ фоновый цикл

_pending: dict[str, tuple[str, float]] = {}       # owner_key -> (подпись, когда впервые увидели)
_failures: dict[str, tuple[int, float]] = {}      # owner_key -> (число сбоев подряд, не пробовать до)


async def _robot_ready() -> tuple[bool, str]:
    try:
        st = await yandex_robot.call("GET", "/status", timeout=30.0)
    except yandex_robot.RobotUnavailable:
        return False, "робот карт не запущен"
    except yandex_robot.RobotRejected as exc:
        return False, exc.detail
    if not st.get("logged_in"):
        return False, "робот не вошёл в Яндекс"
    return True, ""


async def tick() -> None:
    cfg = await repo.get_settings()
    settle = get_settings().yandex_sync_settle_seconds
    now = time.time()
    STATUS["last_tick"] = now
    trash = await repo.trash_list()
    from app.repositories import live_maps_repository

    live_exists = (await live_maps_repository.count()) > 0     # «Живые карты» работают и при выключенной автоматике бригад
    if not (cfg["auto_sync"] or cfg["auto_delete"] or trash or live_exists):
        STATUS["last_message"] = "автоматика выключена"
        return
    if now < STATUS["paused_until"]:
        return
    ready, why = await _robot_ready()
    if not ready:
        STATUS["last_message"] = ""
        STATUS["last_error"] = f"Карты не ведутся: {why}"
        return
    STATUS["last_error"] = ""
    budget = MAX_JOBS_PER_TICK

    if cfg["auto_sync"]:
        today = dt.date.today()
        crews = await crew_repository.list_between(today.isoformat(), (today + dt.timedelta(days=cfg["sync_days_ahead"])).isoformat())
        if crews:
            _, data = await state_repository.read_state()
            owners = build_owners(crews, data["tasks"], await points_repository.list_points())
            for key, owner in owners.items():
                if budget <= 0:
                    break
                plan = make_plan(owner, await repo.get_map(key))
                if plan.kind == "none":
                    _pending.pop(key, None)
                    continue
                sig = owner.signature + plan.kind
                prev = _pending.get(key)
                if prev is None or prev[0] != sig:
                    _pending[key] = (sig, now)        # изменение только что появилось — ждём, не будут ли ещё правки
                    continue
                if now - prev[1] < settle:
                    continue
                fails = _failures.get(key)
                if fails and now < fails[1]:
                    continue
                job = start(owner, plan, auto=True)
                budget -= 1
                log.info("Карты: %s — %s", key, plan.summary)
                STATUS["last_message"] = f"{owner.name}: {plan.summary}"
                while job.state in ("queued", "running"):
                    await asyncio.sleep(1.0)
                _pending.pop(key, None)
                if job.state == "done":
                    _failures.pop(key, None)
                else:
                    n = (fails[0] if fails else 0) + 1
                    _failures[key] = (n, time.time() + min(120 * 2 ** (n - 1), 3600))
                    STATUS["last_error"] = f"{owner.name}: {job.message}"
                    if job.error_code in FATAL_CODES:
                        STATUS["paused_until"] = time.time() + 600
                        return

    # «Живые карты» (списки комплексов, которые обновляются по таймеру) — после карт бригад.
    if live_exists and budget > 0:
        from app.services import live_maps

        budget, fatal = await live_maps.tick_live(budget)
        if fatal:
            return

    # Карту удалённой бригады не удаляем сразу: она остаётся в учёте и уйдёт по сроку хранения (ссылку могли разослать). В корзине — только то, что осталось от прежних версий.
    trash = await repo.trash_list()
    for item in trash[:2]:
        if budget <= 0:
            break
        budget -= 1
        ok, message = await _delete_by_id(item["map_id"])
        if ok:
            await repo.trash_remove(item["map_id"])
            log.info("Карта %s удалена из Конструктора (%s)", item["map_id"][:8], item["reason"])

    if cfg["auto_delete"] and budget > 0:
        limit = (dt.date.today() - dt.timedelta(days=cfg["auto_delete_days"])).isoformat()
        # Карты, созданные до учёта, но с ссылкой в бригаде и уже старые, берём под учёт — чтобы удалить.
        registered = {r["owner_key"] for r in await repo.list_maps()}
        for crew in await crew_repository.list_with_constructor_links():
            key = owner_key_of(crew)
            work_date = crew.get("trip_to") or crew["date"]
            if key not in registered and work_date < limit and map_id_from_link(crew.get("map_link") or ""):
                await repo.upsert_map(
                    key, crew_id=crew["id"], map_id=map_id_from_link(crew["map_link"]), url=crew["map_link"].strip(),
                    name=map_name(crew), rows=None, signature="", work_date=work_date,
                )
                registered.add(key)
        for reg in await repo.list_maps():
            if budget <= 0:
                break
            if reg["work_date"] and reg["work_date"] < limit:
                budget -= 1
                await _delete_registered(reg, reason=f"старше {cfg['auto_delete_days']} дн.")


async def sync_loop() -> None:
    """Фоновый цикл: проверяет бригады и правит карты. Любой сбой — в журнал, цикл не останавливается."""
    interval = max(2, get_settings().yandex_sync_interval_seconds)
    await asyncio.sleep(min(20, interval))  # дать приложению подняться
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Цикл автообновления карт: сбой")
            STATUS["last_error"] = "внутренний сбой автообновления (подробности в журнале приложения)"
        # Дальше — не по часам, а по событию: любое действие, меняющее карты (сохранили календарь, сдан отчёт,
        # изменили бригаду — см. app/core/map_events.py), будит цикл сразу; пока есть изменения, которые «устаиваются»,
        # заглядываем каждые несколько секунд. Короткая пауза не даёт циклу крутиться вхолостую при потоке правок.
        await asyncio.sleep(2)
        await map_events.wait(5 if _pending else interval)
