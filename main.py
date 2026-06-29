"""Media Server Bot — entry point."""
import logging

# ── logging must be configured before any other imports ──────────────────────
from core import config  # also loads .env

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(name)-28s  %(levelname)s  %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ── bot setup ─────────────────────────────────────────────────────────────────
import telebot
from telebot import apihelper

if config.PROXY_URL:
    apihelper.proxy = {'https': config.PROXY_URL}
    logger.info(f'Using proxy: {config.PROXY_URL.split("@")[-1]}')

bot = telebot.TeleBot(config.TOKEN, parse_mode=None)

bot.set_my_commands([
    telebot.types.BotCommand('/torrent', 'Добавить торрент или magnet-ссылку'),
    telebot.types.BotCommand('/torrents', 'Активные загрузки'),
    telebot.types.BotCommand('/pause', 'Пауза торрента'),
    telebot.types.BotCommand('/resume', 'Возобновить торрент'),
    telebot.types.BotCommand('/report', 'Отчёт о сервере'),
    telebot.types.BotCommand('/configure', 'Настроить отчёт'),
    telebot.types.BotCommand('/media', 'Статистика медиатеки'),
    telebot.types.BotCommand('/history', 'История загрузок'),
    telebot.types.BotCommand('/dir', 'Файловый менеджер'),
    telebot.types.BotCommand('/help', 'Все команды'),
])

# ── register handlers ─────────────────────────────────────────────────────────
from handlers import files, history, reports, system, torrent

torrent.register(bot)
reports.register(bot)
files.register(bot)
system.register(bot)
history.register(bot)

# ── background tasks ──────────────────────────────────────────────────────────
from scheduler.tasks import start_background_tasks

start_background_tasks(bot)

# ── run ───────────────────────────────────────────────────────────────────────
logger.info('Bot started. Polling…')
try:
    bot.infinity_polling(timeout=60, long_polling_timeout=60, logger_level=logging.WARNING)
except KeyboardInterrupt:
    logger.info('Stopped by user')
except Exception as e:
    logger.critical(f'Fatal error: {e}', exc_info=True)
finally:
    logger.info('Bot stopped')
