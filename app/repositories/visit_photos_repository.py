"""Photos a fitter sends through the bot for a visit — the file itself
lives on disk under <point_id>/<taken_date>/{id}.{ext} (by default data_dir/visit_photos; with PHOTOS_DIR set — on the
network disk, with a temporary buffer on the server while it is unavailable: see app/services/photo_storage.py),
metadata in the visit_photos table. Follows the same DB-row-first-then-
disk-write convention as report_templates_repository.py.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import zipfile
from collections import deque
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.core.dates import format_date
from app.db.database import get_connection
from app.services import photo_storage

_COLUMNS = "id, task_id, point_id, taken_date, file_ext, uploaded_at, uploaded_by, rel_path, source, file_name"
_KEYS = ["id", "task_id", "point_id", "taken_date", "file_ext", "uploaded_at", "uploaded_by", "rel_path", "source", "file_name"]


def is_imported(photo: dict[str, Any]) -> bool:
    """Фото из прежнего архива — файл найден на сетевом диске, а не загружен через бота (программа его не удаляет)."""
    return photo.get("source") == "import"


def _file_label(photo: dict[str, Any]) -> str:
    """Имя файла в архивах: у прежнего архива — его собственное, у остальных — номер."""
    if is_imported(photo) and photo.get("file_name"):
        return f"{photo['id']}_{photo['file_name']}"
    return f"{photo['id']}.{photo['file_ext']}"


def _row_to_dict(row) -> dict[str, Any]:
    return dict(zip(_KEYS, row))


async def get(photo_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from visit_photos where id = ?", (photo_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_for_task(task_id: str) -> list[dict[str, Any]]:
    """Все фото одного задания (старые первыми) — бот показывает, сколько своих фото монтёр может убрать."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from visit_photos where task_id = ? order by uploaded_at, id", (task_id,)
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def find_by_name(task_id: str, file_name: str) -> Optional[dict[str, Any]]:
    """Фото, уже сохранённое по заданию под таким именем файла (бот называет файл по номеру вложения VK) — чтобы повторная отправка не плодила копии."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from visit_photos where task_id = ? and file_name = ? and coalesce(source, '') = '' order by id limit 1", (task_id, file_name)
        )
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_for_point_date(point_id: str, taken_date: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select {_COLUMNS} from visit_photos where point_id = ? and taken_date = ? order by uploaded_at",
            (point_id, taken_date),
        )
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def counts_for_date(taken_date: str) -> list[dict[str, Any]]:
    """One row per point with at least one photo on this date — point
    address + count — for the /monitoring "Фото по дням" list."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select v.point_id, p.address, count(*)
            from visit_photos v
            left join points p on p.id = v.point_id
            where v.taken_date = ?
            group by v.point_id
            order by p.address
            """,
            (taken_date,),
        )
        rows = await cur.fetchall()
    return [{"point_id": r[0], "point_address": r[1], "count": r[2]} for r in rows]


async def list_days() -> list[dict[str, Any]]:
    """Все дни, за которые есть фото, — новые сверху: число фото и число комплексов (для окна «Фото по дням»)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select taken_date, count(*), count(distinct point_id) from visit_photos "
            "group by taken_date order by taken_date desc"
        )
        rows = await cur.fetchall()
    return [{"date": r[0], "photos": r[1], "points": r[2]} for r in rows]


def _photo_view(row) -> dict[str, Any]:
    """Строка для окна «Фото по дням»: номер, расширение, кто и когда, откуда (прежний архив) и где лежит файл."""
    photo_id, point_id, taken_date, ext, uploaded_at, uploaded_by, rel_path, source, file_name = row
    photo = {"id": photo_id, "point_id": point_id, "taken_date": taken_date, "file_ext": ext, "rel_path": rel_path, "source": source}
    return {"id": photo_id, "ext": ext, "uploaded_at": uploaded_at, "uploaded_by": uploaded_by, "date": taken_date,
            "imported": source == "import", "name": file_name or "", "where": photo_storage.locate_local(photo)}


_VIEW_COLS = "v.id, v.point_id, v.taken_date, v.file_ext, v.uploaded_at, v.uploaded_by, v.rel_path, v.source, v.file_name"


