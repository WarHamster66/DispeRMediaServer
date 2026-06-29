"""Torrent commands: add (file + magnet), list, pause, resume, clear."""
import hashlib
import logging
import os
import tempfile

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.auth import is_authorized
from services import transmission as tr

logger = logging.getLogger(__name__)

# Pending confirmations: {message_id: {torrent_id, file_hash, name, size}}
# This is a dict-per-request so 10 simultaneous uploads all work independently.
_pending: dict[int, dict] = {}


def register(bot) -> None:
    bot.message_handler(commands=['torrent'])(lambda m: _cmd_torrent(bot, m))
    bot.message_handler(commands=['torrents'])(lambda m: _cmd_torrents(bot, m))
    bot.message_handler(commands=['pause'])(lambda m: _cmd_pause(bot, m))
    bot.message_handler(commands=['resume'])(lambda m: _cmd_resume(bot, m))
    bot.message_handler(commands=['clear_downloads'])(lambda m: _cmd_clear(bot, m))
    bot.message_handler(
        func=lambda m: m.document and m.document.mime_type == 'application/x-bittorrent',
        content_types=['document'],
    )(lambda m: _handle_torrent_file(bot, m))
    bot.message_handler(
        func=lambda m: m.text and m.text.strip().startswith('magnet:'),
    )(lambda m: _handle_magnet(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('torrent:'))(
        lambda c: _callback(bot, c)
    )


# ── commands ──────────────────────────────────────────────────────────────────

def _cmd_torrent(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.reply_to(message, 'Отправьте .torrent файл или magnet-ссылку.')


def _cmd_torrents(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        torrents = tr.get_all_torrents()
        if not torrents:
            bot.reply_to(message, 'Нет активных торрентов.')
            return
        states = [
            ('downloading', '⬇️', 'загрузка'),
            ('seeding', '✅', 'раздача'),
            ('stopped', '⏸️', 'пауза'),
            ('checking', '🔄', 'проверка'),
        ]
        lines = []
        for t in torrents:
            pct = f"{t.progress:.1f}%"
            icon, label = next(((i, l) for s, i, l in states if t.status == s), ('❓', str(t.status)))
            lines.append(f"{icon} {t.name}\n   {pct} · {label}")
        bot.reply_to(message, '\n\n'.join(lines))
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_pause(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        torrents = [t for t in tr.get_all_torrents() if t.status == 'downloading']
        if not torrents:
            bot.reply_to(message, 'Нет загружаемых торрентов.')
            return
        kb = InlineKeyboardMarkup()
        for t in torrents:
            kb.add(InlineKeyboardButton(
                f'⏸ {t.name} ({t.progress:.0f}%)',
                callback_data=f'torrent:pause_{t.id}',
            ))
        kb.add(InlineKeyboardButton('❌ Отмена', callback_data='torrent:close'))
        bot.reply_to(message, 'Выберите торрент для паузы:', reply_markup=kb)
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_resume(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        torrents = [t for t in tr.get_all_torrents() if t.status == 'stopped']
        if not torrents:
            bot.reply_to(message, 'Нет остановленных торрентов.')
            return
        kb = InlineKeyboardMarkup()
        for t in torrents:
            kb.add(InlineKeyboardButton(
                f'▶️ {t.name}',
                callback_data=f'torrent:resume_{t.id}',
            ))
        kb.add(InlineKeyboardButton('❌ Отмена', callback_data='torrent:close'))
        bot.reply_to(message, 'Выберите торрент для возобновления:', reply_markup=kb)
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_clear(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        torrents = tr.get_all_torrents()
        if not torrents:
            bot.reply_to(message, 'Список загрузок уже пуст.')
            return
        for t in torrents:
            try:
                tr.remove_torrent(t.id, delete_data=False)
            except Exception as e:
                logger.error(f"Could not remove torrent {t.name}: {e}")
        bot.reply_to(message, f'Удалено {len(torrents)} торрентов из очереди (файлы сохранены).')
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


# ── incoming torrent file ─────────────────────────────────────────────────────

def _handle_torrent_file(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        return

    tmp_path = None
    try:
        file_info = bot.get_file(message.document.file_id)
        tmp_path = os.path.join(tempfile.gettempdir(), message.document.file_name)
        with open(tmp_path, 'wb') as f:
            f.write(bot.download_file(file_info.file_path))

        file_hash = _hash_file(tmp_path)

        # Duplicate check — already being monitored in this session
        if tr.is_active(file_hash):
            bot.reply_to(message, '⚠️ Этот торрент уже загружается.')
            return

        # Add paused to Transmission — also checks disk space and existing torrents
        try:
            torrent = tr.add_torrent_file(tmp_path)
        except tr.InsufficientSpaceError as e:
            bot.reply_to(
                message,
                f'❌ Недостаточно места на диске.\n'
                f'Нужно: {_fmt(e.required)}\n'
                f'Свободно: {_fmt(e.available)}',
            )
            return

        # Check for name duplicate among existing torrents
        try:
            existing = tr.get_all_torrents()
            for existing_t in existing:
                if existing_t.id != torrent.id and existing_t.name == torrent.name:
                    tr.remove_torrent(torrent.id, delete_data=False)
                    bot.reply_to(
                        message,
                        f"⚠️ '{torrent.name}' уже есть в Transmission.\n"
                        f"Статус: {existing_t.status}, прогресс: {existing_t.progress:.1f}%",
                    )
                    return
        except Exception:
            pass  # Non-critical — proceed

        size_str = _fmt(torrent.total_size) if torrent.total_size else 'неизвестно'
        _pending[message.message_id] = {
            'torrent_id': torrent.id,
            'file_hash': file_hash,
            'name': torrent.name,
            'size': torrent.total_size,
        }

        bot.reply_to(
            message,
            f'📥 {torrent.name}\n📦 Размер: {size_str}\n\nКуда скачать?',
            reply_markup=_folder_keyboard(message.message_id),
        )

    except Exception as e:
        logger.error(f'Error handling torrent file: {e}', exc_info=True)
        bot.reply_to(message, f'Ошибка при обработке файла: {e}')
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ── incoming magnet link ──────────────────────────────────────────────────────

def _handle_magnet(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        return
    magnet = message.text.strip()
    file_hash = hashlib.sha256(magnet.encode()).hexdigest()[:20]
    if tr.is_active(file_hash):
        bot.reply_to(message, '⚠️ Этот торрент уже загружается.')
        return
    try:
        torrent = tr.add_magnet(magnet)
        _pending[message.message_id] = {
            'torrent_id': torrent.id,
            'file_hash': file_hash,
            'name': torrent.name or 'magnet',
            'size': 0,
        }
        bot.reply_to(
            message,
            f'🧲 {torrent.name or "Магнет"}\n\nКуда скачать?',
            reply_markup=_folder_keyboard(message.message_id),
        )
        logger.info(f'Magnet added (paused): {torrent.name}')
    except Exception as e:
        logger.error(f'Error adding magnet: {e}', exc_info=True)
        bot.reply_to(message, f'Ошибка при добавлении magnet: {e}')


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return

    data = call.data[len('torrent:'):]

    if data == 'close':
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    if data.startswith('dest_'):
        # формат: dest_<msg_id>_<folder_idx>
        rest = data[len('dest_'):]
        try:
            msg_id_str, idx_str = rest.rsplit('_', 1)
            msg_id, idx = int(msg_id_str), int(idx_str)
        except ValueError:
            bot.answer_callback_query(call.id, 'Неверные данные')
            return
        pending = _pending.pop(msg_id, None)
        if not pending:
            bot.answer_callback_query(call.id, 'Запрос устарел')
            bot.delete_message(call.message.chat.id, call.message.message_id)
            return
        if idx < 0 or idx >= len(config.ALLOWED_FOLDERS):
            bot.answer_callback_query(call.id, 'Папка не найдена')
            return
        folder = config.ALLOWED_FOLDERS[idx]
        location = os.path.join(config.SHARED_FOLDER, folder)
        try:
            tr.set_location(pending['torrent_id'], location)
            tr.mark_active(pending['file_hash'])
            tr.resume_torrent(pending['torrent_id'])
            sent = bot.edit_message_text(
                f"⏳ Загружается: {pending['name']}\n📁 Папка: {folder}\n⏳ 0%",
                call.message.chat.id,
                call.message.message_id,
            )
            tr.start_monitoring(
                bot, call.message.chat.id, sent.message_id,
                pending['torrent_id'], pending['file_hash'],
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')

    elif data.startswith('reject_'):
        msg_id = int(data[7:])
        pending = _pending.pop(msg_id, None)
        if pending:
            try:
                tr.remove_torrent(pending['torrent_id'], delete_data=False)
            except Exception:
                pass
        bot.answer_callback_query(call.id, 'Загрузка отменена')
        bot.delete_message(call.message.chat.id, call.message.message_id)

    elif data.startswith('pause_'):
        torrent_id = int(data[6:])
        try:
            tr.pause_torrent(torrent_id)
            bot.answer_callback_query(call.id, '⏸ Остановлен')
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')

    elif data.startswith('resume_'):
        torrent_id = int(data[7:])
        try:
            tr.resume_torrent(torrent_id)
            bot.answer_callback_query(call.id, '▶️ Возобновлён')
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')


# ── helpers ───────────────────────────────────────────────────────────────────

def _folder_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Keyboard to choose the destination folder among config.ALLOWED_FOLDERS."""
    kb = InlineKeyboardMarkup()
    for i, folder in enumerate(config.ALLOWED_FOLDERS):
        kb.add(InlineKeyboardButton(f'📁 {folder}', callback_data=f'torrent:dest_{msg_id}_{i}'))
    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'torrent:reject_{msg_id}'))
    return kb


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _fmt(b: int) -> str:
    if b >= 1024 ** 3:
        return f'{b / 1024 ** 3:.2f} GB'
    if b >= 1024 ** 2:
        return f'{b / 1024 ** 2:.0f} MB'
    return f'{b / 1024:.0f} KB'
