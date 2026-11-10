"""Хранилище фото выездов (сетевой диск): состояние, настройки подключения, перенос. Настройки и кнопки — только админу, в окне
«Мониторинг → Фото по дням». Логика — app/services/photo_storage.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core import permissions as P
from app.core.deps import require_settings, require_password_ok
from app.models.user import UserOut
from app.services import photo_index, photo_storage

router = APIRouter(prefix="/api/photo-storage", tags=["photo-storage"])


def _fail(exc: photo_storage.PhotoStorageError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("")
async def storage_status(user: UserOut = Depends(require_password_ok)):
    """Состояние хранилища: доступен ли сетевой диск, свободное место, ход переноса и итог последнего пересчёта файлов.
    Настройки подключения (без пароля) видит тот, у кого есть право на настройки системы."""
    info = await photo_storage.status()
    if P.has(user.permissions, "admin.settings"):
        info["config"] = photo_storage.config_view()
    return info


class ConfigIn(BaseModel):
    mode: str = "smb"                       # 'smb' — сетевая папка Windows; 'none' — хранить на диске сервера
    path: str = ""                          # \\сервер\общая_папка\папка_с_комплексами
    port: int = 445
    domain: str = ""
    username: str = ""
    password: Optional[str] = None          # пусто — оставить прежний
    organize: Optional[bool] = None         # раскладывать фото из корня папки комплекса по папкам-датам (по умолчанию да)


@router.put("/config", dependencies=[Depends(require_settings)])
async def save_config(payload: ConfigIn):
    """Сохранить подключение к сетевой папке (или отключить его). Проверяется отдельно — «Проверить подключение»."""
    try:
        await photo_storage.save_config(payload.model_dump())
    except photo_storage.PhotoStorageError as exc:
        raise _fail(exc)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Порт — число от 1 до 65535")
    if payload.mode == "smb":
        await photo_index.start(auto=True)         # сразу посмотреть, что уже лежит в папках комплексов
    info = await photo_storage.status()
    info["config"] = photo_storage.config_view()
    return info


@router.post("/test", dependencies=[Depends(require_settings)])
async def test_connection(payload: ConfigIn):
    """Проверка подключения с введёнными настройками: вход, папка, запись. Ничего не сохраняет."""
    try:
        return await photo_storage.test_connection(payload.model_dump())
    except photo_storage.PhotoStorageError as exc:
        raise _fail(exc)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Порт — число от 1 до 65535")


@router.post("/scan", dependencies=[Depends(require_settings)])
async def storage_scan():
    await photo_storage.scan()
    return await photo_storage.status()


@router.post("/marker", dependencies=[Depends(require_settings)])
async def prepare_disk():
    """«Подготовить диск» (только для папки, подключённой на сервере): метка в папке хранилища. Один раз, убедившись, что
    к серверу подключён нужный диск."""
    try:
        await photo_storage.init_marker()
    except photo_storage.PhotoStorageError as exc:
        raise _fail(exc)
    return await photo_storage.status()


@router.post("/flush", dependencies=[Depends(require_settings)])
async def flush_buffer():
    """Перенести накопленный на сервере буфер на сетевой диск сейчас (иначе это делается само раз в минуту)."""
    result = await photo_storage.flush_spool()
    return {**result, "status": await photo_storage.status()}


@router.post("/migrate", dependencies=[Depends(require_settings)])
async def migrate_old_photos():
    """Перенос фото, уже лежащих на диске сервера, на сетевой диск (освобождает место). Идёт в фоне — состояние в GET."""
    try:
        await photo_storage.start_migration()
    except photo_storage.PhotoStorageError as exc:
        raise _fail(exc)
    return await photo_storage.status()


@router.post("/index", dependencies=[Depends(require_settings)])
async def run_index():
    """Найти фото, которые уже лежат в папках комплексов на сетевом диске, разложить лежащие в корне по датам и занести в программу."""
    try:
        await photo_index.start()
    except photo_storage.PhotoStorageError as exc:
        raise _fail(exc)
    return await photo_storage.status()