async def list_for_date(taken_date: str) -> list[dict[str, Any]]:
    """Фото одного дня, сгруппированные по комплексам (порядок — по адресу, как в архиве за день)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            f"""
            select {_VIEW_COLS}, p.address
            from visit_photos v
            left join points p on p.id = v.point_id
            where v.taken_date = ?
            order by p.address, v.point_id, v.uploaded_at, v.id
            """,
            (taken_date,),
        )
        rows = await cur.fetchall()

    def build() -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            group = groups.setdefault(row[1], {"point_id": row[1], "point_address": row[9], "photos": []})
            group["photos"].append(_photo_view(row[:9]))
        return list(groups.values())

    return await asyncio.to_thread(build)


async def list_points_with_photos() -> list[dict[str, Any]]:
    """Комплексы, по которым есть фото: сколько, за какие даты, сколько из них из прежнего архива — для вкладки «По комплексам»."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select v.point_id, p.address, count(*), min(v.taken_date), max(v.taken_date),
                   sum(case when v.source = 'import' then 1 else 0 end)
            from visit_photos v
            left join points p on p.id = v.point_id
            group by v.point_id
            order by max(v.taken_date) desc, v.point_id
            """
        )
        rows = await cur.fetchall()
    return [{"point_id": r[0], "point_address": r[1], "count": r[2], "first": r[3], "last": r[4], "archive": r[5] or 0} for r in rows]


async def list_for_point(point_id: str) -> dict[str, Any]:
    """Все фото одного комплекса по дням (новые сверху): для кнопки «Фото» на карточке комплекса."""
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_VIEW_COLS} from visit_photos v where v.point_id = ? order by v.taken_date desc, v.uploaded_at, v.id", (point_id,))
        rows = await cur.fetchall()
        cur = await conn.execute("select address from points where id = ?", (point_id,))
        addr = await cur.fetchone()

    def build() -> list[dict[str, Any]]:
        days: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            days.setdefault(row[2], []).append(_photo_view(row))
        return [{"date": d, "photos": ph} for d, ph in days.items()]

    return {"point_id": point_id, "point_address": addr[0] if addr else "", "total": len(rows), "days": await asyncio.to_thread(build)}


async def delete_one(photo_id: int) -> bool:
    photo = await get(photo_id)
    if not photo:
        return False
    await photo_storage.delete_files(photo)
    async with get_connection() as conn:
        await conn.execute("delete from visit_photos where id = ?", (photo_id,))
        await conn.commit()
    return True


async def counts_for_tasks(task_ids: list[str]) -> dict[str, int]:
    """task_id → сколько фото уже загружено по заданию (для карточки комплекса в боте)."""
    result: dict[str, int] = {}
    ids = [i for i in task_ids if i]
    async with get_connection() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            cur = await conn.execute(
                f"select task_id, count(*) from visit_photos where task_id in ({marks}) group by task_id", chunk
            )
            for task_id, n in await cur.fetchall():
                result[task_id] = n
    return result


async def create(
    task_id: str, point_id: str, taken_date: str, file_ext: str, content: bytes, uploaded_by: Optional[str],
    source: Optional[str] = None, file_name: Optional[str] = None,
) -> dict[str, Any]:
    """`source` — откуда фото («forms» — скачано по ссылке из таблицы Яндекс Форм); обычные фото из бота — без источника.
    Прежний архив («import») программа не удаляет, поэтому ему этот источник давать нельзя."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into visit_photos (task_id, point_id, taken_date, file_ext, uploaded_at, uploaded_by, source, file_name) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, point_id, taken_date, file_ext, now, uploaded_by, source, file_name),
        )
        await conn.commit()
        photo_id = cur.lastrowid

    photo = await get(photo_id)
    assert photo is not None
    # Запись файла идёт отдельной задачей и не прерывается, если отправитель не дождался ответа (бот ждёт ограниченное время, сетевой диск бывает медленным): иначе файл мог
    # остаться недописанным при уже созданной записи, а повторная отправка — задвоить фото.
    store_task = asyncio.ensure_future(photo_storage.store(photo, content))
    try:
        await asyncio.shield(store_task)
    except asyncio.CancelledError:
        store_task.add_done_callback(lambda t, pid=photo_id: asyncio.ensure_future(_drop_row_if_failed(pid, t)))
        raise
    except BaseException:
        # Файл нигде не записался (нет места, диск сервера недоступен) — запись в базе без файла не оставляем.
        await _delete_row(photo_id)
        raise
    return photo


