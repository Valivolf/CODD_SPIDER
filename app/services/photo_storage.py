"""Хранилище фото выездов: сетевой диск вместо диска сервера.

Куда пишутся фото, задаётся в приложении («Мониторинг → Фото по дням → Настроить сетевой диск»):
  • SMB — сетевая папка Windows (или Samba/NAS): путь вида \\\\сервер\\папка\\Комплексы, логин и пароль вводятся в настройках;
    приложение подключается само, ничего монтировать на сервере не нужно;
  • «папка на сервере» — PHOTOS_DIR в .env (например, диск, смонтированный на сервере через fstab, NFS и т. п.).
Если ничего не настроено, фото лежат на диске сервера (data/visit_photos), как раньше.

На диске сервера остаётся только временный буфер: если сетевой диск недоступен (выключен, нет сети, сменили пароль), фото не теряются,
а ложатся в data/visit_photos_spool и сами переезжают на диск, когда он вернётся. Уже накопленные на сервере фото переносятся
кнопкой в окне «Фото по дням» — после переноса место на сервере освобождается.

Любое обращение к сетевому диску идёт с таймаутом и в отдельном небольшом пуле потоков: зависший диск не должен подвесить
приложение (сам вызов прервать нельзя, но очередь запросов остаётся живой, а страница отвечает «диск не отвечает»).

Раскладка на диске (одинаковая для SMB и папки): <папка со списком комплексов>/<комплекс>/<гггг-мм-дд>/<номер фото>.<расширение> —
папки комплексов видны и открываются в проводнике как обычно.

Защита «папки на сервере» от тихой записи на диск сервера: пока диск не смонтирован, PHOTOS_DIR — обычная пустая папка сервера,
поэтому хранилищем она считается только с меткой .lan-calendar-photos (кнопка «Подготовить диск»). У SMB такой проблемы нет:
приложение подключается к серверу по сети и, если не вышло, пишет в буфер."""
from __future__ import annotations

import asyncio
import datetime as dt
import errno
import functools
import json
import logging
import os
import re
import shutil
import stat as stat_mod
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.core import field_crypto
from app.core.names import compact_name, fold_name  # noqa: F401 — используются и в photo_index

log = logging.getLogger(__name__)

MARKER = ".lan-calendar-photos"
CONFIG_KEY = "photo_storage"
_PROBE_TTL = 10.0          # как долго верим результату проверки диска, с
_PROBE_TIMEOUT = 25.0      # SMB: соединение + проверка записи
_IO_TIMEOUT = 40.0         # запись/чтение одного фото
_MOVE_TIMEOUT = 180.0      # перенос одного файла (большие фото по медленной сети)
_FLUSH_BATCH = 300         # сколько файлов буфера переносим за проход
_CONNECT_TIMEOUT = 10      # SMB: сколько ждать соединения, с
_INDEX_EVERY = 3600.0      # как часто искать готовые фото в папках комплексов и раскладывать их по датам, с
_INDEX_QUIET_MAX = 2       # поиск ничего не менял несколько раз подряд — интервал удваивается, но не больше чем в 2**2 = 4 раза (до 4 ч); кнопка «Найти фото на диске» работает всегда

