"""Фоновые задачи ИИ. Запрос к модели на процессоре занимает десятки секунд и больше, поэтому веб-запрос только ставит задачу и возвращает
её номер, а страница/бот опрашивают результат. Задача — строка в ai_jobs (статус, запрос, результат, ошибка); файлы шаблонов и результатов —
в data_dir/ai_files (ai_files). Запрос и результат лежат в базе зашифрованными (в таблицах бывают пароли комплексов).

Перезапуск приложения задачу не теряет: вопросы и шаблоны, которые не успели закончиться, запускаются заново (не больше трёх попыток подряд;
если модель ещё загружается — ждут её), а ответ записывается в переписку (chat.py) самим сервером — человек увидит его, когда вернётся.
Задачи без возможности повторить (голос монтёра: запись только в памяти) закрываются ошибкой. Всё старше трёх дней удаляется."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.config import get_settings
from app.core import field_crypto as fc
from app.db.database import get_connection
from app.services.ai import chat, client, progress

logger = logging.getLogger(__name__)

RETENTION_DAYS = 3
MAX_ACTIVE_PER_OWNER = 4
MAX_ATTEMPTS = 3
MODEL_WAIT_SECONDS = 900              # сколько ждать загрузки модели перед запуском задачи (после перезапуска контейнера модель грузится минутами)
_tasks: set[asyncio.Task] = set()
# kind → функция (запрос задачи, владелец) → «выполнялка» (async () -> dict): так задачу можно запустить заново после перезапуска сервера
RESUMERS: dict[str, Callable[[dict[str, Any], str], Callable[[], Awaitable[dict]]]] = {}
F_REQUEST, F_RESULT = "ai_jobs.request", "ai_jobs.result"


def register_resumer(kind: str, factory: Callable[[dict[str, Any], str], Callable[[], Awaitable[dict]]]) -> None:
    RESUMERS[kind] = factory


class JobError(Exception):
    """Ошибка, текст которой можно показать человеку как есть."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def files_dir() -> Path:
    return get_settings().data_dir / "ai_files"


async def create(kind: str, owner: str, request: dict) -> str:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from ai_jobs where owner = ? and status in ('queued', 'running')", (owner,))
        (active,) = await cur.fetchone()
        if active >= MAX_ACTIVE_PER_OWNER:
            raise JobError("У вас уже выполняется несколько запросов — дождитесь их окончания.")
        job_id = secrets.token_hex(10)
        await conn.execute(
            "insert into ai_jobs (id, kind, owner, status, created_at, request) values (?, ?, ?, 'queued', ?, ?)",
            (job_id, kind, owner, _now(), fc.encrypt(F_REQUEST, json.dumps(request, ensure_ascii=False))),
        )
        await conn.commit()
    return job_id


def _row(row) -> dict[str, Any]:
    job_id, kind, owner, status, created, started, finished, request, result, error = row
    return {
        "id": job_id, "kind": kind, "owner": owner, "status": status, "created_at": created, "started_at": started, "finished_at": finished,
        "request": json.loads(fc.decrypt(F_REQUEST, request) or "{}"), "result": json.loads(fc.decrypt(F_RESULT, result)) if result else None, "error": error,
    }


async def get(job_id: str, owner: Optional[str] = None) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, kind, owner, status, created_at, started_at, finished_at, request, result, error from ai_jobs where id = ?", (job_id,)
        )
        row = await cur.fetchone()
    if not row:
        return None
    job = _row(row)
    if owner is not None and job["owner"] != owner:
        return None
    return job


def public(job: dict[str, Any]) -> dict[str, Any]:
    """Для ответа клиенту: без запроса (в нём бывают тексты и пути)."""
    out = {k: job[k] for k in ("id", "kind", "status", "created_at", "started_at", "finished_at", "result", "error")}
    out["queue"] = {"waiting": client._llm_lock.waiting, "busy": client._llm_lock.busy}
    return out


def spawn(job_id: str, runner: Callable[[], Awaitable[dict]], *, resumed: bool = False) -> None:
    """Запускает `runner` в фоне; его результат (dict) сохраняется в задаче и (для вопросов помощнику) в переписке. Если модель ещё загружается —
    сначала ждёт её (для повторного запуска после перезапуска — в любом случае, контейнер ИИ мог ещё не подняться)."""
    async def run() -> None:
        progress.CURRENT_JOB.set(job_id)            # ход выполнения (agent.py → progress.report) привязывается к этой задаче
        try:
            await client.wait_llm_ready(MODEL_WAIT_SECONDS, only_if_loading=not resumed)
        except Exception:  # noqa: BLE001 — ожидание необязательно: дальше задача сама получит понятную ошибку
            logger.debug("Ожидание модели не удалось", exc_info=True)
        async with get_connection() as conn:
            await conn.execute("update ai_jobs set status = 'running', started_at = ?, attempts = attempts + 1 where id = ?", (_now(), job_id))
            await conn.commit()
        result: Optional[dict] = None
        try:
            result = await runner()
            status, payload, error = "done", json.dumps(result, ensure_ascii=False), None
        except (JobError, client.AiUnavailable, client.AiError) as exc:
            status, payload, error = "error", None, str(exc)
        except asyncio.CancelledError:
            # Сервер останавливается: задачу НЕ закрываем ошибкой — после запуска её выполнит resume_unfinished (ответ не потеряется).
            raise
        except Exception as exc:  # noqa: BLE001 — непредвиденное: подробности в журнал, человеку — короткий текст
            from app.services.ai.query_engine import QueryError

            if isinstance(exc, QueryError):
                status, payload, error = "error", None, str(exc)
            else:
                logger.exception("Задача ИИ %s завершилась ошибкой", job_id)
                status, payload, error = "error", None, "Не удалось выполнить запрос (подробности — в журнале сервера)"
        async with get_connection() as conn:
            await conn.execute(
                "update ai_jobs set status = ?, finished_at = ?, result = ?, error = ? where id = ?",
                (status, _now(), fc.encrypt(F_RESULT, payload) if payload else None, error, job_id),
            )
            await conn.commit()
        try:
            await chat.finish(job_id, status, result if status == "done" else None, error)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось записать ответ ИИ в переписку (задача %s)", job_id)
        progress.clear(job_id)

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _fail(job_id: str, message: str) -> None:
    async with get_connection() as conn:
        await conn.execute("update ai_jobs set status = 'error', finished_at = ?, error = ? where id = ?", (_now(), message, job_id))
        await conn.commit()
    await chat.finish(job_id, "error", None, message)


