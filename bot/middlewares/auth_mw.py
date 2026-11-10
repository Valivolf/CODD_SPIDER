"""Авторизация монтёра через lan_calendar (people.vk_id), без локальной БД.

Логика извлечения user_id/payload из сырого события скопирована как есть
из прежней версии — это уже отлаженный код, устойчивый и к message_new,
и к message_event, и к разным формам объекта (dict/pydantic)."""
import json
import logging
import random
import time

from vkbottle import BaseMiddleware
from vkbottle.bot import Message, MessageEvent

from bot import api_client
from bot.keyboards.mechanic_menu import get_deactivated_keyboard, get_unauthorized_keyboard
from bot.service.events import ack_event, is_message_event
from bot.service.mechanic import get_api

logger = logging.getLogger(__name__)

# Два возможных живых процесса бота (сервер организации + иногда чей-то
# ноутбук для тестов) — VK Long Poll отдаёт одни и те же события обоим
# независимо, так что при двух одновременно живых ответы дублируются и
# state в navigation.py у каждого расходится. Этот флаг (см. /api/bot/enabled)
# — единственный способ быстро заглушить один из них с веб-панели, не
# трогая сам процесс. Кэш на 10с, чтобы не дёргать lan_calendar на каждое
# сообщение/фото/кнопку. При недоступности сервера остаёмся в ПРЕЖНЕМ состоянии (был включён — отвечаем «сервер недоступен», был выключен —
# молчим). Если состояние ещё не известно (бот только что запустился, а приложение после перезапуска ещё не
# поднялось), бот молчит: раньше он считал себя включённым и во время каждого деплоя отвечал на сообщения
# даже на машине, где его выключили в веб-панели.
_ENABLED_CACHE_TTL = 10.0
_ENABLED_RETRY_TTL = 3.0
_enabled_cache: bool = False
_enabled_known: bool = False
_enabled_cache_at: float = 0.0
_enabled_ttl: float = _ENABLED_CACHE_TTL


async def _bot_enabled() -> bool:
    global _enabled_cache, _enabled_known, _enabled_cache_at, _enabled_ttl
    now = time.monotonic()
    if now - _enabled_cache_at < _enabled_ttl:
        return _enabled_cache
    try:
        _enabled_cache = await api_client.get_bot_enabled()
        _enabled_known = True
        _enabled_ttl = _ENABLED_CACHE_TTL
    except Exception:  # noqa: BLE001 — недоступен lan_calendar
        _enabled_ttl = _ENABLED_RETRY_TTL      # спросим снова через пару секунд
        if _enabled_known:
            logger.warning("AuthMiddleware: не удалось проверить /api/bot/enabled, остаюсь в прежнем состоянии (%s)", "включён" if _enabled_cache else "выключен")
        else:
            logger.warning("AuthMiddleware: состояние бота ещё не известно (приложение недоступно) — пока молчу")
            _enabled_cache = False
    _enabled_cache_at = now
    return _enabled_cache


