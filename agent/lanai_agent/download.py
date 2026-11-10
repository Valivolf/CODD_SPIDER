"""Скачивание больших файлов (llama.cpp, модели) с докачкой и безопасная распаковка zip."""
from __future__ import annotations

import hashlib
import os
import ssl
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

CHUNK = 1024 * 256
MAX_UNPACKED = 2 * 1024**3                     # защита от «бомбы»: распакованный архив не больше 2 ГБ


class Cancelled(Exception):
    """Скачивание прервано (агент остановлен или настройки изменились)."""


def sha256_file(path: Path, progress: Optional[Callable[[int, int], None]] = None, stop: Optional[threading.Event] = None) -> str:
    """SHA-256 файла (для проверки модели, скачанной с сервера)."""
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            if stop is not None and stop.is_set():
                raise Cancelled()
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


def download(url: str, dest: Path, *, expected_size: int = 0, progress: Optional[Callable[[int, int], None]] = None, stop: Optional[threading.Event] = None,
             headers: Optional[dict[str, str]] = None, verify: Optional[ssl.SSLContext] = None) -> Path:
    """Скачать url в dest; недокачанное продолжается с того же места (файл .part). expected_size (байт) — для проверки целостности: файл не меньше 97 % ожидаемого."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (not expected_size or dest.stat().st_size >= expected_size * 0.97):
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    hdrs = {"User-Agent": "lan-calendar-ai-agent", **(headers or {})}
    if have:
        hdrs["Range"] = f"bytes={have}-"
    ctx = verify or ssl.create_default_context()          # verify — настройки доверия агента (свой сертификат сервера); без него — обычные системные
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0), verify=ctx, trust_env=True) as client:
        with client.stream("GET", url, headers=hdrs) as resp:
            if resp.status_code == 416:                                   # докачивать нечего — файл уже целый
                os.replace(part, dest)
                return dest
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"Сервер файлов ответил {resp.status_code} на {url.split('?')[0]}")
            if resp.status_code == 200 and have:                          # сервер не поддерживает докачку — качаем сначала
                have = 0
                part.unlink(missing_ok=True)
            total = have + int(resp.headers.get("content-length") or 0)
            done = have
            with open(part, "ab" if have else "wb") as f:
                for chunk in resp.iter_bytes(CHUNK):
                    if stop is not None and stop.is_set():
                        raise Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    size = part.stat().st_size
    if expected_size and size < expected_size * 0.97:
        raise RuntimeError(f"Файл скачан не полностью ({size // 2**20} МБ из {expected_size // 2**20} МБ) — повторите")
    os.replace(part, dest)
    return dest


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """Распаковать zip в dest_dir, не выходя за его пределы (защита от «../») и не распаковывая больше MAX_UNPACKED."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_UNPACKED:
            raise RuntimeError("Архив слишком большой после распаковки")
        for info in z.infolist():
            target = (dest_dir / info.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("Архив содержит недопустимый путь")
        z.extractall(dest_dir)


def find_file(root: Path, name: str) -> Optional[Path]:
    for p in root.rglob(name):
        if p.is_file():
            return p
    return None
