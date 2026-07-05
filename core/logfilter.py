"""Фильтр логов: приглушает шум от временных сетевых сбоев.

telebot на каждый обрыв связи с api.telegram.org (прокси моргнул, 502, таймаут)
пишет полный traceback в ~60 строк. Эти ошибки самолечатся (infinity_polling
переподключается), поэтому мы схлопываем их в одну короткую строку не чаще
раза в минуту вместо простыней.
"""
import logging
import time

_TRANSIENT = (
    'Network is unreachable',
    'Max retries exceeded',
    'Read timed out',
    'Bad Gateway',
    'Connection aborted',
    'Connection reset',
    'ProxyConnectionError',
    'NewConnectionError',
    'Temporary failure in name resolution',
    'RemoteDisconnected',
    'ConnectionError',
    'ReadTimeout',
)


class TransientNetworkFilter(logging.Filter):
    """Схлопывает повторяющиеся сетевые ошибки в одну короткую строку."""

    def __init__(self, min_interval: int = 60):
        super().__init__()
        self._min_interval = min_interval
        self._last = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
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
