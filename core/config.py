import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_BASE = Path(__file__).parent.parent
_raw = json.loads((_BASE / 'config.json').read_text(encoding='utf-8'))


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def _ids(raw: str) -> list[int]:
    """'1, 2,3,' → [1, 2, 3] (пробелы и лишние запятые не ломают разбор)."""
    return [int(p.strip()) for p in raw.split(',') if p.strip()]


# Secrets — from .env
TOKEN: str = _require('TELEGRAM_TOKEN')
CHAT_ID: int = int(_require('TELEGRAM_CHAT_ID'))
# Доверенные пользователи — могут пользоваться ботом
CREATOR_IDS: list[int] = _ids(_require('CREATOR_IDS'))
# Администраторы — дополнительно могут перезагружать сервер, обновлять бота и т.п.
# По умолчанию — владелец (TELEGRAM_CHAT_ID), если это личный чат, а не группа.
ADMIN_IDS: list[int] = _ids(os.environ.get('ADMIN_IDS', '')) or ([CHAT_ID] if CHAT_ID > 0 else [])
TRANSMISSION_HOST: str = _require('TRANSMISSION_HOST')
TRANSMISSION_PORT: int = int(os.environ.get('TRANSMISSION_PORT', '9091'))
TRANSMISSION_USER: str = _require('TRANSMISSION_USER')
TRANSMISSION_PASSWORD: str = _require('TRANSMISSION_PASSWORD')
WEATHER_API_KEY: str = os.environ.get('WEATHER_API_KEY', '')  # optional — weather disabled if empty
PROXY_URL: str | None = os.environ.get('PROXY_URL') or None

# Base project directory (used for backups)
BASE_DIR: Path = _BASE

# Paths — from config.json
DISK_PATH: str = _raw['DISK_PATH']
DOWNLOAD_DIR: str = _raw['DOWNLOAD_DIR']
SHARED_FOLDER: str = _raw['SHARED_FOLDER']

LOG_FILE: str = str(_BASE / _raw.get('LOG_FILE', 'logs/server.log'))
HISTORY_FILE: str = str(_BASE / _raw.get('HISTORY_FILE', 'data/download_history.json'))
NETWORK_FILE: str = str(_BASE / _raw.get('NETWORK_FILE', 'data/network_usage.json'))
REPORT_CONFIG_FILE: str = str(_BASE / _raw.get('REPORT_CONFIG_FILE', 'data/report_config.json'))

# Settings
WEATHER_CITY: str = _raw.get('WEATHER_CITY', 'Moscow')
TIMEZONE: str = _raw.get('TIMEZONE', 'system')  # 'system' = время сервера; или 'Asia/Yekaterinburg' и т.п.
USE_ALLOWED_FOLDERS: bool = _raw.get('USE_ALLOWED_FOLDERS', True)
ALLOWED_FOLDERS: list[str] = _raw.get('ALLOWED_FOLDERS', [])
PROTECTED_FOLDERS: list[str] = _raw.get('PROTECTED_FOLDERS', [])
CUSTOM_SERVICES: list[str] = _raw.get('CUSTOM_SERVICES', [])
HDD_DEVICE: str = _raw.get('HDD_DEVICE', '/dev/sdb')
DISK_ALERT_THRESHOLD: int = _raw.get('DISK_ALERT_THRESHOLD_PERCENT', 10)

# Plex (для авто-сканирования библиотеки после загрузки)
PLEX_URL: str = _raw.get('PLEX_URL', 'http://localhost:32400')
PLEX_TOKEN: str = _raw.get('PLEX_TOKEN', '')  # необязателен: с localhost Plex пускает без токена

# Подписки на сериалы с LostFilm (логин и пароль — секрет, лежат в .env)
LOSTFILM_EMAIL: str = os.environ.get('LOSTFILM_EMAIL', '')
LOSTFILM_PASSWORD: str = os.environ.get('LOSTFILM_PASSWORD', '')
LOSTFILM_URL: str = _raw.get('LOSTFILM_URL', '')                  # пусто — первое рабочее зеркало
SERIES_FOLDER: str = _raw.get('SERIES_FOLDER', 'Сериалы')         # куда качать новые серии

# Поиск по RuTracker (логин и пароль — секрет, лежат в .env). Сайт закрыт
# проверкой Cloudflare — её проходит FlareSolverr (ставит setup_rutracker.py)
RUTRACKER_USER: str = os.environ.get('RUTRACKER_USER', '')
RUTRACKER_PASSWORD: str = os.environ.get('RUTRACKER_PASSWORD', '')
RUTRACKER_URL: str = _raw.get('RUTRACKER_URL', 'https://rutracker.org/')
FLARESOLVERR_URL: str = _raw.get('FLARESOLVERR_URL', 'http://127.0.0.1:8191')
SERIES_QUALITY: str = _raw.get('SERIES_QUALITY', '1080')          # предпочитаемое качество
SERIES_CHECK_MINUTES: int = int(_raw.get('SERIES_CHECK_MINUTES', 30))

# Папка для еженедельных бэкапов (по умолчанию ~/media-server-backups)
_backup = _raw.get('BACKUP_DIR', '').strip()
BACKUP_DIR: str = str(Path(_backup).expanduser()) if _backup else str(Path.home() / 'media-server-backups')

# Ensure runtime directories exist
for _p in (LOG_FILE, HISTORY_FILE, NETWORK_FILE, REPORT_CONFIG_FILE):
    Path(_p).parent.mkdir(parents=True, exist_ok=True)
