"""Личная переписка между учётными записями панели."""
from __future__ import annotations

import asyncio
import datetime as dt
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core import permissions as P
from app.core.deps import require_password_ok
from app.db.database import get_connection
from app.models.user import UserOut
from app.repositories import user_repository

router = APIRouter(prefix="/api/account-chat", tags=["личные сообщения"], dependencies=[Depends(require_password_ok)])
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 10
MAX_TEXT = 4000
ALLOWED = {"pdf", "txt", "csv", "doc", "docx", "xls", "xlsx", "zip", "jpg", "jpeg", "png", "gif", "webp", "mp3", "mp4"}


def _check(user: UserOut, write: bool = False) -> None:
    key = "chat.use" if write else "chat.view"
    if not P.has(user.permissions, key):
        raise HTTPException(403, "Нет права на личные сообщения")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _folder() -> Path:
    return get_settings().data_dir / "account_chat_files"


def _file_path(file_id: int, ext: str) -> Path:
    return _folder() / f"{file_id}.{ext}"


async def _check_peer(peer_id: int, me: UserOut) -> None:
    if peer_id == me.id or not await user_repository.get_user_by_id(peer_id):
        raise HTTPException(404, "Учётная запись не найдена")


def _row(row) -> dict:
    return {"id": row[0], "sender_id": row[1], "recipient_id": row[2], "text": row[3], "reply_to": row[4],
            "created_at": row[5], "edited_at": row[6], "deleted_at": row[7], "sender": row[8], "recipient": row[9], "files": []}


async def _messages(peer_id: int, me: UserOut, after_id: int = 0, limit: int = 500) -> list[dict]:
    async with get_connection() as conn:
        cur = await conn.execute("""select m.id,m.sender_id,m.recipient_id,m.text,m.reply_to,m.created_at,m.edited_at,m.deleted_at,
            s.username,r.username from account_messages m join users s on s.id=m.sender_id join users r on r.id=m.recipient_id
            where ((m.sender_id=? and m.recipient_id=?) or (m.sender_id=? and m.recipient_id=?)) and m.id>?
            order by m.id desc limit ?""", (me.id, peer_id, peer_id, me.id, after_id, limit))
        rows = [_row(r) for r in reversed(await cur.fetchall())]
        if rows:
            ids = [x["id"] for x in rows]
            cur = await conn.execute(f"select id,message_id,file_name,file_ext,size,deleted_at from account_message_files where message_id in ({','.join('?' for _ in ids)}) order by id", ids)
            by_id = {m["id"]: m for m in rows}
            for fid, mid, name, ext, size, deleted in await cur.fetchall():
                by_id[mid]["files"].append({"id": fid, "name": name, "ext": ext, "size": size, "expired": bool(deleted)})
        return rows


async def _message_by_id(message_id: int, peer_id: int, me: UserOut) -> dict | None:
    async with get_connection() as conn:
        cur = await conn.execute("""select m.id,m.sender_id,m.recipient_id,m.text,m.reply_to,m.created_at,m.edited_at,m.deleted_at,
            s.username,r.username from account_messages m join users s on s.id=m.sender_id join users r on r.id=m.recipient_id
            where m.id=? and ((m.sender_id=? and m.recipient_id=?) or (m.sender_id=? and m.recipient_id=?))""",
            (message_id, me.id, peer_id, peer_id, me.id))
        row = await cur.fetchone()
        if not row:
            return None
        msg = _row(row)
        cur = await conn.execute("select id,file_name,file_ext,size,deleted_at from account_message_files where message_id=? order by id", (message_id,))
        msg["files"] = [{"id": fid,"name":name,"ext":ext,"size":size,"expired":bool(deleted)} for fid,name,ext,size,deleted in await cur.fetchall()]
        return msg


@router.get("/users")
async def users(user: UserOut = Depends(require_password_ok)):
    _check(user)
    all_users = await user_repository.list_users()
    async with get_connection() as conn:
        cur = await conn.execute("""select case when sender_id=? then recipient_id else sender_id end peer, max(id) last_id
            from account_messages where sender_id=? or recipient_id=? group by peer""", (user.id,user.id,user.id))
        last = {r[0]: r[1] for r in await cur.fetchall()}
    return [{"id": u["id"], "username": u["username"], "last_id": last.get(u["id"], 0)} for u in all_users if u["id"] != user.id]


@router.get("/{peer_id}/messages")
async def get_messages(peer_id: int, after_id: int = 0, limit: int = Query(500, ge=1, le=1000), user: UserOut = Depends(require_password_ok)):
    _check(user); await _check_peer(peer_id, user)
    return await _messages(peer_id, user, after_id, limit)


class MessageIn(BaseModel):
    text: str = Field(default="", max_length=MAX_TEXT)
    reply_to: int | None = None


