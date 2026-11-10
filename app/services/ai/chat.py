"""Переписка с помощником («🤖 Помощник» → «Вопросы по данным») хранится на сервере. Вопрос — это задача (jobs.py), а результат задачи записывается
в сообщение переписки самим сервером, поэтому ответ не теряется, если у человека оборвалась связь, он закрыл страницу или перезагрузил её: при
следующем открытии страница просто загружает переписку и, пока есть неотвеченные вопросы, спрашивает их состояние. С другого компьютера
переписка видна так же.

Текст вопроса и ответ (в таблицах бывают пароли комплексов) лежат в базе зашифрованными (app/core/field_crypto.py). Переписка хранится
30 дней, не больше 200 сообщений на человека."""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Optional

from app.core import field_crypto as fc
from app.db.database import get_connection
from app.services.ai import plan_cache, progress

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
KEEP_PER_OWNER = 200
F_TEXT, F_ANSWER = "ai_chat.text", "ai_chat.answer"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _client_view(answer: Any) -> Any:
    """Ответ для страницы: без `raw_spec` (настоящие значения условий — в базе остаются, клиенту их незачем знать); `refreshable` — ответ можно пересчитать по свежим данным."""
    if not isinstance(answer, dict):
        return answer
    view = {k: v for k, v in answer.items() if k != "raw_spec"}
    view["refreshable"] = bool(answer.get("raw_spec"))
    return view


async def find_by_client_id(owner: str, client_id: str) -> Optional[dict[str, Any]]:
    """Тот же вопрос, присланный повторно (связь оборвалась, а запрос уже дошёл) — не задваивается, а возвращает уже созданную задачу."""
    if not client_id:
        return None
    async with get_connection() as conn:
        cur = await conn.execute("select id, job_id from ai_chat where owner = ? and client_id = ? and role = 'user'", (owner, client_id))
        row = await cur.fetchone()
        if not row:
            return None
        cur = await conn.execute("select id, job_id from ai_chat where owner = ? and role = 'assistant' and id > ? order by id limit 1", (owner, row[0]))
        answer = await cur.fetchone()
    return {"message_id": row[0], "answer_id": answer[0] if answer else None, "job_id": (answer[1] if answer else None) or row[1]}


async def add_question(owner: str, question: str, job_id: str, client_id: str = "") -> dict[str, int]:
    """Вопрос человека и пустой «ожидающий» ответ на него — одной записью, чтобы не остаться с половиной."""
    now = _now()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into ai_chat (owner, role, text, job_id, status, client_id, created_at) values (?, 'user', ?, ?, 'done', ?, ?)",
            (owner, fc.encrypt(F_TEXT, question), job_id, client_id or None, now),
        )
        question_id = cur.lastrowid
        cur = await conn.execute("insert into ai_chat (owner, role, text, job_id, status, created_at) values (?, 'assistant', '', ?, 'pending', ?)", (owner, job_id, now))
        answer_id = cur.lastrowid
        await conn.commit()
    return {"message_id": question_id, "answer_id": answer_id}


async def finish(job_id: str, status: str, result: Optional[dict[str, Any]], error: Optional[str]) -> None:
    """Задача закончилась (ответ или ошибка) — записать в сообщение переписки. Для задач вне переписки (шаблоны, голос) ничего не делает."""
    answer = None
    if status == "done" and isinstance(result, dict):
        answer = fc.encrypt(F_ANSWER, json.dumps(result.get("answer"), ensure_ascii=False))
    async with get_connection() as conn:
        await conn.execute(
            "update ai_chat set status = ?, answer = ?, error = ?, finished_at = ? where job_id = ? and role = 'assistant' and status = 'pending'",
            ("done" if status == "done" else "error", answer, None if status == "done" else (error or "Не удалось получить ответ."), _now(), job_id),
        )
        await conn.commit()


