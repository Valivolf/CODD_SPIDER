"""Чем запитан комплекс: «СЭС» или «Электросеть» (points.power_source;
пусто — не указано). Бот спрашивает это у монтёра при взятии комплекса в
работу, если поле не заполнено."""
from __future__ import annotations

POWER_SOURCES = ["СЭС", "Электросеть"]

_ALIASES = {
    "сэс": "СЭС",
    "ses": "СЭС",
    "солнечная": "СЭС",
    "электросеть": "Электросеть",
    "сеть": "Электросеть",
    "электро": "Электросеть",
    "grid": "Электросеть",
}


def normalize_power_source(value: object) -> str | None:
    """"сэс" → "СЭС", "СЕТЬ" → "Электросеть", "" → "". None — не распознано."""
    text = str(value or "").strip()
    if not text:
        return ""
    return _ALIASES.get(text.lower())
