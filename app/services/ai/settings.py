"""Включение режимов ИИ («Настройки → ИИ», только администратор). Хранится в app_flags (ключ ai_config):
  bot_voice — бот принимает голосовые итоги монтёров (расшифровка + разбор на «что сделано / причина / что нужно»);
  assistant — страница «Помощник» в веб-панели (вопросы по данным, заполнение шаблонов Excel/Word);
  forms_fault — ИИ разбирает отчёты Яндекс Форм, где не удалось починить: что неисправно и что нужно — в отчёт и в карточку комплекса (app/services/ai/forms_fault.py).
По умолчанию всё выключено: ИИ включают осознанно, когда он установлен и проверен.
Отдельно — где считать (по умолчанию «и на сервере»):
  server_llm — языковая модель на самом сервере (контейнер llm); выключено — вопросы считают только компьютеры-агенты, а если свободных нет — понятная ошибка;
  server_asr — распознавание речи на сервере (контейнер asr; оно считается только на сервере) — выключено, голосовые итоги не принимаются."""
from __future__ import annotations

import json
import time
from typing import Any

from app.db.database import get_connection

CONFIG_KEY = "ai_config"
DEFAULTS: dict[str, Any] = {"bot_voice": False, "assistant": False, "forms_fault": False, "server_llm": True, "server_asr": True}

_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def load(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < 10:
        return dict(_cache["value"])
    cfg = dict(DEFAULTS)
    async with get_connection() as conn:
        cur = await conn.execute("select value from app_flags where key = ?", (CONFIG_KEY,))
        row = await cur.fetchone()
    if row:
        try:
            saved = json.loads(row[0])
            for key in DEFAULTS:
                if key in saved:
                    cfg[key] = bool(saved[key])
        except ValueError:
            pass
    _cache.update(at=now, value=cfg)
    return dict(cfg)


async def save(changes: dict[str, Any]) -> dict[str, Any]:
    cfg = await load(force=True)
    for key in DEFAULTS:
        if key in changes and changes[key] is not None:
            cfg[key] = bool(changes[key])
    async with get_connection() as conn:
        await conn.execute(
            "insert into app_flags (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
            (CONFIG_KEY, json.dumps(cfg)),
        )
        await conn.commit()
    _cache.update(at=time.monotonic(), value=cfg)
    return dict(cfg)
