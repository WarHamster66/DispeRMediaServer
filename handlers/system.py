"""System info commands and /start."""
import logging

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.auth import is_authorized
from services import system_monitor

logger = logging.getLogger(__name__)


def register(bot) -> None:
    bot.message_handler(commands=['start'])(lambda m: _cmd_start(bot, m))
    bot.message_handler(commands=['help'])(lambda m: _cmd_help(bot, m))
    bot.message_handler(commands=['uptime'])(lambda m: _simple(bot, m, system_monitor.get_uptime))
    bot.message_handler(commands=['wifi'])(lambda m: _simple(bot, m, system_monitor.get_wifi_signal))
    bot.message_handler(commands=['cpu'])(lambda m: _simple(bot, m, system_monitor.get_cpu_load))
    bot.message_handler(commands=['memory'])(lambda m: _simple(bot, m, system_monitor.get_memory_usage))
    bot.message_handler(commands=['disk'])(lambda m: _simple(bot, m, system_monitor.get_disk_usage))
    bot.message_handler(commands=['logs'])(lambda m: _simple(bot, m, system_monitor.get_last_logs))
    bot.message_handler(commands=['media'])(lambda m: _cmd_media(bot, m))
    bot.message_handler(commands=['export_logs'])(lambda m: _cmd_export_logs(bot, m))
    bot.message_handler(commands=['clear_logs'])(lambda m: _cmd_clear_logs(bot, m))
    bot.message_handler(commands=['backup'])(lambda m: _cmd_backup(bot, m))
    bot.message_handler(commands=['scan'])(lambda m: _cmd_scan(bot, m))


def _simple(bot, message, fn) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.reply_to(message, fn())


def _cmd_start(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('⬇️ Торренты', callback_data='torrent:noop'),
           InlineKeyboardButton('📋 Отчёт', callback_data='report:noop'))
    bot.send_message(
        message.chat.id,
        '👋 Медиасервер онлайн. Используй /help для списка команд.',
    )


def _cmd_help(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    text = (
        '📖 Команды:\n\n'
        '🎬 Торренты\n'
        '/torrent — добавить торрент (файл или magnet)\n'
        '/torrents — список активных загрузок\n'
        '/pause — поставить на паузу\n'
        '/resume — возобновить\n'
        '/clear_downloads — очистить очередь Transmission\n\n'
        '📊 Система\n'
        '/report — полный отчёт о сервере\n'
        '/configure — настроить содержимое отчёта\n'
        '/uptime — время работы\n'
        '/cpu — загрузка CPU\n'
        '/memory — использование RAM\n'
        '/disk — состояние дисков\n'
        '/wifi — уровень WiFi\n'
        '/logs — последние системные логи\n'
        '/export_logs — скачать лог-файл\n'
        '/clear_logs — очистить лог-файл\n'
        '/scan — пересканировать библиотеку Plex\n'
        '/backup — сохранить бэкап настроек на сервер\n\n'
        '📂 Файлы\n'
        '/dir — просмотр и удаление файлов\n'
        '/dir2 — управление папками\n'
        '/dir3 — переименование, перемещение, создание\n'
        '/media — статистика медиатеки\n\n'
        '📜 История\n'
        '/history — история загрузок\n'
    )
    bot.reply_to(message, text)


def _cmd_media(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.send_chat_action(message.chat.id, 'typing')
    bot.reply_to(message, f'📚 Медиатека:\n\n{system_monitor.get_media_stats()}')


def _cmd_export_logs(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        with open(config.LOG_FILE, 'rb') as f:
            bot.send_document(message.chat.id, f, caption='server.log')
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_clear_logs(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        open(config.LOG_FILE, 'w').close()
        bot.reply_to(message, '✅ Лог-файл очищен.')
        logger.info(f'Log file cleared by user {message.from_user.id}')
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_backup(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        from services.backup import save_backup
        path = save_backup()
        bot.reply_to(message, f'🗄 Бэкап сохранён на сервере:\n{path}')
    except Exception as e:
        bot.reply_to(message, f'Ошибка бэкапа: {e}')


def _cmd_scan(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        from services import plex
        n = plex.refresh_libraries()
        bot.reply_to(message, f'🔄 Plex: запущено сканирование {n} библиотек.')
    except Exception as e:
        bot.reply_to(message, f'⚠️ Не удалось обратиться к Plex: {e}')
