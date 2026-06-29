"""File browser — view and delete individual files (replaces disperDIR)."""
import logging
import os

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from file_manager import id_to_path, path_to_id

logger = logging.getLogger(__name__)

_PREFIX = 'browser'


def register(bot) -> None:
    bot.callback_query_handler(func=lambda c: c.data.startswith(f'{_PREFIX}:'))(
        lambda c: _handle(bot, c)
    )


def send_root(bot, chat_id: int) -> None:
    bot.send_message(
        chat_id,
        f"📂 {config.SHARED_FOLDER}",
        reply_markup=_build_keyboard(config.SHARED_FOLDER),
    )


# ── keyboard ──────────────────────────────────────────────────────────────────

def _build_keyboard(path: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = []

    if os.path.abspath(path) != os.path.abspath(config.SHARED_FOLDER):
        parent = os.path.dirname(path)
        buttons.append(InlineKeyboardButton('⬅️ Назад', callback_data=f'{_PREFIX}:dir_{path_to_id(parent)}'))

    try:
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            if config.USE_ALLOWED_FOLDERS and not _is_allowed(item_path):
                continue
            uid = path_to_id(item_path)
            if os.path.isdir(item_path):
                buttons.append(InlineKeyboardButton(f'📁 {item}', callback_data=f'{_PREFIX}:dir_{uid}'))
            else:
                buttons.append(InlineKeyboardButton(f'📄 {item}', callback_data=f'{_PREFIX}:file_{uid}'))
    except Exception as e:
        logger.error(f"Error listing {path}: {e}")

    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            kb.row(buttons[i], buttons[i + 1])
        else:
            kb.add(buttons[i])

    kb.row(InlineKeyboardButton('❌ Закрыть', callback_data=f'{_PREFIX}:close'))
    return kb


def _is_allowed(path: str) -> bool:
    return any(f for f in config.ALLOWED_FOLDERS if f in path)


# ── callback dispatcher ────────────────────────────────────────────────────────

def _handle(bot, call) -> None:
    from core.auth import is_authorized
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return

    action_data = call.data[len(f'{_PREFIX}:'):]

    if action_data == 'close':
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    if ':' in action_data:
        bot.answer_callback_query(call.id, 'Неизвестное действие')
        return

    if action_data.startswith('dir_'):
        uid = action_data[4:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден')
            return
        try:
            bot.edit_message_text(
                f'📂 {path}', call.message.chat.id, call.message.message_id,
                reply_markup=_build_keyboard(path),
            )
        except Exception as e:
            logger.error(e)

    elif action_data.startswith('file_'):
        uid = action_data[5:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден')
            return
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton('✅ Удалить', callback_data=f'{_PREFIX}:delete_{uid}'),
            InlineKeyboardButton('❌ Отмена', callback_data=f'{_PREFIX}:dir_{path_to_id(os.path.dirname(path))}'),
        )
        try:
            bot.edit_message_text(
                f'Удалить файл?\n{path}', call.message.chat.id, call.message.message_id,
                reply_markup=kb,
            )
        except Exception as e:
            logger.error(e)

    elif action_data.startswith('delete_'):
        uid = action_data[7:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден')
            return
        if not os.path.isfile(path):
            bot.answer_callback_query(call.id, 'Файл не найден')
            return
        try:
            os.remove(path)
            bot.answer_callback_query(call.id, 'Файл удалён')
            parent = os.path.dirname(path)
            bot.edit_message_text(
                f'🗑️ Удалён: {path}\n\n📂 {parent}',
                call.message.chat.id, call.message.message_id,
                reply_markup=_build_keyboard(parent),
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')
    else:
        bot.answer_callback_query(call.id, 'Неизвестное действие')
