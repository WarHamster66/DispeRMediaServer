"""Ввод логина и пароля от сайта прямо в боте (LostFilm, RuTracker).

Только в личке; сообщение с паролем бот сразу удаляет. Значения пишутся
в .env и сразу применяются — без перезапуска. Права (только админ)
проверяет вызывающий.
"""
import logging
from typing import Callable

from core import config, envfile

logger = logging.getLogger(__name__)


def ask(bot, chat_id: int, chat_type: str, user_id: int, *, site: str, login_prompt: str,
        keys: tuple[str, str], on_saved: Callable, check: Callable[[str], str | None] = lambda s: None,
        comment: str = '') -> None:
    """Спросить логин, потом пароль, сохранить в .env под keys = (KEY_LOGIN, KEY_PASSWORD).

    check(login) → текст ошибки или None. on_saved(message, login, changed, note)
    вызывается после сохранения: changed — сменился ли логин, note — предупреждение
    (например, что сообщение с паролем не удалось удалить).
    """
    if chat_type != 'private':
        bot.send_message(chat_id, f'🔒 Логин и пароль {site} пришли мне в личные сообщения: '
                                  'открой чат с ботом и повтори команду.')
        return
    msg = bot.send_message(chat_id, f'{login_prompt}\nСохраню в .env на сервере. '
                                    'Передумал — отправь любую команду.')
    bot.clear_step_handler_by_chat_id(chat_id)  # не копим ожидания от прошлых шагов
    flow = dict(site=site, keys=keys, on_saved=on_saved, check=check, comment=comment, user_id=user_id)
    bot.register_next_step_handler(msg, lambda m: _login(bot, m, flow))


def _login(bot, message, flow: dict) -> None:
    text = (message.text or '').strip()
    if text.startswith('/'):
        bot.process_new_messages([message])  # прислали команду — выполнить её
        return
    if message.from_user.id != flow['user_id']:
        return
    error = flow['check'](text) if text else 'Пусто — пришли ещё раз:'
    if error:
        msg = bot.reply_to(message, error)
        bot.register_next_step_handler(msg, lambda m: _login(bot, m, flow))
        return
    msg = bot.reply_to(message, f"🔑 Теперь пароль от {flow['site']}.\n"
                                'Сообщение с паролем сразу удалю из чата.')
    bot.register_next_step_handler(msg, lambda m: _password(bot, m, flow, text))


def _password(bot, message, flow: dict, login: str) -> None:
    password = (message.text or '').strip()
    if password.startswith('/'):
        bot.process_new_messages([message])
        return
    if message.from_user.id != flow['user_id']:
        return
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)  # пароль не должен висеть в чате
        note = ''
    except Exception:
        note = '\n⚠️ Не смог удалить сообщение с паролем — удали его сам.'
    if not password:
        bot.send_message(chat_id, 'Пароль пустой — начни заново.')
        return
    key_login, key_password = flow['keys']
    try:
        envfile.set_values(config.BASE_DIR / '.env', {key_login: login, key_password: password},
                           comment=flow['comment'])
    except Exception as e:
        logger.error(f"Could not save {flow['site']} account to .env: {e}")
        bot.send_message(chat_id, f'⚠️ Не смог записать .env: {e}')
        return
    changed = login.lower() != (getattr(config, key_login, '') or '').lower()
    setattr(config, key_login, login)
    setattr(config, key_password, password)
    flow['on_saved'](message, login, changed, note)
