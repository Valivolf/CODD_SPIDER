"""Клиенты к локальным ИИ-сервисам: распознавание речи (контейнер asr, faster-whisper) и языковая модель (контейнер llm, llama.cpp).

Процессор на сервере один на всё приложение, поэтому запросы к модели идут ПО ОДНОМУ, в порядке важности (голосовой итог монтёра
важнее вопроса помощнику) — см. `_PriorityLock`. Сервис недоступен — `AiUnavailable` (с понятным текстом), ответ модели не разобрать —
`AiError`."""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.services.ai import agents
from app.services.ai import settings as ai_settings

logger = logging.getLogger(__name__)

PRIORITY_BOT = 0          # голосовой итог монтёра (его ждёт человек на комплексе)
PRIORITY_USER = 5         # вопрос или файл помощнику в веб-панели
PRIORITY_BACKGROUND = 9   # прогрев кэша подсказки и прочее фоновое


class AiUnavailable(RuntimeError):
    """ИИ не установлен, не запущен или не отвечает."""


class AiError(RuntimeError):
    """Сервис ответил, но результат непригоден (не JSON, ошибка модели)."""


class _PriorityLock:
    """Замок с очередью по важности: меньше число — раньше; при равной важности — по порядку прихода."""

    def __init__(self) -> None:
        self._busy = False
        self._seq = 0
        self._waiters: list[tuple[int, int, asyncio.Future]] = []

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._waiters if not f.done())

    @property
    def busy(self) -> bool:
        return self._busy

    async def acquire(self, priority: int) -> None:
        if not self._busy and not self._waiters:
            self._busy = True
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (priority, self._seq, fut))
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.release()          # место уже отдали нам, но мы отменены — передаём дальше
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)     # замок остаётся занятым — переходит следующему
                return
        self._busy = False

    @asynccontextmanager
    async def slot(self, priority: int):
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()


_llm_lock = _PriorityLock()
_asr_lock = _PriorityLock()
_stats: dict[str, Any] = {"llm_last_seconds": None, "asr_last_seconds": None, "llm_calls": 0, "asr_calls": 0, "llm_last_usage": None}


