"""ИИ для бота монтёра (общий ключ бота, как у остальных /api/bot/*): голосовой итог по комплексу → расшифровка → разбор на «что сделано /
причина / что нужно». Расшифровка и модель работают на процессоре минуту и дольше, поэтому запрос ставится задачей, бот опрашивает результат."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.core.deps import require_bot_key
from app.repositories import people_repository
from app.services.ai import client, jobs, report_parser
from app.services.ai import settings as ai_settings

router = APIRouter(prefix="/api/bot/ai", tags=["bot-ai"], dependencies=[Depends(require_bot_key)])

MAX_AUDIO = 25 * 1024 * 1024


@router.get("/config")
async def config():
    """Включён ли ИИ в боте (переключатель «Настройки → ИИ») и отвечают ли сервисы — бот решает, принимать ли голосовые."""
    cfg = await ai_settings.load()
    state = await client.status() if cfg["bot_voice"] else None
    return {"bot_voice": cfg["bot_voice"], "asr_ready": bool(state and state["asr"].get("ok")), "llm_ready": bool(state and state["llm"].get("ok"))}


@router.post("/voice")
async def voice(
    vk_id: int = Form(...), task_id: str = Form(""), point_id: str = Form(""), visit_type: str = Form(""), file: UploadFile = File(...),
):
    cfg = await ai_settings.load()
    if not cfg["bot_voice"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "ИИ в боте выключен администратором")
    person = await people_repository.get_by_vk_id(vk_id)
    if not person or not person["active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найден или неактивен")
    audio = await file.read()
    if not audio:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустая запись")
    if len(audio) > MAX_AUDIO:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Запись слишком большая")
    name = file.filename or "voice.ogg"
    try:
        job_id = await jobs.create("voice", f"bot:{vk_id}", {"task_id": task_id, "point_id": point_id, "visit_type": visit_type, "size": len(audio)})
    except jobs.JobError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))

    async def runner() -> dict:
        transcript = await client.transcribe(audio, name, prompt=report_parser.ASR_PROMPT, priority=client.PRIORITY_BOT)
        text = transcript["text"]
        if not text.strip():
            raise jobs.JobError("Речь не распознана — запись пустая или слишком тихая. Запишите ещё раз ближе к микрофону.")
        parsed = await report_parser.parse(text, visit_type=visit_type, point_id=point_id, priority=client.PRIORITY_BOT)
        return {"transcript": text, "duration": transcript["duration"], **parsed}

    jobs.spawn(job_id, runner)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job(job_id: str, vk_id: int):
    found = await jobs.get(job_id, owner=f"bot:{vk_id}")
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return jobs.public(found)