# Что считаем фото при поиске в уже существующих папках комплексов (без учёта регистра: .JPG = .jpg).
IMAGE_EXTS = frozenset({"jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "bmp", "heic", "heif", "tif", "tiff"})
# Служебные папки, в которые заглядывать не нужно (миниатюры NAS, корзины, макось).
SKIP_DIRS = frozenset({"@eadir", "#recycle", "$recycle.bin", "__macosx", "thumbs", ".thumbnails", "system volume information"})

# Отдельный пул для обращений к сетевому диску: если он завис, застрять могут только эти потоки.
_share_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="photo-share")


class PhotoStorageError(Exception):
    """Понятная человеку причина, по которой операцию с хранилищем сделать нельзя."""


# ------------------------------------------------------------------ имена

def safe_name(name: str) -> str:
    """Имя папки, допустимое и на SMB-диске, и в проводнике: без \\ / : * ? " < > |, без точек и пробелов по краям."""
    cleaned = "".join(c for c in str(name) if c not in '\\/:*?"<>|').strip(" .")
    return cleaned or "point"


_ISO_DIR = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_DIR = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")


def date_dir(iso: object) -> str:
    """Имя папки дня: «2026-10-09» → «09-10-2026» (так дата записана везде в программе; прежние папки «ГГГГ-ММ-ДД» переименовывает поиск фото)."""
    m = _ISO_DIR.match(str(iso or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else str(iso or "")


def flip_date_dirs(rel: str) -> str:
    """Путь с папками дня в другой записи («ГГГГ-ММ-ДД» ↔ «ДД-ММ-ГГГГ»): файл мог лечь до переименования папок."""
    out = []
    for part in rel.split("/"):
        m = _ISO_DIR.match(part) or _DMY_DIR.match(part)
        out.append(f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else part)
    return "/".join(out)


def _rel(photo: dict[str, Any], safe: bool = True) -> str:
    if photo.get("rel_path"):                       # путь на диске записан как есть (прежний архив, объединённые комплексы)
        return str(photo["rel_path"])
    folder = safe_name(photo["point_id"]) if safe else str(photo["point_id"])
    return f"{folder}/{date_dir(photo['taken_date'])}/{photo['id']}.{photo['file_ext']}"


def rel_variants(photo: dict[str, Any], safe: bool = True) -> list[str]:
    """Где искать файл на диске: по новому имени папки дня и по прежнему («ГГГГ-ММ-ДД»), пока папки не переименованы."""
    main = _rel(photo, safe)
    flipped = flip_date_dirs(main)
    return [main] if flipped == main else [main, flipped]


def _local_path(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))


# ------------------------------------------------------------------ простые файловые операции (диск сервера / смонтированная папка)

def _write_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def _scan_dir(root: Path) -> dict[str, int]:
    files = size = 0
    if root.is_dir():
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if name.startswith(".") or name.endswith(".part"):
                    continue
                files += 1
                try:
                    size += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    return {"files": files, "bytes": size}


def _usage_dir(path: Path) -> Optional[dict[str, int]]:
    try:
        u = shutil.disk_usage(path)
        return {"total": u.total, "free": u.free}
    except OSError:
        return None


# ------------------------------------------------------------------ диски

class LocalBackend:
    """Папка на сервере: смонтированный сетевой диск (PHOTOS_DIR). Без метки хранилищем не считается."""

    kind = "folder"

    def __init__(self, root: Path):
        self.root = root

    def target(self) -> str:
        return str(self.root)

    def probe(self) -> tuple[bool, str]:
        if not self.root.is_dir():
            return False, "папка хранилища недоступна — сетевой диск не подключён к серверу"
        if not (self.root / MARKER).is_file():
            return False, "в папке нет метки хранилища: диск не подключён или ещё не подготовлен (кнопка «Подготовить диск»)"
        test = self.root / f".write-test-{os.getpid()}"
        try:
            test.write_bytes(b"1")
            test.unlink()
        except OSError as exc:
            return False, f"на сетевой диск нельзя записать: {exc.strerror or exc}"
        return True, ""

    def init_marker(self) -> None:
        if not self.root.is_dir():
            raise PhotoStorageError("Папка хранилища недоступна — сначала подключите сетевой диск к серверу")
        (self.root / MARKER).write_text("lan-calendar photo storage\n", encoding="utf-8")

    def write(self, rel: str, content: bytes) -> None:
        _write_file(_local_path(self.root, rel), content)

    def read_first(self, rels: list[str]) -> Optional[bytes]:
        for rel in rels:
            try:
                return _local_path(self.root, rel).read_bytes()
            except OSError:
                continue
        return None

    def delete(self, rels: list[str]) -> int:
        n = 0
        for rel in rels:
            try:
                _local_path(self.root, rel).unlink()
                n += 1
            except OSError:
                continue
        return n

    def put_file(self, src: Path, rel: str) -> None:
        """Копирует файл с диска сервера на диск (через .part), сверяет размер. Исходник не удаляет."""
        size = src.stat().st_size
        dst = _local_path(self.root, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and dst.stat().st_size == size:
            return
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != size:
            tmp.unlink(missing_ok=True)
            raise OSError(f"размер после копирования не совпал: {src.name}")
        os.replace(tmp, dst)

    def scan(self) -> dict[str, int]:
        return _scan_dir(self.root)

    def usage(self) -> Optional[dict[str, int]]:
        return _usage_dir(self.root)

    def sample(self) -> dict[str, Any]:
        names = sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")) if self.root.is_dir() else []
        return {"folders": len(names), "names": names[:8]}

    def normalize_date_dirs(self, top: str) -> list[tuple[str, str]]:
        """Папки дня «ГГГГ-ММ-ДД» в папке комплекса → «ДД-ММ-ГГГГ». Папка с новым именем уже есть — файлы переезжают в неё
        (одноимённые не затираются). Возвращает [(старый путь, новый путь)]."""
        base = _local_path(self.root, top)
        if not base.is_dir():
            return []
        done: list[tuple[str, str]] = []
        for old, new in _iso_dirs_to_rename([p.name for p in base.iterdir() if p.is_dir()]):
            src, dst = base / old, base / new
            try:
                if not dst.exists():
                    src.rename(dst)
                else:
                    for f in [f for f in src.rglob("*") if f.is_file()]:
                        rel_in = f.relative_to(src).as_posix()
                        target = _unique_rel(lambda r: (dst / r).exists(), rel_in)
                        (dst / target).parent.mkdir(parents=True, exist_ok=True)
                        os.replace(f, dst / target)
                    shutil.rmtree(src, ignore_errors=True)
                done.append((f"{top}/{old}", f"{top}/{new}"))
            except OSError as exc:
                log.warning("Не удалось переименовать папку %s: %s", src, exc)
        return done

    def list_folders(self) -> list[str]:
        """Папки верхнего уровня (по одной на комплекс)."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name.casefold() not in SKIP_DIRS)

    def walk_images(self, top: str) -> list[tuple[str, int, float]]:
        """Все фото внутри папки комплекса (рекурсивно): (путь от корня хранилища, размер, время изменения)."""
        out: list[tuple[str, int, float]] = []
        base = _local_path(self.root, top)
        for dirpath, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.casefold() not in SKIP_DIRS]
            for name in names:
                if name.startswith(".") or name.rsplit(".", 1)[-1].casefold() not in IMAGE_EXTS or "." not in name:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = "/".join(Path(full).relative_to(self.root).parts)
                out.append((rel, st.st_size, st.st_mtime))
        return out

    def move_many(self, pairs: list[tuple[str, str]]) -> list[tuple[str, Optional[str]]]:
        """Переложить файлы внутри хранилища: [(откуда, куда)] → [(куда на самом деле, ошибка или None)]. Существующий файл не затирается."""
        out: list[tuple[str, Optional[str]]] = []
        for src, dst in pairs:
            try:
                target = _unique_rel(lambda r: _local_path(self.root, r).exists(), dst)
                _local_path(self.root, target).parent.mkdir(parents=True, exist_ok=True)
                os.replace(_local_path(self.root, src), _local_path(self.root, target))
                out.append((target, None))
            except OSError as exc:
                out.append((dst, str(exc.strerror or exc)))
        return out


def _iso_dirs_to_rename(names: list[str]) -> list[tuple[str, str]]:
    return [(n, date_dir(n)) for n in names if _ISO_DIR.match(n)]


def _unique_rel(exists, rel: str) -> str:
    """Свободное имя рядом с занятым: «a.jpg» → «a (2).jpg»."""
    if not exists(rel):
        return rel
    head, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for n in range(2, 1000):
        cand = f"{head + '/' if head else ''}{stem} ({n}){'.' + ext if ext else ''}"
        if not exists(cand):
            return cand
    raise OSError("не подобрать свободное имя файла")


def _smb():
    try:
        import smbclient  # noqa: PLC0415 — в образе есть; на машине без библиотеки остальное приложение работает
    except ImportError as exc:                                  # pragma: no cover
        raise OSError("не установлена библиотека SMB (smbprotocol)") from exc
    return smbclient


def _is_missing(exc: BaseException) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    text = str(exc)
    return (isinstance(exc, OSError) and exc.errno == errno.ENOENT) or "OBJECT_NAME_NOT_FOUND" in text or "OBJECT_PATH_NOT_FOUND" in text


_NT_LOGON = {0xC000006D, 0xC000006A, 0xC0000064, 0xC000006E, 0xC0000070, 0xC0000071, 0xC0000072, 0xC0000234, 0xC000015B}
_NT_SHARE = {0xC00000CC, 0xC00000BE}
_NT_DENIED = {0xC0000022}


def explain_smb_error(exc: BaseException, server: str = "", share: str = "") -> str:
    """Причина сбоя SMB человеческими словами (по коду статуса сервера, а где его нет — по тексту ошибки)."""
    name, text = type(exc).__name__, str(exc)
    upper = text.upper()
    code = getattr(exc, "ntstatus", None)
    if code is None:
        code = getattr(exc, "status", None)
    if code in _NT_LOGON or "LOGON_FAILURE" in upper or "AUTHENTICATION" in name.upper() or "WRONG_PASSWORD" in upper:
        return "неверный логин или пароль (или учётная запись заблокирована)"
    if "GUEST" in upper:                        # Samba/Windows молча пускают неизвестного пользователя гостем — для записи это отказ
        return "сервер не принял этот логин или пароль (вход получился только гостем) — проверьте логин и пароль"
    if code in _NT_SHARE or "BAD_NETWORK_NAME" in upper or "BADNETWORKNAME" in name.upper():
        return f"общая папка «{share}» не найдена на сервере — проверьте её имя"
    if code in _NT_DENIED or "ACCESS_DENIED" in upper or isinstance(exc, PermissionError):
        return "нет прав: у этого пользователя нет доступа к папке или записи в неё"
    if _is_missing(exc):
        return "папка не найдена на диске — создайте её или поправьте путь"
    if "GETADDRINFO" in upper or "FAILED TO CONNECT" in upper or isinstance(exc, (ConnectionError, TimeoutError)) or "TIMED OUT" in upper or "UNREACHABLE" in upper or "REFUSED" in upper:
        hint = ""
        if server and not re.fullmatch(r"[\d.:]+", server):      # имя, а не IP: из контейнера имена компьютеров Windows часто не определяются
            hint = ". Указано имя компьютера — попробуйте вместо него IP-адрес (например \\\\192.168.1.10\\папка): сервер приложения может не знать это имя"
        return f"сервер {server or ''} недоступен — проверьте имя или IP и сеть{hint}"
    if "SOCKET WAS CLOSED" in upper or "CONNECTIONCLOSED" in name.upper():
        return "связь с сервером оборвалась"
    if "NO SPACE" in upper or "DISK_FULL" in upper or "QUOTA" in upper:
        return "на сетевом диске нет места"
    return f"{name}: {text[:200]}"


def _smb_exists(smb, path: str, kw: dict) -> bool:
    try:
        smb.stat(path, **kw)
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_missing(exc):
            return False
        raise


def _smb_rmtree_empty(smb, path: str, kw: dict) -> None:
    """Убрать опустевшую папку вместе с пустыми подпапками (если внутри что-то осталось — не трогаем)."""
    try:
        for e in smb.scandir(path, **kw):
            if e.is_dir():
                _smb_rmtree_empty(smb, path + "\\" + e.name, kw)
        smb.rmdir(path, **kw)
    except Exception:  # noqa: BLE001 — не пусто или нет прав: пусть останется
        pass


def _to_ts(value: Any) -> float:
    """Время файла из ответа SMB-сервера (datetime в UTC без часового пояса) → секунды."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()
    return float(value or 0)


def _guarded(fn):
    """SMB-ошибки (не только OSError) → OSError с понятным текстом; «нет файла» → FileNotFoundError."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except PhotoStorageError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _is_missing(exc):
                raise FileNotFoundError(str(exc)) from exc
            raise OSError(explain_smb_error(exc, self.server, self.share)) from exc
    return wrapper


class SmbBackend:
    """Сетевая папка Windows/Samba, подключение из самого приложения (\\\\server\\share\\folder), логин и пароль — из настроек."""

    kind = "smb"

    def __init__(self, cfg: dict[str, Any], isolated: bool = False):
        # isolated — собственный пул соединений (для «Проверить подключение»): иначе библиотека подхватит уже открытую сессию
        # того же пользователя, и неверный пароль «пройдёт».
        self._cache: Optional[dict] = {} if isolated else None
        self.server = str(cfg.get("server") or "").strip()
        self.share = str(cfg.get("share") or "").strip()
        self.folder = "/".join(p for p in str(cfg.get("folder") or "").replace("\\", "/").split("/") if p)
        self.port = int(cfg.get("port") or 445)
        self.domain = str(cfg.get("domain") or "").strip()
        self.username = str(cfg.get("username") or "").strip()
        self.password = str(cfg.get("password") or "")

    def target(self) -> str:
        return "\\\\" + self.server + "\\" + self.share + ("\\" + self.folder.replace("/", "\\") if self.folder else "")

    def _kw(self) -> dict[str, Any]:
        user = f"{self.domain}\\{self.username}" if self.domain and self.username else self.username
        kw: dict[str, Any] = {"username": user or None, "password": self.password if user else None, "port": self.port, "connection_timeout": _CONNECT_TIMEOUT}
        if self._cache is not None:
            kw["connection_cache"] = self._cache
        return kw

    def close(self) -> None:
        """Закрыть собственные соединения (только у isolated)."""
        if self._cache is not None:
            try:
                _smb().reset_connection_cache(connection_cache=self._cache)
            except Exception:  # noqa: BLE001
                pass

    def _unc(self, rel: str = "") -> str:
        parts = ["\\\\" + self.server, self.share]
        if self.folder:
            parts.append(self.folder.replace("/", "\\"))
        if rel:
            parts.append(rel.replace("/", "\\"))
        return "\\".join(parts)

    share_names: list[str] = []         # что сервер показал этому пользователю (после неудачной проверки имени общей папки)
    share_fixed = False                 # имя общей папки поправлено по списку сервера (регистр, пробелы, буквы-двойники)

    def _find_share(self, smb, kw) -> Optional[str]:
        """Общая папка не найдена по имени — спрашиваем у сервера список и ищем то же имя в другом написании. None — не нашли."""
        from app.services import smb_shares  # noqa: PLC0415

        try:
            names = smb_shares.list_shares(self.server, smb, **kw)
        except Exception as exc:  # noqa: BLE001 — список общих папок отдают не все серверы; без него остаётся прежнее сообщение
            log.info("Не удалось получить список общих папок сервера %s: %s", self.server, exc)
            names = []
        self.share_names = names
        same = [n for n in names if compact_name(n) == compact_name(self.share)]
        return same[0] if len(same) == 1 else None

    def _share_missing_text(self) -> str:
        text = f"общая папка «{self.share}» не найдена на сервере"
        if self.share_names:
            shown = ", ".join(f"«{n}»" for n in self.share_names[:12])
            return f"{text}. Сервер показывает этому пользователю: {shown}. Впишите одну из них (если нужной тут нет — у пользователя нет к ней доступа)"
        return f"{text} — проверьте её имя (и что у этого пользователя есть к ней доступ)"

    def probe(self) -> tuple[bool, str]:
        try:
            smb = _smb()
            kw = self._kw()
            try:
                smb.stat("\\\\" + self.server + "\\" + self.share, **kw)
            except Exception as exc:  # noqa: BLE001 — общей папки нет: у Samba и Windows для этого разные коды, смотрим на сам факт
                if not (_is_missing(exc) or getattr(exc, "ntstatus", None) in _NT_SHARE):
                    raise
                fixed = self._find_share(smb, kw)
                if fixed is None:
                    return False, self._share_missing_text()
                self.share, self.share_fixed = fixed, True
                smb.stat("\\\\" + self.server + "\\" + self.share, **kw)
            base = self._unc()
            if not stat_mod.S_ISDIR(smb.stat(base, **kw).st_mode):
                return False, "указанный путь — не папка"
            test = base + f"\\.write-test-{os.getpid()}"
            with smb.open_file(test, mode="wb", **kw) as f:
                f.write(b"1")
            smb.remove(test, **kw)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, explain_smb_error(exc, self.server, self.share)

    @_guarded
    def write(self, rel: str, content: bytes) -> None:
        smb, kw = _smb(), self._kw()
        final = self._unc(rel)
        smb.makedirs(final.rsplit("\\", 1)[0], exist_ok=True, **kw)
        part = final + ".part"
        with smb.open_file(part, mode="wb", **kw) as f:
            f.write(content)
        smb.replace(part, final, **kw)

    @_guarded
    def read_first(self, rels: list[str]) -> Optional[bytes]:
        smb, kw = _smb(), self._kw()
        for rel in rels:
            try:
                with smb.open_file(self._unc(rel), mode="rb", **kw) as f:
                    return f.read()
            except Exception as exc:  # noqa: BLE001
                if _is_missing(exc):
                    continue
                raise
        return None

    @_guarded
    def delete(self, rels: list[str]) -> int:
        smb, kw = _smb(), self._kw()
        n = 0
        for rel in rels:
            try:
                smb.remove(self._unc(rel), **kw)
                n += 1
            except Exception as exc:  # noqa: BLE001
                if not _is_missing(exc):
                    raise
        return n

    @_guarded
    def put_file(self, src: Path, rel: str) -> None:
        smb, kw = _smb(), self._kw()
        size = src.stat().st_size
        final = self._unc(rel)
        try:
            if smb.stat(final, **kw).st_size == size:
                return
        except Exception as exc:  # noqa: BLE001
            if not _is_missing(exc):
                raise
        smb.makedirs(final.rsplit("\\", 1)[0], exist_ok=True, **kw)
        part = final + ".part"
        with open(src, "rb") as fin, smb.open_file(part, mode="wb", **kw) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
        if smb.stat(part, **kw).st_size != size:
            smb.remove(part, **kw)
            raise OSError(f"размер после копирования не совпал: {src.name}")
        smb.replace(part, final, **kw)

    @_guarded
    def scan(self) -> dict[str, int]:
        smb, kw = _smb(), self._kw()
        files = size = 0
        stack = [self._unc()]
        while stack:
            cur = stack.pop()
            for entry in smb.scandir(cur, **kw):
                if entry.name.startswith(".") or entry.name.endswith(".part"):
                    continue
                if entry.is_dir():
                    stack.append(cur + "\\" + entry.name)
                else:
                    files += 1
                    # entry.stat() библиотеки ходит без порта и логина — спрашиваем размер явно, с параметрами подключения
                    size += smb.stat(cur + "\\" + entry.name, **kw).st_size
        return {"files": files, "bytes": size}

    def usage(self) -> Optional[dict[str, int]]:
        try:
            v = _smb().stat_volume("\\\\" + self.server + "\\" + self.share, **self._kw())
            return {"total": int(v.total_size), "free": int(v.caller_available_size)}
        except Exception:  # noqa: BLE001 — не все серверы отдают сведения о томе
            return None

    @_guarded
    def sample(self) -> dict[str, Any]:
        smb, kw = _smb(), self._kw()
        names = sorted(e.name for e in smb.scandir(self._unc(), **kw) if e.is_dir() and not e.name.startswith("."))
        return {"folders": len(names), "names": names[:8]}

    @_guarded
    def normalize_date_dirs(self, top: str) -> list[tuple[str, str]]:
        """Папки дня «ГГГГ-ММ-ДД» в папке комплекса → «ДД-ММ-ГГГГ» (переименование на сервере, без копирования). Папка с новым
        именем уже есть — файлы переезжают в неё, одноимённые не затираются. Возвращает [(старый путь, новый путь)]."""
        smb, kw = _smb(), self._kw()
        base = self._unc(top)
        try:
            names = [e.name for e in smb.scandir(base, **kw) if e.is_dir()]
        except Exception as exc:  # noqa: BLE001
            if _is_missing(exc):
                return []
            raise
        done: list[tuple[str, str]] = []
        for old, new in _iso_dirs_to_rename(names):
            src, dst = base + "\\" + old, base + "\\" + new
            try:
                try:
                    smb.stat(dst, **kw)
                    exists = True
                except Exception as exc:  # noqa: BLE001
                    if not _is_missing(exc):
                        raise
                    exists = False
                if not exists:
                    smb.rename(src, dst, **kw)
                else:
                    stack = [(src, "")]
                    while stack:
                        cur, prefix = stack.pop()
                        for e in smb.scandir(cur, **kw):
                            if e.is_dir():
                                stack.append((cur + "\\" + e.name, prefix + e.name + "/"))
                                continue
                            rel_in = prefix + e.name
                            target = _unique_rel(lambda r: _smb_exists(smb, dst + "\\" + r.replace("/", "\\"), kw), rel_in)
                            smb.makedirs((dst + "\\" + target.replace("/", "\\")).rsplit("\\", 1)[0], exist_ok=True, **kw)
                            smb.rename(cur + "\\" + e.name, dst + "\\" + target.replace("/", "\\"), **kw)
                    _smb_rmtree_empty(smb, src, kw)
                done.append((f"{top}/{old}", f"{top}/{new}"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось переименовать папку %s: %s", src, explain_smb_error(exc, self.server, self.share))
        return done

    @_guarded
    def list_folders(self) -> list[str]:
        """Папки верхнего уровня (по одной на комплекс)."""
        smb, kw = _smb(), self._kw()
        return sorted(e.name for e in smb.scandir(self._unc(), **kw)
                      if e.is_dir() and not e.name.startswith(".") and e.name.casefold() not in SKIP_DIRS)

    @_guarded
    def walk_images(self, top: str) -> list[tuple[str, int, float]]:
        """Все фото внутри папки комплекса (рекурсивно): (путь от корня хранилища, размер, время изменения)."""
        smb, kw = _smb(), self._kw()
        out: list[tuple[str, int, float]] = []
        stack = [(self._unc(top), top, 0)]
        while stack:
            path, rel, depth = stack.pop()
            for e in smb.scandir(path, **kw):
                name = e.name
                if name.startswith("."):
                    continue
                if e.is_dir():
                    if depth < 10 and name.casefold() not in SKIP_DIRS:
                        stack.append((path + "\\" + name, rel + "/" + name, depth + 1))
                    continue
                if "." not in name or name.rsplit(".", 1)[-1].casefold() not in IMAGE_EXTS:
                    continue
                info = getattr(e, "smb_info", None)               # размер и время уже пришли со списком папки — отдельных запросов нет
                if info is not None:
                    size, mtime = int(info.end_of_file), _to_ts(info.last_write_time)
                else:                                              # подставной клиент в тестах
                    st = e.stat()
                    size, mtime = st.st_size, st.st_mtime
                out.append((rel + "/" + name, size, mtime))
        return out

    def move_many(self, pairs: list[tuple[str, str]]) -> list[tuple[str, Optional[str]]]:
        """Переложить файлы внутри общей папки (на сервере, без копирования): [(откуда, куда)] → [(куда на самом деле, ошибка или None)].
        Существующий файл с таким именем не затирается: к новому имени добавляется « (2)»."""
        smb, kw = _smb(), self._kw()

        def exists(rel: str) -> bool:
            try:
                smb.stat(self._unc(rel), **kw)
                return True
            except Exception as exc:  # noqa: BLE001
                if _is_missing(exc):
                    return False
                raise

        out: list[tuple[str, Optional[str]]] = []
        for src, dst in pairs:
            try:
                target = _unique_rel(exists, dst)
                smb.makedirs(self._unc(target).rsplit("\\", 1)[0], exist_ok=True, **kw)
                smb.rename(self._unc(src), self._unc(target), **kw)
                out.append((target, None))
            except Exception as exc:  # noqa: BLE001
                out.append((dst, explain_smb_error(exc, self.server, self.share)))
        return out


# ------------------------------------------------------------------ настройки (в базе) и выбор диска

_backend: Optional[Any] = None
_cfg: dict[str, Any] = {}       # сохранённые настройки SMB (с паролем — наружу не отдаются)


_QUOTES = "\"'`«»“”„‘’‹›"


def _clean_path_text(path: str) -> str:
    """Привести вставленный путь к виду, который понимает проводник: убрать то, что приезжает при копировании.
    Невидимые символы (метки направления текста, нулевой ширины — их добавляет Windows в «Свойства → Безопасность»),
    неразрывные пробелы (из чатов и документов → обычный пробел), кавычки вокруг пути («Копировать как путь»)."""
    out = []
    for ch in str(path or ""):
        cat = unicodedata.category(ch)
        if cat == "Cf" or ch == "﻿":
            continue                              # невидимый формат-символ
        if cat == "Zs" and ch != " ":
            ch = " "                              # неразрывный и прочие пробелы
        elif cat in ("Zl", "Zp") or ch in "\r\n\t":
            ch = " "
        out.append(ch)
    text = "".join(out).strip()
    while len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
        text = text[1:-1].strip()
    return text.strip(_QUOTES + " ")


def parse_share_path(path: str) -> tuple[str, str, str]:
    """«\\\\сервер\\папка\\Комплексы\\Фото», «//сервер/папка/…», «"\\\\сервер\\папка"» (в кавычках, как копирует Windows) или
    «smb://сервер/папка/…» (в том числе с %20 вместо пробелов) → (сервер, общая папка, путь внутри неё). Русские имена и пробелы допустимы."""
    text = _clean_path_text(path)
    low = text.lower().replace("\\", "/")
    for prefix in ("smb://", "cifs://", "file://"):
        if low.startswith(prefix):
            text = urllib.parse.unquote(text[len(prefix):])      # в адресной строке пробелы и кириллица приходят как %20, %D0…
            break
    text = text.replace("\\", "/")
    if text.lower().startswith("//?/unc/"):
        text = text[8:]
    if re.match(r"^[A-Za-z]:(/|$)", text):
        raise PhotoStorageError(
            f"«{text[:2]}» — это буква сетевого диска на вашем компьютере, а приложение стоит на сервере и её не знает. "
            "Нужен полный сетевой путь вида \\\\сервер\\общая_папка\\папка: в проводнике нажмите правой кнопкой на диске → «Свойства» "
            "(там видно «\\\\сервер\\папка»), либо выполните в командной строке «net use».")
    parts = [p.strip(" ") for p in text.split("/")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        raise PhotoStorageError("Путь должен выглядеть так: \\\\сервер\\общая_папка\\папка_с_комплексами (например \\\\192.168.1.10\\photos\\Комплексы)")
    server, share, folder = parts[0], parts[1], parts[2:]
    if any(p in (".", "..") or any(ord(c) < 32 for c in p) for p in parts):
        raise PhotoStorageError("В пути есть недопустимые символы")
    return server, share, "/".join(folder)


def build_backend(cfg: Optional[dict[str, Any]]) -> Optional[Any]:
    if cfg and cfg.get("mode") == "smb" and cfg.get("server") and cfg.get("share"):
        return SmbBackend(cfg)
    s = get_settings()
    if s.photos_dir is not None:
        return LocalBackend(Path(s.photos_dir))
    return None


PASSWORD_FIELD = "photo_storage.password"


def _dump_cfg(cfg: dict[str, Any]) -> str:
    """Настройки в JSON для базы: пароль — зашифрованным."""
    out = dict(cfg)
    if out.get("password"):
        out["password"] = field_crypto.encrypt(PASSWORD_FIELD, out["password"])
    return json.dumps(out, ensure_ascii=False)


async def load_config() -> None:
    """Читает настройки из базы и выбирает диск (при запуске и после сохранения настроек)."""
    global _backend, _cfg
    from app.db.database import get_connection  # noqa: PLC0415

    async with get_connection() as conn:
        cur = await conn.execute("select value from app_flags where key = ?", (CONFIG_KEY,))
        row = await cur.fetchone()
    try:
        _cfg = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        _cfg = {}
    stored = _cfg.get("password")
    if stored:
        # пароль сетевой папки в базе лежит зашифрованным (app/core/field_crypto.py); в памяти — открытый, как и прежде
        _cfg["password"] = field_crypto.decrypt(PASSWORD_FIELD, stored)
        if not field_crypto.is_encrypted(stored):
            async with get_connection() as conn:                 # настройки, сохранённые старой версией, — перезаписываем зашифрованными
                await conn.execute("update app_flags set value = ? where key = ?", (_dump_cfg(_cfg), CONFIG_KEY))
                await conn.commit()
    _backend = build_backend(_cfg)
    _probe.update(at=0.0)
    try:
        _smb().reset_connection_cache()
    except OSError:
        pass


async def save_config(data: dict[str, Any]) -> None:
    """Сохранить настройки SMB. data: mode ('smb' | 'none'), path, port, domain, username, password (пустой — не менять)."""
    from app.db.database import get_connection  # noqa: PLC0415

    mode = str(data.get("mode") or "none")
    if mode not in ("smb", "none"):
        raise PhotoStorageError("Неизвестный способ хранения")
    new = dict(_cfg)
    new["mode"] = mode
    if mode == "smb":
        server, share, folder = parse_share_path(data.get("path"))
        port = int(data.get("port") or 445)
        if not 1 <= port <= 65535:
            raise PhotoStorageError("Порт — число от 1 до 65535")
        new.update(server=server, share=share, folder=folder, port=port, domain=str(data.get("domain") or "").strip(),
                   username=str(data.get("username") or "").strip())
        if str(data.get("password") or ""):
            new["password"] = str(data["password"])
        if data.get("organize") is not None:
            new["organize"] = bool(data["organize"])
        # Имя общей папки сверяем со списком сервера: поправим регистр, пробелы и русские/латинские буквы-двойники. Не вышло — сохраняем как есть.
        probe_be = SmbBackend({**new, "mode": "smb"}, isolated=True)
        try:
            await _run_share(probe_be.probe, timeout=_PROBE_TIMEOUT)
            if probe_be.share_fixed and probe_be.share != new["share"]:
                log.info("Хранилище фото: имя общей папки «%s» исправлено на «%s» по списку сервера", new["share"], probe_be.share)
                new["share"] = probe_be.share
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            await asyncio.to_thread(probe_be.close)
    async with get_connection() as conn:
        await conn.execute(
            "insert into app_flags (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
            (CONFIG_KEY, _dump_cfg(new)),
        )
        await conn.commit()
    await load_config()


def config_view() -> dict[str, Any]:
    """Настройки для окна администратора — без пароля."""
    s = get_settings()
    return {
        "mode": _cfg.get("mode") or "none",
        "path": ("\\\\" + _cfg["server"] + "\\" + _cfg["share"] + ("\\" + _cfg["folder"].replace("/", "\\") if _cfg.get("folder") else ""))
        if _cfg.get("server") and _cfg.get("share") else "",
        "port": int(_cfg.get("port") or 445), "domain": _cfg.get("domain") or "", "username": _cfg.get("username") or "",
        "password_set": bool(_cfg.get("password")), "env_folder": str(s.photos_dir) if s.photos_dir is not None else "",
        "organize": organize_enabled(),
    }


def organize_enabled() -> bool:
    """Раскладывать ли фото из корня папки комплекса по папкам-датам (по умолчанию да)."""
    return bool(_cfg.get("organize", True))


async def test_connection(data: dict[str, Any]) -> dict[str, Any]:
    """Проверка подключения с теми настройками, что введены в окне (ничего не сохраняет)."""
    server, share, folder = parse_share_path(data.get("path"))
    cfg = {"mode": "smb", "server": server, "share": share, "folder": folder, "port": data.get("port") or 445,
           "domain": data.get("domain") or "", "username": data.get("username") or "",
           "password": str(data.get("password") or "") or _cfg.get("password", "")}
    be = SmbBackend(cfg, isolated=True)
    try:
        try:
            ok, reason = await _run_share(be.probe, timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            ok, reason = False, f"сервер {server} не отвечает (таймаут)"
        res: dict[str, Any] = {"ok": ok, "message": reason, "target": be.target(), "server": server, "share": be.share, "folder": folder,
                               "shares": be.share_names, "share_fixed": be.share_fixed}
        if ok:
            try:
                res.update(await _run_share(be.sample, timeout=_IO_TIMEOUT))
            except (OSError, asyncio.TimeoutError):
                pass
            try:
                res["disk"] = await _run_share(be.usage, timeout=10)
            except asyncio.TimeoutError:
                res["disk"] = None
        return res
    finally:
        await asyncio.to_thread(be.close)


# ------------------------------------------------------------------ состояние диска

def configured() -> bool:
    return _backend is not None


def spool_root() -> Path:
    return get_settings().data_dir / "visit_photos_spool"


def legacy_root() -> Path:
    """Где фото лежали до выноса на сетевой диск (и лежат, пока он не настроен)."""
    return get_settings().data_dir / "visit_photos"


_probe: dict[str, Any] = {"at": 0.0, "ok": True, "reason": "", "running": False}


async def _run_share(fn, *args, timeout: float):
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_share_pool, functools.partial(fn, *args)), timeout)


async def share_state(force: bool = False) -> tuple[bool, str]:
    """(доступен ли сетевой диск, почему нет). Диск не настроен — всегда «да» (всё на диске сервера)."""
    be = _backend
    if be is None:
        return True, ""
    now = time.monotonic()
    if not force and now - _probe["at"] < _PROBE_TTL:
        return _probe["ok"], _probe["reason"]
    if _probe["running"]:                       # прошлая проверка ещё висит на диске — не плодим ожидающих
        return False, "диск не отвечает"
    _probe["running"] = True
    try:
        ok, reason = await _run_share(be.probe, timeout=_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        ok, reason = False, "диск не отвечает (таймаут)"
    except OSError as exc:
        ok, reason = False, f"ошибка диска: {exc}"
    finally:
        _probe["running"] = False
    was = _probe["ok"]
    _probe.update(at=time.monotonic(), ok=ok, reason=reason)
    if ok != was:
        (log.info if ok else log.warning)("Хранилище фото: %s", "сетевой диск доступен" if ok else f"сетевой диск недоступен — {reason}")
    return ok, reason


async def share_available() -> bool:
    return (await share_state())[0]


def _mark_unavailable(reason: str) -> None:
    _probe.update(at=time.monotonic(), ok=False, reason=reason)


# ------------------------------------------------------------------ запись / чтение / удаление

def _local_candidates(photo: dict[str, Any]) -> list[Path]:
    roots = [spool_root(), legacy_root()] if configured() else [legacy_root()]
    out: list[Path] = []
    for root in roots:
        for safe in (True, False):
            for rel in rel_variants(photo, safe):
                p = _local_path(root, rel)
                if p not in out:
                    out.append(p)
    return out


async def store(photo: dict[str, Any], content: bytes) -> str:
    """Сохранить файл фото: 'share' — на сетевой диск, 'spool' — во временный буфер сервера, 'local' — диск не настроен."""
    be = _backend
    if be is None:
        try:
            await asyncio.to_thread(_write_file, _local_path(legacy_root(), _rel(photo, False)), content)
        except OSError as exc:
            if exc.errno != errno.EINVAL:        # имя комплекса недопустимо для файловой системы сервера — пишем под безопасным именем
                raise
            await asyncio.to_thread(_write_file, _local_path(legacy_root(), _rel(photo)), content)
        return "local"
    if await share_available():
        try:
            await _run_share(be.write, _rel(photo), content, timeout=_IO_TIMEOUT)
            return "share"
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("Хранилище фото: запись на сетевой диск не удалась (%s) — фото ушло в буфер сервера", exc)
            _mark_unavailable("запись на сетевой диск не удалась")
    await asyncio.to_thread(_write_file, _local_path(spool_root(), _rel(photo)), content)
    return "spool"


def _read_first_local(paths: list[Path]) -> Optional[bytes]:
    for p in paths:
        try:
            return p.read_bytes()
        except OSError:          # нет файла, недопустимое для этой файловой системы имя папки и т. п. — пробуем следующее место
            continue
    return None


async def read(photo: dict[str, Any]) -> bytes:
    """Содержимое фото. FileNotFoundError — файла нигде нет; TimeoutError — сетевой диск не отвечает."""
    found = await asyncio.to_thread(_read_first_local, _local_candidates(photo))
    if found is not None:
        return found
    be = _backend
    if be is not None:
        try:
            found = await _run_share(be.read_first, rel_variants(photo), timeout=_IO_TIMEOUT * 2)
        except asyncio.TimeoutError as exc:
            _mark_unavailable("диск не отвечает")
            raise TimeoutError("Сетевой диск с фото не отвечает") from exc
        except FileNotFoundError:
            found = None
        except OSError as exc:           # диск недоступен (сервер выключен, обрыв связи, сменили пароль) — это не «файла нет»
            _mark_unavailable(str(exc))
            raise TimeoutError(f"Сетевой диск с фото недоступен: {exc}") from exc
        if found is not None:
            return found
    raise FileNotFoundError(photo["id"])


def locate_local(photo: dict[str, Any]) -> str:
    """Где лежит файл фото, если смотреть только диск самого сервера (быстро, без обращений к сети):
    'server' — файл на диске сервера (ещё не перенесён на сетевой диск), 'buffer' — во временном буфере (диск был недоступен),
    'disk' — на сетевом диске (или хранилище не настроено и фото нет на сервере: тогда его просто нет)."""
    if photo.get("source") == "import":
        return "disk"
    for p in _local_candidates(photo):
        try:
            if p.is_file():
                return "buffer" if spool_root() in p.parents else "server"
        except OSError:
            continue
    return "disk" if configured() else "missing"


def read_sync(photo: dict[str, Any]) -> Optional[bytes]:
    """То же, синхронно — для архива «за все дни», который собирается в потоке. None — файла нет."""
    found = _read_first_local(_local_candidates(photo))
    if found is not None:
        return found
    if _backend is not None:
        try:
            return _backend.read_first(rel_variants(photo))
        except OSError:
            return None
    return None


async def delete_files(photo: dict[str, Any]) -> None:
    """Убрать файл фото отовсюду, где он лежит. Если сетевой диск не отвечает — файл на нём остаётся, запись из базы всё равно уйдёт.
    Файлы прежнего архива (найденные на диске, а не загруженные через бота) программа не удаляет."""
    if photo.get("source") == "import":
        return

    def _del_local() -> None:
        for p in _local_candidates(photo):
            try:
                p.unlink()
            except OSError:
                continue
    await asyncio.to_thread(_del_local)
    be = _backend
    if be is not None:
        try:
            await _run_share(be.delete, rel_variants(photo), timeout=_IO_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("Хранилище фото: не удалось удалить файл фото %s с сетевого диска (%s)", photo.get("id"), exc)


def rename_local_date_dirs() -> int:
    """Папки «ГГГГ-ММ-ДД» на диске самого сервера (прежние фото и буфер) → «ДД-ММ-ГГГГ». Возвращает, сколько переименовано."""
    done = 0
    for root in (legacy_root(), spool_root()):
        if not root.is_dir():
            continue
        for point_dir in [p for p in root.iterdir() if p.is_dir()]:
            for old, new in _iso_dirs_to_rename([p.name for p in point_dir.iterdir() if p.is_dir()]):
                src, dst = point_dir / old, point_dir / new
                try:
                    if not dst.exists():
                        src.rename(dst)
                    else:
                        for f in [f for f in src.rglob("*") if f.is_file()]:
                            target = dst / _unique_rel(lambda r: (dst / r).exists(), f.relative_to(src).as_posix())
                            target.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(f, target)
                        shutil.rmtree(src, ignore_errors=True)
                    done += 1
                except OSError as exc:
                    log.warning("Не удалось переименовать папку %s: %s", src, exc)
    return done


# ------------------------------------------------------------------ перенос: буфер → диск, старые фото → диск

def _iter_files(root: Path, limit: Optional[int] = None) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if name.startswith(".") or name.endswith(".part"):
                continue
            out.append(Path(dirpath) / name)
            if limit is not None and len(out) >= limit:
                return out
    return out


def _prune_empty(root: Path) -> None:
    if not root.is_dir():
        return
    for dirpath, dirs, names in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p != root and not dirs and not names:
            try:
                p.rmdir()
            except OSError:
                pass


def _rel_on_disk(src: Path, root: Path) -> str:
    """Путь файла на сетевом диске: имя папки комплекса приводится к допустимому на диске."""
    parts = list(src.relative_to(root).parts)
    parts[0] = safe_name(parts[0])
    if len(parts) >= 3:
        parts[1] = date_dir(parts[1])
    return "/".join(parts)


def _unlink(src: Path) -> None:
    try:
        src.unlink()
    except FileNotFoundError:
        pass


async def _move_file(src: Path, root: Path) -> bool:
    """Файл с диска сервера → сетевой диск (с проверкой размера), потом удаление с сервера. False — исходника уже нет."""
    if not src.exists():
        return False
    await _run_share(_backend.put_file, src, _rel_on_disk(src, root), timeout=_MOVE_TIMEOUT)
    await asyncio.to_thread(_unlink, src)
    return True


async def flush_spool(limit: int = _FLUSH_BATCH) -> dict[str, Any]:
    """Фото из временного буфера → сетевой диск (когда он доступен). Возвращает {moved, failed, left}."""
    if not configured():
        return {"moved": 0, "failed": 0, "left": 0, "skipped": "хранилище не настроено"}
    root = spool_root()
    files = await asyncio.to_thread(_iter_files, root, limit + 1)
    if not files:
        return {"moved": 0, "failed": 0, "left": 0}
    ok, reason = await share_state(force=True)
    if not ok:
        return {"moved": 0, "failed": 0, "left": len(files), "skipped": reason}
    moved = failed = 0
    for src in files[:limit]:
        try:
            if await _move_file(src, root):
                moved += 1
        except (OSError, asyncio.TimeoutError) as exc:
            failed += 1
            log.warning("Хранилище фото: не удалось перенести %s на сетевой диск (%s)", src, exc)
            if isinstance(exc, asyncio.TimeoutError):
                _mark_unavailable("диск не отвечает")
                break
    await asyncio.to_thread(_prune_empty, root)
    return {"moved": moved, "failed": failed, "left": max(0, len(files) - moved)}


async def storage_loop() -> None:
    """Раз в минуту переносит накопившийся буфер на сетевой диск."""
    await asyncio.sleep(20)
    next_index = time.monotonic() + 100            # поиск готовых фото на диске: вскоре после запуска, дальше раз в час (реже, пока он ничего нового не находит)
    quiet_runs = 0                                 # сколько поисков подряд ничего не изменили
    while True:
        try:
            if configured():
                res = await flush_spool()
                if res.get("moved"):
                    log.info("Хранилище фото: из буфера на сетевой диск перенесено %s", res["moved"])
                if time.monotonic() >= next_index:
                    from app.services import photo_index  # noqa: PLC0415

                    prev = photo_index.state()
                    if prev.get("finished_at"):    # прошлый поиск закончился: ничего не добавил, не переложил и не потерял — следующий можно позже
                        changed = prev["added"] or prev["moved"] or prev["removed"] or prev["renamed"] or prev["errors"]
                        quiet_runs = 0 if changed else min(quiet_runs + 1, _INDEX_QUIET_MAX)
                    next_index = time.monotonic() + _INDEX_EVERY * (2 ** quiet_runs)
                    await photo_index.start(auto=True)
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            log.exception("Хранилище фото: сбой переноса буфера")
        await asyncio.sleep(60)


_mig: dict[str, Any] = {"state": "idle", "total": 0, "done": 0, "failed": 0, "bytes": 0, "started_at": "", "finished_at": "", "message": ""}
_mig_task: Optional[asyncio.Task] = None


def migration_state() -> dict[str, Any]:
    return dict(_mig)


async def start_migration() -> dict[str, Any]:
    """Перенос фото с диска сервера (data/visit_photos) на сетевой диск — освобождает место на сервере. Идёт в фоне."""
    global _mig_task
    if not configured():
        raise PhotoStorageError("Сетевой диск не настроен: задайте его в настройках («Настроить сетевой диск»)")
    if isinstance(_backend, LocalBackend) and legacy_root().resolve() == _backend.root.resolve():
        raise PhotoStorageError("Папка хранилища совпадает с папкой фото на сервере — переносить нечего")
    ok, reason = await share_state(force=True)
    if not ok:
        raise PhotoStorageError(f"Сетевой диск сейчас недоступен: {reason}")
    if _mig_task is not None and not _mig_task.done():
        return migration_state()
    files = await asyncio.to_thread(_iter_files, legacy_root())
    _mig.update(state="running", total=len(files), done=0, failed=0, bytes=0, message="",
                started_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), finished_at="")
    _mig_task = asyncio.create_task(_migrate(files))
    return migration_state()


async def _migrate(files: list[Path]) -> None:
    root = legacy_root()
    try:
        for src in files:
            try:
                size = src.stat().st_size if src.exists() else 0
                if await _move_file(src, root):
                    _mig["done"] += 1
                    _mig["bytes"] += size
            except (OSError, asyncio.TimeoutError) as exc:
                _mig["failed"] += 1
                _mig["message"] = f"{src.name}: {exc}"
                log.warning("Хранилище фото: перенос %s не удался (%s)", src, exc)
                if isinstance(exc, asyncio.TimeoutError):
                    _mark_unavailable("диск не отвечает")
                    _mig["message"] = "сетевой диск перестал отвечать — перенос остановлен, запустите его снова позже"
                    break
        await asyncio.to_thread(_prune_empty, root)
        _mig["state"] = "done" if not _mig["failed"] else "finished_with_errors"
    except Exception as exc:  # noqa: BLE001
        log.exception("Хранилище фото: перенос оборвался")
        _mig.update(state="error", message=str(exc)[:200])
    finally:
        _mig["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ состояние для окна

_last_scan: dict[str, Any] = {}


async def scan() -> dict[str, Any]:
    """Пересчёт файлов: на сетевом диске, в буфере и на диске сервера (на большом диске — десятки секунд)."""
    res: dict[str, Any] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "share": None}
    res["local"] = await asyncio.to_thread(_scan_dir, legacy_root())
    res["spool"] = await asyncio.to_thread(_scan_dir, spool_root()) if configured() else None
    if configured() and (await share_state())[0]:
        try:
            res["share"] = await _run_share(_backend.scan, timeout=600)
        except (OSError, asyncio.TimeoutError):
            res["share"] = None
    _last_scan.clear()
    _last_scan.update(res)
    return res


async def status(do_scan: bool = False) -> dict[str, Any]:
    s = get_settings()
    ok, reason = await share_state(force=True)
    be = _backend
    info: dict[str, Any] = {
        "configured": be is not None, "mode": (be.kind if be is not None else ""), "path": be.target() if be is not None else "",
        "label": s.photos_share_label, "available": ok, "reason": reason,
        "local_disk": await asyncio.to_thread(_usage_dir, s.data_dir), "share_disk": None, "migration": migration_state(),
        "needs_marker": isinstance(be, LocalBackend),
    }
    if be is not None and ok:
        try:
            info["share_disk"] = await _run_share(be.usage, timeout=10)
        except asyncio.TimeoutError:
            pass
    if do_scan:
        await scan()
    info["scan"] = dict(_last_scan) if _last_scan else None
    info["server_files"] = await _local_file_counts()
    from app.services import photo_index  # noqa: PLC0415 — photo_index сам импортирует этот модуль

    info["index"] = photo_index.state()
    info["organize"] = organize_enabled()
    return info


_local_counts: dict[str, Any] = {"at": 0.0, "val": None}


async def _local_file_counts() -> dict[str, Any]:
    """Сколько фото лежит на диске самого сервера (не перенесено на сетевой диск) и во временном буфере — быстро, без сети."""
    now = time.monotonic()
    if _local_counts["val"] is None or now - _local_counts["at"] > 5:
        legacy = await asyncio.to_thread(_scan_dir, legacy_root())
        spool = await asyncio.to_thread(_scan_dir, spool_root())
        _local_counts.update(at=now, val={"server": legacy, "buffer": spool})
    return _local_counts["val"]


async def init_marker() -> None:
    """Подготовить папку-хранилище на сервере: положить метку. Делает админ один раз, убедившись, что смонтирован нужный диск."""
    if not isinstance(_backend, LocalBackend):
        raise PhotoStorageError("Метка нужна только для папки, подключённой на сервере (PHOTOS_DIR)")
    try:
        await _run_share(_backend.init_marker, timeout=_IO_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise PhotoStorageError("Сетевой диск не отвечает") from exc
    except OSError as exc:
        raise PhotoStorageError(f"Не удалось записать метку на диск: {exc.strerror or exc}") from exc
    await share_state(force=True)
