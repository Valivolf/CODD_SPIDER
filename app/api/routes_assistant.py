"""Помощник в веб-панели («🤖 Помощник»): вопросы по данным на обычном языке и заполнение шаблонов Excel/Word. Доступен всем ролям, но
данные отдаются строго по правам роли (app/services/ai/catalog.py), а запросы идут задачами (минутами, не секундами — модель на процессоре).
Помощник данные приложения не меняет: пишет только свои задачи, переписку и файлы-результаты (поэтому «только просмотр» им пользоваться может).

Переписка хранится на сервере (app/services/ai/chat.py): вопрос — задача, ответ записывает сам сервер, поэтому после обрыва связи или закрытой
страницы ответ дожидается человека. Задачи переживают и перезапуск сервера (jobs.resume_unfinished)."""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.services.ai import catalog as cat
import logging

from app.services.ai import chat, client, doc_fill, export_doc, jobs, plan_cache, qa, understand
from app.services.ai import settings as ai_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistant", tags=["assistant"])

MEDIA = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

# Все примеры разобраны заранее (qa.PRESET_PLANS): ответ на них мгновенный, без модели.
EXAMPLES = {
    "dispatch": [
        "Сколько было выездов по инвентаризации за прошлый месяц?",
        "Какие комплексы сейчас не работают?",
        "Покажи выезды за прошлую неделю, где нужна автовышка",
        "Сколько ремонтов у каждой бригады в этом месяце?",
        "Когда последний раз были на комплексе P-3?",
        "Кто сейчас в отпуске?",
        "Какие задания стоят на завтра?",
    ],
    "warehouse": ["Сколько роутеров на складе?", "Какое оборудование сейчас у монтёров?", "Покажи оборудование, которое снято с комплексов и ждёт приёмки"],
    "absences": ["Кто отсутствует на этой неделе?", "Кто сейчас в отпуске?", "Сколько дней отсутствовал каждый человек в этом месяце?"],
}


def _owner(user: UserOut) -> str:
    return f"user:{user.id}"


