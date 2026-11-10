"""Фото, которые уже лежали на сетевом диске до программы («прежний архив»).

Что делает поиск (раз в час сам, после сохранения настроек диска и по кнопке «Найти фото на диске» в окне «Фото по дням»):
  1. Берёт папки верхнего уровня в «папке со списком комплексов» и сопоставляет каждую с комплексом из справочника —
     без учёта регистра, ё/е, пробелов и знаков, русские и латинские буквы-двойники считаются одинаковыми
     («апк-123» = «АПК 123»; «АПК-123 ул. Ленина» тоже подойдёт). Папки без пары остаются как есть и показываются в окне.
  2. Папки дня, созданные ранее в записи «ГГГГ-ММ-ДД», переименовывает в «ДД-ММ-ГГГГ» (как дата записана везде в программе; если
     папка с новым именем уже есть — файлы переезжают в неё). Фото, лежащие прямо в папке комплекса (без папки с датой),
     раскладывает по папкам-датам «ДД-ММ-ГГГГ» — по дате сохранения файла (время изменения). Файлы переносятся на самом сервере (переименованием, без копирования), существующие не затираются.
     Отключается галочкой в настройках диска.
  3. Каждое найденное фото (в том числе во вложенных папках; расширения .JPG/.jpg равны) заносит в базу — оно появляется в
     «Фото по дням» и «Фото по комплексам»; дата берётся из имени папки/файла, иначе из даты сохранения файла. Сами файлы
     не копируются и не удаляются; пропавшие с диска исчезают из списка при следующем поиске.
Фото, которые программа сама сохранила (номер_комплекса/дата/номер.jpg), повторно не заносятся."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Any, Optional

from app.db.database import get_connection
from app.services import photo_storage as ps

log = logging.getLogger(__name__)

_WALK_TIMEOUT = 600.0         # обход папки одного комплекса, с
_FRESH_SECONDS = 120          # файл, изменённый только что, ещё может копироваться — не трогаем до следующего поиска
_INSERT_BATCH = 300

_state: dict[str, Any] = {}
_task: Optional[asyncio.Task] = None


def _fresh_state() -> dict[str, Any]:
    return {"state": "idle", "auto": False, "started_at": "", "finished_at": "", "folders": 0, "matched": 0, "done": 0,
            "found": 0, "added": 0, "moved": 0, "renamed": 0, "removed": 0, "errors": 0, "message": "",
            "unmatched": [], "unmatched_count": 0, "ambiguous": []}


_state.update(_fresh_state())


def state() -> dict[str, Any]:
    return {**_state, "unmatched": list(_state["unmatched"]), "ambiguous": list(_state["ambiguous"])}


# ------------------------------------------------------------------ сопоставление папок с комплексами

_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")


def tokens(text: str) -> list[str]:
    """Слова и числа в имени без регистра и без знаков: «АПК-123 (ул. Ленина)» → апк, 123, ул, ленина; «Z12» → z, 12."""
    return _TOKEN_RE.findall(ps.fold_name(text))


def _blocked(t: list[str], i: int, j: int) -> bool:
    """Номер комплекса — только часть более длинного номера в имени папки: «12» против «12А» или «А-12» — чужой комплекс."""
    key = t[i:j]
    if key[-1].isdigit() and j < len(t) and t[j].isalpha() and len(t[j]) == 1:      # «12 А»: буква-суффикс номера
        return True
    if key[0].isdigit() and i > 0 and t[i - 1].isalpha() and len(t[i - 1]) == 1:    # «А 12»: буква-префикс номера
        return True
    return False


def match_folders(folders: list[str], point_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    """{папка: номер комплекса} и список «спорных» папок (подходит несколько комплексов).
    Сначала совпадение целиком (без регистра и знаков), затем номер комплекса как отдельные слова внутри имени папки
    (побеждает самое длинное совпадение)."""
    by_compact: dict[str, list[str]] = {}
    by_tokens: dict[tuple[str, ...], list[str]] = {}
    for pid in point_ids:
        t = tuple(tokens(pid))
        if not t:
            continue
        by_compact.setdefault("".join(t), []).append(pid)
        by_tokens.setdefault(t, []).append(pid)
    matches: dict[str, str] = {}
    ambiguous: list[str] = []
    for folder in folders:
        t = tokens(folder)
        if not t:
            continue
        exact = by_compact.get("".join(t))
        if exact:
            if len(set(exact)) == 1:
                matches[folder] = exact[0]
            else:
                ambiguous.append(folder)
            continue
        best: tuple[int, int] = (0, 0)
        best_ids: list[str] = []
        for i in range(len(t)):
            for j in range(i + 1, len(t) + 1):
                hit = by_tokens.get(tuple(t[i:j]))
                if not hit or _blocked(t, i, j):
                    continue
                score = (j - i, sum(len(x) for x in t[i:j]))
                if score > best:
                    best, best_ids = score, hit
                elif score == best:
                    best_ids = best_ids + hit
        if best_ids:
            if len(set(best_ids)) == 1:
                matches[folder] = best_ids[0]
            else:
                ambiguous.append(folder)
    return matches, ambiguous


# ------------------------------------------------------------------ дата фото

_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-._ /](0?[1-9]|1[0-2])[-._ /](0?[1-9]|[12]\d|3[01])(?!\d)")
_DMY = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-._ /](0?[1-9]|1[0-2])[-._ /]((?:19|20)\d{2})(?!\d)")
_YMD8 = re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_DMY8 = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])(0[1-9]|1[0-2])((?:19|20)\d{2})(?!\d)")
_DMY2 = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[-._](0?[1-9]|1[0-2])[-._](\d{2})(?!\d)")


def parse_date(text: str) -> Optional[str]:
    """Дата в имени папки или файла: 2026-10-09, 09.10.2026, 20261009, 09.10.26 → '2026-10-09'. Нет даты — None."""
    last_year = dt.date.today().year + 1
    for rx, order in ((_YMD, "ymd"), (_DMY, "dmy"), (_YMD8, "ymd"), (_DMY8, "dmy"), (_DMY2, "dmy2")):
        for m in rx.finditer(text):
            a, b, c = (int(x) for x in m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, b, a)
            if order == "dmy2":
                y += 2000
            try:
                day = dt.date(y, mo, d)
            except ValueError:
                continue
            if 2000 <= day.year <= last_year:
                return day.isoformat()
    return None


def date_from_mtime(mtime: float) -> str:
    return dt.datetime.fromtimestamp(mtime).date().isoformat()


def photo_date(rel: str, mtime: float) -> str:
    """Дата фото: из имени ближайшей папки с датой, затем из имени файла, иначе — дата сохранения файла."""
    parts = rel.split("/")
    for name in reversed(parts[1:-1]):
        d = parse_date(name)
        if d:
            return d
    stem = parts[-1].rsplit(".", 1)[0]
    return parse_date(stem) or date_from_mtime(mtime)


# ------------------------------------------------------------------ запуск и ход поиска

async def start(auto: bool = False) -> dict[str, Any]:
    """Запустить поиск в фоне. auto=True — плановый запуск: если делать нечего (нет диска, диск недоступен) — молча ничего."""
    global _task
    if _task is not None and not _task.done():
        return state()
    if not ps.configured() or ps._backend is None or ps._backend.kind not in ("smb", "folder"):
        if auto:
            return state()
        raise ps.PhotoStorageError("Сетевой диск не настроен: задайте его в настройках («Настроить сетевой диск»)")
    ok, reason = await ps.share_state(force=not auto)
    if not ok:
        if auto:
            return state()
        raise ps.PhotoStorageError(f"Сетевой диск сейчас недоступен: {reason}")
    _state.update(_fresh_state())
    _state.update(state="running", auto=auto, started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    _task = asyncio.create_task(_run())
    return state()


async def _load_existing() -> tuple[set[str], dict[str, tuple[int, str]]]:
    """Что уже есть в базе: пути фото, которые сохранила программа, и пути найденных ранее файлов (путь → номер записи, комплекс)."""
    app_keys: set[str] = set()
    known: dict[str, tuple[int, str]] = {}
    async with get_connection() as conn:
        cur = await conn.execute("select id, point_id, taken_date, file_ext, rel_path from visit_photos")
        for pid_, point_id, taken_date, ext, rel_path in await cur.fetchall():
            if rel_path:
                known[str(rel_path).casefold()] = (pid_, point_id)
            else:
                for variant in ps.rel_variants({"id": pid_, "point_id": point_id, "taken_date": taken_date, "file_ext": ext}):
                    app_keys.add(variant.casefold())
    return app_keys, known


async def _point_ids() -> list[str]:
    async with get_connection() as conn:
        cur = await conn.execute("select id from points")
        return [r[0] for r in await cur.fetchall()]


def adopt_known_folders(folders: list[str], matches: dict[str, str], ambiguous: list[str],
                        known: dict[str, tuple[int, str]], point_ids: list[str]) -> int:
    """Папка, которая по названию ни к какому комплексу не подходит, но в базе за ней закреплены фото одного комплекса (комплекс переименовали, дубли объединили), — его папка.
    Без этого фото из такой папки при полном обходе считались бы «пропавшими с диска» и убирались из списка. Возвращает, сколько папок опознано."""
    by_cf = {f.casefold(): f for f in folders}
    owners: dict[str, set[str]] = {}
    for rel, (_row_id, point_id) in known.items():
        top = rel.split("/")[0]
        if top in by_cf:
            owners.setdefault(top, set()).add(point_id)
    ids = set(point_ids)
    adopted = 0
    for top, pids in owners.items():
        folder = by_cf[top]
        if folder in matches or folder in ambiguous or len(pids) != 1:
            continue
        pid = next(iter(pids))
        if pid in ids:
            matches[folder] = pid
            adopted += 1
    return adopted


async def _run() -> None:
    be = ps._backend
    try:
        folders = await ps._run_share(be.list_folders, timeout=180)
        point_ids = await _point_ids()
        matches, ambiguous = match_folders(folders, point_ids)
        app_keys, known = await _load_existing()
        adopt_known_folders(folders, matches, ambiguous, known, point_ids)
        unmatched = [f for f in folders if f not in matches and f not in ambiguous]
        _state.update(folders=len(folders), matched=len(matches), unmatched_count=len(unmatched), unmatched=unmatched[:40],
                      ambiguous=ambiguous[:20])
        try:        # прежние папки дня на диске самого сервера (фото до переноса, буфер)
            _state["renamed"] += await asyncio.to_thread(ps.rename_local_date_dirs)
        except OSError as exc:
            log.warning("Поиск фото: папки дня на диске сервера не переименованы (%s)", exc)
        organize = ps.organize_enabled()
        seen: set[str] = set()
        complete = True
        for folder, point_id in matches.items():
            try:
                _state["renamed"] += len(await ps._run_share(be.normalize_date_dirs, folder, timeout=_WALK_TIMEOUT))
                files = await ps._run_share(be.walk_images, folder, timeout=_WALK_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                complete = False
                _state["errors"] += 1
                _state["message"] = f"{folder}: {exc or 'диск не ответил вовремя'}"
                log.warning("Поиск фото: папка «%s» не прочитана (%s)", folder, exc)
                if isinstance(exc, asyncio.TimeoutError):
                    break
                continue
            if organize:
                files = await _organize_root(be, files)
            _state["found"] += len(files)
            await _register(point_id, files, app_keys, known, seen)
            _state["done"] += 1
        if complete:
            await _forget_missing(known, seen)
        _state["state"] = "done" if not _state["errors"] else "finished_with_errors"
    except Exception as exc:  # noqa: BLE001
        log.exception("Поиск фото на сетевом диске оборвался")
        _state.update(state="error", message=str(exc)[:200] or exc.__class__.__name__)
    finally:
        _state["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if _state["added"] or _state["moved"] or _state["removed"] or _state["renamed"]:
            log.info("Поиск фото: добавлено %s, разложено по датам %s, папок дня переименовано %s, убрано %s",
                     _state["added"], _state["moved"], _state["renamed"], _state["removed"])


async def _organize_root(be, files: list[tuple[str, int, float]]) -> list[tuple[str, int, float]]:
    """Фото, лежащие прямо в папке комплекса, → в папки по дате сохранения. Возвращает список файлов с новыми путями."""
    now = time.time()
    pairs: list[tuple[str, str]] = []
    for rel, _size, mtime in files:
        parts = rel.split("/")
        if len(parts) == 2 and now - mtime >= _FRESH_SECONDS:
            pairs.append((rel, f"{parts[0]}/{ps.date_dir(date_from_mtime(mtime))}/{parts[1]}"))
    if not pairs:
        return files
    try:
        results = await ps._run_share(be.move_many, pairs, timeout=120 + 3 * len(pairs))
    except (OSError, asyncio.TimeoutError) as exc:
        _state["errors"] += 1
        _state["message"] = f"не удалось разложить фото по датам: {exc or 'диск не ответил вовремя'}"
        return files
    new_path: dict[str, str] = {}
    for (src, _dst), (final, err) in zip(pairs, results):
        if err is None:
            new_path[src] = final
            _state["moved"] += 1
        else:
            _state["errors"] += 1
            _state["message"] = f"{src}: {err}"
    return [(new_path.get(rel, rel), size, mtime) for rel, size, mtime in files]


_APP_NAME_RE = re.compile(r"^(\d+)\.[A-Za-z0-9]+$")


async def _app_made(candidates: list[tuple[str, str]]) -> set[str]:
    """Из найденных на диске файлов — какие программа сохранила сама (номер_фото.jpg в папке даты). Нужно, когда фото пришло
    от бота уже после того, как поиск прочитал список базы."""
    by_id: dict[int, str] = {}
    for key, rel in candidates:
        parts = rel.split("/")
        m = _APP_NAME_RE.match(parts[-1]) if len(parts) == 3 else None
        if m:
            by_id[int(m.group(1))] = key
    found: set[str] = set()
    if not by_id:
        return found
    ids = list(by_id)
    async with get_connection() as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            cur = await conn.execute(
                f"select id, point_id, taken_date, file_ext from visit_photos where rel_path is null and id in ({','.join('?' for _ in chunk)})", chunk)
            for pid_, point_id, taken_date, ext in await cur.fetchall():
                if by_id[pid_] in {v.casefold() for v in ps.rel_variants({"id": pid_, "point_id": point_id, "taken_date": taken_date, "file_ext": ext})}:
                    found.add(by_id[pid_])
    return found


async def _register(point_id: str, files: list[tuple[str, int, float]], app_keys: set[str],
                    known: dict[str, tuple[int, str]], seen: set[str]) -> None:
    fresh: list[tuple[str, str, int, float]] = []
    relink: list[tuple[str, int]] = []
    dups: list[int] = []
    for rel, size, mtime in files:
        key = rel.casefold()
        alt = ps.flip_date_dirs(rel).casefold()       # тот же файл в записи папки дня «ГГГГ-ММ-ДД» / «ДД-ММ-ГГГГ»
        seen.add(key)
        seen.add(alt)
        hit = key if key in known else (alt if alt in known else None)
        if key in app_keys or alt in app_keys:
            if hit:                             # когда-то занесено как «прежнее», а на деле это фото программы
                dups.append(known[hit][0])
            continue
        if hit:
            if known[hit][1] != point_id:
                relink.append((point_id, known[hit][0]))
            continue
        fresh.append((key, rel, size, mtime))
    app_made = await _app_made([(k, r) for k, r, _s, _m in fresh])
    rows = []
    for key, rel, size, mtime in fresh:
        if key in app_made:
            app_keys.add(key)
            continue
        name = rel.rsplit("/", 1)[-1]
        rows.append((
            "", point_id, photo_date(rel, mtime), name.rsplit(".", 1)[-1].casefold(),
            dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat(), "прежний архив", rel, "import", name, size,
        ))
    if not rows and not relink and not dups:
        return
    async with get_connection() as conn:
        for start in range(0, len(rows), _INSERT_BATCH):
            await conn.executemany(
                "insert or ignore into visit_photos (task_id, point_id, taken_date, file_ext, uploaded_at, uploaded_by, rel_path, source, file_name, file_size) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows[start:start + _INSERT_BATCH])
        if relink:
            await conn.executemany("update visit_photos set point_id = ? where id = ?", relink)
        if dups:
            await conn.executemany("delete from visit_photos where id = ? and source = 'import'", [(d,) for d in dups])
        await conn.commit()
    _state["added"] += len(rows)
    for d in dups:
        for key, (row_id, _p) in list(known.items()):
            if row_id == d:
                del known[key]


async def _forget_missing(known: dict[str, tuple[int, str]], seen: set[str]) -> None:
    """Файлы, которых больше нет на диске (удалены или переименованы человеком) → убрать из списка. Только при полном обходе."""
    gone = [row_id for key, (row_id, _p) in known.items() if key not in seen]
    if not gone:
        return
    async with get_connection() as conn:
        for start in range(0, len(gone), 500):
            chunk = gone[start:start + 500]
            await conn.execute(f"delete from visit_photos where source = 'import' and id in ({','.join('?' for _ in chunk)})", chunk)
        await conn.commit()
    _state["removed"] += len(gone)
