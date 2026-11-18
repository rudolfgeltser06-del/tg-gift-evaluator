import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import settings
from database import init_db
from handlers import router

logging.basicConfig(level=logging.INFO)


async def healthcheck(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def on_startup(bot: Bot):
    await init_db()
    if settings.WEBHOOK_URL:
        webhook_full = f"{settings.WEBHOOK_URL.rstrip('/')}/webhook"
        await bot.set_webhook(webhook_full, drop_pending_updates=True)
        logging.info(f"Webhook установлен: {webhook_full}")


async def main():
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    if settings.WEBHOOK_URL:
        app = web.Application()
        app.router.add_get("/", healthcheck)
        app.router.add_get("/health", healthcheck)

        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=settings.PORT)
        await site.start()
        logging.info(f"Сервер запущен на порту {settings.PORT}")
        await asyncio.Event().wait()
    else:
        await init_db()
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Бот запущен в режиме Polling")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
