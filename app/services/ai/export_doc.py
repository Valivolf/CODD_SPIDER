"""Файл Excel или Word по ответу помощника — «выгрузи в Excel», «сделай справку в Word».

Данные в файл попадают ТОЛЬКО из базы, теми же условиями и по тем же правам роли, что и ответ в чате: запрос заново читается без ограничения «первые 200–1000 строк»
(в Excel — до EXPORT_ROWS строк). Модель в содержимое таблицы не вмешивается: она писала только словесный ответ, и он (с числами, уже сверенными с данными) идёт в Word
текстом над таблицей. Файл кладётся в хранилище файлов помощника (jobs.save_file, kind=output), скачивается тем же адресом, что и заполненные шаблоны."""
from __future__ import annotations

import datetime as dt
import io
import re
from typing import Any, Optional

from app.services.ai import qa

EXPORT_ROWS = 5000        # строк в Excel
DOCX_ROWS = 400           # строк таблицы в Word (длиннее — читать неудобно: полный список — в Excel)
_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')
MEDIA_NAMES = {"xlsx": "Excel", "docx": "Word"}


class ExportError(RuntimeError):
    """Файл собрать не из чего или не получилось (понятный человеку текст)."""


def _stem(question: str, fmt: str, answer: Optional[dict[str, Any]] = None) -> str:
    """Имя файла по сути ответа, а не по тексту просьбы («Выгрузи неработающие комплексы» → «Выгрузка — Комплексы, Статус комплекса = Не работает»): у карточек — номера комплексов, у таблиц — описание запроса."""
    answer = answer or {}
    cards = answer.get("cards") or []
    if cards:
        base = "Комплекс " + ", ".join(str(c.get("point_id")) for c in cards[:3])
    elif answer.get("description"):
        base = str(answer["description"]).replace(" · ", ", ").replace("«", "").replace("»", "")
    else:
        base = question or ""
    base = _BAD_NAME.sub(" ", base).strip()
    base = re.sub(r"\s+", " ", base)[:70].rstrip(" .,") or "Ответ помощника"
    return f"{'Справка' if fmt == 'docx' else 'Выгрузка'} — {base} ({dt.date.today().strftime('%d-%m-%Y')})"


def _as_of(answer: dict[str, Any]) -> dt.date:
    try:
        return dt.date.fromisoformat(str(answer.get("as_of") or ""))
    except ValueError:
        return dt.date.today()


async def _table(answer: dict[str, Any], role: str) -> Optional[dict[str, Any]]:
    """Полные данные для файла: тот же запрос ещё раз, без урезания; None — у ответа нет запроса (разговор, досье)."""
    spec = answer.get("raw_spec")
    if not spec:
        return None
    spec = dict(spec)
    if not spec.get("aggregates") and not spec.get("group_by"):
        spec["limit"] = EXPORT_ROWS                       # перечень записей — целиком, а не первые 200
    out = await qa.execute(spec, role, today=_as_of(answer), source="export", max_rows=EXPORT_ROWS)
    if out["kind"] in ("denied", "error"):
        raise ExportError(out.get("text") or "Не удалось получить данные для файла")
    return out


def _cards_table(answer: dict[str, Any]) -> Optional[dict[str, Any]]:
    cards = answer.get("cards") or []
    if not cards:
        return None
    cols = [("point_id", "Комплекс"), ("address", "Адрес"), ("status", "Статус"), ("power_source", "Питание"), ("fault_reason", "Почему не работает"),
            ("fault_need", "Что нужно для восстановления"), ("last_visit", "Последний выезд"), ("visits_count", "Выездов по отчётам")]
    return {"kind": "table", "columns": [{"label": label} for _, label in cols], "rows": [[c.get(key) if c.get(key) is not None else "" for key, _ in cols] for c in cards],
            "total": len(cards), "truncated": False, "description": answer.get("description") or "Карточка комплекса, отчёты выездов, план"}


_SHOWN_FIRST = re.compile(r"\s*Показаны первые \d+( из \d+)?\.?")


def _say(answer: dict[str, Any], data: Optional[dict[str, Any]] = None) -> str:
    """Словесный ответ из чата. В файле записей больше, чем было в чате, — фраза «Показаны первые 50» там была бы неправдой, её убираем (остальное в ответе — про весь набор)."""
    text = str(answer.get("narration") or answer.get("text") or "").strip()
    if data and data.get("kind") == "table" and len(data.get("rows") or []) > len(answer.get("rows") or []):
        text = _SHOWN_FIRST.sub("", text).strip()
    return text


def _now_text() -> str:
    return dt.datetime.now().strftime("%d.%m.%Y %H:%M") + " (МСК)"


