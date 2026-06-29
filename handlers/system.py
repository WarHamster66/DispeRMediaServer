"""System info commands and /start."""
import logging
import subprocess
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.auth import is_authorized
from services import system_monitor

logger = logging.getLogger(__name__)

SERVICE_NAME = 'media-server'
_UPDATE_FLAG = config.BASE_DIR / 'data' / '.update_notify'


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
    bot.message_handler(commands=['update'])(lambda m: _do_update(bot, m.chat.id, m.from_user.id))
    bot.callback_query_handler(func=lambda c: c.data == 'sys:update')(lambda c: _cb_update(bot, c))
    _notify_after_restart(bot)


def _notify_after_restart(bot) -> None:
    """Если перезапуск был вызван командой /update — сообщить, что бот снова онлайн."""
    if not _UPDATE_FLAG.exists():
        return
    try:
        chat_id = int(_UPDATE_FLAG.read_text().strip())
        commit = subprocess.run(
            ['git', '-C', str(config.BASE_DIR), 'log', '-1', '--format=%h %s'],
            capture_output=True, text=True,
        ).stdout.strip()
        bot.send_message(chat_id, f'✅ Обновление применено, бот снова в строю.\n{commit}')
    except Exception as e:
        logger.warning(f'Update notify failed: {e}')
    finally:
        try:
            _UPDATE_FLAG.unlink()
        except OSError:
            pass


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
    kb.add(InlineKeyboardButton('🔄 Обновить медиасервер', callback_data='sys:update'))
    bot.send_message(
        message.chat.id,
        '👋 Медиасервер онлайн. Используй /help для списка команд.',
        reply_markup=kb,
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
        '/backup — сохранить бэкап настроек на сервер\n'
        '/update — обновить медиасервер с GitHub и перезапустить\n\n'
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


def _cb_update(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    bot.answer_callback_query(call.id, 'Запускаю обновление…')
    _do_update(bot, call.message.chat.id, call.from_user.id)


def _do_update(bot, chat_id: int, user_id: int) -> None:
    """git pull + обновление зависимостей + перезапуск сервиса."""
    if not is_authorized(user_id):
        bot.send_message(chat_id, 'Нет доступа.')
        return

    base = str(config.BASE_DIR)
    bot.send_message(chat_id, '🔄 Проверяю обновления…')

    # git pull
    try:
        r = subprocess.run(['git', '-C', base, 'pull', '--ff-only'],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        bot.send_message(chat_id, f'❌ Ошибка git pull: {e}')
        return

    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        bot.send_message(chat_id, f'❌ git pull не удался:\n{out[:600]}')
        return
    if 'up to date' in out.lower() or 'up-to-date' in out.lower():
        bot.send_message(chat_id, '✅ Уже последняя версия.')
        return

    # обновляем зависимости (вдруг появились новые)
    pip = config.BASE_DIR / 'venv' / 'bin' / 'pip'
    if pip.exists():
        try:
            subprocess.run([str(pip), 'install', '-q', '-r', str(config.BASE_DIR / 'requirements.txt')],
                           timeout=300)
        except Exception as e:
            logger.warning(f'pip update failed: {e}')

    commit = subprocess.run(['git', '-C', base, 'log', '-1', '--format=%h %s'],
                            capture_output=True, text=True).stdout.strip()
    bot.send_message(chat_id, f'✅ Обновлено до:\n{commit}\n\n♻️ Перезапускаюсь…')

    # Оставляем флажок, чтобы после рестарта прислать «бот снова в строю»
    try:
        _UPDATE_FLAG.write_text(str(chat_id))
    except Exception as e:
        logger.warning(f'Could not write update flag: {e}')

    # Перезапуск выполняет systemd (наш процесс при этом завершится).
    # Требуется правило sudoers NOPASSWD на systemctl restart (ставит установщик).
    subprocess.Popen(['sudo', '-n', 'systemctl', 'restart', SERVICE_NAME])
