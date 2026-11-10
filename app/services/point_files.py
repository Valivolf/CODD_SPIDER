"""Файлы в карточке комплекса: документы, фото, схемы — всё, что нужно держать «при комплексе» (паспорт, акт, проект, письмо, схема подключения…).

Файлы лежат на диске сервера (data/point_files/<номер записи>.dat), сведения о них — в таблице point_files (имя, тип, размер, кто и когда загрузил, комментарий). Имя на диске —
только номер записи: настоящее имя файла в путь не попадает. Тип определяется ПО СОДЕРЖИМОМУ, а не по имени (имени верить нельзя):
  • image — JPEG, PNG, GIF, WebP, BMP (показываются в самой странице);
  • pdf — показывается встроенным просмотром браузера;
  • docx / xlsx — сервер делает безопасный предпросмотр (текст, таблицы, небольшие картинки), оригинал скачивается;
  • text — txt, csv, log, md, json, xml и подобные (в странице — как текст);
  • office_old (doc, xls, ppt), pptx, прочее — предпросмотра нет, только «Скачать».
Страницы, сценарии и исполняемые файлы (html, svg, js, exe, bat, …) не принимаются: их нельзя показывать в браузере, а хранить в карточке незачем.
Резервная копия базы файлы НЕ включает — они хранятся только на диске сервера (папка data/point_files)."""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

from app.config import get_settings
from app.core.text import decode_entities
from app.db.database import get_connection

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_POINT = 300
MAX_UNCOMPRESSED_BYTES = 150 * 1024 * 1024        # docx/xlsx — zip-архивы: защита от «бомбы» (маленький файл, гигабайты после распаковки)
MAX_NAME = 120
MAX_COMMENT = 500
PREVIEW_TEXT_CHARS = 200_000
PREVIEW_HTML_BYTES = 2_500_000

BLOCKED_EXT = {"exe", "dll", "bat", "cmd", "com", "scr", "msi", "ps1", "vbs", "vbe", "js", "jse", "jar", "sh", "apk", "lnk", "hta", "reg", "html", "htm", "xhtml", "xht", "mht", "mhtml", "svg", "php"}
TEXT_EXT = {"txt", "csv", "tsv", "log", "md", "json", "xml", "ini", "cfg", "conf", "yaml", "yml", "kml"}
OLD_OFFICE_EXT = {"doc", "xls", "ppt", "dot", "xlt", "pps"}
IMAGE_MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp"}
KIND_LABEL = {"image": "Изображение", "pdf": "PDF", "docx": "Документ Word", "xlsx": "Таблица Excel", "text": "Текст", "pptx": "Презентация", "office_old": "Файл Office (старый формат)", "other": "Файл"}
PREVIEWABLE = {"image", "pdf", "docx", "xlsx", "text"}


