"""Folder manager — browse and delete entire folders (replaces disperDIR2)."""
import logging
import os
import shutil

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from file_manager import id_to_path, is_allowed, is_inside_share, listdir_allowed, nav_buttons, page_of, path_to_id

logger = logging.getLogger(__name__)

_PREFIX = 'folder'


def register(bot) -> None:
    bot.callback_query_handler(func=lambda c: c.data.startswith(f'{_PREFIX}:'))(
        lambda c: _handle(bot, c)
    )


def send_root(bot, chat_id: int) -> None:
    bot.send_message(
        chat_id,
        f'📂 {config.SHARED_FOLDER}',
        reply_markup=_build_keyboard(config.SHARED_FOLDER),
    )


# ── keyboard ──────────────────────────────────────────────────────────────────

def _build_keyboard(path: str, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    if os.path.abspath(path) != os.path.abspath(config.SHARED_FOLDER):
        parent = os.path.dirname(path)
        kb.row(InlineKeyboardButton('⬅️ Назад', callback_data=f'{_PREFIX}:dir_{path_to_id(parent)}'))

    # по 2 кнопки на папку (открыть / удалить) — значит, папок на странице вдвое меньше
    folders, page, pages = page_of(listdir_allowed(path, dirs_only=True), page, size=20)
    for item in folders:
        item_path = os.path.join(path, item)
        uid = path_to_id(item_path)
        row = [InlineKeyboardButton(f'📁 {item}', callback_data=f'{_PREFIX}:dir_{uid}')]
        if not _is_protected(item_path):
            row.append(InlineKeyboardButton(f'🗑️ {item}', callback_data=f'{_PREFIX}:confirm_{uid}'))
        kb.row(*row)

    here = path_to_id(path)
    nav = nav_buttons(lambda n: f'{_PREFIX}:dirp_{here}_{n}', page, pages)
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton('❌ Закрыть', callback_data=f'{_PREFIX}:close'))
    return kb


def _is_allowed(path: str) -> bool:
    return is_allowed(path)


def _is_protected(path: str) -> bool:
    return os.path.basename(path) in config.PROTECTED_FOLDERS


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

    if action_data.startswith('dirp_'):
        uid, _, page = action_data[5:].rpartition('_')
        path = id_to_path(uid)
        if not path or not page.isdigit():
            bot.answer_callback_query(call.id, 'Путь не найден')
            return
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=_build_keyboard(path, int(page)))
        except Exception as e:
            logger.debug(e)
        bot.answer_callback_query(call.id)

    elif action_data.startswith('dir_'):
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

    elif action_data.startswith('confirm_'):
        uid = action_data[8:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден')
            return
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton('✅ Да, удалить', callback_data=f'{_PREFIX}:delete_{uid}'),
            InlineKeyboardButton('❌ Отмена', callback_data=f'{_PREFIX}:dir_{path_to_id(os.path.dirname(path))}'),
        )
        try:
            bot.edit_message_text(
                f'⚠️ Удалить папку и всё её содержимое?\n{path}',
                call.message.chat.id, call.message.message_id,
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
        if _is_protected(path):
            bot.answer_callback_query(call.id, 'Эта папка защищена от удаления')
            return
        if not os.path.isdir(path):
            bot.answer_callback_query(call.id, 'Папка не найдена')
            return
        # Нельзя удалить корень шары и точки монтирования (за ними — целый диск)
        if (not is_inside_share(path)
                or os.path.realpath(path) == os.path.realpath(config.SHARED_FOLDER)
                or os.path.ismount(path)):
            bot.answer_callback_query(call.id, 'Эту папку удалить нельзя')
            return
        try:
            shutil.rmtree(path)
            from core.audit import audit
            audit(call.from_user, 'DELETE_FOLDER', path)
            bot.answer_callback_query(call.id, 'Папка удалена')
            parent = os.path.dirname(path)
            bot.edit_message_text(
                f'🗑️ Удалена: {path}\n\n📂 {parent}',
                call.message.chat.id, call.message.message_id,
                reply_markup=_build_keyboard(parent),
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')
    else:
        bot.answer_callback_query(call.id, 'Неизвестное действие')
