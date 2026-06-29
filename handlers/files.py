"""File management commands: /dir, /dir2, /dir3."""
from core.auth import is_authorized
from file_manager import browser, file_ops, folder_ops


def register(bot) -> None:
    bot.message_handler(commands=['dir'])(lambda m: _cmd_dir(bot, m))
    bot.message_handler(commands=['dir2'])(lambda m: _cmd_dir2(bot, m))
    bot.message_handler(commands=['dir3'])(lambda m: _cmd_dir3(bot, m))
    browser.register(bot)
    folder_ops.register(bot)
    file_ops.register(bot)


def _cmd_dir(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    browser.send_root(bot, message.chat.id)


def _cmd_dir2(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    folder_ops.send_root(bot, message.chat.id)


def _cmd_dir3(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    file_ops.send_root(bot, message.chat.id)