async def _require_enabled() -> None:
    cfg = await ai_settings.load()
    if not cfg["assistant"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Помощник выключен администратором (Настройки → ИИ).")


def _examples(role: str) -> list[str]:
    """Примеры вопросов — по тому, какие данные доступны роли: календарь и выезды → «диспетчерские»; склад и комплексы → «складские»; только отсутствия → про отсутствия."""
    names = {d.name for d in cat.role_datasets(role)}
    if names & {"tasks", "visits", "crews"}:
        return EXAMPLES["dispatch"]
    if names & {"equipment", "points"}:
        return EXAMPLES["warehouse"]
    if "absences" in names:
        return EXAMPLES["absences"]
    return []


@router.get("/info")
async def info(user: UserOut = Depends(require_password_ok)):
    cfg = await ai_settings.load()
    state = await client.status() if cfg["assistant"] else None
    return {
        "enabled": cfg["assistant"], "ready": bool(state and state["llm"].get("ok")), "role": user.role, "examples": _examples(user.role),
        "datasets": [{"name": d.name, "label": d.label.split(" (")[0]} for d in cat.role_datasets(user.role)],
        "queue": state["queue"] if state else None, "last_seconds": state["last"]["llm_seconds"] if state else None,
        "plans": await plan_cache.stats(),
    }


class AskIn(BaseModel):
    question: str
    history: list[dict] = Field(default_factory=list)      # устарело: предыдущие вопросы сервер берёт из переписки сам
    client_id: str = Field(default="", max_length=64)       # уникальный номер вопроса от страницы: повторная отправка (обрыв связи) не задваивается
    mode: str = Field(default="auto", max_length=10)        # auto — агент для вопросов-объяснений; agent — подробный разбор записей пачками; plain — без агента


def _mode(value: str) -> str:
    return value if value in ("auto", "agent", "plain") else "auto"


async def _attach_previous(owner: str, question: str, answer: dict) -> None:
    """Тот же вопрос задавали раньше — показываем, изменились ли данные: «тогда было 4, сейчас 5» (или «данные те же»). Данные при этом считаются заново, как всегда."""
    if answer.get("kind") not in ("number", "table", "empty") or answer.get("source") == "evidence":
        return
    try:
        prev = await chat.previous_answer(owner, question)
        cmp = qa.compare(prev["answer"], answer) if prev else None
    except Exception:  # noqa: BLE001 — сравнение необязательно
        return
    if prev and cmp:
        answer["previous"] = {"at": prev["at"], **cmp}


async def _attach_file(owner: str, role: str, question: str, answer: dict) -> None:
    """Просили файл («выгрузи в Excel», «справку в Word») — собирает программа из тех же данных (export_doc.py) и кладёт в хранилище файлов помощника; в ответе — `file` для кнопки «Скачать»
    (или `file_error` с причиной, если собрать не из чего). Формат и текст запроса запоминаются в ответе, чтобы «Обновить» собрал свежий файл."""
    fmt = answer.pop("file_request", None) or answer.get("file_format")
    if not fmt or not owner:
        return
    answer["file_format"] = fmt
    answer.setdefault("file_question", understand.strip_file_words(question, deep=False))
    answer.pop("file", None)
    answer.pop("file_error", None)
    try:
        body, name = await export_doc.build(answer, answer["file_question"], role, fmt)
        saved = await jobs.save_file(owner, "output", name, body)
        answer["file"] = {"id": saved["id"], "name": saved["name"], "ext": saved["ext"], "size": saved["size"]}
    except export_doc.ExportError as exc:
        answer["file_error"] = str(exc)
    except Exception:  # noqa: BLE001 — сбой при сборке файла не должен ронять сам ответ
        logger.exception("Не удалось собрать файл по ответу помощника")
        answer["file_error"] = "Файл собрать не удалось — ответ выше при этом верный."


def _qa_runner(question: str, role: str, history: list[dict], owner: str = "", mode: str = "auto"):
    async def runner() -> dict:
        answer = await qa.answer(question, role, history=history, mode=mode)
        if owner:
            await _attach_previous(owner, question, answer)
            await _attach_file(owner, role, question, answer)
        return {"question": question, "answer": answer}

    return runner


jobs.register_resumer("qa", lambda request, owner: _qa_runner(request["question"], request["role"], request.get("history") or [], owner, _mode(request.get("mode", "auto"))))


@router.post("/ask")
async def ask(payload: AskIn, user: UserOut = Depends(require_password_ok)):
    await _require_enabled()
    question = " ".join(payload.question.split())[:600]
    if len(question) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Напишите вопрос.")
    owner = _owner(user)
    client_id = payload.client_id.strip()
    dup = await chat.find_by_client_id(owner, client_id)
    if dup:
        return {"job_id": dup["job_id"], "message_id": dup["message_id"], "answer_id": dup["answer_id"], "duplicate": True}
    history = await chat.history_for(owner, 2)
    try:
        job_id = await jobs.create("qa", owner, {"question": question, "role": user.role, "history": history, "mode": _mode(payload.mode)})
    except jobs.JobError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    try:
        ids = await chat.add_question(owner, question, job_id, client_id)
    except Exception:  # noqa: BLE001 — тот же вопрос пришёл одновременно дважды (повтор после обрыва): оставляем первый
        await jobs._fail(job_id, "Повторный запрос")
        dup = await chat.find_by_client_id(owner, client_id)
        if dup:
            return {"job_id": dup["job_id"], "message_id": dup["message_id"], "answer_id": dup["answer_id"], "duplicate": True}
        raise
    jobs.spawn(job_id, _qa_runner(question, user.role, history, owner, _mode(payload.mode)))
    return {"job_id": job_id, **ids}


class MessageRef(BaseModel):
    message_id: int


def _as_of(answer: dict) -> dt.date:
    try:
        return dt.date.fromisoformat(str(answer.get("as_of") or ""))
    except ValueError:
        return dt.date.today()


@router.post("/refresh")
async def refresh_answer(payload: MessageRef, user: UserOut = Depends(require_password_ok)):
    """«Обновить»: тот же запрос по СВЕЖИМ данным, без модели (мгновенно). Период остаётся прежним (день, на который отвечали). Если данные изменились, ответ в переписке
    обновляется, а в ответе остаётся пометка «было X, стало Y»; у ответа-рассказа (агента) текст не переписывается, только отмечается, что данные изменились."""
    await _require_enabled()
    owner = _owner(user)
    old = await chat.get_answer(owner, payload.message_id)
    if not old or not old.get("raw_spec"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Этот ответ нельзя пересчитать")
    new = await qa.execute(old["raw_spec"], user.role, today=_as_of(old), source=old.get("source") or "")
    if new["kind"] in ("denied", "error"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, new.get("text") or "Не удалось пересчитать")
    now_iso = new["computed_at"]
    if old.get("kind") == "agent":
        seen = {"kind": "table", "rows": (old.get("records") or {}).get("rows") or [], "total": (old.get("agent") or {}).get("rows_total")}
        n_read = len(seen["rows"])
        cmp = qa.compare(seen, {**new, "rows": (new.get("rows") or [])[:n_read]}) if new.get("kind") == "table" else {"changed": True, "was": seen["total"], "now": 0, "added": [], "removed": []}
        old["checked_at"] = now_iso
        old["stale"] = bool(cmp and cmp["changed"])
        old["stale_info"] = cmp if old["stale"] else None
        if old.get("file_format"):                                              # просили файл — собираем заново по свежим данным
            await _attach_file(owner, user.role, old.get("file_question") or "", old)
        await chat.save_answer(owner, payload.message_id, old)
        return {"answer": chat._client_view(old), "change": cmp}               # noqa: SLF001
    cmp = qa.compare(old, new)
    updates = list(old.get("updates") or [])
    if cmp and cmp["changed"]:
        updates = (updates + [{"at": old.get("computed_at"), "was": cmp["was"], "now": cmp["now"], "added": cmp["added"], "removed": cmp["removed"]}])[-5:]
    new["updates"] = updates
    if old.get("previous"):
        new["previous"] = old["previous"]
    if old.get("file_format"):
        new["file_format"], new["file_question"] = old["file_format"], old.get("file_question") or ""
        await _attach_file(owner, user.role, new["file_question"], new)
    await chat.save_answer(owner, payload.message_id, new)
    return {"answer": chat._client_view(new), "change": cmp}                   # noqa: SLF001


@router.post("/evidence")
async def answer_evidence(payload: MessageRef, user: UserOut = Depends(require_password_ok)):
    """«Из чего посчитано»: записи, на которых держится число или итог в ответе (те же условия, без подсчёта) — чтобы цифру можно было сверить глазами."""
    await _require_enabled()
    old = await chat.get_answer(_owner(user), payload.message_id)
    if not old or not old.get("raw_spec"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Для этого ответа записи показать нельзя")
    out = await qa.evidence(old["raw_spec"], user.role, today=_as_of(old))
    if out["kind"] in ("denied", "error"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, out.get("text") or "Не удалось получить записи")
    return {k: out.get(k) for k in ("kind", "text", "columns", "rows", "total", "truncated", "description", "computed_at")}


@router.get("/chat")
async def chat_messages(user: UserOut = Depends(require_password_ok)):
    """Переписка человека (сохранена на сервере). Страница спрашивает её, пока есть вопросы без ответа; `queue`/`last_seconds` — для подписи ожидания."""
    messages = await chat.list_messages(_owner(user))
    return {
        "messages": messages, "pending": any(m["role"] == "assistant" and m["status"] == "pending" for m in messages),
        "server_time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "queue": {"waiting": client._llm_lock.waiting, "busy": client._llm_lock.busy},          # noqa: SLF001 — состояние очереди в памяти, без запросов к модели
        "last_seconds": client._stats["llm_last_seconds"],                                      # noqa: SLF001
    }


@router.delete("/chat")
async def chat_clear(user: UserOut = Depends(require_password_ok)):
    return {"removed": await chat.clear(_owner(user))}


@router.get("/jobs/{job_id}")
async def job(job_id: str, user: UserOut = Depends(require_password_ok)):
    found = await jobs.get(job_id, owner=_owner(user))
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return jobs.public(found)


def qe_parse(value: str):
    from app.services.ai.query_engine import parse_date

    return parse_date(value)


def _doc_plan_runner(owner: str, role: str, file_id: str, name: str, instruction: str, pf: str, pt: str, today: dt.date, analysis=None):
    async def runner() -> dict:
        nonlocal analysis
        if analysis is None:                        # после перезапуска сервера: шаблон читаем с диска заново
            file = await jobs.get_file(file_id, owner=owner)
            if not file:
                raise jobs.JobError("Файл шаблона уже удалён — загрузите его заново.")
            analysis = await doc_fill.analyze(await jobs.read_file(file["id"], file["ext"]), name)
        raw = await doc_fill.make_plan(analysis, instruction, pf, pt, today=today)
        if not raw.get("can_fill"):
            raise jobs.JobError(str(raw.get("reason") or "По шаблону и описанию не удалось понять, чем заполнять — уточните описание."))
        checked = doc_fill.validate_plan(raw, analysis, role)
        plan = checked["plan"]
        if not plan["tables"] and (raw.get("tables") or not plan["placeholders"]):
            # таблицы просили, а заполнять нечем (нет доступа к данным) — молча заполнить одни метки было бы обманом
            raise jobs.JobError("; ".join(checked["warnings"]) or "Не удалось подобрать данные для этого шаблона — уточните описание.")
        period = doc_fill.effective_period(plan, pf or None, pt or None)
        return {"file_id": file_id, "name": name, "kind": analysis.kind, "plan": plan, "period": period,
                "preview": doc_fill.preview_lines(plan, analysis, period, today), "warnings": checked["warnings"]}

    return runner


jobs.register_resumer("doc_plan", lambda r, owner: _doc_plan_runner(owner, r["role"], r["file_id"], r["name"], r["instruction"], r.get("pf", ""), r.get("pt", ""),
                                                                    dt.date.fromisoformat(r["today"])))


@router.post("/files/analyze")
async def analyze_file(
    file: UploadFile = File(...), instruction: str = Form(""), period_from: str = Form(""), period_to: str = Form(""),
    user: UserOut = Depends(require_password_ok),
):
    """Шаг 1: разобрать шаблон и составить план заполнения (модель, минуты). Файл ещё не заполняется — человек видит план и подтверждает."""
    await _require_enabled()
    content = await file.read()
    name = file.filename or "шаблон"
    if not instruction.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Опишите, как заполнить шаблон: что взять, за какой период.")
    try:
        analysis = await doc_fill.analyze(content, name)
    except doc_fill.TemplateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not analysis.tables and not analysis.placeholders:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(analysis.notes) or "В шаблоне нечего заполнять.")
    pf, pt = period_from.strip(), period_to.strip()
    for value in (pf, pt):
        if value and qe_parse(value) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не понял дату «{value}» — укажите в виде ГГГГ-ММ-ДД.")
    saved = await jobs.save_file(_owner(user), "template", name, content)
    today = dt.date.today()
    instruction = instruction.strip()[:800]
    try:
        job_id = await jobs.create("doc_plan", _owner(user), {"file_id": saved["id"], "name": name, "instruction": instruction, "role": user.role,
                                                              "pf": pf, "pt": pt, "today": today.isoformat()})
    except jobs.JobError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    jobs.spawn(job_id, _doc_plan_runner(_owner(user), user.role, saved["id"], name, instruction, pf, pt, today, analysis))
    return {"job_id": job_id}


class FillIn(BaseModel):
    job_id: str
    period_from: str = ""
    period_to: str = ""


def _doc_fill_runner(owner: str, role: str, plan_job_id: str, pf: Optional[str], pt: Optional[str]):
    async def runner() -> dict:
        plan_job = await jobs.get(plan_job_id, owner=owner)
        if not plan_job or plan_job["kind"] != "doc_plan" or plan_job["status"] != "done" or not plan_job["result"]:
            raise jobs.JobError("План заполнения не найден — разберите шаблон заново.")
        plan_result = plan_job["result"]
        template = await jobs.get_file(plan_result["file_id"], owner=owner)
        if not template:
            raise jobs.JobError("Файл шаблона уже удалён — загрузите его заново.")
        content = await jobs.read_file(template["id"], template["ext"])
        data, report, ext = await doc_fill.fill(content, template["name"], plan_result["plan"], role, (pf, pt))
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", template["name"])
        out = await jobs.save_file(owner, "output", f"{stem} (заполнено).{ext}", data)
        return {"file_id": out["id"], "name": out["name"], "tables": report.tables, "placeholders": report.placeholders, "warnings": report.warnings}

    return runner


jobs.register_resumer("doc_fill", lambda r, owner: _doc_fill_runner(owner, r["role"], r["plan_job"], r.get("pf") or None, r.get("pt") or None))


@router.post("/files/fill")
async def fill_file(payload: FillIn, user: UserOut = Depends(require_password_ok)):
    """Шаг 2: заполнить шаблон по готовому плану (быстро — модель не нужна); период можно сменить, не составляя план заново."""
    await _require_enabled()
    plan_job = await jobs.get(payload.job_id, owner=_owner(user))
    if not plan_job or plan_job["kind"] != "doc_plan" or plan_job["status"] != "done" or not plan_job["result"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "План заполнения не найден — разберите шаблон заново.")
    template = await jobs.get_file(plan_job["result"]["file_id"], owner=_owner(user))
    if not template:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл шаблона уже удалён — загрузите его заново.")
    for value in (payload.period_from.strip(), payload.period_to.strip()):
        if value and qe_parse(value) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не понял дату «{value}» — укажите в виде ГГГГ-ММ-ДД.")
    pf = payload.period_from.strip() or None
    pt = payload.period_to.strip() or None
    try:
        job_id = await jobs.create("doc_fill", _owner(user), {"plan_job": payload.job_id, "role": user.role, "pf": pf or "", "pt": pt or ""})
    except jobs.JobError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    jobs.spawn(job_id, _doc_fill_runner(_owner(user), user.role, payload.job_id, pf, pt))
    return {"job_id": job_id}


@router.get("/files/{file_id}")
async def download(file_id: str, user: UserOut = Depends(require_password_ok)):
    file = await jobs.get_file(file_id, owner=_owner(user))
    if not file or file["kind"] != "output":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден или уже удалён")
    try:
        content = await jobs.read_file(file["id"], file["ext"])
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл уже удалён")
    return Response(content, media_type=MEDIA.get(file["ext"], "application/octet-stream"),
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(file['name'])}"})
