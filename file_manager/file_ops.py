"""File operations — rename, move, create folders (replaces disperDIR3).

Fixes:
  - Uses shutil.move instead of os.rename (works across filesystems).
  - Move destination is browsed page-by-page instead of os.walk (avoids huge keyboards).
  - Input is validated to prevent path traversal.
"""
import logging
import os
import shutil

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from file_manager import id_to_path, path_to_id

logger = logging.getLogger(__name__)

_PREFIX = 'fileops'

# Per-user interaction state: {user_id: {'action': str, 'path': str, 'msg_id': int}}
_state: dict[int, dict] = {}


def register(bot) -> None:
    bot.callback_query_handler(func=lambda c: c.data.startswith(f'{_PREFIX}:'))(
        lambda c: _handle(bot, c)
    )


def send_root(bot, chat_id: int) -> None:
    bot.send_message(chat_id, 'Выберите действие:', reply_markup=_action_keyboard(config.SHARED_FOLDER))


# ── keyboards ─────────────────────────────────────────────────────────────────

def _action_keyboard(path: str) -> InlineKeyboardMarkup:
    uid = path_to_id(path)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('📁 Создать папку', callback_data=f'{_PREFIX}:create_{uid}'))
    kb.add(InlineKeyboardButton('✏️ Переименовать файл', callback_data=f'{_PREFIX}:rename_file_{uid}'))
    kb.add(InlineKeyboardButton('📂 Переименовать папку', callback_data=f'{_PREFIX}:rename_folder_{uid}'))
    kb.add(InlineKeyboardButton('🚚 Переместить', callback_data=f'{_PREFIX}:move_src_{uid}'))
    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'{_PREFIX}:close'))
    return kb


def _browse_keyboard(path: str, callback_prefix: str, extra_btn: InlineKeyboardButton | None = None) -> InlineKeyboardMarkup:
    """Generic directory-listing keyboard. callback_prefix is appended with _{uid}."""
    kb = InlineKeyboardMarkup()

    if os.path.abspath(path) != os.path.abspath(config.SHARED_FOLDER):
        parent = os.path.dirname(path)
        kb.add(InlineKeyboardButton('⬅️ Назад', callback_data=f'{callback_prefix}_{path_to_id(parent)}'))

    if extra_btn:
        kb.add(extra_btn)

    try:
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            if not _is_allowed(item_path):
                continue
            uid = path_to_id(item_path)
            if os.path.isdir(item_path):
                kb.add(InlineKeyboardButton(f'📁 {item}', callback_data=f'{callback_prefix}_{uid}'))
            else:
                kb.add(InlineKeyboardButton(f'📄 {item}', callback_data=f'{callback_prefix}_{uid}'))
    except Exception as e:
        logger.error(f"Error listing {path}: {e}")

    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'{_PREFIX}:close'))
    return kb


def _is_allowed(path: str) -> bool:
    if not config.USE_ALLOWED_FOLDERS:
        return True
    return any(f for f in config.ALLOWED_FOLDERS if f in path)


def _safe_path(new_path: str) -> bool:
    """Prevent path traversal outside SHARED_FOLDER."""
    return os.path.abspath(new_path).startswith(os.path.abspath(config.SHARED_FOLDER))


# ── callback dispatcher ────────────────────────────────────────────────────────

