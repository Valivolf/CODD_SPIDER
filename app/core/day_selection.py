"""Выбор дней для выгрузок: произвольный набор дат, а не только «с … по …».

Календарь позволяет отметить любые дни (в том числе с пропусками между
крайними), клиент присылает их списком через запятую — «2026-10-01,2026-10-03».
Здесь он разбирается и проверяется одинаково для всех выгрузок (выезды по
дням, отчёты из конструктора).
"""
from __future__ import annotations

import datetime as dt

MAX_DAYS = 366


def parse_days_param(raw: str | list[str] | None) -> list[str]:
    """Отсортированный список ISO-дат без повторов. ValueError — если дата
    неверная, список пуст или дней больше MAX_DAYS."""
    if raw is None:
        raise ValueError("Не выбрано ни одного дня")
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    days: set[str] = set()
    for item in items:
        item = str(item).strip()
        if not item:
            continue
        try:
            days.add(dt.date.fromisoformat(item).isoformat())
        except ValueError:
            raise ValueError(f"Неверная дата: {item}")
    if not days:
        raise ValueError("Не выбрано ни одного дня")
    if len(days) > MAX_DAYS:
        raise ValueError(f"Выбрано больше {MAX_DAYS} дней")
    return sorted(days)


def describe_days(days: list[str], fmt) -> str:
    """Подпись набора дней: подряд идущие — «с … по …», иначе — перечень
    (для длинного — первый, последний и число дней). `fmt` — форматтер даты."""
    if not days:
        return ""
    first, last = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
    contiguous = (last - first).days + 1 == len(days)
    if len(days) == 1:
        return fmt(days[0])
    if contiguous:
        return f"{fmt(days[0])} — {fmt(days[-1])}"
    if len(days) <= 6:
        return ", ".join(fmt(d) for d in days)
    return f"{fmt(days[0])} … {fmt(days[-1])}, выбрано дней: {len(days)}"
