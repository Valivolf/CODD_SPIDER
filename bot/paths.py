"""Резолвинг путей приложения с учётом запуска из PyInstaller-сборки.

При запуске из .exe (PyInstaller onefile/onedir) sys.executable указывает на
сам exe, а .env должен лежать РЯДОМ с ним, а не во временном каталоге
распаковки (sys._MEIPASS) — иначе он будет теряться между запусками.
"""

import sys
from pathlib import Path


def get_app_dir() -> Path:
    """Каталог, где лежит exe (или корень проекта при запуске из исходников)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # bot/paths.py -> bot/ -> корень проекта
    return Path(__file__).resolve().parent.parent


APP_DIR = get_app_dir()
# Больше не хранит свою БД/PDF-отчёты — lan_calendar единственный источник
# данных (см. bot/api_client.py) — DATA_DIR остаётся только на случай,
# если что-то ещё захочет писать рядом с .env.
DATA_DIR = APP_DIR / "data"
ENV_FILE = APP_DIR / ".env"
