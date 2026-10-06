"""Контроль доступа.

Два уровня:
  • доверенные (CREATOR_IDS) — пользуются ботом: торренты, отчёты, файлы;
  • администраторы (ADMIN_IDS) — плюс опасные операции: перезагрузка сервера,
    обновление бота, очистка логов, журнал аудита. Админ всегда считается доверенным.

Попытки доступа от посторонних пишутся в аудит-лог (не чаще раза в час на ID).
"""
import time

from core import config

_denied_logged: dict[tuple[int, str], float] = {}


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def is_authorized(user_id: int) -> bool:
    if user_id in config.CREATOR_IDS or user_id in config.ADMIN_IDS:
        return True
    _log_denied(user_id, 'ACCESS_DENIED')
    return False


def require_admin(user_id: int) -> bool:
    """True если админ; иначе пишет попытку в аудит и возвращает False."""
    if is_admin(user_id):
        return True
    _log_denied(user_id, 'ADMIN_DENIED')
    return False


def _log_denied(user_id: int, action: str) -> None:
    now = time.time()
    key = (user_id, action)
    if now - _denied_logged.get(key, 0) < 3600:
        return
    _denied_logged[key] = now
    from core.audit import audit  # локальный импорт: audit сам импортирует config
    audit(user_id, action)
