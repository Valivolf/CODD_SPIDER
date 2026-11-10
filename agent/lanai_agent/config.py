"""Настройки агента (JSON в %APPDATA%\\LanCalendarAIAgent\\config.json). Токен агента хранится здесь же — файл доступен только владельцу профиля Windows."""
from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from . import paths

GPU_MODES = ("auto", "off", "force")


@dataclass
class Config:
    server_url: str = ""
    enroll_key: str = ""                 # нужен только для первого подключения; после регистрации стирается
    token: str = ""
    agent_id: str = ""
    name: str = field(default_factory=lambda: (socket.gethostname() or "Компьютер")[:60])
    limit_pct: int = 80                  # доля ресурсов компьютера (процессор, память, видеопамять), которую агент может занять
    gpu_mode: str = "auto"               # auto — использовать, только если карта достаточно сильная; off — никогда; force — всегда (на свой риск)
    model_id: str = "auto"               # auto — подобрать по компьютеру
    run: bool = True                     # включён ли агент (кнопка «Запустить / Остановить»)
    only_idle: bool = False              # работать, только пока владелец не за компьютером
    idle_minutes: int = 5
    autostart: bool = False
    allow_http: bool = False             # только для проверок в своей сети без шифрования
    ca_file: str = ""
    llama_tag: str = "b11815"            # версия llama.cpp (релиз ggml-org/llama.cpp); проверена на этой версии
    llama_exe: str = ""                  # для проверок/своей сборки: готовый llama-server.exe вместо скачанного
    model_path: str = ""                 # для проверок/своей модели: готовый .gguf вместо каталога
    ctx_per_slot: int = 8192
    approved_downloads: list = field(default_factory=list)   # что пользователь разрешил скачать (ключи источников): то же скачивание повторно не спрашивается
    auto_download: bool = False                          # скачивать без вопроса: только консольный режим с --yes-download и проверки; из окна не включается

    def clean(self) -> "Config":
        self.limit_pct = max(10, min(100, int(self.limit_pct or 80)))
        if self.gpu_mode not in GPU_MODES:
            self.gpu_mode = "auto"
        self.idle_minutes = max(1, min(240, int(self.idle_minutes or 5)))
        self.server_url = (self.server_url or "").strip().rstrip("/")
        self.name = " ".join((self.name or "").split())[:60] or "Компьютер"
        self.ctx_per_slot = max(2048, min(32768, int(self.ctx_per_slot or 8192)))
        self.approved_downloads = [str(x)[:200] for x in (self.approved_downloads or []) if x][-60:]
        return self


_lock = threading.Lock()


def load() -> Config:
    cfg = Config()
    try:
        raw = json.loads(paths.config_file().read_text(encoding="utf-8"))
        known = {f.name for f in fields(Config)}
        for k, v in raw.items():
            if k in known:
                setattr(cfg, k, v)
    except (OSError, ValueError):
        pass
    return cfg.clean()


def save(cfg: Config) -> None:
    with _lock:
        path = paths.config_file()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(cfg.clean()), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
