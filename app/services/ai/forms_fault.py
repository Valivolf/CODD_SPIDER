"""ИИ разбирает отчёты из Яндекс Форм, где неисправность устранить не удалось: из текста «итог работ» достаёт, В ЧЁМ ПРОБЛЕМА и ЧТО НУЖНО ДЛЯ ВОССТАНОВЛЕНИЯ,
и записывает это в отчёт и в карточку комплекса («Неисправность / описание» → «Почему не работает» и «Что нужно»).

Откуда берутся отчёты: монтёр в форме пишет итог одним текстом («Не восстановлен. Нет питания, сгорел автомат, нужен новый автомат 16 А и электрик»). При загрузке таблицы
(app/services/forms_import.py) такой отчёт получает итог «Не работает», а весь текст попадает в поле «что нужно» (причина пустая). Здесь этот текст раскладывается:
  cause — что неисправно / почему комплекс не работает (то, что названо в тексте);
  needs — что нужно для восстановления (запчасти, оборудование, автовышка, доступ…).
Модель только раскладывает написанное и ничего не придумывает; результат проверяется кодом (`grounded`): числа и значимые слова ответа должны быть в исходном тексте, иначе ответ
отбрасывается и в отчёте остаётся прежний текст. Запись в карточку комплекса идёт по обычному правилу app/core/fault.py: отчёт, который СТАРШЕ уже записанной неисправности
(свежий отчёт, ручная правка), карточку не перебивает. Что обработано, пишется в report_ai_fault (повторно один и тот же отчёт не разбирается); отчёты, где поля «что неисправно» /
«что нужно» уже правили руками, пропускаются.

Работает в фоне (`loop`): раз в минуту и сразу после загрузки таблицы (`kick`), запросы к модели — с самым низким приоритетом (голос монтёра и вопросы помощнику идут вперёд).
Включается в «Настройки → ИИ» (флаг forms_fault); кнопка «Разобрать ИИ сейчас» на странице загрузки форм работает и при выключенном флаге (это явное действие человека)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any, Optional

from app.db.database import get_connection
from app.repositories import points_repository, task_visit_reports_repository
from app.services.ai import client
from app.services.ai import settings as ai_settings

logger = logging.getLogger(__name__)

CAUSE_MAX = 400
NEEDS_MAX = 600
RETRY_AFTER_HOURS = 6
MAX_ATTEMPTS = 3
PAUSE_BETWEEN = 1.0            # между отчётами — чтобы не занимать модель подряд, когда ей нужно отвечать людям
LOOP_EVERY = 60.0

SCHEMA = {
    "type": "object",
    "properties": {"cause": {"type": "string"}, "needs": {"type": "string"}},
    "required": ["cause", "needs"], "additionalProperties": False,
}

SYSTEM = """Ты помощник диспетчера. Монтёр написал в форме итог работ на комплексе видеофиксации (АПК): неисправность устранить НЕ удалось. Разложи написанное по двум полям:

cause — в чём проблема: что неисправно или почему комплекс не работает, как названо в тексте (например: «Нет питания: сгорел автомат на опоре», «Обрыв кабеля до камеры», «Роутер не включается»). Кратко, одно-два предложения. Если причина в тексте не названа — пустая строка.
needs — что нужно для восстановления: запчасти, оборудование, инструмент, автовышка вместо лестницы, доступ, согласование, помощь специалиста (например: «Новый автомат 16 А, электрик», «Автовышка — на лестницу не подняться»). Перечислением, кратко. Если в тексте не названо, что нужно, — пустая строка.

