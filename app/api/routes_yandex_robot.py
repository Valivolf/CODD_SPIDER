"""Робот Яндекс.Конструктора карт: карты бригад создаются и обновляются сами.

Само приложение ведёт карты (app/services/yandex_map_sync.py): когда у бригады меняются точки, фоновый
цикл правит её карту в Конструкторе (только точечно: ссылка не меняется) и кладёт ссылку в поле «Ссылка на
карту» бригады (crews.map_link) — её видит и монтёр в боте. Эти маршруты — кнопка «Создать/обновить
карту» в окне бригады, настройки автоматики и окно входа в Яндекс.

Вход в Яндекс делает администратор сам в окне календаря («Яндекс-аккаунт для карт»): окно показывает
картинку браузера робота (поток кадров) и передаёт клики/клавиши. Пароль в приложении не сохраняется и
не пишется в журнал — он уходит только в браузер робота, на страницу Яндекса; в профиле робота остаётся
лишь сессия (cookie).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.core.deps import require_settings, require_password_ok
from app.repositories import crew_repository, points_repository, state_repository
from app.repositories import yandex_map_repository as repo
from app.services import yandex_map_sync as sync
from app.services import yandex_robot

router = APIRouter(prefix="/api/yandex-robot", tags=["yandex-robot"])
logger = logging.getLogger(__name__)


async def _proxy(method: str, path: str, **kwargs: Any) -> Any:
    try:
        return await yandex_robot.call(method, path, **kwargs)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(exc.status_code if exc.status_code in (400, 404) else status.HTTP_502_BAD_GATEWAY, exc.detail)


def _profile(profile: str) -> dict[str, str]:
    if profile not in ("maps", "forms"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный профиль браузера")
    return {"profile": profile}


@router.get("/status", dependencies=[Depends(require_password_ok)])
async def robot_status(verify: bool = False, profile: str = "maps", check: str = "") -> dict[str, Any]:
    """Состояние робота для кнопки «Создать карту» и окна аккаунта. Недоступный робот —
    не ошибка, а `available: false` с подсказкой. profile — какой вход (maps — для карт и по умолчанию для Форм; forms — отдельный для Форм),
    check=forms — проверить доступ именно к Яндекс Формам (а не к Конструктору)."""
    params: dict[str, Any] = _profile(profile)
    if verify:
        params["verify"] = 1
    if check in ("forms", "maps"):
        params["check"] = check
    try:
        data = await yandex_robot.call("GET", "/status", params=params, timeout=240.0 if verify else 25.0)
    except yandex_robot.RobotUnavailable as exc:
        return {"available": False, "logged_in": False, "login": "", "busy": False, "message": str(exc)}
    except yandex_robot.RobotRejected as exc:
        return {"available": False, "logged_in": False, "login": "", "busy": False, "message": exc.detail}
    return {
        "available": True,
        "logged_in": bool(data.get("logged_in")),
        "login": data.get("login", ""),
        "busy": bool(data.get("busy")),
        "message": data.get("message", ""),
        "viewport": data.get("viewport"),
    }


# ------------------------------------------------------- окно входа (только администратор)

@router.get("/view", dependencies=[Depends(require_settings)])
async def view_image(after: int = -1, wait: float = 0.0, profile: str = "maps") -> Response:
    """Кадр окна входа; after=N&wait=S — долгий опрос: ждать кадра новее N (204 — без изменений)."""
    try:
        resp = await yandex_robot.call("GET", "/view", params={"after": after, "wait": wait, **_profile(profile)}, timeout=wait + 40.0, raw=True)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.detail)
    headers = {"Cache-Control": "no-store"}
    for name in ("X-Frame-Seq", "X-View-Width", "X-View-Height"):
        if name in resp.headers:
            headers[name] = resp.headers[name]
    if resp.status_code == 204:
        return Response(status_code=204, headers=headers)
    return Response(content=resp.content, media_type="image/jpeg", headers=headers)


class OpenIn(BaseModel):
    url: str = Field(default="", max_length=500)


class ClickIn(BaseModel):
    x: float = Field(ge=0, le=5000)
    y: float = Field(ge=0, le=5000)
    double: bool = False


class TypeIn(BaseModel):
    text: str = Field(max_length=2000)


class KeyIn(BaseModel):
    key: str = Field(max_length=30)


class ScrollIn(BaseModel):
    dy: float = Field(default=300, ge=-5000, le=5000)


@router.post("/view/open", dependencies=[Depends(require_settings)])
async def view_open(body: OpenIn, profile: str = "maps") -> Any:
    return await _proxy("POST", "/view/open", json=body.model_dump(), params=_profile(profile), timeout=60.0)


@router.post("/view/click", dependencies=[Depends(require_settings)])
async def view_click(body: ClickIn, profile: str = "maps") -> Any:
    return await _proxy("POST", "/view/click", json=body.model_dump(), params=_profile(profile))


@router.post("/view/type", dependencies=[Depends(require_settings)])
async def view_type(body: TypeIn, profile: str = "maps") -> Any:
    # Текст может быть паролем — нигде не логируем и не сохраняем, только передаём дальше.
    return await _proxy("POST", "/view/type", json=body.model_dump(), params=_profile(profile))


@router.post("/view/key", dependencies=[Depends(require_settings)])
async def view_key(body: KeyIn, profile: str = "maps") -> Any:
    return await _proxy("POST", "/view/key", json=body.model_dump(), params=_profile(profile))


@router.post("/view/scroll", dependencies=[Depends(require_settings)])
async def view_scroll(body: ScrollIn, profile: str = "maps") -> Any:
    return await _proxy("POST", "/view/scroll", json=body.model_dump(), params=_profile(profile))


class ResizeIn(BaseModel):
    width: int = Field(ge=320, le=2560)
    height: int = Field(ge=240, le=1600)


@router.post("/view/resize", dependencies=[Depends(require_settings)])
async def view_resize(body: ResizeIn, profile: str = "maps") -> Any:
    """Экран браузера робота подгоняется под окно календаря — картинка идёт на весь экран, а не квадратом."""
    return await _proxy("POST", "/view/resize", json=body.model_dump(), params=_profile(profile))


@router.post("/audio/start", dependencies=[Depends(require_settings)])
async def audio_start(profile: str = "maps") -> Any:
    """Окно просит звук страницы робота (например, голосовая проверка «Вы не робот»): запись начинается заново, чтобы пришёл заголовок потока."""
    return await _proxy("POST", "/audio/start", params=_profile(profile))


@router.get("/audio", dependencies=[Depends(require_settings)])
async def audio_chunks(after: int = -1, wait: float = 0.0, profile: str = "maps") -> Response:
    """Звук окна входа: куски WebM/Opus новее after (долгий опрос до wait секунд; 204 — пока тихо)."""
    wait = min(max(wait, 0.0), 5.0)
    try:
        resp = await yandex_robot.call("GET", "/audio", params={"after": after, "wait": wait, **_profile(profile)}, timeout=wait + 20.0, raw=True)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.detail)
    headers = {"Cache-Control": "no-store"}
    for name in ("X-Audio-Seq", "X-Audio-Stream"):
        if name in resp.headers:
            headers[name] = resp.headers[name]
    if resp.status_code == 204:
        return Response(status_code=204, headers=headers)
    return Response(content=resp.content, media_type="audio/webm", headers=headers)


@router.get("/netcheck", dependencies=[Depends(require_settings)])
async def netcheck() -> Any:
    """Какие адреса Яндекса открываются с сервера (для сети с фильтром по доменам)."""
    return await _proxy("GET", "/netcheck", timeout=40.0)


@router.post("/logout", dependencies=[Depends(require_settings)])
async def logout(profile: str = "maps") -> Any:
    return await _proxy("POST", "/account/logout", params=_profile(profile))


# ------------------------------------------------------------------ настройки автоматики

class SettingsIn(BaseModel):
    auto_sync: Optional[bool] = None
    sync_days_ahead: Optional[int] = Field(default=None, ge=0, le=14)
    auto_delete: Optional[bool] = None
    auto_delete_days: Optional[int] = Field(default=None, ge=1, le=365)


async def _settings_payload() -> dict[str, Any]:
    cfg = await repo.get_settings()
    maps = await repo.list_maps()
    return {
        **cfg,
        "maps_total": len(maps),
        "trash_total": len(await repo.trash_list()),
        "last_tick": sync.STATUS["last_tick"],
        "last_message": sync.STATUS["last_message"],
        "last_error": sync.STATUS["last_error"],
    }


@router.get("/settings", dependencies=[Depends(require_settings)])
async def get_settings_route() -> dict[str, Any]:
    return await _settings_payload()


@router.put("/settings", dependencies=[Depends(require_settings)])
async def put_settings(body: SettingsIn) -> dict[str, Any]:
    await repo.set_settings({k: v for k, v in body.model_dump().items() if v is not None})
    sync.STATUS["last_error"] = ""
    sync.STATUS["paused_until"] = 0.0
    return await _settings_payload()


# ------------------------------------------------------------------- карта бригады

@router.post("/crews/{crew_id}/map")
async def create_crew_map(crew_id: int, _=Depends(require_password_ok)) -> dict[str, Any]:
    """Кнопка «Создать / обновить карту»: нет карты — создаётся; есть — правится точечно (ссылка
    остаётся прежней); ничего не изменилось — «карта актуальна»."""
    crew = await crew_repository.get_crew(crew_id)
    if not crew:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Бригада не найдена")
    _, data = await state_repository.read_state()
    owners = sync.build_owners([crew], data["tasks"], await points_repository.list_points())
    owner = next(iter(owners.values()))
    if not owner.rows:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "У бригады нет заданий с координатами точек — на карте показывать нечего"
            + (f" (без координат: {owner.skipped})" if owner.skipped else ""),
        )
    plan = sync.make_plan(owner, await repo.get_map(owner.key), force=True)
    if plan.kind == "none":
        return {"job_id": None, "uptodate": True, "url": owner.link, "points": len(owner.rows), "skipped": owner.skipped, "state": "done", "kind": "none"}
    job = sync.start(owner, plan)
    return {"job_id": job.id, "uptodate": False, "points": len(owner.rows), "skipped": owner.skipped, "state": job.state, "kind": plan.kind, "summary": plan.summary}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_password_ok)])
async def job_state(job_id: str) -> dict[str, Any]:
    job = sync.get_job(job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено (приложение перезапускалось?)")
    return job.public()


@router.get("/jobs/{job_id}/shot", dependencies=[Depends(require_settings)])
async def job_shot(job_id: str) -> Response:
    job = sync.get_job(job_id)
    robot_id = job.robot_job_id if job else job_id
    if not robot_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Снимка нет")
    try:
        resp = await yandex_robot.call("GET", f"/jobs/{robot_id}/shot", raw=True)
    except yandex_robot.RobotUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except yandex_robot.RobotRejected as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, exc.detail)
    return Response(content=resp.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
