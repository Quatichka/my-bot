import hashlib
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("привет! чем могу помочь?")


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите токен бота в переменной окружения BOT_TOKEN")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))

    # На Render задана переменная RENDER_EXTERNAL_URL — там работаем через вебхук,
    # чтобы Telegram сам "будил" бесплатный сервер при новом сообщении.
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if external_url:
        print("Бот запущен в режиме вебхука.")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path="webhook",
            webhook_url=f"{external_url}/webhook",
            secret_token=hashlib.sha256(token.encode()).hexdigest()[:32],
        )
    else:
        print("Бот запущен. Нажмите Ctrl+C для остановки.")
        app.run_polling()


if __name__ == "__main__":
    main()
