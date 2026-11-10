"""Фото в чате монтёра с диспетчером. Файлы лежат на диске сервера (data_dir/chat_files/<номер вложения>.<расширение>), а не на
сетевой папке с фото выездов: переписка живёт всего неделю (см. chat_repository.purge_old), потом сообщения и файлы удаляются.
Принимаются только картинки — тип определяется по самому файлу, а не по имени, которое прислал клиент."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from app.config import get_settings

MAX_FILES = 10                       # столько фото в одном сообщении (как вложений в VK)
MAX_FILE_SIZE = 8 * 1024 * 1024
MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


class ChatFileError(ValueError):
    """Файл нельзя принять (не картинка, слишком большой…): текст — для показа человеку."""


def chat_dir() -> Path:
    return get_settings().data_dir / "chat_files"


def sniff_ext(content: bytes) -> Optional[str]:
    """Расширение по началу файла: jpg / png / gif / webp; None — это не поддерживаемая картинка."""
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None


def check(files: list[tuple[str, bytes]]) -> list[tuple[str, str, bytes]]:
    """[(имя, содержимое)] → [(имя, расширение, содержимое)]; ChatFileError, если что-то не подходит."""
    if len(files) > MAX_FILES:
        raise ChatFileError(f"В одном сообщении можно прислать не больше {MAX_FILES} фото")
    out = []
    for name, content in files:
        label = (name or "фото")[:60]
        if not content:
            raise ChatFileError(f"Файл «{label}» пустой")
        if len(content) > MAX_FILE_SIZE:
            raise ChatFileError(f"Файл «{label}» больше {MAX_FILE_SIZE // (1024 * 1024)} МБ")
        ext = sniff_ext(content)
        if not ext:
            raise ChatFileError(f"Файл «{label}» — не картинка (поддерживаются JPEG, PNG, GIF, WebP)")
        out.append((label, ext, content))
    return out


def path_for(att_id: int, ext: str) -> Path:
    return chat_dir() / f"{att_id}.{ext}"


async def save(att_id: int, ext: str, content: bytes) -> None:
    def write() -> None:
        target = path_for(att_id, ext)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    await asyncio.to_thread(write)


async def read(att_id: int, ext: str) -> bytes:
    return await asyncio.to_thread(path_for(att_id, ext).read_bytes)


async def remove(items: list[tuple[int, str]]) -> int:
    """Удаляет файлы вложений [(номер, расширение)]; возвращает, сколько файлов действительно было."""
    def run() -> int:
        removed = 0
        for att_id, ext in items:
            try:
                path_for(att_id, ext).unlink()
                removed += 1
            except FileNotFoundError:
                pass
        return removed

    return await asyncio.to_thread(run)


async def remove_orphans(known_ids: set[int]) -> int:
    """Файлы без строки в базе (человека удалили вместе с перепиской, сбой при записи): удаляются, чтобы папка не копила мусор.
    Свежие файлы (моложе 10 минут) не трогаем: сообщение, которое прямо сейчас сохраняется, ещё не записано в базу."""
    def run() -> int:
        import time

        removed = 0
        folder = chat_dir()
        if not folder.exists():
            return 0
        for path in folder.iterdir():
            stem = path.stem
            if stem.isdigit() and int(stem) not in known_ids and path.suffix[1:] in MEDIA_TYPES:
                try:
                    if time.time() - path.stat().st_mtime < 600:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    return await asyncio.to_thread(run)
