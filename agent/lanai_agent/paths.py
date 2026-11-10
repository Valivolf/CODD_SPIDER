"""Где агент хранит настройки, программу llama.cpp, модели и журнал. Всё — в профиле пользователя Windows, прав администратора не нужно.
Переменная LANAI_HOME (для проверок) переносит всё в одну папку."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "LanCalendarAIAgent"


def home() -> Path:
    override = os.environ.get("LANAI_HOME")
    if override:
        p = Path(override)
    else:
        p = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_dir() -> Path:
    """Папка со встроенными в программу файлами (сборки llama.cpp): в exe — внутри него (PyInstaller), при запуске из исходников — agent/resources. LANAI_RESOURCES — для проверок."""
    override = os.environ.get("LANAI_RESOURCES")
    if override:
        return Path(override)
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) / "resources" if bundle else Path(__file__).resolve().parent.parent / "resources"


def config_file() -> Path:
    override = os.environ.get("LANAI_HOME")
    if override:
        return home() / "config.json"
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def sub(name: str) -> Path:
    p = home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def bin_dir() -> Path:
    return sub("llama")


def models_dir() -> Path:
    return sub("models")


def logs_dir() -> Path:
    return sub("logs")


def ca_file() -> Path:
    return home() / "server-ca.crt"