Правила: используй только то, что написано в тексте; ничего не придумывай и не добавляй от себя. Слова «нужно / нужна / требуется / необходимо / надо / не хватило / ждём» описывают то, что ещё НУЖНО, — это только в needs. Уже сделанные действия («проверили», «заменили», «перезагрузили») не пиши ни в cause, ни в needs. Номера, модели и числа переписывай точно. Слова АПК/комплекс — комплекс видеофиксации; СЭС — солнечная электростанция; БП — блок питания; пинг — проверка связи; автовышка — подъёмник вместо лестницы."""

EXAMPLES = (
    ("Тип выезда: Ремонт.\nСтатус в форме: Не восстановлен.\nИтог работ: «Нет питания на шкафу, на опоре сгорел автомат. Нужен новый автомат 16А и электрик»",
     {"cause": "Нет питания: на опоре сгорел автомат.", "needs": "Новый автомат 16 А, электрик."}),
    ("Тип выезда: Ремонт.\nСтатус в форме: Не восстановлен.\nИтог работ: «Камера не выходит на связь, роутер не включается, требуется замена роутера»",
     {"cause": "Камера не выходит на связь, роутер не включается.", "needs": "Замена роутера."}),
    ("Тип выезда: Ремонт.\nСтатус в форме: Не восстановлен.\nИтог работ: «На лестницу не подняться, нужна автовышка»",
     {"cause": "", "needs": "Автовышка — на лестницу не подняться."}),
    ("Тип выезда: Ремонт.\nСтатус в форме: Не восстановлен.\nИтог работ: «Проверили, без изменений»",
     {"cause": "", "needs": ""}),
)

_state: dict[str, Any] = {"running": False, "last_error": "", "last_run": "", "last_done": 0}
_wake = asyncio.Event()
_run_lock = asyncio.Lock()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я]+", (text or "").casefold().replace("ё", "е"))


def _stems(text: str) -> set[str]:
    """Значимые слова (от 5 букв) в виде первых пяти букв — «замены» и «замена» совпадают."""
    return {w[:5] for w in _words(text) if len(w) >= 5}


def grounded(answer: str, source: str) -> bool:
    """Ответ модели опирается на исходный текст: каждое число из ответа есть в тексте («16 А» ↔ «16А»), и не меньше половины значимых слов ответа встречаются в тексте."""
    if not answer.strip():
        return True
    if not set(re.findall(r"\d+", answer)) <= set(re.findall(r"\d+", source)):
        return False
    words = _stems(answer)
    if not words:
        return True
    return len(words & _stems(source)) * 2 >= len(words)


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if text.lower().strip(" .,;") in ("", "нет", "не указано", "не названо", "none", "null", "-", "—", "неизвестно", "не требуется", "ничего", "не названа", "не указана",
                                      "не удалось", "не получилось", "не помогло", "без изменений", "не восстановлен", "не работает", "не устранено", "неисправность не устранена"):
        return ""
    return text[:limit]


def _history() -> list[dict]:
    import json  # noqa: PLC0415

    history: list[dict] = []
    for question, answer in EXAMPLES:
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)})
    return history


async def analyze_text(text: str, *, visit_type: str = "", status_text: str = "", priority: int = client.PRIORITY_BACKGROUND) -> dict[str, Any]:
    """Текст итога → {"cause", "needs", "accepted": bool, "reason"}. Ошибки доступа к модели (AiUnavailable/AiError) пробрасываются — решает вызывающий."""
    source = " ".join((text or "").split())
    user = f"Тип выезда: {visit_type or 'не указан'}.\nСтатус в форме: {status_text or 'не указан'}.\nИтог работ: «{source}»"
    data = await client.llm_json(SYSTEM, user, SCHEMA, schema_name="forms_fault", max_tokens=350, priority=priority, history=_history())
    cause, needs = _clean(data.get("cause"), CAUSE_MAX), _clean(data.get("needs"), NEEDS_MAX)
    if not cause and not needs:
        return {"cause": "", "needs": "", "accepted": False, "reason": "empty"}
    if not (grounded(cause, source) and grounded(needs, source)):
        return {"cause": cause, "needs": needs, "accepted": False, "reason": "rejected"}
    return {"cause": cause, "needs": needs, "accepted": True, "reason": "ok"}


# ---------------------------------------------------------------- очередь отчётов

def _same(a: str, b: str) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


async def _candidates(limit: int, again: bool) -> list[dict[str, Any]]:
    """Отчёты из форм с итогом «Не работает», ещё не разобранные: новые сверху. again — заново взять и отклонённые/сбойные."""
    retry_before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=RETRY_AFTER_HOURS)).isoformat()
    redo = "(a.status in ('rejected', 'error') and a.attempts < 99)" if again else f"(a.status = 'error' and a.attempts < {MAX_ATTEMPTS} and a.analyzed_at < ?)"
    sql = ("select r.task_id, r.point_id, r.visit_type, r.created_at, r.created_by, coalesce(r.fault, ''), coalesce(r.needs, ''), coalesce(a.attempts, 0) "
           "from task_visit_reports r left join report_ai_fault a on a.task_id = r.task_id "
           f"where r.source = 'forms' and r.result_status = 'Не работает' and (a.task_id is null or {redo}) "
           "order by r.created_at desc limit ?")
    params: list[Any] = [] if again else [retry_before]
    params.append(limit)
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        out = []
        for task_id, point_id, vtype, created_at, created_by, fault, needs, attempts in rows:
            cur = await conn.execute("select result_text, status_text from forms_import_rows where task_id = ? order by id", (task_id,))
            forms_rows = await cur.fetchall()
            out.append({"task_id": task_id, "point_id": point_id, "visit_type": vtype or "", "created_at": created_at, "created_by": created_by or "",
                        "fault": fault, "needs": needs, "attempts": attempts, "forms": forms_rows})
    return out


async def _record(task_id: str, point_id: str, status: str, cause: str = "", needs: str = "", source: str = "", error: str = "", attempts: int = 0) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "insert into report_ai_fault (task_id, point_id, analyzed_at, status, cause, needs, source_text, error, attempts) values (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "on conflict(task_id) do update set analyzed_at = excluded.analyzed_at, status = excluded.status, cause = excluded.cause, needs = excluded.needs, "
            "source_text = excluded.source_text, error = excluded.error, attempts = excluded.attempts",
            (task_id, point_id, now, status, cause, needs, source[:2000], error[:300], attempts))
        await conn.commit()


async def _process(c: dict[str, Any]) -> str:
    """Один отчёт. Возвращает итог: ok / empty / rejected / skipped / error; AiUnavailable пробрасывается (модель недоступна — остановить проход)."""
    task_id, point_id = c["task_id"], c["point_id"]
    if c["fault"].strip():
        await _record(task_id, point_id, "skipped", error="причина уже записана (вручную или ботом)")
        return "skipped"
    # исходный текст — то, что написал монтёр в форме; если «что нужно» в отчёте уже поправили руками (оно не равно тексту формы) — не трогаем
    raw = next(((t or "").strip() for t, _s in c["forms"] if (t or "").strip() and (not c["needs"].strip() or _same(t, c["needs"]))), "")
    status_text = next(((s or "").strip() for t, s in c["forms"] if (t or "").strip() == raw), "")
    if not raw:
        await _record(task_id, point_id, "skipped", error="нет текста формы или «что нужно» правили вручную")
        return "skipped"
    try:
        res = await analyze_text(raw, visit_type=c["visit_type"], status_text=status_text)
    except client.AiUnavailable:
        raise
    except client.AiError as exc:
        await _record(task_id, point_id, "error", source=raw, error=str(exc), attempts=c["attempts"] + 1)
        return "error"
    if not res["accepted"]:
        await _record(task_id, point_id, res["reason"], res["cause"], res["needs"], raw)
        return res["reason"]
    await task_visit_reports_repository.set_ai_fault(task_id, res["cause"], res["needs"], by=task_visit_reports_repository.strip_vk(c["created_by"]))
    await _record(task_id, point_id, "ok", res["cause"], res["needs"], raw)
    return "ok"


async def run_pass(*, limit: int = 20, again: bool = False, only_enabled: bool = True) -> dict[str, Any]:
    """Один проход по очереди: до `limit` отчётов. Возвращает счётчики по итогам и причину остановки (если модель недоступна)."""
    result: dict[str, Any] = {"ok": 0, "empty": 0, "rejected": 0, "skipped": 0, "error": 0, "stopped": ""}
    if only_enabled and not (await ai_settings.load()).get("forms_fault"):
        result["stopped"] = "disabled"
        return result
    if _run_lock.locked():
        result["stopped"] = "busy"
        return result
    async with _run_lock:
        _state["running"] = True
        try:
            for c in await _candidates(limit, again):
                try:
                    outcome = await _process(c)
                except client.AiUnavailable as exc:
                    result["stopped"] = str(exc)
                    _state["last_error"] = str(exc)
                    break
                result[outcome] = result.get(outcome, 0) + 1
                await asyncio.sleep(PAUSE_BETWEEN)
            else:
                _state["last_error"] = ""
        finally:
            _state["running"] = False
            _state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _state["last_done"] = result["ok"]
    return result


_tasks: set = set()


def start_manual(again: bool = False) -> bool:
    """Кнопка «Разобрать сейчас»: проход в фоне (включён ли режим в настройках — не важно: это явное действие). False — разбор уже идёт."""
    if _run_lock.locked():
        return False
    task = asyncio.get_running_loop().create_task(run_pass(limit=500, again=again, only_enabled=False))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def kick() -> None:
    """После загрузки таблицы форм: не ждать минуту — разобрать новые отчёты сейчас."""
    _wake.set()


async def loop() -> None:
    """Фон: раз в минуту (и по `kick`) разбирает накопившиеся отчёты, пока включено в настройках и модель отвечает."""
    await asyncio.sleep(40)
    while True:
        try:
            _wake.clear()
            if (await ai_settings.load()).get("forms_fault"):
                while True:
                    res = await run_pass(limit=10)
                    if res["stopped"] or not (res["ok"] + res["empty"] + res["rejected"] + res["skipped"] + res["error"]):
                        break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Разбор неисправностей по отчётам форм не удался")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=LOOP_EVERY)
        except asyncio.TimeoutError:
            pass


async def status() -> dict[str, Any]:
    """Для страницы загрузки форм: сколько ждёт разбора, сколько разобрано и чем закончилось."""
    async with get_connection() as conn:
        cur = await conn.execute("select status, count(*) from report_ai_fault group by status")
        done = {s: n for s, n in await cur.fetchall()}
        cur = await conn.execute(
            "select count(*) from task_visit_reports r left join report_ai_fault a on a.task_id = r.task_id "
            "where r.source = 'forms' and r.result_status = 'Не работает' and a.task_id is null")
        (pending,) = await cur.fetchone()
    cfg = await ai_settings.load()
    return {"enabled": bool(cfg.get("forms_fault")), "pending": pending, "ok": done.get("ok", 0), "empty": done.get("empty", 0), "rejected": done.get("rejected", 0),
            "skipped": done.get("skipped", 0), "error": done.get("error", 0), "running": _state["running"], "last_error": _state["last_error"], "last_run": _state["last_run"]}
