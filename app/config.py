"""Application configuration, loaded from environment / .env."""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Календарь работ"

    # NOTE: the listening host/port are NOT read from here — they're fixed
    # at 0.0.0.0:8000 in docker/Dockerfile's CMD, docker/docker-compose.yml's
    # port mapping, and scripts/Запустить.bat. To change the port, edit all
    # three together; a setting here would be silently ignored by any of them.

    data_dir: Path = ROOT_DIR / "data"
    backup_dir: Path = ROOT_DIR / "backups"
    # Папка, куда примонтирован сетевой диск с фото выездов (в контейнере — /app/photos, см. docker-compose.yml и инструкцию).
    # Не задана — фото лежат на диске сервера (data/visit_photos), как раньше. См. app/services/photo_storage.py.
    photos_dir: Optional[Path] = None
    # Как диск называется для людей («\\NAS\\фото»): показывается в окне «Фото по дням», на работу не влияет.
    photos_share_label: str = ""
    backup_retention: int = 30
    # Сколько секунд перепроверять работавший комплекс, который перестал отвечать, прежде чем считать его неработающим
    # (связь могла пропасть на мгновение). 0 — не ждать, менять статус сразу. См. app/services/point_health_service.py.
    point_down_grace_seconds: int = 120

    # Ключ шифрования ФИО, телефонов персонала, логинов и паролей комплексов в базе (AES-256, 32 байта в base64 — 43 знака). Пусто — ключ создаётся сам
    # и лежит в data/.field_key. Лучше хранить его отдельно от папки данных: в .env рядом с docker-compose. См. app/core/field_crypto.py.
    field_encryption_key: str = ""

    # Bootstrap admin — override in .env for anything beyond local testing.
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # JWT / session cookie.
    secret_key: str = ""
    access_token_expire_minutes: int = 60 * 12
    cookie_name: str = "access_token"
    cookie_secure: bool = False  # set true when served behind HTTPS

    # Shared secret for the монтёр bot's server-to-server calls (see
    # app/api/routes_bot.py, app/core/deps.py::require_bot_key) — the bot
    # has no lan_calendar user session, just this one header. Auto-generated
    # and persisted like secret_key below if not set in .env; the bot's own
    # .env needs the same value.
    bot_api_key: str = ""

    # JavaScript API key for the "Все АПК" map view (Yandex Maps) — free,
    # obtained at https://developer.tech.yandex.ru/, independent of the VK
    # bot token above. Shipped to the browser as-is (these keys are meant
    # to be used client-side, same as any Maps JS SDK key) via apk.html's
    # data-yandex-maps-key attribute. Left blank, the map view shows a
    # "configure YANDEX_MAPS_API_KEY" message instead of failing silently.
    yandex_maps_api_key: str = ""

    # Робот Яндекс.Конструктора карт (docker/docker-compose.yml, сервис yandex-robot,
    # профиль «robot»): адрес сервиса внутри сети compose и общий ключ — как у бота,
    # создаётся и сохраняется приложением (data/robot/.robot_api_key), робот читает тот же файл.
    yandex_robot_url: str = "http://yandex-robot:8100"
    yandex_robot_api_key: str = ""
    # Автообновление карт бригад (app/services/yandex_map_sync.py): как часто проверять бригады (если ничего
    # не менялось; любое действие в календаре проверяет карты сразу) и сколько секунд изменение должно
    # «устояться» (без новых правок), прежде чем карту начнут править.
    yandex_sync_interval_seconds: int = 30
    yandex_sync_settle_seconds: int = 20

    # Локальный ИИ (docker/docker-compose.yml, профиль «ai»; отдельный архив lan-calendar-ai-*.tar): распознавание речи и языковая
    # модель llama.cpp. Адреса — внутри сети compose; не установлены — «ИИ недоступен», остальное приложение работает как обычно.
    # См. app/services/ai/. Включение режимов (голос в боте, помощник) — «Настройки → ИИ», хранится в базе.
    ai_llm_url: str = "http://llm:8080"
    ai_asr_url: str = "http://asr:8000"
    ai_llm_timeout_seconds: int = 1200
    ai_asr_timeout_seconds: int = 900

    # Letterhead text for the printable "План-наряд" work-order document —
    # see app/web/routes_plan_print.py. Edit these in .env to match your
    # organization instead of the printed blank form.
    plan_org_title: str = ""
    plan_approver_name: str = ""
    plan_route_label: str = ""
    plan_safety_officer: str = ""
    # Second ("Согласовано") signature block at the bottom of the form.
    plan_second_approver_title: str = ""
    plan_second_approver_name: str = ""

    @field_validator("photos_dir", mode="before")
    @classmethod
    def _blank_photos_dir(cls, value):
        return None if value is None or not str(value).strip() else value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "calendar.db"


def _write_secret(path: Path, value: str) -> None:
    """Секретный файл (ключ подписи сессий, ключ бота, ключ робота): создаётся с правами 0600 — читать его может только владелец (root в контейнере). Раньше файлы создавались
    с правами по умолчанию (0644), и любой локальный пользователь сервера мог прочитать ключ подписи сессий и выпустить себе сессию администратора."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    _restrict(path)


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:                     # Windows / файловая система без прав — нечего ограничивать
        pass


def _load_or_create_secret_key(settings: Settings) -> str:
    """Persist a generated JWT secret so sessions survive restarts even
    when the operator hasn't set SECRET_KEY in .env."""
    if settings.secret_key:
        return settings.secret_key
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key_file = settings.data_dir / ".secret_key"
    if key_file.exists():
        _restrict(key_file)
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    _write_secret(key_file, key)
    return key


def _load_or_create_bot_api_key(settings: Settings) -> str:
    """Same persistence trick as the JWT secret above, in its own file so
    rotating one doesn't touch the other — copy this value into the bot's
    own .env (LAN_CALENDAR_API_KEY) once generated."""
    if settings.bot_api_key:
        return settings.bot_api_key
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    key_file = settings.data_dir / ".bot_api_key"
    if key_file.exists():
        _restrict(key_file)
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    _write_secret(key_file, key)
    return key


def _load_or_create_robot_api_key(settings: Settings) -> str:
    """Ключ общения приложения с роботом Конструктора карт — тот же приём, что у ключа
    бота. Лежит в отдельной папке data/robot: именно её (только для чтения) монтируют
    роботу, а не весь каталог data с базой."""
    if settings.yandex_robot_api_key:
        return settings.yandex_robot_api_key
    key_dir = settings.data_dir / "robot"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / ".robot_api_key"
    if key_file.exists():
        _restrict(key_file)
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    _write_secret(key_file, key)
    return key


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    settings.secret_key = _load_or_create_secret_key(settings)
    settings.bot_api_key = _load_or_create_bot_api_key(settings)
    settings.yandex_robot_api_key = _load_or_create_robot_api_key(settings)
    return settings
