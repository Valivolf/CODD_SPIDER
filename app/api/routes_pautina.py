"""«Импорт из Паутины» — обновление заведённых комплексов из выгрузки другой программы, см. services/pautina_import.py.

Загружают администратор и диспетчер; «только просмотр» закрыт общей защитой от записи."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.services import pautina_import

router = APIRouter(prefix="/api/pautina", tags=["pautina"], dependencies=[Depends(require_password_ok)])

_READ_CHUNK = 1024 * 1024


async def _editor(user: UserOut = Depends(require_password_ok)) -> UserOut:
    if not P.has(user.permissions, "points.import"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: загружать комплексы из Excel и «Паутины»")
    return user


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Файл слишком большой (больше {max_size // (1024 * 1024)} МБ)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/import", dependencies=[Depends(_editor)])
async def import_pautina(file: UploadFile, dry_run: bool = True):
    """dry_run=true — только проверить (ничего не пишется); dry_run=false — обновить комплексы. В обоих случаях готовится файл
    итогов (report_id → GET /api/pautina/report/{id}) со списком комплексов, которых нет в системе."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, pautina_import.MAX_FILE_SIZE)
    parsed = pautina_import.parse(content)
    if not parsed.rows and parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, parsed.errors[0])
    if not parsed.rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, parsed.notes[0] if parsed.notes else "В файле не найдено строк для импорта")
    plan = await pautina_import.build_plan(parsed)
    applied = None if dry_run else await pautina_import.apply_plan(plan)
    summary = pautina_import.summarize(parsed, plan, dry_run=dry_run, filename=file.filename, report_id=None, applied=applied)
    summary["report_id"] = pautina_import.save_report(pautina_import.build_report(parsed, plan, summary))
    return summary


@router.get("/report/{report_id}", dependencies=[Depends(_editor)])
async def download_report(report_id: str):
    path = pautina_import.report_path(report_id)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл итогов не найден (хранится 14 дней) — загрузите файл ещё раз")
    return FileResponse(
        path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="pautina-import-itogi.xlsx",
    )
