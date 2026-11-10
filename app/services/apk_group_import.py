"""Список комплексов из файла для подгруппы АПК: KML/KMZ, GeoJSON и GPX (так отдаёт свои карты Яндекс), Excel (в том числе файл, который приложение само
выгружает для Конструктора карт: столбцы «Широта / Долгота / Описание / Подпись / Номер метки»), CSV и обычный текст (номера через пробел, запятую или с новой
строки). Из файла берётся метка (номер комплекса) и, если есть, описание и координаты; сопоставление с комплексами справочника — как у остальных импортов
(регистр, пробелы, русские/латинские двойники, хвост «+1», инвентарный номер)."""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Optional
from xml.etree import ElementTree as ET

from app.core.names import compact_name, strip_dup_suffix
from app.services.point_finder import PointFinder, digits_key
from app.services.point_matching import PointIndex

MAX_MARKS = 5000


class ImportError_(ValueError):
    """Файл не удалось прочитать; текст — человеку."""


@dataclass
class Mark:
    label: str
    description: str = ""
    lat: str = ""
    lon: str = ""

    def has_coords(self) -> bool:
        try:
            lat, lon = float(self.lat), float(self.lon)
        except (TypeError, ValueError):
            return False
        return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""



def _safe_fromstring(data) -> "ET.Element":
    """XML из присланного файла (KML, GPX) разбирается только если в нём нет объявления типа документа (DOCTYPE/ENTITY): в этих форматах оно не нужно, а именно через него делают
    атаки «раздувания» сущностей (миллиард смехов) и подстановку внешних файлов. Файл с таким объявлением отклоняется как повреждённый."""
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8", "ignore")
    head = bytes(raw[:200000]).lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ET.ParseError("объявление DOCTYPE/ENTITY не допускается")
    return ET.fromstring(data)


