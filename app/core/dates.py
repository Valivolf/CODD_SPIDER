"""Единый формат даты для всего, что видит человек: ДД-ММ-ГГГГ.

Внутри приложения дата всегда хранится и передаётся как ISO ("YYYY-MM-DD"
— так её отдаёт <input type=date>, по ней сортируют и сравнивают строкой),
и только на выводе (экран, Excel, печать, отчёты, бот) превращается в
"05-10-2026". Зеркало static/js/ui.js → fmtDate/fmtDateTime."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")


def format_date(value: Any) -> str:
    """"2026-10-05", "2026-10-05T12:30:00+00:00", date/datetime → "05-10-2026".
    Пустое → "". Что не похоже на дату — возвращается как есть (пусть лучше
    видно странное значение, чем пустота)."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%d-%m-%Y")
    if isinstance(value, dt.date):
        return value.strftime("%d-%m-%Y")
    s = str(value).strip()
    m = _ISO_DATE_RE.match(s)
    if not m:
        return s
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{d}-{mo}-{y}"


def format_datetime(value: Any, seconds: bool = False) -> str:
    """Дата со временем: "05-10-2026 12:30" (или с секундами). Время берётся
    из строки как есть, без пересчёта часового пояса — так же, как его и
    раньше показывали (срез ISO-строки)."""
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%d-%m-%Y %H:%M:%S" if seconds else "%d-%m-%Y %H:%M")
    s = str(value).strip()
    m = _ISO_DATE_RE.match(s)
    if not m:
        return s
    y, mo, d, hh, mm, ss = m.groups()
    out = f"{d}-{mo}-{y}"
    if hh is not None:
        out += f" {hh}:{mm}"
        if seconds and ss is not None:
            out += f":{ss}"
    return out


def format_period(date_from: str, date_to: str) -> str:
    """"05-10-2026" для одного дня, иначе "05-10-2026 — 09-10-2026"."""
    if date_from == date_to:
        return format_date(date_from)
    return f"{format_date(date_from)} — {format_date(date_to)}"