def _handle(bot, call) -> None:
    from core.auth import is_authorized
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return

    uid_val = call.from_user.id
    raw = call.data[len(f'{_PREFIX}:'):]

    # ── close ──
    if raw == 'close':
        _state.pop(uid_val, None)
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    # ── create folder: pick location ──
    if raw.startswith('create_'):
        uid = raw[7:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        _state[uid_val] = {'action': 'create', 'path': path, 'msg_id': call.message.message_id}
        bot.send_message(call.message.chat.id, f'Введите имя новой папки в {path}:')
        bot.register_next_step_handler(call.message, lambda m: _step_create_folder(bot, m))
        return

    # ── rename file: browse to file ──
    if raw.startswith('rename_file_'):
        uid = raw[12:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        _state[uid_val] = {'action': 'rename_file', 'msg_id': call.message.message_id}
        bot.edit_message_text(
            'Выберите файл для переименования:', call.message.chat.id, call.message.message_id,
            reply_markup=_browse_keyboard(path, f'{_PREFIX}:pick_file'),
        )
        return

    # ── pick file for rename ──
    if raw.startswith('pick_file_'):
        uid = raw[10:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        if os.path.isdir(path):
            bot.edit_message_text(
                f'📁 {path}', call.message.chat.id, call.message.message_id,
                reply_markup=_browse_keyboard(path, f'{_PREFIX}:pick_file'),
            )
            return
        # It's a file
        state = _state.get(uid_val, {})
        state.update({'action': 'rename_file', 'path': path})
        _state[uid_val] = state
        bot.send_message(call.message.chat.id, f'Введите новое имя для:\n{os.path.basename(path)}')
        bot.register_next_step_handler(call.message, lambda m: _step_rename(bot, m, is_file=True))
        return

    # ── rename folder: browse to folder ──
    if raw.startswith('rename_folder_'):
        uid = raw[14:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        _state[uid_val] = {'action': 'rename_folder', 'msg_id': call.message.message_id}
        bot.edit_message_text(
            'Выберите папку для переименования:', call.message.chat.id, call.message.message_id,
            reply_markup=_browse_keyboard(path, f'{_PREFIX}:pick_folder'),
        )
        return

    # ── pick folder for rename ──
    if raw.startswith('pick_folder_'):
        uid = raw[12:]
        path = id_to_path(uid)
        if not path or not os.path.isdir(path):
            bot.answer_callback_query(call.id, 'Папка не найдена'); return
        state = _state.get(uid_val, {})
        state.update({'action': 'rename_folder', 'path': path})
        _state[uid_val] = state
        bot.send_message(call.message.chat.id, f'Введите новое имя для папки:\n{os.path.basename(path)}')
        bot.register_next_step_handler(call.message, lambda m: _step_rename(bot, m, is_file=False))
        return

    # ── move: pick source ──
    if raw.startswith('move_src_'):
        uid = raw[9:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        _state[uid_val] = {'action': 'move', 'msg_id': call.message.message_id}
        bot.edit_message_text(
            'Выберите файл или папку для перемещения:', call.message.chat.id, call.message.message_id,
            reply_markup=_browse_keyboard(path, f'{_PREFIX}:move_item'),
        )
        return

    # ── move: source selected, browse items ──
    if raw.startswith('move_item_'):
        uid = raw[10:]
        path = id_to_path(uid)
        if not path:
            bot.answer_callback_query(call.id, 'Путь не найден'); return
        if os.path.isdir(path) and _state.get(uid_val, {}).get('action') == 'move' and 'src' not in _state.get(uid_val, {}):
            # Navigate into folder
            bot.edit_message_text(
                f'📁 {path}', call.message.chat.id, call.message.message_id,
                reply_markup=_browse_keyboard(path, f'{_PREFIX}:move_item'),
            )
            return
        # File or folder selected as source
        state = _state.get(uid_val, {})
        state['src'] = path
        _state[uid_val] = state
        here_btn = InlineKeyboardButton('📌 Переместить сюда', callback_data=f'{_PREFIX}:move_dest_{path_to_id(config.SHARED_FOLDER)}')
        bot.edit_message_text(
            f'Источник: {os.path.basename(path)}\nВыберите папку назначения:',
            call.message.chat.id, call.message.message_id,
            reply_markup=_browse_dest_keyboard(config.SHARED_FOLDER),
        )
        return

    # ── move: browse destination ──
    if raw.startswith('move_dest_browse_'):
        uid = raw[17:]
        path = id_to_path(uid)
        if not path or not os.path.isdir(path):
            bot.answer_callback_query(call.id, 'Папка не найдена'); return
        bot.edit_message_text(
            f'Куда переместить? ({path})', call.message.chat.id, call.message.message_id,
            reply_markup=_browse_dest_keyboard(path),
        )
        return

    # ── move: destination confirmed ──
    if raw.startswith('move_dest_'):
        uid = raw[10:]
        dest_dir = id_to_path(uid)
        if not dest_dir:
            bot.answer_callback_query(call.id, 'Папка не найдена'); return
        state = _state.get(uid_val, {})
        src = state.get('src')
        if not src:
            bot.answer_callback_query(call.id, 'Источник не выбран'); return
        new_path = os.path.join(dest_dir, os.path.basename(src))
        if not _safe_path(new_path):
            bot.answer_callback_query(call.id, 'Недопустимый путь'); return
        try:
            shutil.move(src, new_path)
            bot.answer_callback_query(call.id, 'Перемещено')
            bot.edit_message_text(
                f'✅ {os.path.basename(src)} → {dest_dir}',
                call.message.chat.id, call.message.message_id,
            )
        except Exception as e:
            bot.send_message(call.message.chat.id, f'❌ Ошибка: {e}')
        finally:
            _state.pop(uid_val, None)
        return

    bot.answer_callback_query(call.id, 'Неизвестное действие')


def _browse_dest_keyboard(path: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting move destination — shows only directories."""
    kb = InlineKeyboardMarkup()
    uid = path_to_id(path)

    kb.add(InlineKeyboardButton(f'📌 Переместить сюда ({os.path.basename(path) or "/"})',
                                callback_data=f'{_PREFIX}:move_dest_{uid}'))

    if os.path.abspath(path) != os.path.abspath(config.SHARED_FOLDER):
        parent = os.path.dirname(path)
        kb.add(InlineKeyboardButton('⬅️ Назад', callback_data=f'{_PREFIX}:move_dest_browse_{path_to_id(parent)}'))

    try:
        for item in sorted(os.listdir(path)):
            item_path = os.path.join(path, item)
            if not os.path.isdir(item_path) or not _is_allowed(item_path):
                continue
            kb.add(InlineKeyboardButton(f'📁 {item}', callback_data=f'{_PREFIX}:move_dest_browse_{path_to_id(item_path)}'))
    except Exception as e:
        logger.error(f"Error listing {path}: {e}")

    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'{_PREFIX}:close'))
    return kb


# ── step handlers (text input) ─────────────────────────────────────────────────

def _step_create_folder(bot, message) -> None:
    from core.auth import is_authorized
    if not is_authorized(message.from_user.id):
        return
    uid_val = message.from_user.id
    state = _state.pop(uid_val, None)
    if not state:
        bot.reply_to(message, '⚠️ Сессия истекла, начните заново.')
        return
    name = message.text.strip()
    new_path = os.path.join(state['path'], name)
    if not _safe_path(new_path):
        bot.reply_to(message, '❌ Недопустимое имя папки.')
        return
    try:
        os.makedirs(new_path, exist_ok=False)
        bot.reply_to(message, f'✅ Папка создана: {new_path}')
    except FileExistsError:
        bot.reply_to(message, '❌ Папка уже существует.')
    except Exception as e:
        bot.reply_to(message, f'❌ Ошибка: {e}')


def _step_rename(bot, message, *, is_file: bool) -> None:
    from core.auth import is_authorized
    if not is_authorized(message.from_user.id):
        return
    uid_val = message.from_user.id
    state = _state.pop(uid_val, None)
    if not state or 'path' not in state:
        bot.reply_to(message, '⚠️ Сессия истекла, начните заново.')
        return
    old_path = state['path']
    new_name = message.text.strip()
    if is_file:
        ext = os.path.splitext(old_path)[1]
        new_name = new_name + ext if not new_name.endswith(ext) else new_name
    new_path = os.path.join(os.path.dirname(old_path), new_name)
    if not _safe_path(new_path):
        bot.reply_to(message, '❌ Недопустимое имя.')
        return
    try:
        shutil.move(old_path, new_path)
        label = 'Файл' if is_file else 'Папка'
        bot.reply_to(message, f'✅ {label} переименован в: {new_name}')
    except Exception as e:
        bot.reply_to(message, f'❌ Ошибка: {e}')
