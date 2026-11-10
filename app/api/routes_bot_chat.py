"""Чат монтёра с диспетчером — сторона бота (тот же server-to-server ключ, что у остальных /api/bot/*).
Бот кладёт сюда сообщения монтёра и забирает ответы диспетчера, чтобы переслать их в VK. История чата в боте не
хранится и не показывается — только в веб-панели."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel

from app.core.deps import require_bot_key
from app.repositories import chat_repository, people_repository
from app.services import chat_files

router = APIRouter(prefix="/api/bot/chat", tags=["bot-chat"], dependencies=[Depends(require_bot_key)])


class ChatIn(BaseModel):
    vk_id: int
    text: str
    task_id: Optional[str] = None
    point_id: Optional[str] = None


@router.post("/send")
async def send(payload: ChatIn):
    person = await people_repository.get_by_vk_id(payload.vk_id)
    if not person or not person["active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найден или неактивен")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустое сообщение")
    msg = await chat_repository.add_incoming(person["id"], text, payload.point_id, payload.task_id)
    return {"id": msg["id"]}


@router.post("/send-photos")
async def send_photos(
    vk_id: int = Form(...), text: str = Form(""), task_id: Optional[str] = Form(None), point_id: Optional[str] = Form(None),
    files: list[UploadFile] = File(default=[]),
):
    """Сообщение монтёра с фото (до 10) и подписью — одно сообщение в чате диспетчера."""
    person = await people_repository.get_by_vk_id(vk_id)
    if not person or not person["active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найден или неактивен")
    try:
        checked = chat_files.check([(f.filename or "фото", await f.read()) for f in files])
    except chat_files.ChatFileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not text.strip() and not checked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Пустое сообщение")
    try:
        msg = await chat_repository.add_incoming(person["id"], text, point_id, task_id, checked)
    except OSError:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, "Не удалось сохранить фото: на сервере нет места или диск недоступен")
    return {"id": msg["id"], "photos": len(msg["attachments"])}


@router.get("/files/{att_id}")
async def get_file(att_id: int):
    """Фото из ответа диспетчера — бот забирает файл и пересылает его монтёру в VK."""
    att = await chat_repository.get_attachment(att_id)
    if not att:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Фото не найдено")
    try:
        content = await chat_files.read(att["file_id"], att["ext"])
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл фото уже удалён")
    return Response(content, media_type=chat_files.MEDIA_TYPES.get(att["ext"], "application/octet-stream"))


@router.post("/claim")
async def claim():
    """Ответы диспетчера, которые ещё не ушли монтёрам (до 20 за раз)."""
    return await chat_repository.claim_pending(20)


class IdsIn(BaseModel):
    ids: list[int] = []


@router.post("/delivered")
async def delivered(payload: IdsIn):
    await chat_repository.mark_delivered(payload.ids)
    return {"ok": True}


class FailedIn(BaseModel):
    id: int
    error: str = ""


@router.post("/failed")
async def failed(payload: FailedIn):
    await chat_repository.report_failure(payload.id, payload.error)
    return {"ok": True}


@router.post("/release")
async def release(payload: IdsIn):
    await chat_repository.release(payload.ids)
    return {"ok": True}