async def resume_unfinished() -> dict[str, int]:
    """При запуске сервера: задачи, не успевшие закончиться, запускаются заново, если так можно (вопросы и шаблоны; не больше MAX_ATTEMPTS раз);
    остальные — ошибка с понятным текстом."""
    async with get_connection() as conn:
        cur = await conn.execute("select id, kind, owner, request, attempts from ai_jobs where status in ('queued', 'running') order by created_at")
        rows = await cur.fetchall()
    resumed = failed = 0
    for job_id, kind, owner, request, attempts in rows:
        factory = RESUMERS.get(kind)
        if factory is None or attempts >= MAX_ATTEMPTS:
            reason = "Сервер был перезапущен — повторите запрос" if factory is None else "Запрос не удалось выполнить после нескольких перезапусков сервера — повторите его"
            await _fail(job_id, reason)
            failed += 1
            continue
        try:
            runner = factory(json.loads(fc.decrypt(F_REQUEST, request) or "{}"), owner)
        except Exception:  # noqa: BLE001 — не получилось восстановить запрос
            logger.exception("ИИ: задачу %s восстановить не удалось", job_id)
            await _fail(job_id, "Сервер был перезапущен — повторите запрос")
            failed += 1
            continue
        async with get_connection() as conn:
            await conn.execute("update ai_jobs set status = 'queued' where id = ?", (job_id,))
            await conn.commit()
        spawn(job_id, runner, resumed=True)
        resumed += 1
    return {"resumed": resumed, "failed": failed}


# ---------------------------------------------------------------- файлы

async def save_file(owner: str, kind: str, name: str, content: bytes) -> dict[str, Any]:
    ext = (name.rsplit(".", 1)[-1].lower() if "." in name else "bin")[:8]
    file_id = secrets.token_hex(10)
    target = files_dir() / f"{file_id}.{ext}"

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    await asyncio.to_thread(write)
    async with get_connection() as conn:
        await conn.execute(
            "insert into ai_files (id, owner, kind, name, ext, size, created_at) values (?, ?, ?, ?, ?, ?, ?)",
            (file_id, owner, kind, name[:200], ext, len(content), _now()),
        )
        await conn.commit()
    return {"id": file_id, "name": name, "ext": ext, "size": len(content)}


async def get_file(file_id: str, owner: Optional[str] = None) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select id, owner, kind, name, ext, size, created_at from ai_files where id = ?", (file_id,))
        row = await cur.fetchone()
    if not row or (owner is not None and row[1] != owner):
        return None
    return {"id": row[0], "owner": row[1], "kind": row[2], "name": row[3], "ext": row[4], "size": row[5], "created_at": row[6]}


async def read_file(file_id: str, ext: str) -> bytes:
    return await asyncio.to_thread((files_dir() / f"{file_id}.{ext}").read_bytes)


async def purge_old(days: int = RETENTION_DAYS) -> dict[str, int]:
    edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute("select id, ext from ai_files where created_at < ?", (edge,))
        old_files = [(r[0], r[1]) for r in await cur.fetchall()]
        await conn.execute("delete from ai_files where created_at < ?", (edge,))
        cur = await conn.execute("delete from ai_jobs where created_at < ? and status not in ('queued', 'running')", (edge,))
        jobs = cur.rowcount
        await conn.commit()

    def remove() -> int:
        n = 0
        for file_id, ext in old_files:
            try:
                (files_dir() / f"{file_id}.{ext}").unlink()
                n += 1
            except FileNotFoundError:
                pass
        return n

    return {"jobs": jobs, "files": await asyncio.to_thread(remove)}


async def retention_loop() -> None:
    try:
        res = await resume_unfinished()
        if res["resumed"] or res["failed"]:
            logger.info("ИИ: прерванных перезапуском задач — продолжено %s, закрыто с ошибкой %s", res["resumed"], res["failed"])
    except Exception:  # noqa: BLE001
        logger.exception("ИИ: не удалось продолжить прерванные задачи")
    while True:
        try:
            await purge_old()
            await chat.purge_old()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("ИИ: не удалось удалить старые задачи")
        await asyncio.sleep(3600)
