"""Импорт оборудования из Excel (.xlsx) через веб-панель склада: анализ структуры файлов → настройка столбцов →
проверка → загрузка. Подробнее — app/services/equipment_import.py."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.deps import require_warehouse, require_warehouse_edit
from app.models.user import UserOut
from app.services import equipment_import as imp

router = APIRouter(prefix="/api/equipment/import/xlsx", tags=["equipment-import"], dependencies=[Depends(require_warehouse)])

_READ_CHUNK = 1024 * 1024
MAX_FILES = 60


async def _read_files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(400, "Выберите файлы Excel")
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Слишком много файлов (не больше {MAX_FILES} за раз)")
    out: list[tuple[str, bytes]] = []
    for f in files:
        chunks, total = [], 0
        while True:
            chunk = await f.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > imp.MAX_FILE_BYTES:
                raise HTTPException(400, f"«{f.filename}»: файл слишком большой")
            chunks.append(chunk)
        out.append(((f.filename or "файл.xlsx").replace("\\", "/").rsplit("/", 1)[-1], b"".join(chunks)))
    return out


def _json(text: Optional[str], what: str):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        raise HTTPException(400, f"Некорректные настройки ({what})")


@router.post("/analyze")
async def analyze(files: list[UploadFile] = File(...), header_rows: Optional[str] = Form(None)):
    """Структура листов и предложенное сопоставление столбцов. `header_rows` — {"файл::лист": номер строки заголовков}."""
    data = await _read_files(files)
    return imp.analyze(data, _json(header_rows, "строки заголовков"))


@router.post("/run")
async def run(
    files: list[UploadFile] = File(...), config: str = Form(...), dry_run: bool = Form(True),
    user: UserOut = Depends(require_warehouse_edit),
):
    """dry_run=true — только отчёт «что будет», в базу ничего не пишется."""
    data = await _read_files(files)
    try:
        return await imp.run_import(data, _json(config, "импорта"), dry_run=dry_run, performed_by=user.username)
    except imp.ImportError_ as exc:
        raise HTTPException(exc.status_code, exc.detail)