def _text(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def _strip_html(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").split())


def parse_kml(data: bytes) -> list[Mark]:
    try:
        root = _safe_fromstring(data)
    except ET.ParseError:
        try:
            root = _safe_fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", _decode(data)))
        except ET.ParseError as exc:
            raise ImportError_("Файл KML повреждён или это не KML.") from exc
    marks: list[Mark] = []
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        name = desc = coords = ""
        for ch in pm.iter():
            tag = _local(ch.tag)
            if tag == "name" and not name:
                name = _text(ch)
            elif tag == "description" and not desc:
                desc = _strip_html(_text(ch))
            elif tag == "coordinates" and not coords:
                coords = _text(ch)
        lon = lat = ""
        if coords:
            first = coords.split()[0].split(",")
            lon, lat = (first + ["", ""])[:2]
        if name:
            marks.append(Mark(name, desc, lat, lon))
    return marks


def parse_kmz(data: bytes) -> list[Mark]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ImportError_("В архиве KMZ нет файла KML.")
            return parse_kml(zf.read(names[0]))
    except zipfile.BadZipFile as exc:
        raise ImportError_("Файл KMZ повреждён.") from exc


def parse_geojson(data: bytes) -> list[Mark]:
    try:
        doc = json.loads(_decode(data))
    except ValueError as exc:
        raise ImportError_("Файл GeoJSON повреждён.") from exc
    features = doc.get("features") if isinstance(doc, dict) else doc
    marks: list[Mark] = []
    for f in features or []:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        name = ""
        for key in ("iconCaption", "name", "title", "label", "caption", "Подпись", "Название"):
            if props.get(key):
                name = str(props[key]).strip()
                break
        desc = _strip_html(str(props.get("description") or props.get("balloonContent") or ""))
        coords = (f.get("geometry") or {}).get("coordinates") or []
        lon, lat = (str(coords[0]), str(coords[1])) if isinstance(coords, list) and len(coords) >= 2 and not isinstance(coords[0], list) else ("", "")
        if name:
            marks.append(Mark(name, desc, lat, lon))
    return marks


def parse_gpx(data: bytes) -> list[Mark]:
    try:
        root = _safe_fromstring(data)
    except ET.ParseError as exc:
        raise ImportError_("Файл GPX повреждён.") from exc
    marks: list[Mark] = []
    for wpt in root.iter():
        if _local(wpt.tag) != "wpt":
            continue
        name = desc = ""
        for ch in wpt:
            if _local(ch.tag) == "name":
                name = _text(ch)
            elif _local(ch.tag) in ("desc", "cmt"):
                desc = desc or _text(ch)
        if name:
            marks.append(Mark(name, desc, wpt.get("lat", ""), wpt.get("lon", "")))
    return marks


# ---- таблицы (Excel, CSV): столбец с номером комплекса определяется по названию
_LABEL_HEADERS = ("подпись", "название", "наименование", "комплекс", "номер комплекса", "номер апк", "апк", "номер", "метка", "имя", "name", "label", "title", "id")
_DESC_HEADERS = ("описание", "description", "комментарий", "примечание", "что нужно")
_LAT_HEADERS = ("широта", "lat", "latitude")
_LON_HEADERS = ("долгота", "lon", "lng", "long", "longitude")


def _norm_header(v: Any) -> str:
    return " ".join(str(v or "").replace("\xa0", " ").split()).casefold().rstrip(":")


def _pick(headers: list[str], names: tuple[str, ...]) -> Optional[int]:
    for name in names:
        if name in headers:
            return headers.index(name)
    return None


def _marks_from_table(rows: list[list[Any]]) -> list[Mark]:
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        return []
    headers = [_norm_header(c) for c in rows[0]]
    label_i = _pick(headers, _LABEL_HEADERS)
    has_header = label_i is not None or _pick(headers, _DESC_HEADERS) is not None or _pick(headers, _LAT_HEADERS) is not None
    if not has_header:
        # без заголовка: первый столбец — номера
        return [Mark(str(r[0]).strip()) for r in rows if str(r[0] or "").strip()]
    if label_i is None:
        # заголовок есть, а столбца с номером нет — берём первый столбец, который не описание и не координаты
        skip = {i for i in (_pick(headers, _DESC_HEADERS), _pick(headers, _LAT_HEADERS), _pick(headers, _LON_HEADERS)) if i is not None}
        label_i = next((i for i in range(len(headers)) if i not in skip), 0)
    desc_i, lat_i, lon_i = _pick(headers, _DESC_HEADERS), _pick(headers, _LAT_HEADERS), _pick(headers, _LON_HEADERS)

    def cell(r: list[Any], i: Optional[int]) -> str:
        return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""

    marks = []
    for r in rows[1:]:
        label = cell(r, label_i)
        if label:
            marks.append(Mark(label, cell(r, desc_i), cell(r, lat_i).replace(",", "."), cell(r, lon_i).replace(",", ".")))
    return marks


def parse_xlsx(data: bytes) -> list[Mark]:
    from openpyxl import load_workbook  # noqa: PLC0415

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — любой сбой чтения файла
        raise ImportError_("Не удалось прочитать Excel-файл.") from exc
    try:
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _marks_from_table(rows)


def parse_csv_or_text(data: bytes) -> list[Mark]:
    text = _decode(data)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    for delim in (";", "\t", ","):
        if all(delim in ln for ln in lines[:3]) and len(lines) > 1:
            return _marks_from_table([row for row in csv.reader(io.StringIO(text), delimiter=delim)])
    return [Mark(t) for t in split_tokens(text)]


_SPLIT_RE = re.compile(r"[\s,;]+")


def split_tokens(text: str) -> list[str]:
    """Номера комплексов из вставленного текста: через пробел, запятую, точку с запятой или с новой строки. Хвост «+1», «+2» остаётся при своём
    комплексе («AR-1 +1» — один комплекс, а не два)."""
    out: list[str] = []
    for part in _SPLIT_RE.split(text or ""):
        part = part.strip()
        if not part:
            continue
        if re.fullmatch(r"\+\s*\d{1,2}", part) and out:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return list(dict.fromkeys(out))


def parse_file(filename: str, data: bytes) -> list[Mark]:
    name = (filename or "").lower()
    if name.endswith(".kml"):
        marks = parse_kml(data)
    elif name.endswith(".kmz"):
        marks = parse_kmz(data)
    elif name.endswith((".geojson", ".json")):
        marks = parse_geojson(data)
    elif name.endswith(".gpx"):
        marks = parse_gpx(data)
    elif name.endswith((".xlsx", ".xlsm")):
        marks = parse_xlsx(data)
    elif name.endswith((".csv", ".txt", ".tsv")):
        marks = parse_csv_or_text(data)
    else:
        raise ImportError_("Нужен файл KML, KMZ, GeoJSON, GPX, Excel (.xlsx), CSV или текст со списком номеров.")
    if len(marks) > MAX_MARKS:
        raise ImportError_(f"В файле слишком много меток ({len(marks)}), максимум — {MAX_MARKS}.")
    # повтор одной и той же метки в файле — один раз
    seen: set[str] = set()
    unique: list[Mark] = []
    for m in marks:
        key = m.label.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


class Matcher:
    """Сопоставление метки с комплексом справочника: сначала строго (номер/инвентарный/«по сути»/хвост «+1»), затем по цифрам номера (если их не меньше
    четырёх и комплекс один). Несколько подходящих — неоднозначность, комплекс не выбирается."""

    def __init__(self, points: list[dict[str, Any]]):
        self.index = PointIndex(points)
        self.finder = PointFinder(points)

    def match(self, label: str) -> tuple[Optional[str], list[str]]:
        hit = self.index.find(label)
        if hit:
            return hit["id"], []
        if len(digits_key(label)) >= 4:
            pid, how, cands = self.finder.find(number=label)
            if pid:
                return pid, []
            if how == "ambiguous":
                return None, cands
        return None, []

    def new_name(self, label: str) -> str:
        return self.index.base_name(label)

    def register(self, point: dict[str, Any]) -> None:
        self.index.add(point)            # только строгий поиск: пересобирать ради новых комплексов весь поиск по цифрам дорого


def new_key(label: str) -> str:
    base, n = strip_dup_suffix(label)
    return compact_name(base if n is not None else label) or label