async def _delete_row(photo_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("delete from visit_photos where id = ?", (photo_id,))
        await conn.commit()


async def _drop_row_if_failed(photo_id: int, task: "asyncio.Future[Any]") -> None:
    """Ожидание записи оборвали, а сама запись закончилась неудачей — запись в базе без файла убираем (удачная остаётся)."""
    if task.cancelled() or task.exception() is not None:
        try:
            await _delete_row(photo_id)
        except Exception:  # noqa: BLE001
            pass


async def read_file(photo: dict[str, Any]) -> bytes:
    """Содержимое фото (FileNotFoundError — файла нет нигде, TimeoutError — сетевой диск не отвечает)."""
    return await photo_storage.read(photo)


def _safe_folder_name(name: str) -> str:
    cleaned = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
    return cleaned or "point"


async def list_files_for_date(taken_date: str) -> list[dict[str, Any]]:
    """Все фото одного дня одним запросом (для архива за день): по комплексам, внутри — по времени загрузки."""
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from visit_photos where taken_date = ? order by point_id, uploaded_at, id", (taken_date,))
        return [_row_to_dict(r) for r in await cur.fetchall()]


def download_name(photo: dict[str, Any]) -> str:
    """Имя файла при скачивании одного фото: «<комплекс>_<ДД-ММ-ГГГГ>_<номер>.jpg» (а не безликое photo-123.jpg)."""
    return f"{_safe_folder_name(photo['point_id'])}_{format_date(photo['taken_date'])}_{_file_label(photo)}"


class _ZipSink:
    """Приёмник для zipfile без перемотки: накопленное отдаём кусками, поэтому архив за все дни не собирается
    целиком в памяти и скачивание начинается сразу."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data) -> int:
        self._buf += data
        self._pos += len(data)
        return len(data)

    def flush(self) -> None:
        pass

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


async def list_all_files() -> list[dict[str, Any]]:
    """Все фото (для архива за все дни): комплекс, день, номер и расширение."""
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLUMNS} from visit_photos order by point_id, taken_date, id")
        return [_row_to_dict(r) for r in await cur.fetchall()]


ZIP_WORKERS = 8                 # сколько фото читается одновременно: по сети (SMB) каждое чтение — отдельный обмен, по одному архив шёл со скоростью «один файл за раз»
ZIP_READ_TIMEOUT = 60.0         # одно фото не отдалось за это время (диск завис) — пропускаем его, а не держим всё скачивание


def stream_zip(photos: list[dict[str, Any]], workers: int = ZIP_WORKERS) -> Iterator[bytes]:
    """Архив фото, отдаётся по частям: <комплекс>/<ДД-ММ-ГГГГ>/<файл>. Скачивание начинается сразу (раньше архив за день целиком собирался в памяти, и браузер ждал).
    Фото (JPEG) уже сжаты — кладём без повторного сжатия (раньше оно ещё и занимало главный поток сервера). Читаются фото вперёд пачкой в нескольких потоках, а в архив
    ложатся строго по порядку. Файл, которого нет на диске или который не отдался вовремя, пропускается."""
    sink = _ZipSink()
    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="photo-zip")
    window: deque = deque()
    items = iter(photos)

    def submit_next() -> None:
        photo = next(items, None)
        if photo is not None:
            window.append((photo, pool.submit(photo_storage.read_sync, photo)))

    try:
        for _ in range(max(1, workers) * 2):
            submit_next()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            while window:
                photo, future = window.popleft()
                try:
                    content = future.result(timeout=ZIP_READ_TIMEOUT)
                except Exception:  # noqa: BLE001 — не прочиталось (диск, права, таймаут): этот файл пропускаем, архив идёт дальше
                    content = None
                submit_next()
                if content is None:
                    continue
                zf.writestr(f"{_safe_folder_name(photo['point_id'])}/{format_date(photo['taken_date'])}/{_file_label(photo)}", content)
                chunk = sink.drain()
                if chunk:
                    yield chunk
        tail = sink.drain()
        if tail:
            yield tail
    finally:
        pool.shutdown(wait=False, cancel_futures=True)         # скачивание оборвали — лишние чтения не продолжаем


stream_zip_all = stream_zip      # прежнее имя (архив за все дни) — тот же потоковый архив


async def delete_for_point_date(point_id: str, taken_date: str) -> int:
    """Deletes every photo for one point on one date — files first (best-
    effort), then the DB rows. Returns how many were removed."""
    photos = [p for p in await list_for_point_date(point_id, taken_date) if not is_imported(p)]
    for photo in photos:
        await photo_storage.delete_files(photo)
    async with get_connection() as conn:
        await conn.execute(
            "delete from visit_photos where point_id = ? and taken_date = ? and coalesce(source, '') != 'import'", (point_id, taken_date)
        )
        await conn.commit()
    return len(photos)
