"""Готовые разборы вопросов помощника. Долго не расчёт данных, а «понять вопрос»: модель на процессоре выдаёт описание запроса по несколько
секунд на каждое слово. Поэтому:
  • самые частые вопросы (кнопки-примеры) разобраны заранее (PRESETS в qa.py) — ответ мгновенный, без модели;
  • каждый удачно разобранный вопрос запоминается: тот же вопрос во второй раз (в той же формулировке, с точностью до регистра и знаков
    препинания) модель не трогает — берётся запомненное описание запроса, а данные считаются заново (ответ всегда свежий).
Запомненное описание не зависит от роли (права проверяются при выполнении). Если описание привязано к дате (период «за сентябрь» модель могла
превратить в 2026-09, «вчера» — в конкретное число), оно действует только в день, когда запомнено. Подсказка модели поменялась — старые разборы
не используются (версия в ключе)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Optional

from app.db.database import get_connection

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
_DATE_RE = re.compile(r"\d{4}-\d{2}")
_NORM_RE = re.compile(r"[^\w\s+\-.]", re.UNICODE)


def normalize(question: str) -> str:
    """Вопрос для сравнения: без регистра, «ё» = «е», без знаков препинания и лишних пробелов."""
    q = _NORM_RE.sub(" ", (question or "").lower().replace("ё", "е"))
    return " ".join(q.replace(" .", " ").split()).strip(" .")


def is_followup(question: str) -> bool:
    """Продолжение прежнего разговора («а за прошлый месяц?», «и по бригадам»): такой вопрос без предыдущего не понять — ни запоминать его
    разбор, ни брать готовый нельзя."""
    words = normalize(question).split()
    if len(words) < 3:
        return True
    if words[0] in {"у", "из", "по", "в", "на"}:              # «у них», «из этих», «по ним» — продолжение; «у каких бригад…», «по каким комплексам…» — самостоятельный вопрос
        return words[1] in {"них", "этих", "него", "нее", "этой", "этого", "ним", "ней", "тех", "того"}
    return words[0] in {"а", "и", "но", "теперь", "тогда", "также", "тоже", "еще", "только", "то", "так", "ну", "потом", "ладно"}


def date_dependent(spec: dict[str, Any]) -> bool:
    period = spec.get("period") or {}
    if period.get("from") or period.get("to"):
        return True
    for flt in spec.get("filters") or []:
        values = [str(flt.get("value") or ""), *[str(v) for v in flt.get("values") or []]]
        if any(_DATE_RE.search(v) for v in values):
            return True
    return False


def _key(version: str, question: str) -> str:
    return f"{version}|{normalize(question)}"


async def lookup(question: str, version: str, today: dt.date) -> Optional[dict[str, Any]]:
    if not normalize(question):
        return None
    async with get_connection() as conn:
        cur = await conn.execute("select spec, created_at from ai_plan_cache where key = ?", (_key(version, question),))
        row = await cur.fetchone()
        if not row:
            return None
        try:
            spec = json.loads(row[0])
        except ValueError:
            return None
        if date_dependent(spec) and row[1][:10] != today.isoformat():
            return None                         # привязано к дате другого дня — разбираем заново (новое запомнится поверх)
        await conn.execute("update ai_plan_cache set hits = hits + 1, last_used = ? where key = ?", (dt.datetime.now(dt.timezone.utc).isoformat(), _key(version, question)))
        await conn.commit()
    return spec


async def store(question: str, spec: dict[str, Any], version: str) -> None:
    if not normalize(question):
        return
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "insert into ai_plan_cache (key, spec, created_at, last_used, hits) values (?, ?, ?, ?, 0) "
            "on conflict(key) do update set spec = excluded.spec, created_at = excluded.created_at, last_used = excluded.last_used",
            (_key(version, question), json.dumps(spec, ensure_ascii=False), now, now),
        )
        await conn.commit()


async def purge_old(days: int = KEEP_DAYS) -> int:
    edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute("delete from ai_plan_cache where last_used < ?", (edge,))
        await conn.commit()
        return cur.rowcount


async def stats() -> dict[str, int]:
    async with get_connection() as conn:
        cur = await conn.execute("select count(*), coalesce(sum(hits), 0) from ai_plan_cache")
        n, hits = await cur.fetchone()
    return {"plans": n, "hits": hits}
