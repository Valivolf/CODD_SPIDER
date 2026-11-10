"""Отчёты из таблицы Яндекс Форм — см. services/forms_import.py.

Загружают администратор и диспетчер. Шаги: «Проверить» (dry_run=true — ничего не пишется) → «Загрузить» (dry_run=false): отчёты
создаются по всем строкам (по заданию ±1 день, без него — в историю комплекса; если за дату отчёт уже есть — строка пропускается),
а фото сразу скачиваются в фоне через браузер робота карт (он под Яндекс-аккаунтом создателя формы; если в папке комплекса за дату
фото уже есть — пропускаются). Ход скачивания — GET /photos/status; вручную («Фото загруженных ранее») — POST /photos."""
from __future__ import annotations

import json
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.services import forms_import as fi
from app.services.ai import client as ai_client, forms_fault

router = APIRouter(prefix="/api/forms-import", tags=["forms-import"], dependencies=[Depends(require_password_ok)])

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


def _public(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "urls"}


@router.post("/import", dependencies=[Depends(_editor)])
async def import_forms(
    file: UploadFile = File(...), dry_run: bool = Form(True), apply_status: bool = Form(False), only_json: str = Form(""),
    user: UserOut = Depends(_editor),
):
    """dry_run — только проверить и показать, что будет сделано; иначе — создать отчёты. `apply_status` — менять ли статус
    комплекса по итогу отчёта (по умолчанию нет: отчёты запоздалые). `only_json` — JSON-список отпечатков строк, по которым
    создавать отчёты (пусто — по всем подходящим)."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите таблицу формы в формате .xlsx")
    content = await _read_capped(file, fi.MAX_FILE_SIZE)
    parsed = fi.parse(content)
    if not parsed.rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, parsed.errors[0] if parsed.errors else "В таблице не найдено строк с отчётами")
    plan = await fi.build_plan(parsed)
    if dry_run:
        return {"dry_run": True, "summary": fi.summarize(plan), "rows": [_public(i) for i in plan], "errors": parsed.errors}
    only: Optional[set[str]] = None
    if only_json.strip():
        try:
            only = {str(h) for h in json.loads(only_json)}
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный список строк")
    batch = secrets.token_hex(6)
    result = await fi.apply_plan(parsed, plan, apply_status=apply_status, only=only, batch=batch, user=user.username)
    rows = await fi.list_rows(batch)
    # Фото качаются сами: у всех строк пачки с найденным комплексом и ссылками (кроме тех, где в папке за дату фото уже есть)
    photo_ids = [r["id"] for r in rows if r["photos_total"] and r["task_id"]]
    job = await fi.start_download(photo_ids) if photo_ids else {**fi.job_status(), "started": False, "reason": "В таблице нет ссылок на фото"}
    forms_fault.kick()          # неисправные отчёты: ИИ разберёт «итог работ» сразу (если включено в настройках)
    return {"dry_run": False, "batch": batch, "created": result["created"], "created_free": result["created_free"],
            "summary": fi.summarize(plan), "rows": [_public(i) for i in plan], "saved": rows, "errors": parsed.errors, "photos": job}


@router.get("/rows", dependencies=[Depends(_editor)])
async def list_saved_rows(batch: str = ""):
    """Строки загруженных таблиц (по умолчанию — все) с состоянием скачивания фото."""
    return {"rows": await fi.list_rows(batch or None), "job": fi.job_status()}


class PhotosIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=2000)


@router.post("/photos", dependencies=[Depends(_editor)])
async def download_photos(body: PhotosIn):
    """Скачать фото выбранных строк через браузер робота карт (в фоне; ход — GET /photos/status)."""
    result = await fi.start_download(body.ids)
    if not result.get("started") and result.get("reason"):
        raise HTTPException(status.HTTP_409_CONFLICT, result["reason"])
    return result


@router.get("/photos/status", dependencies=[Depends(_editor)])
async def photos_status():
    return fi.job_status()


@router.get("/ai-faults", dependencies=[Depends(_editor)])
async def ai_faults_status():
    """ИИ-разбор отчётов, где не удалось починить: сколько ждёт, сколько разобрано (app/services/ai/forms_fault.py)."""
    out = await forms_fault.status()
    try:
        out["llm_ready"] = bool((await ai_client.status())["llm"].get("ok"))
    except Exception:  # noqa: BLE001
        out["llm_ready"] = False
    return out


class AiFaultsIn(BaseModel):
    again: bool = False           # заново взять и отклонённые проверкой, и сбойные


@router.post("/ai-faults", dependencies=[Depends(_editor)])
async def ai_faults_run(body: AiFaultsIn):
    """«Разобрать ИИ сейчас»: в фоне разложить итоги работ неисправных отчётов из форм на «что неисправно» и «что нужно» и записать их в отчёт и в карточку комплекса."""
    state = await ai_client.status(force=True)
    if not state["llm"].get("ok"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, state["llm"].get("message") or "Языковая модель не отвечает — ИИ не установлен или ещё загружается")
    if not forms_fault.start_manual(body.again):
        raise HTTPException(status.HTTP_409_CONFLICT, "Разбор уже идёт — дождитесь окончания")
    return {"started": True}
