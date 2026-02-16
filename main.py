"""Запуск бота (BotFather) и фонового воркера рассылки."""
import asyncio
import logging
import sys

from config import get_settings, ensure_dirs
from app.database import init_db
from app.bot import build_app
from app.task_runner import start_runner

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def main():
    ensure_dirs()
    settings = get_settings()
    if not settings.bot_token:
        logger.error("Задайте BOT_TOKEN в .env (токен от @BotFather)")
        sys.exit(1)
    await init_db(settings.database_url)
    await start_runner()
    app = build_app(settings.bot_token)
    logger.info("Бот запущен (long polling)")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Держим цикл активным (start_polling только запускает фоновую задачу)
    stop = asyncio.Event()
    try:
        await stop.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
