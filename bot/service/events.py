"""Ответ на нажатие callback-кнопки (VK message_event).

Пока бот не ответит на событие (messages.sendMessageEventAnswer), у нажатой
кнопки в VK крутится «загрузка» — и если ответа нет совсем, она крутится
бесконечно. Сообщение с результатом (messages.send) на это не влияет: это два
разных ответа. Поэтому каждый обработчик кнопки отвечает на событие сам,
когда закончил работу (см. handlers/mechanic.py::on_action), а то, что не
подошло ни под один обработчик (старые кнопки, отклонённые middleware), —
отвечает отдельно, с короткой подсказкой."""
import asyncio
import json
import logging

from bot.service.mechanic import get_api

logger = logging.getLogger(__name__)

# VK показывает всплывающую подсказку (snackbar) до 90 символов.
SNACKBAR_MAX = 90


def is_message_event(event) -> bool:
    return isinstance(event, dict) and event.get("type") == "message_event"


async def ack_event(event, text: str | None = None) -> None:
    """Отвечает на нажатие кнопки: без `text` — просто убирает «загрузку» с
    кнопки, с `text` — ещё и показывает короткую подсказку. На одно событие
    VK принимает один ответ, повторный (или по просроченному event_id —
    он живёт около минуты) просто даст ошибку, которую здесь глотаем."""
    if not is_message_event(event):
        return
    obj = event.get("object") or {}
    event_id, user_id, peer_id = obj.get("event_id"), obj.get("user_id"), obj.get("peer_id")
    if not (event_id and user_id and peer_id):
        return
    event_data = None
    if text:
        event_data = json.dumps({"type": "show_snackbar", "text": " ".join(text.split())[:SNACKBAR_MAX]}, ensure_ascii=False)
    try:
        await get_api().messages.send_message_event_answer(
            event_id=event_id, user_id=user_id, peer_id=peer_id, event_data=event_data
        )
    except Exception:  # noqa: BLE001 — уже отвечено/просрочено: кнопка не должна из-за этого ломать обработку
        logger.warning("Не удалось ответить на нажатие кнопки (event_id=%s)", event_id, exc_info=True)


class EventAck:
    """Гарантирует один ответ на событие: после выполнения обработчика (что
    бы с ним ни случилось) и — на случай зависшего обработчика — через
    `limit` секунд, чтобы кнопка не крутилась дольше."""

    def __init__(self, event, limit: float = 15.0):
        self.event = event
        self.limit = limit
        self._done = False
        self._timer = None

    async def _send(self, text: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        await ack_event(self.event, text)

    def start(self) -> "EventAck":
        self._timer = asyncio.get_running_loop().call_later(self.limit, lambda: asyncio.ensure_future(self._send()))
        return self

    async def finish(self, text: str | None = None) -> None:
        if self._timer:
            self._timer.cancel()
        await self._send(text)
