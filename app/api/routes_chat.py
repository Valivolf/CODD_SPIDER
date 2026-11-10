"""Чат с монтёрами для веб-панели (окно «Чаты»): список переписок, история, ответ. Сторона бота — /api/bot/chat/*
(routes_bot_chat.py). Роли: диспетчер, админ; «только просмотр» читает, но не пишет (viewer_guard).
В сообщении можно прислать фото (до 10); переписка вместе с фото хранится неделю."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import chat_repository, people_repository
from app.services import chat_files

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_password_ok)])


def _check_role(user: UserOut) -> None:
    if not P.has(user.permissions, "chat.view"):
        raise HTTPException(403, "Нет права: читать чат с монтёрами")


async def _person(person_id: int) -> dict:
    person = await people_repository.get_person(person_id)
    if not person or not person.get("vk_id"):
        raise HTTPException(404, "Человек не найден или не привязан к VK")
    return person


@router.get("/conversations")
async def conversations(q: str = "", all: bool = False, user: UserOut = Depends(require_password_ok)):
    _check_role(user)
    return await chat_repository.conversations(q, include_all=all)


@router.get("/unread")
async def unread(user: UserOut = Depends(require_password_ok)):
    _check_role(user)
    return await chat_repository.unread_summary()


@router.get("/files/{att_id}")
async def get_file(att_id: int, user: UserOut = Depends(require_password_ok)):
    """Фото из переписки (для показа в окне чата)."""
    _check_role(user)
    att = await chat_repository.get_attachment(att_id)
    if not att:
        raise HTTPException(404, "Фото не найдено — переписка хранится неделю")
    try:
        content = await chat_files.read(att["file_id"], att["ext"])
    except FileNotFoundError:
        raise HTTPException(404, "Файл фото уже удалён")
    return Response(content, media_type=chat_files.MEDIA_TYPES.get(att["ext"], "application/octet-stream"),
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/{person_id}/messages")
async def messages(person_id: int, after_id: int = 0, limit: int = Query(300, ge=1, le=1000), user: UserOut = Depends(require_password_ok)):
    _check_role(user)
    await _person(person_id)
    return await chat_repository.messages(person_id, after_id, limit)


@router.post("/{person_id}/read")
async def read(person_id: int, user: UserOut = Depends(require_password_ok)):
    _check_role(user)
    return {"read": await chat_repository.mark_read(person_id)}


class SendIn(BaseModel):
    text: str


@router.post("/{person_id}/send")
async def send(person_id: int, payload: SendIn, user: UserOut = Depends(require_password_ok)):
    _check_role(user)
    person = await _person(person_id)
    if not person.get("active"):
        raise HTTPException(409, "Человек деактивирован — написать ему нельзя")
    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Напишите сообщение")
    msg = await chat_repository.add_outgoing(person_id, text, user.username)
    await chat_repository.mark_read(person_id)     # ответил — значит, прочитал
    return msg


@router.post("/{person_id}/send-photos")
async def send_photos(
    person_id: int, text: str = Form(""), files: list[UploadFile] = File(default=[]), user: UserOut = Depends(require_password_ok),
):
    """Ответ с фото (до 10) и, если нужно, подписью."""
    _check_role(user)
    person = await _person(person_id)
    if not person.get("active"):
        raise HTTPException(409, "Человек деактивирован — написать ему нельзя")
    try:
        checked = chat_files.check([(f.filename or "фото", await f.read()) for f in files])
    except chat_files.ChatFileError as exc:
        raise HTTPException(400, str(exc))
    text = text.strip()
    if not text and not checked:
        raise HTTPException(400, "Напишите сообщение или приложите фото")
    try:
        msg = await chat_repository.add_outgoing(person_id, text, user.username, checked)
    except OSError:
        raise HTTPException(507, "Не удалось сохранить фото: на сервере нет места или диск недоступен")
    await chat_repository.mark_read(person_id)
    return msg
