"""Фильтр логов: приглушает шум от временных сетевых сбоев.

telebot на каждый обрыв связи с api.telegram.org (прокси моргнул, 502, таймаут)
пишет полный traceback в ~60 строк. Эти ошибки самолечатся (infinity_polling
переподключается), поэтому мы схлопываем их в одну короткую строку не чаще
раза в минуту вместо простыней.
"""
import logging
import re
import time

_TRANSIENT = (
    'Network is unreachable',
    'Max retries exceeded',
    'Read timed out',
    'Bad Gateway',            # 502
    'Gateway Time-out',       # 504
    'Too Many Requests',      # 429
    'retry after',            # 429 hint
    'Connection aborted',
    'Connection reset',
    'ProxyConnectionError',
    'NewConnectionError',
    'Temporary failure in name resolution',
    'RemoteDisconnected',
    'ConnectionError',
    'ReadTimeout',
)


# Токен бота (есть в адресе каждого запроса к Telegram) и логин:пароль прокси
_TOKEN_RE = re.compile(r'\d{6,}:[A-Za-z0-9_-]{30,}')
_URL_AUTH_RE = re.compile(r'(://)[^/@\s]+@')


def redact(text: str) -> str:
    return _URL_AUTH_RE.sub(r'***@', _TOKEN_RE.sub('***', text))


class RedactingFormatter(logging.Formatter):
    """Прячет секреты в готовой строке лога, включая traceback."""

    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return redact(self._inner.format(record))


def redact_handlers(*logger_names: str) -> None:
    """Включить скрытие секретов у обработчиков указанных логгеров ('' — корневой)."""
    for name in logger_names:
        for h in logging.getLogger(name or None).handlers:
            if not isinstance(h.formatter, RedactingFormatter):
                h.setFormatter(RedactingFormatter(h.formatter or logging.Formatter()))


class TransientNetworkFilter(logging.Filter):
    """Схлопывает повторяющиеся сетевые ошибки в одну короткую строку."""

    def __init__(self, min_interval: int = 60):
        super().__init__()
        self._min_interval = min_interval
        self._last = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        # Только записи telebot/urllib3: фильтр висит и на общих обработчиках, и
        # без этой проверки ошибки погоды, Transmission или LostFilm с похожим
        # текстом превращались в «сбой связи с Telegram» или пропадали совсем.
        if not record.name.startswith(('TeleBot', 'urllib3')):
            return True
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text += ' ' + repr(record.exc_info[1])

        if not any(sig in text for sig in _TRANSIENT):
            return True  # не сетевая ошибка — пропускаем как есть

        now = time.time()
        if now - self._last < self._min_interval:
            return False  # недавно уже сообщали — глушим полностью

        self._last = now
        record.msg = '⚠️ Временный сбой связи с Telegram (сеть/прокси) — переподключаюсь…'
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.levelno = logging.WARNING
        record.levelname = 'WARNING'
        return True