async def _insert(peer_id: int, text: str, reply_to: int | None, user: UserOut, uploads: list[UploadFile]):
    text = text.strip()
    if len(uploads) > MAX_FILES: raise HTTPException(400, f"Не больше {MAX_FILES} вложений за раз")
    blobs = []
    for upload in uploads:
        name = Path(upload.filename or "файл").name[:160]
        ext = Path(name).suffix.lower().lstrip(".")
        data = await upload.read(MAX_FILE + 1)
        if not ext or ext not in ALLOWED: raise HTTPException(400, f"Формат файла «{name}» не поддерживается")
        if not data or len(data) > MAX_FILE: raise HTTPException(400, f"Файл «{name}» пустой или больше 8 МБ")
        blobs.append((name, ext, data))
    if not text and not blobs: raise HTTPException(400, "Напишите сообщение или приложите файл")
    now = _now()
    async with get_connection() as conn:
        if reply_to:
            cur = await conn.execute("select sender_id,recipient_id from account_messages where id=? and deleted_at is null", (reply_to,))
            pair = await cur.fetchone()
            if not pair or set(pair) != {user.id, peer_id}: raise HTTPException(400, "Сообщение для ответа не найдено")
        cur = await conn.execute("insert into account_messages(sender_id,recipient_id,text,reply_to,created_at) values(?,?,?,?,?)", (user.id,peer_id,text[:MAX_TEXT],reply_to,now))
        mid = cur.lastrowid
        saved = []
        try:
            for name, ext, data in blobs:
                cur = await conn.execute("insert into account_message_files(message_id,file_name,file_ext,size,created_at) values(?,?,?,?,?)", (mid,name,ext,len(data),now))
                fid = cur.lastrowid
                path = _file_path(fid,ext); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data); saved.append(path)
            await conn.commit()
        except BaseException:
            for path in saved:
                try: path.unlink()
                except OSError: pass
            await conn.rollback(); raise
    return (await _messages(peer_id,user,mid-1,1))[0]


@router.post("/{peer_id}/send")
async def send(peer_id: int, payload: MessageIn, user: UserOut = Depends(require_password_ok)):
    _check(user, True); await _check_peer(peer_id,user)
    return await _insert(peer_id,payload.text,payload.reply_to,user,[])


@router.post("/{peer_id}/files")
async def send_files(peer_id: int, text: str = Form(""), reply_to: int | None = Form(None), files: list[UploadFile] = File(default=[]), user: UserOut = Depends(require_password_ok)):
    _check(user,True); await _check_peer(peer_id,user)
    return await _insert(peer_id,text,reply_to,user,files)


@router.get("/files/{file_id}")
async def get_file(file_id: int, user: UserOut = Depends(require_password_ok)):
    _check(user)
    async with get_connection() as conn:
        cur = await conn.execute("select f.message_id,f.file_name,f.file_ext,f.deleted_at,m.sender_id,m.recipient_id from account_message_files f join account_messages m on m.id=f.message_id where f.id=?", (file_id,))
        row = await cur.fetchone()
    if not row or user.id not in (row[4],row[5]): raise HTTPException(404,"Вложение не найдено")
    if row[3]: raise HTTPException(410,"Срок хранения файла истёк (14 дней)")
    try: data = await asyncio.to_thread(_file_path(file_id,row[2]).read_bytes)
    except FileNotFoundError: raise HTTPException(404,"Файл уже удалён")
    media = mimetypes.guess_type(row[1])[0] or "application/octet-stream"
    return Response(data,media_type=media,headers={"Content-Disposition":f"attachment; filename*=UTF-8''{__import__('urllib.parse').parse.quote(row[1])}","Cache-Control":"private, no-store"})


class EditIn(BaseModel):
    text: str = Field(min_length=1,max_length=MAX_TEXT)


@router.put("/messages/{message_id}")
async def edit(message_id: int,payload: EditIn,user: UserOut=Depends(require_password_ok)):
    _check(user,True)
    async with get_connection() as conn:
        cur=await conn.execute("update account_messages set text=?,edited_at=? where id=? and sender_id=? and deleted_at is null",(payload.text.strip(),_now(),message_id,user.id))
        if not cur.rowcount: raise HTTPException(404,"Сообщение нельзя изменить")
        await conn.commit()
        cur=await conn.execute("select recipient_id from account_messages where id=?",(message_id,)); peer=(await cur.fetchone())[0]
    return await _message_by_id(message_id,peer,user)


@router.delete("/messages/{message_id}")
async def delete(message_id: int,user: UserOut=Depends(require_password_ok)):
    _check(user,True)
    async with get_connection() as conn:
        cur=await conn.execute("update account_messages set text='',deleted_at=? where id=? and sender_id=? and deleted_at is null",(_now(),message_id,user.id))
        if not cur.rowcount: raise HTTPException(404,"Сообщение нельзя удалить")
        await conn.commit()
    return {"deleted":True}


async def attachment_retention_loop():
    """Удаляет только файлы старше 14 дней; история и отметки о вложениях сохраняются."""
    while True:
        try:
            cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=14)).isoformat()
            async with get_connection() as conn:
                cur=await conn.execute("select id,file_ext from account_message_files where deleted_at is null and created_at<?",(cutoff,)); old=await cur.fetchall()
                for fid,ext in old:
                    try: await asyncio.to_thread(_file_path(fid,ext).unlink,missing_ok=True)
                    except OSError: continue
                    await conn.execute("update account_message_files set deleted_at=? where id=?",(_now(),fid))
                await conn.commit()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Не удалось удалить просроченные вложения личных сообщений")
        await asyncio.sleep(3600)
