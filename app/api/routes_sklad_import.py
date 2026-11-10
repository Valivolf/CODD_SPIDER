"""Импорт базы прежнего приложения «Склад» (warehouse.db) через веб-панель склада — с предпросмотром."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.config import get_settings
from app.core.deps import require_warehouse, require_warehouse_edit
from app.models.user import UserOut
from app.services import sklad_import

router = APIRouter(prefix="/api/equipment/import", tags=["equipment-import"], dependencies=[Depends(require_warehouse)])

_READ_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(400, "Файл слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/sklad")
async def import_sklad(
    file: UploadFile = File(...), mode: str = Form("merge"), dry_run: bool = Form(True),
    import_history: bool = Form(True), user: UserOut = Depends(require_warehouse_edit),
):
    """dry_run=true — только отчёт «что будет сделано», в базу ничего не пишется."""
    raw = await _read_capped(file, sklad_import.MAX_SOURCE_BYTES)
    if not raw:
        raise HTTPException(400, "Файл пустой")
    workdir = get_settings().data_dir / "tmp"
    workdir.mkdir(parents=True, exist_ok=True)
    path = None
    try:
        path = sklad_import.prepare_source(raw, workdir, f"sklad-import-{uuid.uuid4().hex}.db")
        src = await asyncio.to_thread(sklad_import.read_source, path)
        return await sklad_import.run_import(
            src, mode=mode, dry_run=dry_run, import_history=import_history, performed_by=user.username, filename=file.filename or "",
        )
    except sklad_import.ImportError_ as exc:
        raise HTTPException(exc.status_code, exc.detail)
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
