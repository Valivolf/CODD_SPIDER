"""Карточка комплекса: файлы при комплексе (документы, фото, схемы) и сведения для полноэкранной карточки.

  GET    /api/point-card?point_id=…     подгруппы, место в плане и файлы комплекса (одним запросом для открытия карточки)
  GET    /api/point-files?point_id=…    список файлов комплекса
  POST   /api/point-files               загрузка одного файла (point_id, file, comment) — право «менять комплексы»
  GET    /api/point-files/{id}/file     сам файл: изображение, PDF и текст показываются в браузере, остальное скачивается (?download=1 — всегда скачать)
  GET    /api/point-files/{id}/preview  безопасный предпросмотр: Word и Excel → HTML, текст → текст
  DELETE /api/point-files/{id}          удалить файл — право «менять комплексы»

Читать могут те, у кого есть право видеть комплексы (или календарь); загружать и удалять — те, у кого право менять комплексы (таблица адресов — app/core/permissions.py)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.repositories import apk_groups_repository, point_plans_repository
from app.services import point_files

router = APIRouter(prefix="/api", tags=["point-files"], dependencies=[Depends(require_password_ok)])

_READ_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    """Читает загрузку кусками и останавливается, как только размер превышен, — не копит в памяти файл любого размера."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Файл слишком большой: можно не больше {max_size // 1_000_000} МБ")
        chunks.append(chunk)
    return b"".join(chunks)


async def _point_exists(point_id: str) -> str:
    from app.db.database import get_connection  # noqa: PLC0415

    pid = (point_id or "").strip()
    async with get_connection() as conn:
        cur = await conn.execute("select id from points where id = ?", (pid,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комплекс не найден")
    return row[0]


@router.get("/point-card")
async def point_card(point_id: str = Query(...)):
    """Что ещё показывает карточка комплекса, кроме самих полей: подгруппы (с описанием и «что нужно» по комплексу), запись в плане и файлы."""
    pid = await _point_exists(point_id)
    groups = await apk_groups_repository.groups_of_point(pid)
    plan = await point_plans_repository.find_by_point(pid)
    return {
        "groups": groups,
        "plan": ({"id": plan["id"], "group_name": plan["group_name"], "work_type": plan["work_type"], "priority": plan["priority"], "description": plan["description"]} if plan else None),
        "files": await point_files.list_for_point(pid),
    }


@router.get("/point-files")
async def list_files(point_id: str = Query(...)):
    pid = await _point_exists(point_id)
    return await point_files.list_for_point(pid)


@router.post("/point-files", status_code=status.HTTP_201_CREATED)
async def upload_file(point_id: str = Form(...), comment: str = Form(""), file: UploadFile = File(...), user: UserOut = Depends(require_password_ok)):
    """Один файл за запрос: страница шлёт по одному, чтобы большая пачка не упиралась в предел запроса и сбой одного файла не отменял остальные."""
    pid = await _point_exists(point_id)
    content = await _read_capped(file, point_files.MAX_FILE_BYTES)
    try:
        return await point_files.save(pid, file.filename or "", content, user.username, comment)
    except point_files.FileError as exc:
        raise HTTPException(exc.status_code, exc.message)


async def _file_or_404(file_id: int) -> dict:
    rec = await point_files.get(file_id)
    if not rec or not point_files.disk_path(file_id).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден" if not rec else "Файл не найден на диске сервера")
    return rec


@router.get("/point-files/{file_id}/file")
async def file_content(file_id: int, download: bool = False):
    rec = await _file_or_404(file_id)
    name = rec["name"]
    ascii_name = "".join(c if c.isascii() and c.isalnum() or c in "._-" else "_" for c in name) or f"file-{file_id}"
    inline = point_files.inline_ok(rec) and not download
    headers = {
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=600",
    }
    media = rec["mime"] if inline else "application/octet-stream"
    return FileResponse(point_files.disk_path(file_id), media_type=media, headers=headers)


@router.get("/point-files/{file_id}/preview")
async def file_preview(file_id: int):
    rec = await _file_or_404(file_id)
    try:
        data = point_files.preview(rec, point_files.read_bytes(file_id))
    except point_files.FileError as exc:
        raise HTTPException(exc.status_code, exc.message)
    return JSONResponse(data, headers={"Cache-Control": "private, max-age=600"})


@router.delete("/point-files/{file_id}")
async def delete_file(file_id: int):
    if not await point_files.delete(file_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    return {"removed": 1}
