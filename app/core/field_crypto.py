"""Шифрование отдельных полей в базе: логины и пароли комплексов, ФИО и телефоны персонала (и пароль сетевой папки с фото).

В файле базы (и в любой её копии — ежедневные копии, «Копия», перенос на другой сервер) эти значения лежат только в виде
`enc:v1:<base64>` — AES-256-GCM, у каждого значения свой случайный одноразовый номер (nonce), имя поля входит в проверяемые данные,
поэтому значение нельзя подсунуть в другое поле. Ключ хранится НЕ в базе и НЕ в копиях:
  1. переменная окружения FIELD_ENCRYPTION_KEY (32 байта в base64; лучше всего — в .env рядом с docker-compose);
  2. иначе файл <папка данных>/.field_key (создаётся при первом запуске, права 0600).
Без ключа значения расшифровать нельзя — ключ нужно сохранить отдельно (Настройки → «🔑 Ключ шифрования»).

Приложение читает зашифрованные поля через «вычисляемые столбцы» SQLite (full_name = lc_dec(full_name_enc)): весь прежний SQL —
выборки, сортировки, поиск, соединения — работает без изменений, а в самой базе открытого текста нет. Записывают такие поля через
`encrypt()` в столбцы *_enc (см. app/db/field_encryption.py). Расшифровка — на сервере по запросу и только тем ролям, которым положено
(пароли комплексов — администратору и оператору); у остальных ролей поля вычищаются так же, как раньше."""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
KEY_FILE_NAME = ".field_key"
_CACHE_LIMIT = 20000

_lock = threading.Lock()
_key: Optional[bytes] = None
_key_source = ""
_aes: Optional[AESGCM] = None
_cache: dict[tuple[str, str], str] = {}
_failures = 0                         # сколько значений не удалось расшифровать (чужой ключ / повреждение) с запуска


def _key_path() -> Path:
    return Path(get_settings().data_dir) / KEY_FILE_NAME


def _decode_key(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
    if len(raw) != 32:
        raise ValueError("Ключ шифрования должен быть 32 байта (в base64)")
    return raw


def _load_key() -> bytes:
    global _key, _key_source, _aes
    if _key is not None:
        return _key
    with _lock:
        if _key is not None:
            return _key
        env = _env_key()
        if env:
            _key, _key_source = _decode_key(env), "переменная FIELD_ENCRYPTION_KEY"
        else:
            path = _key_path()
            if path.exists():
                _key, _key_source = _decode_key(path.read_text(encoding="utf-8")), f"файл {path.name} в папке данных"
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                new = secrets.token_bytes(32)
                path.write_text(base64.urlsafe_b64encode(new).decode("ascii") + "\n", encoding="utf-8")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                _key, _key_source = new, f"файл {path.name} в папке данных (создан сейчас)"
                logger.warning("Создан ключ шифрования полей: %s. СОХРАНИТЕ его отдельно (Настройки → «Ключ шифрования»): без него зашифрованные "
                               "логины, пароли, ФИО и телефоны не прочитать.", path)
        _aes = AESGCM(_key)
        return _key


def reset_for_tests() -> None:
    """Сбросить загруженный ключ и кэш (тесты меняют папку данных / ключ)."""
    global _key, _aes, _key_source, _failures
    with _lock:
        _key, _aes, _key_source, _failures = None, None, "", 0
        _cache.clear()


def key_source() -> str:
    _load_key()
    return _key_source


def export_key() -> str:
    """Ключ в base64 (для резервной копии ключа администратором)."""
    return base64.urlsafe_b64encode(_load_key()).decode("ascii").rstrip("=")       # 43 знака (без «=» на конце): проще переписать и вставить


def fingerprint() -> str:
    """Короткий отпечаток ключа (8 знаков): по нему видно, что на двух серверах стоит один и тот же ключ, не показывая сам ключ."""
    return hashlib.sha256(b"lc-field-key:" + _load_key()).hexdigest()[:8]


def _env_key() -> str:
    """Ключ из окружения (docker env_file) либо из .env при запуске без Docker (настройки приложения)."""
    return (os.environ.get("FIELD_ENCRYPTION_KEY") or get_settings().field_encryption_key or "").strip()


def env_key_set() -> bool:
    return bool(_env_key())


def can_decrypt_with(key_text: str, field: str, value: str) -> bool:
    """Подходит ли ключ `key_text` к зашифрованному значению (проверка перед сменой ключа)."""
    try:
        raw_key = _decode_key(key_text)
        raw = base64.urlsafe_b64decode(value[len(PREFIX):] + "=" * (-len(value[len(PREFIX):]) % 4))
        AESGCM(raw_key).decrypt(raw[:12], raw[12:], field.encode("utf-8"))
        return True
    except (InvalidTag, ValueError):
        return False


def apply_key(key_text: str) -> None:
    """Поставить другой ключ (файл в папке данных), прежний сохраняется рядом. Вызывающий обязан проверить, что ключ подходит к базе."""
    global _key, _aes, _key_source
    new = _decode_key(key_text)
    path = _key_path()
    with _lock:
        if path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path.replace(path.with_name(f"{KEY_FILE_NAME}.old-{stamp}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.urlsafe_b64encode(new).decode("ascii") + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _key, _key_source, _aes = new, f"файл {path.name} в папке данных", AESGCM(new)
        _cache.clear()


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(field: str, value: Any) -> Any:
    """Пустое и None остаются как есть (пустое поле — это и есть «нет значения»); уже зашифрованное повторно не шифруется."""
    if value is None or value == "" or is_encrypted(value):
        return value
    _load_key()
    assert _aes is not None
    nonce = secrets.token_bytes(12)
    blob = _aes.encrypt(nonce, str(value).encode("utf-8"), field.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(nonce + blob).decode("ascii")


def decrypt(field: str, value: Any) -> Any:
    """Открытый текст (старые данные, пока не перешифрованы) возвращается как есть; не расшифровалось — пустая строка и запись в счётчик сбоев."""
    global _failures
    if not is_encrypted(value):
        return value
    cached = _cache.get((field, value))
    if cached is not None:
        return cached
    _load_key()
    assert _aes is not None
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX):] + "=" * (-len(value[len(PREFIX):]) % 4))
        plain = _aes.decrypt(raw[:12], raw[12:], field.encode("utf-8")).decode("utf-8")
    except (InvalidTag, ValueError):
        _failures += 1
        if _failures in (1, 50, 1000):
            logger.error("Не удалось расшифровать поле %s: ключ шифрования не подходит к базе (база с другого сервера?)", field)
        return ""
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[(field, value)] = plain
    return plain


def failures() -> int:
    return _failures


# ---- SQL-функции для вычисляемых столбцов: lc_dec_<поле>(значение). У каждого поля своя функция (имя поля входит в проверку шифра).
FIELDS = {
    "people.full_name": "lc_dec_pfn",
    "people.phone": "lc_dec_pph",
    "points.login": "lc_dec_plg",
    "points.password": "lc_dec_ppw",
}


def register_sqlite(conn: Any) -> None:
    """Подключить функции расшифровки к обычному соединению sqlite3 (прямые обращения к файлу: проверка копии при восстановлении и т. п.)."""
    for field, fn in FIELDS.items():
        conn.create_function(fn, 1, (lambda v, _f=field: decrypt(_f, v)), deterministic=True)


async def register_aiosqlite(conn: Any) -> None:
    for field, fn in FIELDS.items():
        await conn.create_function(fn, 1, (lambda v, _f=field: decrypt(_f, v)), deterministic=True)