def _xlsx(question: str, answer: dict[str, Any], data: Optional[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook  # noqa: PLC0415
    from openpyxl.styles import Alignment, Font, PatternFill  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"
    if data and data["kind"] == "number":
        columns, rows = [{"label": "Показатель"}, {"label": "Значение"}], [[data.get("label"), data.get("value")]]
    elif data and data["kind"] == "table":
        columns, rows = data["columns"], data["rows"]
    else:
        columns, rows = [{"label": "Ответ"}], [[_say(answer)]]
    head_fill = PatternFill("solid", fgColor="DDE6F5")
    ws.append([c["label"] for c in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rows:
        ws.append(["" if v is None else v for v in row])
    for r in ws.iter_rows(min_row=2):
        for cell in r:
            if isinstance(cell.value, str):
                cell.data_type = "s"                    # текст, начинающийся с «=», не должен стать формулой
                cell.alignment = Alignment(wrap_text=True, vertical="top")
    for i, col in enumerate(columns, start=1):
        longest = max([len(str(col["label"]))] + [len(str(r[i - 1])) for r in rows[:300] if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = max(8, min(60, longest + 2))
    ws.freeze_panes = "A2"
    if rows and len(columns) > 0:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"

    info = wb.create_sheet("О выгрузке")
    total = (data or {}).get("total") or len(rows)
    lines = [("Вопрос", question), ("Как посчитано", (data or answer).get("description") or answer.get("description") or ""), ("Сформировано", _now_text()),
             ("Записей в файле", len(rows)), ("Всего по запросу", total if data and data["kind"] == "table" else len(rows))]
    if data and data.get("truncated"):
        lines.append(("Примечание", f"Файл содержит первые {len(rows)} записей из {total}: уточните вопрос (период, комплекс), чтобы получить остальные."))
    if answer.get("kind") != "table" and _say(answer, data) and data and data["kind"] == "table":
        lines.append(("Ответ помощника", _say(answer, data)))
    lines.append(("Источник", "Данные системы «Календарь работ» на момент формирования; поля с паролями и контактами — только если у вашей роли есть право их видеть."))
    for k, v in lines:
        info.append([k, v])
    info.column_dimensions["A"].width = 24
    info.column_dimensions["B"].width = 100
    for r in info.iter_rows():
        r[0].font = Font(bold=True)
        r[1].alignment = Alignment(wrap_text=True, vertical="top")
        if isinstance(r[1].value, str):
            r[1].data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx(question: str, answer: dict[str, Any], data: Optional[dict[str, Any]]) -> bytes:
    from docx import Document  # noqa: PLC0415
    from docx.enum.section import WD_ORIENT  # noqa: PLC0415
    from docx.shared import Pt  # noqa: PLC0415

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(11)
    columns: list[dict] = []
    rows: list[list] = []
    if data and data["kind"] == "table":
        columns, rows = data["columns"], data["rows"]
    elif data and data["kind"] == "number":
        columns, rows = [{"label": "Показатель"}, {"label": "Значение"}], [[data.get("label"), data.get("value")]]
    if len(columns) > 6:
        sec = doc.sections[0]
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    doc.add_heading("Справка", level=1)
    p = doc.add_paragraph()
    p.add_run("Запрос: ").bold = True
    p.add_run(question)
    p = doc.add_paragraph()
    p.add_run("Сформировано: ").bold = True
    p.add_run(_now_text())
    say = _say(answer, data)
    if say:
        doc.add_heading("Ответ", level=2)
        for para in [x for x in say.split("\n") if x.strip()]:
            doc.add_paragraph(para.strip())
    if rows:
        total = (data or {}).get("total") or len(rows)
        shown = rows[:DOCX_ROWS]
        doc.add_heading("Данные", level=2)
        table = doc.add_table(rows=1, cols=len(columns))
        table.style = "Table Grid"
        for i, col in enumerate(columns):
            cell = table.rows[0].cells[i]
            cell.text = ""
            cell.paragraphs[0].add_run(str(col["label"])).bold = True
        for row in shown:
            cells = table.add_row().cells
            for i in range(len(columns)):
                cells[i].text = "" if i >= len(row) or row[i] is None else str(row[i])
        for r in table.rows:
            for c in r.cells:
                for para in c.paragraphs:
                    for run in para.runs:
                        run.font.size = Pt(9)
        if len(rows) > len(shown) or total > len(rows):
            note = doc.add_paragraph().add_run(f"В справке показаны первые {len(shown)} записей из {total}. Полный список — выгрузкой в Excel (напишите то же самое «…в Excel»).")
            note.italic = True
    cards = answer.get("cards") or []
    if cards and not rows:
        doc.add_heading("Сведения по комплексам", level=2)
        for c in cards:
            doc.add_heading(str(c.get("point_id") or ""), level=3)
            for label, key in (("Адрес", "address"), ("Статус", "status"), ("Питание", "power_source"), ("Почему не работает", "fault_reason"),
                               ("Что нужно для восстановления", "fault_need"), ("Последний выезд", "last_visit"), ("Выездов по отчётам", "visits_count")):
                if c.get(key) not in (None, ""):
                    pp = doc.add_paragraph()
                    pp.add_run(f"{label}: ").bold = True
                    pp.add_run(str(c[key]))
    how = (data or {}).get("description") or answer.get("description")
    if how:
        p = doc.add_paragraph()
        r = p.add_run(f"Как посчитано: {how}")
        r.italic = True
        r.font.size = Pt(9)
    p = doc.add_paragraph()
    r = p.add_run("Данные системы «Календарь работ» на момент формирования справки.")
    r.italic = True
    r.font.size = Pt(9)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def build(answer: dict[str, Any], question: str, role: str, fmt: str) -> tuple[bytes, str]:
    """(содержимое файла, имя файла). fmt — 'xlsx' или 'docx'. ExportError — файл собрать не из чего."""
    if fmt not in MEDIA_NAMES:
        raise ExportError("Неизвестный формат файла")
    kind = answer.get("kind")
    if kind not in qa.FILE_KINDS:
        raise ExportError("Из этого ответа файл не собрать")
    if kind == "empty":
        raise ExportError("По этому запросу ничего не нашлось — файл получился бы пустым, поэтому я его не делаю. Уточните период или условия.")
    data = await _table(answer, role) if kind in ("table", "number", "empty", "agent") else None
    if kind == "dossier":
        data = _cards_table(answer)
    if data is not None and data["kind"] == "empty":
        data = None
    if data is None and not _say(answer):
        raise ExportError("По этому запросу ничего не нашлось — файл пустой, поэтому я его не делаю.")
    body = _xlsx(question, answer, data) if fmt == "xlsx" else _docx(question, answer, data)
    return body, f"{_stem(question, fmt, {**answer, 'description': (data or {}).get('description') or answer.get('description')})}.{fmt}"
