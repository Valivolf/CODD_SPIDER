"""Модели для компьютеров-агентов, которые лежат на самом сервере (папка data/agent_models).

Сервер стоит в закрытой сети без выхода в интернет, а компьютеры сотрудников — как правило, с интернетом. Средние модели (4–8 млрд параметров, несколько разных семейств; список — RECOMMENDED
ниже, он же SERVER_HOSTED в agent/lanai_agent/catalog.py) администратор один раз кладёт в эту папку, и программа-агент скачивает их ОТСЮДА: быстро, без обращения к интернету, по токену и
железу этого компьютера (тот же доступ, что у остальных /api/agent/*). Остальные модели программа скачивает из интернета сама.

Что лежит в папке, сервер находит сам: файлы «*.gguf» (проверяется заголовок «GGUF»; недокачанное и чужие файлы не отдаются). Контрольная сумма SHA-256 считается в фоне при первом показе и
запоминается рядом (sha256.json, пересчёт — если файл изменился): программа сверяет скачанное с ней. Файл отдаётся с поддержкой докачки (Range): оборвалась связь — продолжение с того же места."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings

log = logging.getLogger(__name__)

# Рекомендуемый набор для папки (имена файлов Q4_K_M; размеры в ГБ по 10⁹ байт). Держать в согласии с SERVER_HOSTED в agent/lanai_agent/catalog.py (сверяется тестом).
RECOMMENDED: list[dict[str, Any]] = [
    {"file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "title": "Qwen3 4B Instruct (2507)", "size_gb": 2.50, "source": "huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF"},
    {"file": "gemma-3-4b-it-Q4_K_M.gguf", "title": "Gemma 3 4B", "size_gb": 2.49, "source": "huggingface.co/ggml-org/gemma-3-4b-it-GGUF"},
    {"file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "title": "Qwen2.5 7B", "size_gb": 4.68, "source": "huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF"},
    {"file": "Ministral-3-8B-Instruct-2512-Q4_K_M.gguf", "title": "Ministral 3 8B", "size_gb": 5.20, "source": "huggingface.co/mistralai/Ministral-3-8B-Instruct-2512-GGUF"},
]

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._()+-]{0,118}\.gguf$")
SIDECAR = "sha256.json"
_CHUNK = 1 << 22

_hash_task: Optional[asyncio.Task] = None


def models_dir() -> Path:
    return get_settings().data_dir / "agent_models"


def _is_gguf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"GGUF"
    except OSError:
        return False


def _load_sidecar() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads((models_dir() / SIDECAR).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_sidecar(data: dict[str, dict[str, Any]]) -> None:
    folder = models_dir()
    tmp = folder / (SIDECAR + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, folder / SIDECAR)


def scan() -> list[dict[str, Any]]:
    """Файлы моделей в папке: [{file, size, mtime, valid, sha256}] (sha256 пуст, пока не посчитан)."""
    folder = models_dir()
    if not folder.is_dir():
        return []
    side = _load_sidecar()
    out = []
    for p in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file() or not NAME_RE.match(p.name):
            continue
        st = p.stat()
        known = side.get(p.name) or {}
        fresh = known.get("size") == st.st_size and known.get("mtime") == int(st.st_mtime)
        out.append({"file": p.name, "size": st.st_size, "mtime": int(st.st_mtime), "valid": _is_gguf(p), "sha256": known.get("sha256", "") if fresh else ""})
    return out


def path_for(name: str) -> Optional[Path]:
    """Путь к отдаваемому файлу модели или None (нет такого, имя недопустимое, не GGUF)."""
    if not NAME_RE.match(name or ""):
        return None
    p = (models_dir() / name)
    try:
        if p.resolve().parent != models_dir().resolve() or not p.is_file() or not _is_gguf(p):
            return None
    except OSError:
        return None
    return p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_missing_sync() -> int:
    side = _load_sidecar()
    done = 0
    for item in scan():
        if item["sha256"] or not item["valid"]:
            continue
        path = models_dir() / item["file"]
        try:
            digest = _sha256(path)
            st = path.stat()
        except OSError:
            continue
        if st.st_size != item["size"]:                      # файл менялся, пока считали
            continue
        side[item["file"]] = {"size": st.st_size, "mtime": int(st.st_mtime), "sha256": digest}
        _save_sidecar(side)
        done += 1
        log.info("Модель для компьютеров %s: контрольная сумма посчитана", item["file"])
    return done


def ensure_hashing() -> None:
    """Запускает в фоне расчёт SHA-256 для файлов без него (один расчёт за раз, сервер не задерживается)."""
    global _hash_task
    if _hash_task is not None and not _hash_task.done():
        return
    try:
        _hash_task = asyncio.get_running_loop().create_task(asyncio.to_thread(_hash_missing_sync))
    except RuntimeError:
        _hash_task = None


async def list_models(for_agent: bool = False) -> list[dict[str, Any]]:
    """Модели на сервере. Агенту — только годные файлы GGUF; администратору — все, с пометкой «не GGUF»."""
    items = await asyncio.to_thread(scan)
    if any(i["valid"] and not i["sha256"] for i in items):
        ensure_hashing()
    if for_agent:
        return [{"file": i["file"], "size": i["size"], "sha256": i["sha256"]} for i in items if i["valid"]]
    return items


def remove(name: str) -> bool:
    p = path_for(name) or ((models_dir() / name) if NAME_RE.match(name or "") and (models_dir() / name).is_file() else None)
    if not p:
        return False
    p.unlink()
    side = _load_sidecar()
    if side.pop(name, None) is not None:
        _save_sidecar(side)
    return True