class AuthMiddleware(BaseMiddleware[Message | MessageEvent]):
    async def pre(self):
        if isinstance(self.event, dict):
            event_type = self.event.get("type")
            if event_type in ["message_read", "message_reply", "message_typing_state"]:
                return True

        if not await _bot_enabled():
            return self.stop("Bot disabled from web panel")

        user_id = None
        peer_id = None
        payload = {}

        if isinstance(self.event, dict):
            user_id = self.event.get("user_id") or self.event.get("from_id")
            peer_id = self.event.get("peer_id")
            payload = self.event.get("payload", {})
            if "object" in self.event:
                obj = self.event["object"]
                if isinstance(obj, dict):
                    if not user_id:
                        user_id = obj.get("user_id") or obj.get("from_id")
                    if not peer_id:
                        peer_id = obj.get("peer_id")
                    obj_payload = obj.get("payload")
                    if obj_payload:
                        payload = obj_payload
                    if isinstance(obj.get("message"), dict):
                        if not user_id:
                            user_id = obj["message"].get("from_id")
                        if not peer_id:
                            peer_id = obj["message"].get("peer_id")
        else:
            user_id = getattr(self.event, "user_id", None) or getattr(self.event, "from_id", None)
            peer_id = getattr(self.event, "peer_id", None)
            payload = getattr(self.event, "payload", {})
            if hasattr(self.event, "object"):
                obj = self.event.object
                if isinstance(obj, dict):
                    if not user_id:
                        user_id = obj.get("user_id") or obj.get("from_id")
                    if not peer_id:
                        peer_id = obj.get("peer_id")
                    obj_payload = obj.get("payload")
                    if obj_payload:
                        payload = obj_payload
                else:
                    if not user_id:
                        user_id = getattr(obj, "user_id", getattr(obj, "from_id", None))
                    if not peer_id:
                        peer_id = getattr(obj, "peer_id", None)
                    obj_payload = getattr(obj, "payload", None)
                    if obj_payload:
                        payload = obj_payload

        if not user_id:
            return True  # системное событие без пользователя — пропускаем

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        elif not isinstance(payload, dict):
            payload = {}

        try:
            user = await api_client.get_me(user_id)
        except Exception:  # noqa: BLE001 — lan_calendar недоступен сейчас
            logger.exception("AuthMiddleware: lan_calendar API недоступен")
            await self._send_rejection(
                "⚠️ Сервер сейчас недоступен, попробуйте через минуту.", None
            )
            return self.stop("API unavailable")

        if not user:
            if payload.get("action") == "request_join":
                try:
                    await self._handle_join_request(user_id, peer_id or user_id)
                finally:
                    await ack_event(self.event)  # иначе у кнопки «Отправить заявку» вечная загрузка
                return self.stop("Join request handled")
            await self._send_rejection(
                "⛔ Ваш VK не привязан ни к одному монтёру в системе.\n"
                "Обратитесь к диспетчеру или нажмите «Отправить заявку».",
                get_unauthorized_keyboard(),
                snackbar="⛔ Ваш VK не привязан к монтёру. Обратитесь к диспетчеру.",
            )
            return self.stop("Unknown vk_id")

        if not user.get("active"):
            await self._send_rejection(
                "⛔ Ваш аккаунт деактивирован.\nОбратитесь к диспетчеру.",
                get_deactivated_keyboard(),
                snackbar="⛔ Аккаунт деактивирован. Обратитесь к диспетчеру.",
            )
            return self.stop("Deactivated")

        self._set_context("user", user)
        return True

    def _set_context(self, key: str, value):
        if isinstance(self.event, dict):
            self.event[key] = value
        else:
            self.event.__dict__[key] = value

    async def _send_rejection(self, text: str, keyboard, snackbar: str | None = None):
        if isinstance(self.event, Message):
            await self.event.answer(text, keyboard=keyboard)
        elif isinstance(self.event, MessageEvent):
            await self.event.show_snackbar(text)
        elif is_message_event(self.event):
            # Нажатие callback-кнопки приходит сырым dict: отвечаем подсказкой
            # (она же убирает «загрузку» с кнопки).
            await ack_event(self.event, snackbar or text)

    async def _handle_join_request(self, vk_id: int, peer_id: int) -> None:
        """Кто-то нажал «Отправить заявку» — вместо того чтобы диспетчер
        сам выспрашивал VK ID, заявка уходит в lan_calendar и отдаётся в
        разделе «Персонал» (кнопки «Добавить»/«Заблокировать»)."""
        vk_name = await self._fetch_vk_name(vk_id)
        try:
            result = await api_client.submit_join_request(vk_id, vk_name)
        except Exception:  # noqa: BLE001 — lan_calendar недоступен сейчас
            logger.exception("AuthMiddleware: не удалось отправить заявку на доступ")
            await self._reply(peer_id, "⚠️ Сервер сейчас недоступен, попробуйте через минуту.")
            return
        if result.get("status") == "blocked":
            await self._reply(peer_id, "⛔ Доступ ограничен. Обратитесь к диспетчеру напрямую.")
        elif result.get("cooldown_active"):
            await self._reply(peer_id, "⏳ Заявка уже отправлена, ожидайте решения диспетчера.")
        else:
            await self._reply(
                peer_id,
                "✅ Заявка отправлена диспетчеру. Как только вас привяжут в системе — бот заработает.",
            )

    @staticmethod
    async def _fetch_vk_name(vk_id: int) -> str | None:
        try:
            users = await get_api().users.get(user_ids=[vk_id])
        except Exception:  # noqa: BLE001 — лучше заявка без имени, чем никак
            return None
        if not users:
            return None
        u = users[0]
        first = getattr(u, "first_name", "") or ""
        last = getattr(u, "last_name", "") or ""
        name = f"{first} {last}".strip()
        return name or None

    @staticmethod
    async def _reply(peer_id: int, text: str) -> None:
        await get_api().messages.send(peer_id=peer_id, message=text, random_id=random.randint(1, 2**31))
