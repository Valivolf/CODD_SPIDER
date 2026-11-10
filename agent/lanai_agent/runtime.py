"""Запуск и обслуживание llama-server (llama.cpp): процесс с пониженным приоритетом, привязанный к агенту (закроется вместе с ним), проверка готовности, замер скорости, запрос."""
from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import catalog, paths

_CREATE_NO_WINDOW = 0x08000000
_BELOW_NORMAL = 0x00004000

# Поля запроса, которые агент передаёт модели; остальное из задания отбрасывается (сервер доверенный, но лишнего агент не исполняет).
ALLOWED_KEYS = ("messages", "temperature", "max_tokens", "cache_prompt", "response_format", "chat_template_kwargs", "top_p", "seed", "stop")
MAX_PROMPT_CHARS = 400_000
MAX_TOKENS = 4096


class LlamaStartError(RuntimeError):
    """llama-server не запустился или не стал готовым."""


def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
    """Оставить в задании только разрешённое, ограничить размеры; форму сообщений проверить."""
    out = {k: payload[k] for k in ALLOWED_KEYS if k in payload}
    msgs = out.get("messages")
    if not isinstance(msgs, list) or not msgs or not all(isinstance(m, dict) and isinstance(m.get("role"), str) and isinstance(m.get("content"), str) for m in msgs):
        raise ValueError("в задании нет сообщений")
    if sum(len(m["content"]) for m in msgs) > MAX_PROMPT_CHARS:
        raise ValueError("слишком длинное задание")
    try:
        out["max_tokens"] = max(1, min(MAX_TOKENS, int(out.get("max_tokens") or 512)))
    except (TypeError, ValueError):
        out["max_tokens"] = 512
    out["stream"] = False
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _job_object() -> Optional[int]:
    """Windows Job Object: когда агент завершается (даже аварийно), система сама закрывает llama-server — «сирот» не остаётся."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes  # noqa: PLC0415

        k32 = ctypes.windll.kernel32                                           # type: ignore[attr-defined]
        job = k32.CreateJobObjectW(None, None)

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXT()
        info.Basic.LimitFlags = 0x2000                                         # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        return job
    except Exception:  # noqa: BLE001 — запасной вариант: останавливаем сами
        return None


_JOB = None


def _attach(proc: subprocess.Popen) -> None:
    global _JOB
    if sys.platform != "win32":
        return
    try:
        if _JOB is None:
            _JOB = _job_object()
        if _JOB:
            ctypes.windll.kernel32.AssignProcessToJobObject(_JOB, int(proc._handle))        # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


class LlamaRuntime:
    def __init__(self, exe: Path, model_path: Path, plan: catalog.Plan, inst: catalog.Instance, log: Callable[[str], None]) -> None:
        self.exe, self.model_path, self.plan, self.inst, self.log = exe, model_path, plan, inst, log
        self.log_path = paths.logs_dir() / f"llama-server-{inst.kind}.log"
        self.port = free_port()
        self.proc: Optional[subprocess.Popen] = None
        self.minimal = False
        self.tps = 0.0
        self.prompt_tps = 0.0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        log_path = self.log_path
        args = [str(self.exe), *catalog.llama_args(self.plan, self.inst, str(self.model_path), self.port, self.minimal)]
        self.log(f"Запускаю llama-server ({ {'gpu': 'на видеокарте', 'cpu': 'на процессоре', 'hybrid': 'видеокарта + процессор'}.get(self.inst.kind, self.inst.kind) }): " + " ".join(a if " " not in a else f'"{a}"' for a in args[1:]))
        flags = (_CREATE_NO_WINDOW | _BELOW_NORMAL) if sys.platform == "win32" else 0
        self._logf = open(log_path, "wb")
        self.proc = subprocess.Popen(args, stdout=self._logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags, cwd=str(self.exe.parent))
        _attach(self.proc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def log_tail(self, n: int = 12) -> str:
        try:
            data = self.log_path.read_bytes()[-4000:].decode("utf-8", errors="replace")
            return "\n".join(data.strip().splitlines()[-n:])
        except OSError:
            return ""

    def wait_ready(self, timeout: float, cancelled: Callable[[], bool]) -> None:
        """Ждать, пока модель загрузится (llama-server отвечает 503, пока читает файл). Процесс умер или время вышло — LlamaStartError."""
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=3.0, trust_env=False) as c:
            while True:
                if cancelled():
                    raise LlamaStartError("прервано")
                if not self.alive():
                    raise LlamaStartError(f"llama-server завершился (код {self.proc.returncode if self.proc else '?'}): {self.log_tail(4)}")
                try:
                    r = c.get(self.base + "/health")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise LlamaStartError("модель не загрузилась за отведённое время")
                time.sleep(1.0)

    def chat(self, payload: dict[str, Any], timeout: float = 900.0) -> tuple[int, Optional[dict[str, Any]], str]:
        with httpx.Client(timeout=httpx.Timeout(10.0, read=timeout), trust_env=False) as c:
            r = c.post(self.base + "/v1/chat/completions", json=payload)
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, body if isinstance(body, dict) else None, "" if body is not None else r.text[:500]

    def benchmark(self) -> tuple[float, float]:
        """Короткий замер: скорость генерации и чтения «подсказки», токенов в секунду. Сервер выбирает агентов по этой цифре."""
        payload = {"messages": [{"role": "user", "content": "Перечисли через запятую шесть цветов радуги и назови автора картины «Чёрный квадрат»."}], "temperature": 0.0,
                   "max_tokens": 48, "cache_prompt": False, "stream": False, "chat_template_kwargs": {"enable_thinking": False}}
        status, body, _ = self.chat(payload, 300.0)
        t = (body or {}).get("timings") or {}
        if status == 200 and t.get("predicted_per_second"):
            self.tps = round(float(t["predicted_per_second"]), 1)
            self.prompt_tps = round(float(t.get("prompt_per_second") or 0), 1)
        return self.tps, self.prompt_tps

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                try:
                    p.wait(8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(5)
            except OSError:
                pass
        if p and sys.platform == "win32":                       # добить возможных «детей» процесса (скрипт-обёртка, чужая сборка)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, creationflags=_CREATE_NO_WINDOW)
        try:
            self._logf.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc = None