class FileError(Exception):
    """Понятная человеку причина отказа."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _root() -> Path:
    return get_settings().data_dir / "point_files"


def disk_path(file_id: int) -> Path:
    return _root() / f"{int(file_id)}.dat"


def clean_name(name: Optional[str]) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(c for c in base if c.isprintable() and c not in '<>:"|?*').strip(" .")
    return base[-MAX_NAME:] or "файл"


def _ext_of(name: str) -> str:
    m = re.search(r"\.([A-Za-z0-9]{1,8})$", name)
    return m.group(1).lower() if m else ""


def sniff_image(content: bytes) -> Optional[str]:
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[:2] == b"BM" and len(content) > 30:
        return "bmp"
    return None


def _zip_names(content: bytes) -> Optional[list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = zf.infolist()
            if len(infos) > 20000 or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
                raise FileError("Файл подозрительно большой после распаковки — проверьте, что это обычный документ")
            return [i.filename for i in infos]
    except zipfile.BadZipFile:
        return None


def _looks_text(content: bytes) -> bool:
    head = content[:8192]
    if b"\x00" in head:
        return False
    for enc in ("utf-8", "cp1251"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def detect(name: str, content: bytes) -> dict[str, str]:
    """{kind, mime, ext} по содержимому файла. Бросает FileError для запрещённых типов."""
    ext = _ext_of(name)
    if ext in BLOCKED_EXT:
        raise FileError(f"Файлы «.{ext}» (страницы, сценарии, программы) в карточку не принимаются")
    img = sniff_image(content)
    if img:
        return {"kind": "image", "mime": IMAGE_MIME[img], "ext": img}
    if b"%PDF-" in content[:1024]:
        return {"kind": "pdf", "mime": "application/pdf", "ext": "pdf"}
    if content[:2] == b"PK":
        names = _zip_names(content)
        if names is not None:
            if "word/document.xml" in names:
                return {"kind": "docx", "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "ext": ext if ext in ("docx", "docm", "dotx") else "docx"}
            if "xl/workbook.xml" in names:
                return {"kind": "xlsx", "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "ext": ext if ext in ("xlsx", "xlsm", "xltx") else "xlsx"}
            if "ppt/presentation.xml" in names:
                return {"kind": "pptx", "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "ext": ext if ext in ("pptx", "pptm") else "pptx"}
        return {"kind": "other", "mime": "application/octet-stream", "ext": ext or "zip"}
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return {"kind": "office_old", "mime": "application/octet-stream", "ext": ext if ext in OLD_OFFICE_EXT else "doc"}
    if ext in TEXT_EXT and _looks_text(content):
        return {"kind": "text", "mime": "text/plain; charset=utf-8", "ext": ext}
    return {"kind": "other", "mime": "application/octet-stream", "ext": ext}


def inline_ok(rec: dict[str, Any]) -> bool:
    """Показывать в самом браузере (Content-Disposition: inline) можно только изображение, PDF и текст; остальное — скачивание."""
    return rec["kind"] in ("image", "pdf", "text")


def _row(r) -> dict[str, Any]:
    d = dict(zip(("id", "point_id", "name", "ext", "kind", "mime", "size", "comment", "uploaded_at", "uploaded_by"), r))
    d["kind_label"] = KIND_LABEL.get(d["kind"], "Файл")
    d["previewable"] = d["kind"] in PREVIEWABLE
    return d


_COLS = "id, point_id, name, ext, kind, mime, size, comment, uploaded_at, uploaded_by"


async def list_for_point(point_id: str) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLS} from point_files where point_id = ? order by uploaded_at desc, id desc", (point_id,))
        return [_row(r) for r in await cur.fetchall()]


async def get(file_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(f"select {_COLS} from point_files where id = ?", (file_id,))
        row = await cur.fetchone()
    return _row(row) if row else None


async def save(point_id: str, filename: str, content: bytes, uploaded_by: str, comment: str = "") -> dict[str, Any]:
    if not content:
        raise FileError("Пустой файл")
    if len(content) > MAX_FILE_BYTES:
        raise FileError(f"Файл слишком большой ({len(content) // 1_000_000} МБ): можно не больше {MAX_FILE_BYTES // 1_000_000} МБ", 413)
    name = clean_name(filename)
    info = detect(name, content)
    comment = decode_entities(comment or "").strip()[:MAX_COMMENT]
    async with get_connection() as conn:
        cur = await conn.execute("select 1 from points where id = ?", (point_id,))
        if not await cur.fetchone():
            raise FileError("Комплекс не найден", 404)
        cur = await conn.execute("select count(*) from point_files where point_id = ?", (point_id,))
        if (await cur.fetchone())[0] >= MAX_FILES_PER_POINT:
            raise FileError(f"У комплекса уже {MAX_FILES_PER_POINT} файлов — удалите ненужные")
        cur = await conn.execute(
            "insert into point_files (point_id, name, ext, kind, mime, size, comment, uploaded_at, uploaded_by) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (point_id, name, info["ext"], info["kind"], info["mime"], len(content), comment, dt.datetime.now(dt.timezone.utc).isoformat(), uploaded_by or ""))
        file_id = cur.lastrowid
        try:
            path = disk_path(file_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        except OSError:
            await conn.rollback()
            raise FileError("Не удалось сохранить файл: на сервере нет места или диск недоступен", 507)
        await conn.commit()
    return (await get(file_id))  # type: ignore[return-value]


async def delete(file_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from point_files where id = ?", (file_id,))
        await conn.commit()
        removed = cur.rowcount > 0
    if removed:
        try:
            disk_path(file_id).unlink(missing_ok=True)
        except OSError:
            log.warning("Не удалось удалить файл %s с диска", file_id)
    return removed


async def delete_for_point(point_id: str) -> int:
    """Комплекс удалён из справочника — вместе с ним уходят и файлы его карточки (с диска и из таблицы). Возвращает, сколько файлов убрано."""
    async with get_connection() as conn:
        cur = await conn.execute("select id from point_files where point_id = ?", (point_id,))
        ids = [r[0] for r in await cur.fetchall()]
        if ids:
            await conn.execute("delete from point_files where point_id = ?", (point_id,))
            await conn.commit()
    for file_id in ids:
        try:
            disk_path(file_id).unlink(missing_ok=True)
        except OSError:
            log.warning("Не удалось удалить файл %s с диска", file_id)
    return len(ids)


def read_bytes(file_id: int) -> bytes:
    return disk_path(file_id).read_bytes()


# ---------------------------------------------------------------- предпросмотр: docx / xlsx / text → безопасный HTML или текст

def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _decode_text(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "replace")


def preview_text(content: bytes) -> dict[str, Any]:
    text = _decode_text(content[: PREVIEW_TEXT_CHARS * 4])
    truncated = len(content) > PREVIEW_TEXT_CHARS * 4 or len(text) > PREVIEW_TEXT_CHARS
    return {"kind": "text", "text": text[:PREVIEW_TEXT_CHARS], "truncated": truncated}


def preview_docx(content: bytes) -> dict[str, Any]:
    """Документ Word → простой безопасный HTML: абзацы, заголовки, списки, таблицы, жирный/курсив/подчёркнутый, небольшие картинки. Весь текст экранируется, теги — только свои."""
    from docx import Document  # noqa: PLC0415
    from docx.table import Table  # noqa: PLC0415
    from docx.text.paragraph import Paragraph  # noqa: PLC0415

    doc = Document(io.BytesIO(content))
    out: list[str] = []
    state = {"img_bytes": 0, "truncated": False}

    def pic_html(run) -> str:
        res = []
        for rid in run._r.xpath(".//a:blip/@r:embed"):
            try:
                part = doc.part.related_parts[rid]
            except KeyError:
                continue
            mime = part.content_type
            blob = part.blob
            if mime not in ("image/png", "image/jpeg", "image/gif") or len(blob) > 1_500_000 or state["img_bytes"] + len(blob) > 8_000_000:
                continue
            state["img_bytes"] += len(blob)
            res.append(f'<img src="data:{mime};base64,{base64.b64encode(blob).decode()}" alt="" style="max-width:100%;height:auto">')
        return "".join(res)

    def para_html(p) -> str:
        parts = []
        for item in p.iter_inner_content():
            runs = [item] if hasattr(item, "bold") else list(getattr(item, "runs", []))
            for run in runs:
                text = _esc(run.text).replace("\n", "<br>").replace("\t", "&emsp;")
                if text:
                    if getattr(run, "bold", None):
                        text = f"<b>{text}</b>"
                    if getattr(run, "italic", None):
                        text = f"<i>{text}</i>"
                    if getattr(run, "underline", None):
                        text = f"<u>{text}</u>"
                    parts.append(text)
                if hasattr(run, "_r"):
                    parts.append(pic_html(run))
        return "".join(parts)

    def block(p) -> str:
        inner = para_html(p)
        style = (p.style.name if p.style is not None else "") or ""
        low = style.lower()
        if not inner.strip():
            return "<p>&nbsp;</p>" if "<img" not in inner else f"<p>{inner}</p>"
        if low == "title":
            return f"<h1>{inner}</h1>"
        if low.startswith("heading") or low.startswith("заголовок"):
            digits = "".join(ch for ch in low if ch.isdigit())
            level = min(max(int(digits[:1] or 1), 1), 4) + 1
            return f"<h{level}>{inner}</h{level}>"
        pPr = p._p.pPr
        if (pPr is not None and pPr.numPr is not None) or "list" in low or "спис" in low:
            return f"<p style=\"margin-left:1.2em\">• {inner}</p>"
        return f"<p>{inner}</p>"

    def table_html(t) -> str:
        rows = []
        for row in t.rows[:500]:
            cells = []
            seen = set()
            for cell in row.cells:
                if id(cell._tc) in seen:        # объединённые ячейки python-docx отдаёт повторно
                    continue
                seen.add(id(cell._tc))
                cells.append("<td>" + "<br>".join(para_html(p) for p in cell.paragraphs) + "</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        return "<table>" + "".join(rows) + "</table>"

    count = 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            out.append(block(Paragraph(child, doc)))
        elif tag == "tbl":
            out.append(table_html(Table(child, doc)))
        count += 1
        if count >= 3000 or sum(len(x) for x in out) > PREVIEW_HTML_BYTES:
            state["truncated"] = True
            break
    return {"kind": "html", "html": "".join(out), "truncated": state["truncated"]}


def preview_xlsx(content: bytes) -> dict[str, Any]:
    """Таблица Excel → HTML-таблицы по листам (до 6 листов, до 300 строк и 40 столбцов на лист), значения без формул."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    parts: list[str] = []
    truncated = False
    shown = 0
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        if shown >= 6:
            truncated = True
            break
        shown += 1
        rows = []
        n = 0
        for row in ws.iter_rows(min_row=1, max_row=301, max_col=40, values_only=True):
            n += 1
            if n > 300:
                truncated = True
                break
            cells = []
            for v in row:
                if isinstance(v, dt.datetime):
                    v = v.strftime("%d.%m.%Y %H:%M") if (v.hour or v.minute) else v.strftime("%d.%m.%Y")
                elif isinstance(v, dt.date):
                    v = v.strftime("%d.%m.%Y")
                elif isinstance(v, float) and v.is_integer():
                    v = int(v)
                cells.append(f"<td>{_esc(v)}</td>")
            while cells and cells[-1] == "<td></td>":
                cells.pop()
            rows.append("<tr>" + "".join(cells) + "</tr>")
        parts.append(f"<h3>{_esc(ws.title)}</h3>" + ("<table>" + "".join(rows) + "</table>" if rows else "<p><i>лист пустой</i></p>"))
        if sum(len(x) for x in parts) > PREVIEW_HTML_BYTES:
            truncated = True
            break
    wb.close()
    return {"kind": "html", "html": "".join(parts), "truncated": truncated}


def preview(rec: dict[str, Any], content: bytes) -> dict[str, Any]:
    kind = rec["kind"]
    try:
        if kind == "text":
            return preview_text(content)
        if kind == "docx":
            return preview_docx(content)
        if kind == "xlsx":
            return preview_xlsx(content)
    except FileError:
        raise
    except Exception:  # noqa: BLE001 — битый или необычный файл: просто без предпросмотра
        log.warning("Предпросмотр файла %s не получился", rec.get("id"), exc_info=True)
        return {"kind": "none", "note": "Не удалось показать содержимое этого файла — его можно скачать."}
    return {"kind": "none", "note": "Для этого типа файлов предпросмотра нет — его можно скачать."}
