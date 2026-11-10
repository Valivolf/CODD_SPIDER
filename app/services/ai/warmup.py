"""Прогрев подсказок языковой модели. Подсказка помощника — это ≈3000 «слов» (описание данных, правила, примеры), и на процессоре
первый разбор «с нуля» занимает минуты. Модель запоминает уже прочитанное начало запроса, поэтому достаточно один раз «прочитать» постоянную
часть (с ответом в одно слово) — следующие вопросы отвечаются быстро. Греем при запуске приложения и при включении режима в настройках;
работает в фоне с низкой важностью (голосовой итог монтёра и вопросы людей идут вперёд)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from app.services.ai import client, doc_fill, narrate, qa, query_engine as qe, report_parser
from app.services.ai import settings as ai_settings

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


async def _warm_voice() -> None:
    await client.llm_chat(
        report_parser.SYSTEM, "Тип выезда: Ремонт.\nРасшифровка: «приехали, заменили блок питания»", max_tokens=1,
        priority=client.PRIORITY_BACKGROUND, history=report_parser._messages_history(),
    )


async def _warm_assistant() -> None:
    today = dt.date.today()
    enums = await qe.enum_values()
    await client.llm_chat(
        qa.build_system(enums), "Сколько всего комплексов?" + qa.date_note(today), max_tokens=1,
        priority=client.PRIORITY_BACKGROUND, history=qa._history_messages(),
    )
    # Вторая постоянная часть: подсказка, по которой модель пишет ответ словами по данным (образцы в начале) — иначе первый ответ после запуска читает её с нуля
    system, history = narrate.data_prefix()
    await client.llm_chat(system, "Вопрос: Сколько всего комплексов?\n\nДанные:\nВсего найдено записей: 0.", max_tokens=1, priority=client.PRIORITY_BACKGROUND, history=history)


async def _warm_docfill() -> None:
    system, history = doc_fill.prompt_prefix(await qe.enum_values())
    await client.llm_chat(system, "Шаблон: Excel «Пример.xlsx».\nT1 — таблица, лист «Лист1», заголовки в строке 1: столбец 1 «Дата».", max_tokens=1,
                          priority=client.PRIORITY_BACKGROUND, history=history)


async def warm() -> list[str]:
    """Прогревает подсказки включённых режимов. Возвращает, что прогрето (для проверок); ошибки — в журнал, не наружу."""
    cfg = await ai_settings.load(force=True)
    done: list[str] = []
    if not cfg.get("server_llm", True):
        return done                                  # ИИ на сервере выключен — считают компьютеры-агенты, греть нечего
    for name, enabled, fn in (("voice", cfg.get("bot_voice"), _warm_voice), ("assistant", cfg.get("assistant"), _warm_assistant),
                              ("docfill", cfg.get("assistant"), _warm_docfill)):
        if not enabled:
            continue
        try:
            await fn()
            done.append(name)
        except (client.AiUnavailable, client.AiError) as exc:
            logger.info("Прогрев подсказки «%s» не удался: %s", name, exc)
            break
        except Exception:  # noqa: BLE001
            logger.exception("Прогрев подсказки «%s» не удался", name)
            break
    return done


def schedule() -> None:
    """Прогреть в фоне (после включения режима в настройках)."""
    task = asyncio.get_running_loop().create_task(warm())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def startup_warm(delay: float = 45.0, attempts: int = 40, pause: float = 30.0) -> None:
    """При запуске: ждёт, пока модель загрузится в память (это долго), и греет включённые режимы. Выключено/недоступно — ничего не делает."""
    await asyncio.sleep(delay)
    for _ in range(attempts):
        try:
            cfg = await ai_settings.load(force=True)
            if not (cfg.get("bot_voice") or cfg.get("assistant")) or not cfg.get("server_llm", True):
                return
            if (await client.status(force=True))["llm"].get("ok"):
                await warm()
                return
        except Exception:  # noqa: BLE001
            logger.debug("Ожидание модели для прогрева", exc_info=True)
        await asyncio.sleep(pause)


async def watch_model(pause: float = 30.0) -> None:
    """Контейнер модели перезапустили (или модель на время пропала) — её кэш подсказки пуст, и первый вопрос читал бы подсказку 3–5 минут. Как только модель снова
    отвечает, греем подсказки заново, не дожидаясь вопроса. Работает всё время, пока приложение запущено; режимы ИИ выключены — ничего не делает."""
    was_ok: Optional[bool] = None
    while True:
        try:
            cfg = await ai_settings.load(force=True)
            if (cfg.get("bot_voice") or cfg.get("assistant")) and cfg.get("server_llm", True):
                ok = bool((await client.status(force=True))["llm"].get("ok"))
                if was_ok is False and ok:
                    logger.info("ИИ: модель снова отвечает (перезапуск контейнера?) — прогреваю подсказки заново")
                    await warm()
                was_ok = ok
            else:
                was_ok = None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("Слежение за моделью", exc_info=True)
        await asyncio.sleep(pause)