async def list_messages(owner: str, limit: int = 80) -> list[dict[str, Any]]:
    """Последние сообщения по порядку; у неотвеченных — состояние задачи (в очереди / думает, с какого момента)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, role, text, job_id, status, answer, error, created_at, finished_at from ai_chat where owner = ? order by id desc limit ?", (owner, limit)
        )
        rows = list(reversed(await cur.fetchall()))
        pending_ids = [r[3] for r in rows if r[1] == "assistant" and r[4] == "pending" and r[3]]
        jobs: dict[str, tuple] = {}
        if pending_ids:
            marks = ",".join("?" * len(pending_ids))
            cur = await conn.execute(
                f"select j.id, j.status, j.started_at, j.attempts, "
                f"(select count(*) from ai_jobs o where o.status in ('queued', 'running') and o.kind in ('qa', 'doc_plan', 'voice') and o.created_at < j.created_at) "
                f"from ai_jobs j where j.id in ({marks})", pending_ids)
            jobs = {r[0]: r for r in await cur.fetchall()}
    out: list[dict[str, Any]] = []
    for mid, role, text, job_id, status, answer, error, created, finished in rows:
        item: dict[str, Any] = {"id": mid, "role": role, "status": status, "created_at": created, "finished_at": finished}
        if role == "user":
            item["text"] = fc.decrypt(F_TEXT, text)
        else:
            item["job_id"] = job_id
            if answer:
                try:
                    item["answer"] = _client_view(json.loads(fc.decrypt(F_ANSWER, answer)))
                except ValueError:
                    item["error"] = "Не удалось прочитать сохранённый ответ."
            if error:
                item["error"] = error
            if status == "pending":
                job = jobs.get(job_id)
                item["job"] = {"status": job[1] if job else "lost", "started_at": job[2] if job else None, "attempts": job[3] if job else 0,
                               "ahead": job[4] if job else 0, "progress": progress.get(job_id) if job_id else None}
        out.append(item)
    return out


async def history_for(owner: str, count: int = 2) -> list[dict[str, Any]]:
    """Последние разобранные вопросы человека (вопрос + описание запроса) — чтобы работало «а за прошлый месяц?»."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select id, answer from ai_chat where owner = ? and role = 'assistant' and status = 'done' and answer is not null order by id desc limit ?", (owner, count)
        )
        rows = list(reversed(await cur.fetchall()))
        out: list[dict[str, Any]] = []
        for mid, answer in rows:
            try:
                data = json.loads(fc.decrypt(F_ANSWER, answer)) or {}
            except ValueError:
                continue
            spec = data.get("spec")
            cur = await conn.execute("select text from ai_chat where owner = ? and role = 'user' and id < ? order by id desc limit 1", (owner, mid))
            q = await cur.fetchone()
            if q and (spec or data.get("narration")):
                out.append({"question": fc.decrypt(F_TEXT, q[0]), "spec": spec, "answer": str(data.get("narration") or data.get("text") or "")[:300]})
    return out


async def clear(owner: str) -> int:
    async with get_connection() as conn:
        cur = await conn.execute("delete from ai_chat where owner = ?", (owner,))
        await conn.commit()
        return cur.rowcount


async def purge_old(days: int = RETENTION_DAYS) -> int:
    edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute("delete from ai_chat where created_at < ?", (edge,))
        removed = cur.rowcount
        # лишнее сверх лимита на человека — самое старое
        cur = await conn.execute("select owner, count(*) from ai_chat group by owner having count(*) > ?", (KEEP_PER_OWNER,))
        for owner, _ in await cur.fetchall():
            await conn.execute(
                "delete from ai_chat where owner = ? and id not in (select id from ai_chat where owner = ? order by id desc limit ?)", (owner, owner, KEEP_PER_OWNER)
            )
        await conn.commit()
    return removed


async def get_answer(owner: str, message_id: int) -> Optional[dict[str, Any]]:
    """Готовый ответ помощника целиком (с `raw_spec`) — для «Обновить» и «Из чего посчитано». Чужие сообщения не отдаются."""
    async with get_connection() as conn:
        cur = await conn.execute("select answer from ai_chat where id = ? and owner = ? and role = 'assistant' and status = 'done' and answer is not null", (message_id, owner))
        row = await cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(fc.decrypt(F_ANSWER, row[0]))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def save_answer(owner: str, message_id: int, answer: dict[str, Any]) -> None:
    async with get_connection() as conn:
        await conn.execute("update ai_chat set answer = ? where id = ? and owner = ? and role = 'assistant'",
                           (fc.encrypt(F_ANSWER, json.dumps(answer, ensure_ascii=False)), message_id, owner))
        await conn.commit()


async def previous_answer(owner: str, question: str, limit: int = 60) -> Optional[dict[str, Any]]:
    """Самый свежий прежний ответ ЭТОГО человека на тот же вопрос (с точностью до регистра и знаков): {"answer", "at"} или None. Нужен, чтобы при повторном вопросе
    показать, что в данных изменилось с прошлого раза («тогда было 4, сейчас 5»)."""
    wanted = plan_cache.normalize(question)
    if not wanted:
        return None
    async with get_connection() as conn:
        cur = await conn.execute(
            "select u.text, a.answer, a.finished_at from ai_chat u join ai_chat a on a.owner = u.owner and a.role = 'assistant' and a.job_id = u.job_id "
            "where u.owner = ? and u.role = 'user' and a.status = 'done' and a.answer is not null order by u.id desc limit ?", (owner, limit))
        rows = await cur.fetchall()
    for text, answer, finished in rows:
        if plan_cache.normalize(fc.decrypt(F_TEXT, text)) != wanted:
            continue
        try:
            data = json.loads(fc.decrypt(F_ANSWER, answer))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("kind") in ("number", "table", "empty"):
            return {"answer": data, "at": data.get("computed_at") or finished}
    return None
