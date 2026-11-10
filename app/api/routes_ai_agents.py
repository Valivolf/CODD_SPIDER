"""ИИ-агенты на компьютерах сотрудников (устройство и безопасность — app/services/ai/agents.py).

Два набора адресов:
  /api/agent/*      — для самой программы-агента (агент → сервер): регистрация по ключу подключения, heartbeat, «длинный опрос» задания, результат.
                      Права здесь не по сеансу пользователя, а по токену агента (Authorization: Bearer …); таблицу прав (permissions.py) эти адреса не проходят — как и служебные
                      вызовы бота /api/bot/*. Неверные регистрации считаются как неудачные входы с этого адреса и блокируют его при переборе ключей.
  /api/ai-agents/*  — для администратора («Настройки → ИИ…», право «Настройки системы»): режим вкл/выкл, список компьютеров (отвязать), одноразовые ключи подключения (строка «lcai://адрес/ключ»), проверка агента."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core import rate_limit
from app.core.clientip import client_ip
from app.core.deps import require_agent, require_password_ok
from app.models.user import UserOut
from app.services import security_log
from app.services.ai import agent_models, agents

agent_router = APIRouter(prefix="/api/agent", tags=["ai-agent"])
admin_router = APIRouter(prefix="/api/ai-agents", tags=["ai-agents"], dependencies=[Depends(require_password_ok)])


# ---------------------------------------------------------------- для программы-агента
class RegisterIn(BaseModel):
    enroll_key: str = Field(max_length=200)
    name: str = Field(default="", max_length=120)
    hardware: dict[str, Any] = Field(default_factory=dict)
    version: str = Field(default="", max_length=40)
    hw_id: str = Field(default="", max_length=80)                    # отпечаток железа, посчитанный программой (привязка компьютера)


@agent_router.post("/register")
async def register(body: RegisterIn, request: Request):
    ip = client_ip(request)
    left = rate_limit.agent_register_locked(ip)
    if left:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Слишком много неверных ключей подключения с этого адреса. Подождите {max(1, round(left / 60))} мин. и вставьте актуальную строку подключения.")
    try:
        res = await agents.register(body.enroll_key, body.name, body.hardware, body.version, ip, body.hw_id)
    except agents.RegisterRejected as exc:
        if exc.counts:                                       # только подбор неизвестных ключей; использованный/просроченный ключ — не подбор, и счётчик входа в панель не трогаем
            rate_limit.agent_register_failure(ip)
        await security_log.record("admin_action", ip=ip, ok=False, details={"event": "подключение ИИ-агента отклонено", "reason": str(exc)[:120]})
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    rate_limit.agent_register_clear(ip)
    await security_log.record("admin_action", username=res["name"], ip=ip, details={"event": "ИИ-агент подключён", "agent": res["agent_id"]})
    return res


class HeartbeatIn(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


@agent_router.post("/heartbeat")
async def heartbeat(body: HeartbeatIn, agent: dict = Depends(require_agent)):
    return await agents.heartbeat(agent, body.state)


@agent_router.get("/next")
async def next_job(wait: float = Query(20.0, ge=1.0, le=agents.POLL_MAX), agent: dict = Depends(require_agent)):
    """Агент свободен — держит этот запрос открытым; получает задание или пустой ответ по истечении времени."""
    cfg = await agents.config()
    if not cfg["enabled"] or not agent.get("enabled", True):
        return {"job": None, "paused": True}
    job = await agents.next_job(agent, wait)
    return {"job": job, "paused": False}


@agent_router.get("/models")
async def server_models(agent: dict = Depends(require_agent)):
    """Модели, которые лежат на сервере (папка data/agent_models): программа скачивает их отсюда, остальные — из интернета."""
    return {"models": await agent_models.list_models(for_agent=True)}


@agent_router.get("/models/{name}")
async def server_model_file(name: str, agent: dict = Depends(require_agent)):
    """Файл модели с сервера: по токену и железу компьютера, с докачкой (Range)."""
    cfg = await agents.config()
    if not cfg["enabled"] or not agent.get("enabled", True):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Режим «ИИ на компьютерах» выключен или компьютер отключён администратором")
    path = agent_models.path_for(name)
    if not path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Такой модели на сервере нет")
    return FileResponse(path, media_type="application/octet-stream", filename=name, headers={"Cache-Control": "no-store"})


class ResultIn(BaseModel):
    status: Optional[int] = None
    body: Optional[dict[str, Any]] = None
    text: str = Field(default="", max_length=20000)
    error: str = Field(default="", max_length=400)
    timings: Optional[dict[str, Any]] = None


@agent_router.post("/result/{job_id}")
async def result(job_id: str, body: ResultIn, agent: dict = Depends(require_agent)):
    ok = await agents.submit_result(agent, job_id, body.model_dump())
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено (устарело или чужое)")
    return {"ok": True}


# ---------------------------------------------------------------- для администратора
@admin_router.get("")
async def overview():
    snap = await agents.snapshot()
    snap["keys"] = await agents.list_keys()
    snap["models"] = await agent_models.list_models()
    snap["models_dir"] = "data/agent_models"
    snap["models_recommended"] = agent_models.RECOMMENDED
    return snap


@admin_router.delete("/models/{name}")
async def delete_model(name: str, request: Request, user: UserOut = Depends(require_password_ok)):
    """Убрать файл модели с сервера (с диска): компьютеры больше не смогут скачать её отсюда (уже скачанное у них остаётся)."""
    if not agent_models.remove(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл модели не найден")
    await security_log.record("admin_action", username=user.username, role=user.role, ip=client_ip(request), details={"event": "удалён файл модели для ИИ-агентов", "file": name})
    return {"ok": True}


class ConfigIn(BaseModel):
    enabled: Optional[bool] = None
    min_params_b: Optional[float] = None
    offline_after: Optional[int] = None           # через сколько секунд без сигналов агент считается «не отвечает»


@admin_router.put("/config")
async def put_config(body: ConfigIn, request: Request, user: UserOut = Depends(require_password_ok)):
    before = await agents.config(force=True)
    cfg = await agents.save_config(body.model_dump())
    if before["enabled"] != cfg["enabled"]:
        await security_log.record("admin_action", username=user.username, role=user.role, ip=client_ip(request),
                                  details={"event": "режим «ИИ на компьютерах сотрудников» " + ("включён" if cfg["enabled"] else "выключен")})
    return cfg


class KeyIn(BaseModel):
    label: str = Field(default="", max_length=60)
    days: int = Field(default=agents.KEY_DAYS, ge=1, le=365)
    server_url: str = Field(default="", max_length=200)              # адрес сервера для компьютеров сотрудников; пусто — тот, по которому открыта панель


@admin_router.post("/keys", status_code=status.HTTP_201_CREATED)
async def create_key(body: KeyIn, request: Request, user: UserOut = Depends(require_password_ok)):
    """Одноразовый ключ и готовая строка подключения «lcai://адрес/ключ» (показывается один раз): её целиком вставляют в программу-агент."""
    server = body.server_url.strip() or (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    try:
        res = await agents.create_key(body.label, body.days, user.username, server)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    await security_log.record("admin_action", username=user.username, role=user.role, ip=client_ip(request), details={"event": "создан одноразовый ключ подключения ИИ-агента", "label": res["label"]})
    return res


@admin_router.delete("/keys/{key_id}")
async def delete_key(key_id: int):
    if not await agents.delete_key(key_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ключ не найден")
    return {"ok": True}


class AgentPatch(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = Field(default=None, max_length=60)
    note: Optional[str] = Field(default=None, max_length=200)


@admin_router.put("/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatch):
    if not await agents.update_agent(agent_id, enabled=body.enabled, name=body.name, note=body.note):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компьютер не найден")
    return {"ok": True}


@admin_router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    if not await agents.remove_agent(agent_id, "отвязан администратором"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компьютер не найден")
    return {"ok": True}


@admin_router.post("/{agent_id}/test")
async def test_agent(agent_id: str):
    try:
        return await agents.test_agent(agent_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Компьютер не найден") from None
    except (agents.AgentUnavailable, agents.AgentError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
