"""Download history command."""
from core.auth import is_authorized
from services import download_history


def register(bot) -> None:
    bot.message_handler(commands=['history'])(lambda m: _cmd_history(bot, m))


def _cmd_history(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.reply_to(message, f'📜 Последние загрузки:\n\n{download_history.format_history(20)}')
