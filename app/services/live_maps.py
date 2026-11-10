"""«Живые карты»: именованные карты в Яндекс.Конструкторе, которые приложение само обновляет по таймеру.

Как и карты бригад (yandex_map_sync.py), карта ведётся роботом: приложение считает, какие точки должны
быть на карте и какого они цвета (по статусу комплекса — тем же цветом, что на «Все АПК»), сравнивает с тем,
что лежит на карте сейчас, и правит её ТОЧЕЧНО (перекрасить / убрать / добавить). Ссылка остаётся той же ВСЕГДА: карта не пересоздаётся и не удаляется
при правках (много правок — порциями, остальное следующим проходом); удаляется только по сроку хранения из настроек робота.

Условия (kind): «есть доступ», «доступ и действующая поверка», свой список комплексов, «отвалились за
период», фильтр со страницы «Все АПК». Состав — зафиксированный при создании (меняются цвета и статусы) или
пересчитываемый по условию на каждом обновлении (membership = fixed | dynamic).
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
from typing import Any, Optional

from app.core.dates import format_date
from app.core.text import decode_entities
from app.repositories import live_maps_repository as repo
from app.repositories import point_statuses_repository, points_repository
from app.repositories import yandex_map_repository as map_repo
from app.services import excel_service, yandex_robot
from app.services import yandex_map_sync as ym

log = logging.getLogger(__name__)

KIND_LABELS = {
    "access": "Есть доступ",
    "access_cert": "Есть доступ и действует поверка",
    "list": "Свой список комплексов",
    "dropped": "Отвалились за период",
    "filter": "Фильтр из «Все АПК»",
}
CERT_FILTERS = ("valid", "expired", "missing", "invalid")     # поверка: действует / истекла / нет / нет или истекла
INTERVALS = (5, 10, 15, 30, 60)          # минут между обновлениями
DEFAULT_INTERVAL = 5
MAX_NAME = 80
WORKING = "Работает"


class LiveMapError(ValueError):
    """Ошибка условия карты — текст показывается человеку."""


# ------------------------------------------------------------------ условие карты

def _clean_ids(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,;\n\r\t]+", raw)
    seen: set[str] = set()
    out: list[str] = []
    for item in raw or []:
        pid = str(item).strip()
        key = pid.casefold()
        if pid and key not in seen:
            seen.add(key)
            out.append(pid)
    return out


def normalize_params(kind: str, params: Optional[dict]) -> dict:
    """Приводит условие к виду, в котором оно хранится; бросает LiveMapError, если условие неполное."""
    if kind not in KIND_LABELS:
        raise LiveMapError("Неизвестный тип карты")
    p = params or {}
    out: dict[str, Any] = {}
    if kind != "list":
        cats: list[int] = []
        for c in p.get("category_ids") or []:
            try:
                n = int(c)
            except (TypeError, ValueError):
                continue
            if n > 0 and n not in cats:
                cats.append(n)
        out["category_ids"] = cats
        out["uncategorized"] = bool(p.get("uncategorized"))
    if kind == "access":
        out["statuses"] = [WORKING]
    elif kind == "access_cert":
        out["statuses"] = [WORKING]
        out["certificate"] = "valid"
    elif kind == "filter":
        out["search"] = str(p.get("search") or "").strip()[:100]
        out["statuses"] = [str(s) for s in (p.get("statuses") or []) if s][:30]
    elif kind == "dropped":
        try:
            hours = int(p.get("dropped_hours"))
        except (TypeError, ValueError):
            raise LiveMapError("Укажите период, за который искать отвалившиеся комплексы")
        if not 1 <= hours <= 24 * 365:
            raise LiveMapError("Период — от 1 часа до года")
        out["dropped_hours"] = hours
    else:  # list
        ids = _clean_ids(p.get("point_ids"))
        if not ids:
            raise LiveMapError("Укажите хотя бы один комплекс")
        out["point_ids"] = ids
    # Поверка — отдельное условие, сочетается с любым видом карты, категориями и составом («с поверкой / без» не зависит от
    # остальных выборов). У прежнего вида «доступ и действует поверка» она всегда «действует».
    if kind != "access_cert":
        cert = str(p.get("certificate") or "")
        out["certificate"] = cert if cert in CERT_FILTERS else ""
    return out


def cert_matches(point: dict, cert: str) -> bool:
    """Подходит ли комплекс под условие по поверке (verification_valid_until — «ГГГГ-ММ-ДД» или пусто)."""
    if not cert:
        return True
    valid_until = str(point.get("verification_valid_until") or "")
    today = dt.date.today().isoformat()
    if cert == "valid":
        return bool(valid_until) and valid_until >= today
    if cert == "expired":
        return bool(valid_until) and valid_until < today
    if cert == "missing":
        return not valid_until
    return not valid_until or valid_until < today        # invalid: без поверки (нет или истекла)


def default_membership(kind: str) -> str:
    """Карта по условию («есть доступ», «отвалились», фильтр) по умолчанию пересчитывается — на ней только то, что подходит
    сейчас. «Зафиксировать» (комплексы остаются, меняется цвет) выбирают отдельно: потерявшие доступ там станут красными."""
    return "fixed" if kind == "list" else "dynamic"


async def _all_points() -> list[dict]:
    items, _ = await points_repository.search_points("", [], None, 0, False, [], "", "")
    return items


async def select_points(kind: str, params: dict) -> tuple[list[dict], list[str]]:
    """Комплексы, подходящие под условие, и (для списка) номера, которых нет в справочнике."""
    if kind == "list":
        by_key = {p["id"].strip().casefold(): p for p in await _all_points()}
        found: list[dict] = []
        unknown: list[str] = []
        for pid in params.get("point_ids") or []:
            point = by_key.get(pid.strip().casefold())
            if point:
                found.append(point)
            else:
                unknown.append(pid)
        cert = params.get("certificate", "")
        return [p for p in found if cert_matches(p, cert)], unknown
    items, _ = await points_repository.search_points(
        params.get("search", ""), params.get("category_ids") or [], None, 0, bool(params.get("uncategorized")),
        params.get("statuses") or [], "recent_down" if kind == "dropped" else "", params.get("certificate", ""),
    )
    if kind == "dropped":
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=params["dropped_hours"])).isoformat()
        items = [p for p in items if p.get("port_down_since") and p["port_down_since"] >= cutoff]
    return items, []


def _coords(point: dict) -> Optional[tuple[str, str]]:
    lat = str(point.get("lat") or "").strip().replace(",", ".")
    lon = str(point.get("lon") or "").strip().replace(",", ".")
    try:
        if lat and lon and not (float(lat) == 0 and float(lon) == 0):
            return lat, lon
    except ValueError:
        pass
    return None


def count_with_coords(points: list[dict]) -> int:
    return sum(1 for p in points if _coords(p))


async def current_points(lm: dict) -> list[dict]:
    """Комплексы, которые должны быть на карте сейчас (свежие статусы)."""
    if lm["kind"] == "list" or lm["membership"] == "fixed":
        wanted = lm["members"] or lm["params"].get("point_ids") or []
        by_id = {p["id"]: p for p in await _all_points()}
        return [by_id[i] for i in wanted if i in by_id]
    points, _ = await select_points(lm["kind"], lm["params"])
    return points


def build_rows(points: list[dict], colors: dict[str, str]) -> tuple[list[list], int]:
    """Строки импорта в Конструктор: [широта, долгота, описание, подпись, "", цвет-preset]. Цвет метки — по цвету
    статуса комплекса. В описании только то, что меняется со сменой статуса (не «сколько часов недоступен»), чтобы
    карта не перекрашивалась и не переписывалась на каждом обновлении. Возвращает (строки, без координат)."""
    rows: list[list] = []
    skipped = 0
    for p in points:
        xy = _coords(p)
        if not xy:
            skipped += 1
            continue
        status = p.get("status") or ""
        status_text = "Частично на складе" if status == "На складе" and p.get("stock_partial") else (status or "Не известно")
        parts = [
            decode_entities(p.get("address") or ""),
            decode_entities(p.get("description") or "")[:140],
            f"Статус: {status_text}",
        ]
        if p.get("port_down_since") and status != WORKING:
            parts.append(f"Нет доступа с {format_date(p['port_down_since'])}")
        rows.append([
            xy[0], xy[1], " · ".join(x for x in parts if x), decode_entities(p.get("id") or ""), "",
            excel_service.yandex_preset_for_color(colors.get(status, "")),
        ])
    return rows, skipped


def _signature(name: str, rows: list[list]) -> str:
    return hashlib.sha256(json.dumps([name, rows], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


async def status_colors() -> dict[str, str]:
    return {s["name"]: s["color"] for s in await point_statuses_repository.list_statuses()}


async def build_for(lm: dict, colors: Optional[dict[str, str]] = None) -> tuple[list[list], int]:
    return build_rows(await current_points(lm), colors if colors is not None else await status_colors())


# ------------------------------------------------------------------ план действия

def _create_body(lm: dict, rows: list[list], skipped: int, old_map_id: str = "") -> dict:
    return {
        "kind": "create", "name": lm["name"], "filename": f"live-{lm['id']}.xlsx",
        "xlsx_b64": base64.b64encode(excel_service._yandex_workbook_bytes(rows)).decode("ascii"), "map_id": old_map_id,
        "meta": {"owner": f"live:{lm['id']}", "points": len(rows), "skipped": skipped},
    }


def make_plan(lm: dict, rows: list[list], skipped: int, *, force: bool = False) -> dict:
    """{kind: none|create|patch|adopt, body, summary, map_id, rows_after, partial}. Карту только правим (порциями), не пересоздаём и не удаляем."""
    name = lm["name"]
    map_id = lm["map_id"]
    meta = {"owner": f"live:{lm['id']}", "skipped": skipped}
    if not rows:
        if not map_id:
            return {"kind": "none", "summary": "", "map_id": ""}
        old = lm["rows"]
        if old is None:
            return {"kind": "adopt", "map_id": map_id, "summary": "подходящих точек не осталось — читаю карту и очищаю её (ссылка остаётся)"}
        if not old:
            return {"kind": "none", "summary": "", "map_id": map_id}
        rem, _, _, partial = ym.chunk_ops(old, [], [])
        after = ym.rows_after(old, rem, [], [])
        return {"kind": "patch", "map_id": map_id, "summary": "подходящих точек не осталось — очищаю карту (карту не удаляю)", "rows_after": after, "partial": partial,
                "body": ym.patch_body(map_id, name, rem, [], [], len(after), {**meta, "points": len(after)})}

    sig = _signature(name, rows)
    if not map_id:
        return {"kind": "create", "body": _create_body(lm, rows, skipped), "map_id": "", "summary": f"создаётся карта ({len(rows)} точек)"}
    if lm["url"] and lm["signature"] == sig and not force:
        return {"kind": "none", "summary": "", "map_id": map_id}
    old_rows = lm["rows"]
    if old_rows is None:
        return {"kind": "adopt", "map_id": map_id, "summary": "читаю точки на карте и правлю её точечно (ссылка остаётся прежней)"}
    removed, added, changed = ym.diff_rows(old_rows, rows)
    ops = len(removed) + len(added) + len(changed)
    name_changed = lm["synced_name"] != name
    if ops == 0 and not name_changed:
        return {"kind": "none", "summary": "", "map_id": map_id}
    rem, chg, add, partial = ym.chunk_ops(removed, changed, added)
    after = ym.rows_after(old_rows, rem, chg, add)
    return {
        "kind": "patch", "map_id": map_id, "ops": ops, "rows_after": after, "partial": partial,
        "summary": "правлю карту точечно: " + ym.ops_summary(rem, chg, add, name_changed) + (f" (первая порция, всего правок {ops})" if partial else ""),
        "body": ym.patch_body(map_id, name, rem, chg, add, len(after), {**meta, "points": len(after)}),
    }


# ------------------------------------------------------------------ выполнение

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def active_job(map_id: int) -> Optional[ym.AppJob]:
    job = ym._jobs.get(ym._active.get(f"live:{map_id}", ""))
    return job if job is not None and job.state in ("queued", "running") else None


async def _save_sync(lm: dict, url: str, rows: list[list], skipped: int, *, partial: bool = False) -> None:
    """Что лежит на карте после задания. partial — сделана только порция правок: подпись пустая, и следующий проход продолжит."""
    await repo.update_fields(
        lm["id"], map_id=ym.map_id_from_link(url), url=url, rows=rows, signature="" if partial else _signature(lm["name"], rows),
        synced_name=lm["name"], points_count=len(rows), skipped=skipped, last_sync=_now(), last_error="",
    )


async def _adopt(job: ym.AppJob, lm: dict, rows: list[list], skipped: int, map_id: str) -> Optional[str]:
    """Содержимое карты неизвестно: прочитать точки и применить правки порциями. Ссылка, "missing" (карты нет в аккаунте) или None (ошибка уже записана в задание)."""
    key = f"live:{lm['id']}"
    rj = await ym._robot_job(job, {"kind": "read", "map_id": map_id, "meta": {"owner": key}})
    if rj["state"] != "done":
        if rj.get("error_code") == "map_missing":
            return "missing"
        ym._fail(job, rj)
        return None
    texts = rj.get("objects") or []
    removed, added, changed = ym.diff_by_text(texts, rows)
    current, url, total, done = len(texts), lm["url"], len(removed) + len(added) + len(changed), 0
    while removed or added or changed:
        rem, chg, add, _ = ym.chunk_ops(removed, changed, added)
        removed, changed, added = removed[len(rem):], changed[len(chg):], added[len(add):]
        current = current - len(rem) + len(add)
        done += len(rem) + len(chg) + len(add)
        job.message = f"Правлю карту точечно ({done} из {total})…"
        rj = await ym._robot_job(job, ym.patch_body(map_id, lm["name"], rem, chg, add, current, {"owner": key, "points": current, "skipped": skipped}))
        if rj["state"] != "done":
            ym._fail(job, rj)
            return None
        url = rj["url"]
    return url


async def _execute(job: ym.AppJob, lm: dict, plan: dict, rows: list[list], skipped: int) -> None:
    key = f"live:{lm['id']}"
    async with ym._lock_for(key):
        try:
            job.state = "running"
            job.message = plan.get("summary") or "Запускаю…"
            kind, body = plan["kind"], plan.get("body") or {}
            if kind == "patch":
                ops = len(body.get("add", [])) + len(body.get("remove", [])) + len(body.get("change", []))
                rj = await ym._robot_job(job, body, timeout=120 + 30 * ops)
                if rj["state"] == "done":
                    partial = bool(plan.get("partial"))
                    await _save_sync(lm, rj["url"], plan.get("rows_after") if plan.get("rows_after") is not None else rows, skipped, partial=partial)
                    ym._finish(job, url=rj["url"], message="Карта обновлена" + (" (часть правок — остальные следом)" if partial else ""))
                    return
                if rj.get("error_code") != "map_missing":
                    ym._fail(job, rj)          # правка не вышла — карту НЕ пересоздаём и не удаляем (ссылка остаётся), следующий проход попробует снова
                    return
                kind, body = "create", _create_body(lm, rows, skipped)
                job.message = "Карты нет в аккаунте Яндекса (удалена вручную) — создаю новую…"
            elif kind == "adopt":
                got = await _adopt(job, lm, rows, skipped, plan["map_id"])
                if got is None:
                    return
                if got == "missing":
                    kind, body = "create", _create_body(lm, rows, skipped)
                    job.message = "Карты нет в аккаунте Яндекса (удалена вручную) — создаю новую…"
                else:
                    await _save_sync(lm, got, rows, skipped)
                    ym._finish(job, url=got, message="Карта приведена в порядок (ссылка прежняя)")
                    return
            if kind == "create":
                rj = await ym._robot_job(job, body)
                if rj["state"] != "done":
                    ym._fail(job, rj)
                    return
                url = rj["url"]
                await _save_sync(lm, url, rows, skipped)
                ym._finish(job, url=url, message="Карта создана")
        except yandex_robot.RobotUnavailable as exc:
            job.state, job.error_code, job.message = "error", "unavailable", str(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой при работе с живой картой %s", key)
            job.state, job.error_code, job.message = "error", "unknown", f"Сбой: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            job.finished = time.time()
            if ym._active.get(key) == job.id:
                ym._active.pop(key, None)


async def _run(job: ym.AppJob, lm: dict, plan: dict, rows: list[list], skipped: int) -> None:
    await _execute(job, lm, plan, rows, skipped)
    if job.state == "error":
        # Ошибку видно в окне «Живые карты» рядом с картой, пока следующее обновление не пройдёт.
        await repo.update_fields(lm["id"], last_error=job.message[:700])


def start_job(lm: dict, plan: dict, rows: list[list], skipped: int, *, auto: bool = False) -> ym.AppJob:
    """Запускает выполнение плана в фоне; на одну карту одновременно идёт одно задание."""
    key = f"live:{lm['id']}"
    active = active_job(lm["id"])
    if active is not None:
        return active
    job = ym.AppJob(id=uuid.uuid4().hex[:12], owner_key=key, kind=plan["kind"], created=time.time(), points=len(rows), skipped=skipped, auto=auto)
    ym._jobs[job.id] = job
    ym._active[key] = job.id
    task = asyncio.create_task(_run(job, lm, plan, rows, skipped))
    ym._tasks.add(task)
    task.add_done_callback(ym._tasks.discard)
    ym._trim_jobs()
    return job


async def sync_now(map_id: int, *, force: bool = False) -> tuple[Optional[ym.AppJob], dict, int]:
    """Кнопка «Обновить сейчас»: считает состав и запускает задание (или «карта актуальна»).
    Возвращает (задание или None, план, число точек)."""
    lm = await repo.get(map_id)
    if lm is None:
        raise LookupError("Карта не найдена")
    rows, skipped = await build_for(lm)
    plan = make_plan(lm, rows, skipped, force=force)
    await repo.update_fields(map_id, last_check_ts=time.time(), points_count=len(rows), skipped=skipped)
    if plan["kind"] == "none":
        return None, plan, len(rows)
    return start_job(lm, plan, rows, skipped), plan, len(rows)


async def rebuild_members(map_id: int) -> list[str]:
    """«Пересобрать состав»: заново применяет условие и запоминает подходящие комплексы."""
    lm = await repo.get(map_id)
    if lm is None:
        raise LookupError("Карта не найдена")
    if lm["kind"] == "list":
        raise LiveMapError("У своего списка состав задаёте вы — измените список")
    points, _ = await select_points(lm["kind"], lm["params"])
    ids = [p["id"] for p in points]
    await repo.update_fields(map_id, members=ids)
    return ids


# ------------------------------------------------------------------ фоновый цикл

async def tick_live(budget: int) -> tuple[int, bool]:
    """Обновляет те живые карты, которым пора. Возвращает (остаток заданий на этот проход, нужно ли остановиться:
    робот не вошёл в Яндекс / капча)."""
    maps = [m for m in await repo.list_all() if m["enabled"]]
    if not maps:
        return budget, False
    colors = await status_colors()
    for lm in maps:
        if budget <= 0:
            break
        key = f"live:{lm['id']}"
        now = time.time()
        if now - lm["last_check_ts"] < max(1, lm["interval_minutes"]) * 60:
            continue
        fails = ym._failures.get(key)
        if fails and now < fails[1]:
            continue
        if active_job(lm["id"]) is not None:
            continue
        try:
            rows, skipped = await build_for(lm, colors)
        except Exception:  # noqa: BLE001
            log.exception("Живая карта %s: не удалось посчитать состав", key)
            continue
        plan = make_plan(lm, rows, skipped)
        await repo.update_fields(lm["id"], last_check_ts=now, points_count=len(rows), skipped=skipped)
        if plan["kind"] == "none":
            ym._failures.pop(key, None)
            if lm["last_error"]:
                await repo.update_fields(lm["id"], last_error="")
            continue
        budget -= 1
        job = start_job(lm, plan, rows, skipped, auto=True)
        log.info("Живая карта %s (%s): %s", key, lm["name"], plan.get("summary"))
        ym.STATUS["last_message"] = f"{lm['name']}: {plan.get('summary')}"
        while job.state in ("queued", "running"):
            await asyncio.sleep(1.0)
        if job.state == "done":
            ym._failures.pop(key, None)
        else:
            n = (fails[0] if fails else 0) + 1
            ym._failures[key] = (n, time.time() + min(120 * 2 ** (n - 1), 3600))
            ym.STATUS["last_error"] = f"{lm['name']}: {job.message}"
            if job.error_code in ym.FATAL_CODES:
                ym.STATUS["paused_until"] = time.time() + 600
                return budget, True
    return budget, False


async def forget_map(lm: dict) -> None:
    """Живую карту убрали из списка: в Конструкторе она остаётся (ссылка продолжает работать, её могли разослать) и уйдёт по сроку хранения — как и остальные карты:
    запись о ней кладётся в общий учёт карт с сегодняшней датой, а срок хранения (настройка робота) отсчитывается от неё."""
    if lm["map_id"]:
        await map_repo.upsert_map(f"orphan:{lm['map_id']}", crew_id=0, map_id=lm["map_id"], url=lm["url"] or "", name=lm["name"], rows=None, signature="",
                                  work_date=dt.date.today().isoformat())


# ------------------------------------------------------------------ для окна

def public(lm: dict) -> dict[str, Any]:
    job = active_job(lm["id"])
    return {
        "id": lm["id"], "name": lm["name"], "kind": lm["kind"], "kind_label": KIND_LABELS.get(lm["kind"], lm["kind"]),
        "membership": lm["membership"], "interval_minutes": lm["interval_minutes"], "enabled": lm["enabled"],
        "url": lm["url"], "points_count": lm["points_count"], "skipped": lm["skipped"], "members_count": len(lm["members"] or []),
        "last_sync": lm["last_sync"], "last_check_ts": lm["last_check_ts"], "last_error": lm["last_error"],
        "params": lm["params"], "created_by": lm["created_by"], "created_at": lm["created_at"],
        "job": ({"id": job.id, "state": job.state, "message": job.message, "kind": job.kind} if job else None),
    }
