"""Audit log — журнал действий пользователей (кто что удалил/добавил/изменил).

Пишется в logs/audit.log ОТДЕЛЬНО от основного лога и не очищается командой
/clear_logs — чтобы спустя недели можно было выяснить, кто удалил фильм.
Записи дублируются и в основной лог (через propagate).
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core import config

AUDIT_FILE = Path(config.LOG_FILE).parent / 'audit.log'

_logger = logging.getLogger('audit')
_logger.setLevel(logging.INFO)
_logger.propagate = True  # дублировать в общий лог/журнал systemd

_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
_handler.setFormatter(logging.Formatter('%(asctime)s  %(message)s'))
_logger.addHandler(_handler)


def audit(user, action: str, detail: str = '') -> None:
    """Записать действие. user — объект telebot User, int id или строка ('system')."""
    if hasattr(user, 'id'):
        name = (getattr(user, 'username', None) or getattr(user, 'first_name', None) or '').strip()
        who = f'{user.id} (@{name})' if name else str(user.id)
    else:
        who = str(user)
    _logger.info(f'{who}  {action}  {detail}'.rstrip())


def tail(lines: int = 15) -> str:
    """Последние записи журнала — для команды /audit."""
    try:
        with open(AUDIT_FILE, encoding='utf-8', errors='replace') as f:
            entries = f.readlines()[-lines:]
        return ''.join(entries).strip() or 'Журнал пуст.'
    except FileNotFoundError:
        return 'Журнал пуст.'