def _urls() -> tuple[str, str]:
    s = get_settings()
    return s.ai_llm_url.rstrip("/"), s.ai_asr_url.rstrip("/")


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(connect=8.0, read=read, write=120.0, pool=30.0)


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _clean_content(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


class _AgentResp:
    """Ответ, полученный от компьютера-агента, в том же виде, что и ответ httpx (status_code, text, json()), — остальной разбор общий."""

    def __init__(self, routed: dict[str, Any]) -> None:
        self.status_code = int(routed["status"])
        self._body = routed.get("body")
        self.text = str(routed.get("text") or (json.dumps(self._body, ensure_ascii=False)[:500] if self._body else ""))

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("нет тела ответа")
        return self._body


async def llm_chat(
    system: str, user: str, *, schema: Optional[dict] = None, schema_name: str = "answer", max_tokens: int = 700,
    temperature: float = 0.0, priority: int = PRIORITY_USER, history: Optional[list[dict]] = None,
) -> str:
    """Один запрос к языковой модели; `schema` — JSON-схема: модель физически не сможет ответить иначе (грамматика llama.cpp).
    Возвращает текст ответа."""
    base, _ = _urls()
    messages: list[dict] = [{"role": "system", "content": system}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user})
    payload: dict[str, Any] = {
        "messages": messages, "temperature": temperature, "max_tokens": max_tokens, "stream": False, "cache_prompt": True,
        "chat_template_kwargs": {"enable_thinking": False},      # у Qwen3 — без «размышлений», только ответ (Qwen2.5 «размышлений» не имеет, параметр игнорируется)
    }
    if schema is not None:
        payload["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
    timeout = get_settings().ai_llm_timeout_seconds
    # Сначала — свободный компьютер-агент (app/services/ai/agents.py: самый быстрый из подходящих); нет такого или он не справился — считает сервер, как раньше.
    routed = None
    try:
        routed = await agents.dispatch(payload, background=priority >= PRIORITY_BACKGROUND, system_key=hashlib.sha1(system.encode("utf-8")).hexdigest()[:12], timeout=timeout)
    except Exception:  # noqa: BLE001 — сбой маршрутизации не должен оставлять без ответа: считаем на сервере
        logger.warning("Маршрутизация к ИИ-агентам не удалась — считаю на сервере", exc_info=True)
    if routed is not None:
        resp = _AgentResp(routed)
        _stats["llm_last_seconds"] = routed["seconds"]
        _stats["llm_calls"] += 1
        _stats["llm_last_agent"] = routed["agent"]
    else:
        if not (await ai_settings.load()).get("server_llm", True):
            raise AiUnavailable("ИИ на сервере выключен, а свободных компьютеров с ИИ сейчас нет — подключите компьютер с программой-агентом или включите ИИ на сервере (Настройки → ИИ)")
        if not base:
            raise AiUnavailable("Адрес языковой модели не задан")
        _stats["llm_last_agent"] = ""
        async with _llm_lock.slot(priority):
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=_timeout(timeout)) as client:
                    resp = await client.post(f"{base}/v1/chat/completions", json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise AiUnavailable("Языковая модель не отвечает (контейнер ИИ не запущен или не установлен)") from exc
            except httpx.TimeoutException as exc:
                raise AiError("Языковая модель не успела ответить — сервер занят или слишком слабый для этого запроса") from exc
            _stats["llm_last_seconds"] = round(time.monotonic() - started, 1)
            _stats["llm_calls"] += 1
    if resp.status_code == 503:
        raise AiUnavailable("Языковая модель ещё загружается — подождите минуту")
    if resp.status_code >= 400:
        raise AiError(f"Ошибка языковой модели ({resp.status_code}): {resp.text[:200]}")
    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        _stats["llm_last_usage"] = {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AiError("Языковая модель вернула непонятный ответ") from exc
    return _clean_content(content)


async def llm_json(system: str, user: str, schema: dict, **kwargs: Any) -> dict[str, Any]:
    """То же, но ответ — JSON-объект по схеме (одна повторная попытка с небольшой «температурой», если вдруг не разобрался)."""
    last: Optional[Exception] = None
    for attempt in range(2):
        text = await llm_chat(system, user, schema=schema, temperature=0.0 if attempt == 0 else 0.3, **kwargs)
        try:
            data = json.loads(text)
        except ValueError as exc:
            last = exc
            logger.warning("ИИ вернул не JSON (попытка %s): %s", attempt + 1, text[:200])
            continue
        if isinstance(data, dict):
            return data
        last = AiError("ответ не объект")
    raise AiError("Модель не смогла выдать разбор в нужном виде — попробуйте переформулировать") from last


async def transcribe(audio: bytes, filename: str = "voice.ogg", *, language: str = "ru", prompt: str = "", priority: int = PRIORITY_USER) -> dict[str, Any]:
    """Голос → текст. Возвращает {"text", "language", "duration"}."""
    _, base = _urls()
    if not (await ai_settings.load()).get("server_asr", True):
        raise AiUnavailable("Распознавание речи на сервере выключено (Настройки → ИИ) — голосовые итоги сейчас не принимаются")
    if not base:
        raise AiUnavailable("Адрес распознавания речи не задан")
    timeout = get_settings().ai_asr_timeout_seconds
    async with _asr_lock.slot(priority):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_timeout(timeout)) as client:
                resp = await client.post(
                    f"{base}/transcribe", data={"language": language, "prompt": prompt}, files={"file": (filename, audio)},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise AiUnavailable("Распознавание речи не отвечает (контейнер ИИ не запущен или не установлен)") from exc
        except httpx.TimeoutException as exc:
            raise AiError("Распознавание речи не успело — запись слишком длинная или сервер занят") from exc
        _stats["asr_last_seconds"] = round(time.monotonic() - started, 1)
        _stats["asr_calls"] += 1
    if resp.status_code == 503:
        raise AiUnavailable("Распознавание речи ещё загружается — подождите минуту")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise AiError(f"Не удалось распознать запись: {str(detail)[:200]}")
    try:
        data = resp.json()
        return {"text": str(data.get("text") or "").strip(), "language": data.get("language") or language, "duration": float(data.get("duration") or 0)}
    except (ValueError, TypeError) as exc:
        raise AiError("Распознавание речи вернуло непонятный ответ") from exc


_status_cache: dict[str, Any] = {"at": 0.0, "value": None}
_cpu_cache: Optional[dict[str, Any]] = None


def cpu_info() -> dict[str, Any]:
    """Процессор, каким его видит программа (в контейнере — тот же, что у виртуальной машины): число ядер, модель, есть ли AVX2/FMA. Модель на процессоре
    без AVX2 работает в 3–5 раз медленнее — так бывает, когда в настройках виртуальной машины выбран «обобщённый» тип процессора вместо «как у хоста»."""
    global _cpu_cache
    if _cpu_cache is None:
        info: dict[str, Any] = {"cores": os.cpu_count() or 0, "model": "", "avx2": None, "fma": None, "avx512": None}
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            flags: set[str] = set()
            model = ""
            for line in text.splitlines():
                if line.startswith("model name") and not model:
                    model = line.partition(":")[2].strip()
                elif line.startswith("flags") and not flags:
                    flags = set(line.partition(":")[2].split())
            if flags:
                info.update(model=model, avx2="avx2" in flags, fma="fma" in flags, avx512=any(f.startswith("avx512") for f in flags))
        except OSError:
            pass                                    # не Linux (запуск без Docker на Windows) — неизвестно
        _cpu_cache = info
    return _cpu_cache


async def wait_llm_ready(timeout: float = 900.0, *, only_if_loading: bool = True, poll: float = 8.0) -> bool:
    """Подождать, пока языковая модель загрузится. only_if_loading: ждать только если она «загружается» (ответ 503) — не установленная модель
    сразу даёт понятную ошибку, а не десятиминутное ожидание. Возвращает, готова ли модель."""
    deadline = time.monotonic() + timeout
    while True:
        llm = (await status(force=True))["llm"]
        if llm.get("ok"):
            return True
        if only_if_loading and not llm.get("loading"):
            return False
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)


async def status(force: bool = False) -> dict[str, Any]:
    """Состояние сервисов для окна «Настройки → ИИ» и проверок: {llm: {ok, model, ...}, asr: {ok, model, ...}, queue: {...}}."""
    now = time.monotonic()
    if not force and _status_cache["value"] is not None and now - _status_cache["at"] < 5:
        return _status_cache["value"]
    llm_base, asr_base = _urls()

    async def probe(base: str, path: str) -> dict[str, Any]:
        if not base:
            return {"ok": False, "message": "адрес не задан"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(4.0)) as client:
                resp = await client.get(f"{base}{path}")
            if resp.status_code == 503:
                return {"ok": False, "loading": True, "message": "модель загружается"}
            if resp.status_code >= 400:
                return {"ok": False, "message": f"ответ {resp.status_code}"}
            try:
                body = resp.json()
            except ValueError:
                body = {}
            return {"ok": True, "info": body}
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
            return {"ok": False, "message": "не отвечает (не установлен или не запущен)"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}"}

    cfg = await ai_settings.load()
    off = {"ok": False, "disabled": True, "message": "выключено на сервере"}      # выключатель «Настройки → ИИ»: контейнер не опрашиваем совсем
    llm, asr = await asyncio.gather(
        probe(llm_base, "/health") if cfg.get("server_llm", True) else asyncio.sleep(0, off),
        probe(asr_base, "/health") if cfg.get("server_asr", True) else asyncio.sleep(0, off),
    )
    ag = await agents.availability()
    if not llm.get("ok") and ag["enabled"] and ag["online"] > 0:       # на сервере модели нет (или она грузится), но есть компьютеры-агенты — ИИ работает через них
        llm = {"ok": True, "via_agents": True, "model": f"на компьютерах сотрудников (на связи: {ag['online']})"}
    if llm.get("ok") and not llm.get("via_agents"):
        info = await probe(llm_base, "/v1/models")
        models = ((info.get("info") or {}).get("data") or []) if info.get("ok") else []
        if models:
            llm["model"] = str(models[0].get("id") or "")
    if asr.get("ok"):
        llm_info = asr.get("info") or {}
        asr["model"] = str(llm_info.get("model") or "")
    value = {
        "llm": {k: v for k, v in llm.items() if k != "info"}, "asr": {k: v for k, v in asr.items() if k != "info"},
        "queue": {"llm_waiting": _llm_lock.waiting, "llm_busy": _llm_lock.busy, "asr_waiting": _asr_lock.waiting, "asr_busy": _asr_lock.busy},
        "agents": ag,
        "last": {"llm_seconds": _stats["llm_last_seconds"], "llm_agent": _stats.get("llm_last_agent", ""), "asr_seconds": _stats["asr_last_seconds"], "llm_tokens": _stats["llm_last_usage"]},
        "ready": bool(llm.get("ok") and asr.get("ok")),
        "cpu": cpu_info(),
    }
    _status_cache.update(at=now, value=value)
    return value
