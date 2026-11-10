"""Автозагрузка отчётов из Яндекс Форм: раз в N минут робот под аккаунтом СОЗДАТЕЛЯ формы выгружает таблицу ответов («Ответы» →
«XLSX с доп.полями» → «Скачать»), приложение разбирает её так же, как загруженную вручную (services/forms_import.py): отчёты создаются
по новым строкам, уже имеющиеся пропускаются, фото скачиваются тем же аккаунтом (и докачиваются те, что раньше не скачались).

Настройки (app_flags, ключ forms_auto): enabled (по умолчанию ВЫКЛЮЧЕНО), url (страница «Ответы» формы — её запоминает окно входа в Яндекс для Форм),
interval_minutes (по умолчанию 30), apply_status (менять ли статус комплекса по отчёту — по умолчанию нет), profile (под каким входом робот работает с Формами:
«maps» — тем же, что для карт, это по умолчанию; «forms» — отдельным входом для Форм, если форма принадлежит другому аккаунту). Состояние последнего запуска хранится
в app_flags (forms_auto_state). Вход делает администратор: «Настройки → Яндекс Формы: вход и автозагрузка»."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import secrets
import time
import urllib.parse
from typing import Any

from app.db.database import get_connection
from app.services import forms_import as fi
from app.services import yandex_robot
from app.services.ai import forms_fault

logger = logging.getLogger(__name__)

CONFIG_KEY = "forms_auto"
STATE_KEY = "forms_auto_state"
DEFAULTS: dict[str, Any] = {"enabled": False, "url": "", "interval_minutes": 30, "apply_status": False, "profile": "maps"}
MIN_INTERVAL, MAX_INTERVAL = 5, 1440
AUTHOR = "Яндекс Форма (авто)"
RETRY_PHOTO_DAYS = 75               # файлы форм хранятся 3 месяца — старше докачивать бессмысленно
_LOCK = asyncio.Lock()
_RUNNING = {"on": False, "step": ""}
_ALLOWED_SUFFIXES = ("yandex.ru", "yandex.com", "ya.ru", "yandex.by", "yandex.kz")


def valid_url(url: str) -> str:
    """Страница формы: https и адрес Яндекса (иначе робот не откроет). Пустая строка допустима (не задана)."""
    url = (url or "").strip()
    if not url:
        return ""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not any(host == s or host.endswith("." + s) for s in _ALLOWED_SUFFIXES):
        raise ValueError("Нужна ссылка на страницу формы Яндекса (https://forms.yandex.ru/…)")
    return url[:1000]


async def _load_json(key: str) -> dict[str, Any]:
    async with get_connection() as conn:
        cur = await conn.execute("select value from app_flags where key = ?", (key,))
        row = await cur.fetchone()
    if row:
        try:
            data = json.loads(row[0])
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {}


async def _save_json(key: str, data: dict[str, Any]) -> None:
    async with get_connection() as conn:
        await conn.execute("insert into app_flags (key, value) values (?, ?) on conflict(key) do update set value = excluded.value", (key, json.dumps(data, ensure_ascii=False)))
        await conn.commit()


async def load() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    saved = await _load_json(CONFIG_KEY)
    for key, default in DEFAULTS.items():
        if key in saved:
            cfg[key] = type(default)(saved[key]) if not isinstance(default, bool) else bool(saved[key])
    cfg["interval_minutes"] = max(MIN_INTERVAL, min(MAX_INTERVAL, int(cfg["interval_minutes"])))
    return cfg


async def save(changes: dict[str, Any]) -> dict[str, Any]:
    cfg = await load()
    if changes.get("url") is not None:
        cfg["url"] = valid_url(changes["url"])
    if changes.get("enabled") is not None:
        cfg["enabled"] = bool(changes["enabled"])
    if changes.get("apply_status") is not None:
        cfg["apply_status"] = bool(changes["apply_status"])
    if changes.get("profile") is not None:
        if changes["profile"] not in ("maps", "forms"):
            raise ValueError("Вход для Форм: «тот же, что для карт» или «отдельный»")
        cfg["profile"] = changes["profile"]
    if changes.get("interval_minutes") is not None:
        n = int(changes["interval_minutes"])
        if not MIN_INTERVAL <= n <= MAX_INTERVAL:
            raise ValueError(f"Периодичность — от {MIN_INTERVAL} минут до {MAX_INTERVAL} (сутки)")
        cfg["interval_minutes"] = n
    if cfg["enabled"] and not cfg["url"]:
        raise ValueError("Сначала укажите страницу «Ответы» формы — без неё автозагрузке нечего открывать")
    await _save_json(CONFIG_KEY, cfg)
    return cfg


async def raw_config() -> dict[str, Any]:
    return await _load_json(CONFIG_KEY)


async def restore_raw_config(raw: dict[str, Any]) -> None:
    """После восстановления базы с другого сервера: автозагрузка — свойство ЭТОГО сервера (его робота и его входа в Яндекс)."""
    if raw:
        await _save_json(CONFIG_KEY, raw)
    else:
        async with get_connection() as conn:
            await conn.execute("delete from app_flags where key in (?, ?)", (CONFIG_KEY, STATE_KEY))
            await conn.commit()


async def state() -> dict[str, Any]:
    st = await _load_json(STATE_KEY)
    st.setdefault("last_run", 0.0)
    st.setdefault("ok", None)
    st.setdefault("message", "")
    st.setdefault("error", "")
    st.setdefault("stats", {})
    st["running"] = _RUNNING["on"]
    st["step"] = _RUNNING["step"]
    return st


async def _finish(trigger: str, ok: bool, message: str = "", error: str = "", stats: dict[str, Any] | None = None) -> dict[str, Any]:
    st = {"last_run": time.time(), "ok": ok, "message": message, "error": error, "stats": stats or {}, "trigger": trigger}
    await _save_json(STATE_KEY, st)
    if error:
        logger.warning("Автозагрузка из Яндекс Форм: %s", error)
    elif message:
        logger.info("Автозагрузка из Яндекс Форм: %s", message)
    return st


async def run_once(trigger: str = "auto") -> dict[str, Any]:
    """Одна выгрузка: таблица ответов → отчёты → фото. Не бросает исключений — результат (в т.ч. причина сбоя) записывается в состояние."""
    if _LOCK.locked():
        return {"started": False, "reason": "Выгрузка уже идёт"}
    async with _LOCK:
        _RUNNING.update(on=True, step="Проверяю робота…")
        try:
            cfg = await load()
            if not cfg["url"]:
                return await _finish(trigger, False, error="Не задана страница «Ответы» формы")
            try:
                st = await yandex_robot.call("GET", "/status", params={"profile": cfg["profile"]}, timeout=60.0)
            except yandex_robot.RobotUnavailable as exc:
                return await _finish(trigger, False, error=str(exc))
            except yandex_robot.RobotRejected as exc:
                return await _finish(trigger, False, error=exc.detail)
            if not st.get("has_session"):
                return await _finish(trigger, False, error="В браузере робота не выполнен вход в Яндекс — «Настройки → Яндекс Формы: вход и автозагрузка → Войти в Яндекс»")
            _RUNNING["step"] = "Выгружаю таблицу ответов из Яндекс Форм…"
            try:
                resp = await yandex_robot.call("POST", "/forms/export", json={"url": cfg["url"], "profile": cfg["profile"]}, timeout=660.0, raw=True)
            except yandex_robot.RobotUnavailable as exc:
                return await _finish(trigger, False, error=str(exc))
            except yandex_robot.RobotRejected as exc:
                return await _finish(trigger, False, error=exc.detail)
            _RUNNING["step"] = "Разбираю таблицу…"
            parsed = fi.parse(resp.content)
            if not parsed.rows:
                return await _finish(trigger, False, error=parsed.errors[0] if parsed.errors else "В выгруженной таблице нет строк с отчётами")
            plan = await fi.build_plan(parsed)
            batch = secrets.token_hex(6)
            _RUNNING["step"] = "Создаю отчёты…"
            result = await fi.apply_plan(parsed, plan, apply_status=cfg["apply_status"], only=None, batch=batch, user=AUTHOR)
            summary = fi.summarize(plan)
            # фото: у строк пачки с ссылками (новые) и у ранее не скачавшихся, но не старше срока хранения файлов на стороне Яндекса
            rows = await fi.list_rows(batch)
            limit = (dt.datetime.now() - dt.timedelta(days=RETRY_PHOTO_DAYS)).isoformat()
            photo_ids = [r["id"] for r in rows if r["photos_total"] and r["task_id"] and (r["form_time"] or "") >= limit]
            job = await fi.start_download(photo_ids) if photo_ids else {"started": False}
            forms_fault.kick()                  # неисправные отчёты из этой загрузки: ИИ разберёт «итог работ» (если включено в настройках)
            stats = {
                "rows": len(parsed.rows), "created": result["created"], "created_free": result["created_free"],
                "already": summary.get("exists", 0) + summary.get("skip", 0), "no_point": summary.get("no_point", 0), "ambiguous": summary.get("ambiguous", 0),
                "photo_rows": len(photo_ids), "photos_started": bool(job.get("started")), "photos_total": job.get("total", 0),
            }
            made = result["created"] + result["created_free"]
            message = (f"Строк в таблице: {len(parsed.rows)}; новых отчётов: {made}" + (f" (без задания: {result['created_free']})" if result["created_free"] else "")
                       + (f"; фото на скачивании: {job.get('total', 0)}" if job.get("started") else ""))
            return await _finish(trigger, True, message=message, stats=stats)
        except Exception as exc:  # noqa: BLE001 — любая неожиданность записывается, а не роняет фоновый цикл
            logger.exception("Автозагрузка из Яндекс Форм: сбой")
            return await _finish(trigger, False, error=f"Сбой автозагрузки: {type(exc).__name__}: {exc}"[:300])
        finally:
            _RUNNING.update(on=False, step="")


def start_now() -> bool:
    """Запустить выгрузку в фоне (кнопка «Загрузить сейчас»)."""
    if _LOCK.locked() or _RUNNING["on"]:
        return False
    _RUNNING.update(on=True, step="Запускаю…")           # сразу видно в окне: «идёт», ещё до того, как задача начала работу
    asyncio.get_running_loop().create_task(run_once("manual"))
    return True


def is_due(cfg: dict[str, Any], last_run: float, now: float) -> bool:
    """Пора ли выгружать: включено, страница задана и с прошлого запуска (удачного или нет) прошло N минут."""
    return bool(cfg["enabled"] and cfg["url"] and now - float(last_run or 0) >= cfg["interval_minutes"] * 60)


async def loop() -> None:
    """Раз в минуту смотрит, не пора ли: включено, страница задана, с прошлого запуска прошло N минут (неудача тоже считается запуском —
    повторять каждую минуту незачем)."""
    await asyncio.sleep(90)                           # дать приложению и роботу запуститься
    while True:
        try:
            cfg = await load()
            if cfg["enabled"] and cfg["url"]:
                st = await _load_json(STATE_KEY)
                if is_due(cfg, st.get("last_run") or 0, time.time()):
                    await run_once("auto")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Цикл автозагрузки из Яндекс Форм")
        await asyncio.sleep(60)


async def pick_diag_url() -> str:
    """Ссылка на фото для проверки: первая, которая не скачалась (или любая из сохранённых строк)."""
    rows = await fi.list_rows(None, limit=500)
    for want in ("error", "partial", ""):
        for r in rows:
            for it in r["photo_urls"]:
                if it.get("url") and (not want or it.get("state") == want or (want == "error" and r["photos_state"] == "error")):
                    return it["url"]
    return ""


async def diag(url: str = "") -> dict[str, Any]:
    """«Почему не скачивается»: что отвечает Яндекс и что видит браузер робота (под тем входом, что выбран в настройках)."""
    cfg = await load()
    url = (url or "").strip() or await pick_diag_url()
    if not url:
        raise ValueError("Нет ни одной ссылки на фото для проверки — загрузите таблицу формы или вставьте ссылку на фото")
    out = await yandex_robot.call("POST", "/download/diag", json={"url": url, "profile": cfg["profile"]}, timeout=200.0)
    out["checked_url"] = url
    return out
