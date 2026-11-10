"""Время сервера — московское (Краснодар живёт по нему же).

Контейнер по умолчанию живёт по UTC: «сегодня» в приложении (период «сегодня» у помощника, имена копий базы, папки фото по дням, «Когда» в выгрузках,
журнал) с 00:00 до 03:00 по Москве было бы ещё вчерашним днём. Поэтому при запуске выставляется часовой пояс процесса (TZ + tzset): после этого
`datetime.now()`, `date.today()` и время в журнале — московские. В базе по-прежнему лежит UTC (метки с «+00:00»), браузер показывает его в своём поясе.

Нужны файлы часовых поясов (пакет tzdata) — в обычном образе они есть; если их нет, берётся запись POSIX «MSK-3», которой файлы не нужны (Москва
без перехода на летнее время, UTC+3)."""
from __future__ import annotations

import os
import time

DEFAULT_ZONE = "Europe/Moscow"
POSIX_FALLBACK = "MSK-3"          # POSIX-запись: имя MSK, смещение +3 часа от UTC (знак у POSIX обратный)


def _zone_known(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 — нет tzdata / незнакомое имя
        return False


def apply(zone: str | None = None) -> str:
    """Выставляет часовой пояс процесса и возвращает применённое значение TZ. Явно заданный в окружении пояс (например, TZ=Asia/Yekaterinburg) не
    перебивается; не задан — Москва."""
    if not hasattr(time, "tzset"):      # Windows (разработка): у системы свой пояс, подмена TZ там только путает — ничего не меняем
        return os.environ.get("TZ", "")
    wanted = (zone or os.environ.get("TZ") or DEFAULT_ZONE).strip() or DEFAULT_ZONE
    if wanted.startswith(":"):
        wanted = wanted[1:]
    if "/" in wanted and not _zone_known(wanted):
        wanted = POSIX_FALLBACK if wanted == DEFAULT_ZONE else wanted
    os.environ["TZ"] = wanted
    time.tzset()
    return wanted
