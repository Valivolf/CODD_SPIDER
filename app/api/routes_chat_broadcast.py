"""Массовая рассылка монтёрам в VK-бота (окно «Чаты» → «📢 Рассылка»): кому (все, по условиям, по выбору), что (текст и фото),
история с итогами доставки. Само сообщение уходит каждому получателю как обычный ответ диспетчера — через бота и очередь чата
(chat_repository.add_broadcast), поэтому повторы, статусы и «бот выключен — ждёт» те же. Нужно право `chat.broadcast`
(оно подразумевает чат: читать и писать)."""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core import permissions as P
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import bot_settings_repository, chat_repository, crew_repository, people_repository
from app.services import chat_files

router = APIRouter(prefix="/api/chat/broadcast", tags=["chat-broadcast"], dependencies=[Depends(require_password_ok)])

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MODES = ("all", "filter", "pick")


def _check(user: UserOut) -> None:
    if not P.has(user.permissions, "chat.broadcast"):
        raise HTTPException(403, "Нет права: массовая рассылка монтёрам")


@router.get("/people")
async def people(date: Optional[str] = None, user: UserOut = Depends(require_password_ok)):
    """Кому можно написать (активные, привязанные к VK) — с ролью, бригадой на дату и отметкой отсутствия: из этого окно собирает
    «всем / по условиям / по выбору». Не привязанных к VK и деактивированных в списке нет — их число в `no_vk`."""
    _check(user)
    day = date if date and _DATE.match(date) else dt.date.today().isoformat()
    everyone = await people_repository.list_people(active_only=True)
    crews = await crew_repository.list_for_date(day)
    absent = await people_repository.list_absent_on(day)
    crew_of: dict[int, list[int]] = {}
    for crew in crews:
        for member in crew["members"]:
            crew_of.setdefault(member["id"], []).append(crew["id"])
    items = [
        {"person_id": p["id"], "full_name": p["full_name"], "role_type": p["role_type"], "phone": p["phone"] or "",
         "crew_ids": crew_of.get(p["id"], []), "absent": p["id"] in absent, "absent_reason": absent.get(p["id"], "")}
        for p in everyone if p.get("vk_id")
    ]
    return {
        "date": day,
        "bot_enabled": await bot_settings_repository.get_enabled(),
        "max_recipients": chat_repository.MAX_BROADCAST_RECIPIENTS,
        "max_text": chat_repository.MAX_TEXT,
        "max_photos": chat_files.MAX_FILES,
        "crews": [{"id": c["id"], "name": c["name"] or "", "size": len(c["members"])} for c in crews],
        "people": items,
        "no_vk": sum(1 for p in everyone if not p.get("vk_id")),
    }


@router.get("")
async def history(user: UserOut = Depends(require_password_ok)):
    _check(user)
    return await chat_repository.broadcasts(30)


@router.post("")
async def send(
    text: str = Form(""), person_ids: str = Form("[]"), mode: str = Form("pick"), filter_desc: str = Form(""), token: str = Form(""),
    files: list[UploadFile] = File(default=[]), user: UserOut = Depends(require_password_ok),
):
    """Отправить рассылку выбранным людям. `person_ids` — JSON-список; `mode` и `filter_desc` — как выбраны получатели (для истории);
    `token` — одноразовый номер окна: повторная отправка с ним ничего не дублирует."""
    _check(user)
    try:
        ids = [int(i) for i in json.loads(person_ids)]
    except (ValueError, TypeError):
        raise HTTPException(400, "Список получателей передан неверно")
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise HTTPException(400, "Не выбрано ни одного получателя")
    if len(ids) > chat_repository.MAX_BROADCAST_RECIPIENTS:
        raise HTTPException(400, f"Не больше {chat_repository.MAX_BROADCAST_RECIPIENTS} получателей в одной рассылке")
    text = text.replace("\r\n", "\n").strip()               # форма из браузера (multipart) переводит строки в CRLF
    if len(text) > chat_repository.MAX_TEXT:
        raise HTTPException(400, f"Сообщение длиннее {chat_repository.MAX_TEXT} символов — сократите")
    try:
        checked = chat_files.check([(f.filename or "фото", await f.read()) for f in files])
    except chat_files.ChatFileError as exc:
        raise HTTPException(400, str(exc))
    if not text and not checked:
        raise HTTPException(400, "Напишите сообщение или приложите фото")
    if not await chat_repository.reachable_ids(ids):
        raise HTTPException(409, "Никому из выбранных написать нельзя: они не привязаны к VK или деактивированы")
    try:
        result = await chat_repository.add_broadcast(
            ids, text, user.username, checked, mode=mode if mode in MODES else "pick", filter_desc=filter_desc.strip(), token=token.strip()[:64] or None,
        )
    except OSError:
        raise HTTPException(507, "Не удалось сохранить фото: на сервере нет места или диск недоступен")
    result["bot_enabled"] = await bot_settings_repository.get_enabled()
    return result


@router.get("/{broadcast_id}")
async def detail(broadcast_id: int, user: UserOut = Depends(require_password_ok)):
    _check(user)
    item = await chat_repository.broadcast_detail(broadcast_id)
    if not item:
        raise HTTPException(404, "Рассылка не найдена — переписка и рассылки хранятся неделю")
    return item


@router.post("/{broadcast_id}/retry")
async def retry(broadcast_id: int, user: UserOut = Depends(require_password_ok)):
    """Повторить тем, кому не доставилось (например, человек разрешил сообщения от бота)."""
    _check(user)
    if not await chat_repository.broadcast_detail(broadcast_id):
        raise HTTPException(404, "Рассылка не найдена — переписка и рассылки хранятся неделю")
    return {"requeued": await chat_repository.retry_broadcast(broadcast_id)}
