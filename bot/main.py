import logging
import os
import sys
import time

from vkbottle import Bot

from bot.config import settings
from bot.handlers.mechanic import get_mechanic_router
from bot.middlewares.auth_mw import AuthMiddleware, _bot_enabled
from bot.service import mechanic as mechanic_service

# Время — московское (как у приложения: app/core/tz.py); образ тот же, пояс приходит из TZ в docker-compose.
if hasattr(time, "tzset"):          # на Windows (разработка) у системы свой пояс
    os.environ.setdefault("TZ", "Europe/Moscow")
    time.tzset()

# Консоль Windows (cmd.exe) по умолчанию использует локальную кодовую страницу
# (например, cp1251), которая не может закодировать эмодзи из логов - без
# этого логгер падал с UnicodeEncodeError на каждом сообщении с эмодзи.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main():
    logger.info("🚀 Starting mechanic bot (lan_calendar-backed, no local DB)...")
    logger.info("lan_calendar API: %s", settings.lan_calendar_api_url)

    bot = Bot(token=settings.vk_token)

    bot.labeler.message_view.register_middleware(AuthMiddleware)
    bot.labeler.raw_event_view.register_middleware(AuthMiddleware)
    logger.info("🛡 Middleware registered")

    bot.labeler.load(get_mechanic_router())
    logger.info("✅ Router loaded")

    # Ответы диспетчера из веб-панели (чат) пересылаются монтёрам раз в несколько секунд — только пока бот включён.
    @bot.loop_wrapper.interval(seconds=3)
    async def _deliver_chat():
        try:
            if await _bot_enabled():
                await mechanic_service.deliver_chat_messages()
        except Exception:
            logger.exception("Чат: ошибка пересылки ответов диспетчера")

    logger.info("📡 Connecting to VK API (Long Poll)...")
    try:
        bot.run_forever()
    except Exception:
        logger.exception("💥 Critical error")


if __name__ == '__main__':
    main()
