"""Загрузка истории выездов из Excel (дата, кто, комплекс, что сделано, статус) — см. services/visit_history_import.py.

Загружают администратор и диспетчер; «только просмотр» закрыт общей защитой от записи."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.services import visit_history_import as vhi

router = APIRouter(prefix="/api/visit-history", tags=["visit-history"], dependencies=[Depends(require_password_ok)])

_READ_CHUNK = 1024 * 1024


async def _editor(user: UserOut = Depends(require_password_ok)) -> UserOut:
    if not P.has(user.permissions, "reports.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: вносить и править отчёты о выездах")
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


@router.get("/template", dependencies=[Depends(_editor)])
async def download_template():
    return StreamingResponse(
        vhi.build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="visit-history-template.xlsx"'},
    )


@router.post("/import", dependencies=[Depends(_editor)])
async def import_history(
    file: UploadFile,
    dry_run: bool = True,
    default_type: str = "",
    allow_unknown_points: bool = False,
):
    """dry_run=true — только проверить и показать, что будет загружено (ничего не пишется);
    dry_run=false — записать. Тот же файл можно прогнать сколько угодно раз: уже загруженное не дублируется."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, vhi.MAX_FILE_SIZE)
    parsed = vhi.parse(content)
    if not parsed.rows and parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, parsed.errors[0])
    plan = await vhi.build_plan(parsed, default_type or None, allow_unknown_points)
    if dry_run:
        return vhi.summarize(plan, dry_run=True)
    batch = vhi.new_batch_id()
    added = await vhi.apply_plan(plan, batch)
    return vhi.summarize(plan, dry_run=False, batch=batch if added else None, added=added)


@router.get("/batches", dependencies=[Depends(_editor)])
async def list_batches():
    return await vhi.list_batches()


@router.delete("/batches/{batch}", dependencies=[Depends(_editor)])
async def delete_batch(batch: str):
    deleted = await vhi.delete_batch(batch)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой загрузки нет")
    return {"deleted": deleted}


@router.delete("/{task_id}", dependencies=[Depends(_editor)])
async def delete_record(task_id: str):
    """Одна загруженная запись (опечатка в файле); записи, созданные монтёрами и диспетчерами, так не удаляются."""
    if not await vhi.delete_one(task_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Загруженной записи с таким номером нет")
    return {"ok": True}
