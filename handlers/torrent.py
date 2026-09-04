"""Torrent commands: add (file + magnet), list, pause, resume, clear."""
import hashlib
import logging
import os
import tempfile
import threading
import time

import psutil
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit
from core.auth import is_authorized
from services import transmission as tr

logger = logging.getLogger(__name__)

# Pending confirmations: {message_id: {torrent_id, file_hash, name, size, ts, chat_id, menu_msg_id}}
# This is a dict-per-request so 10 simultaneous uploads all work independently.
_pending: dict[int, dict] = {}
_pending_lock = threading.Lock()

# Если пользователь не выбрал папку за это время — торрент убирается из очереди
_PENDING_TTL = 30 * 60  # 30 минут


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
    threading.Thread(target=_pending_cleanup_loop, args=(bot,),
                     name='PendingCleanup', daemon=True).start()


def _pending_cleanup_loop(bot) -> None:
    """Раз в минуту убирает торренты, для которых так и не выбрали папку."""
    while True:
        time.sleep(60)
        now = time.time()
        expired = []
        with _pending_lock:
            for msg_id, p in list(_pending.items()):
                if now - p.get('ts', now) > _PENDING_TTL:
                    expired.append(p)
                    _pending.pop(msg_id, None)
        for p in expired:
            try:
                tr.remove_torrent(p['torrent_id'], delete_data=False)
            except Exception as e:
                logger.warning(f"Could not remove expired torrent {p['name']}: {e}")
            audit('system', 'PENDING_EXPIRED', p['name'])
            try:
                bot.edit_message_text(
                    f"⏰ Папка не выбрана за 30 минут — «{p['name']}» удалён из очереди.\n"
                    f"Отправь торрент заново, если он ещё нужен.",
                    p['chat_id'], p['menu_msg_id'],
                )
            except Exception:
                pass


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
        # Сбрасываем реестр активных, иначе те же торренты нельзя будет добавить заново
        tr.clear_active()
        with _pending_lock:
            _pending.clear()
        audit(message.from_user, 'CLEAR_DOWNLOADS', f'{len(torrents)} шт.')
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
        if _still_active(file_hash):
            bot.reply_to(
                message,
                '⚠️ Этот торрент уже загружается.\n'
                'Если это не так — выполни /clear_downloads и отправь снова.',
            )
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
        sent = bot.reply_to(
            message,
            f'📥 {torrent.name}\n📦 Размер: {size_str}\n\nКуда скачать?',
            reply_markup=_folder_keyboard(message.message_id),
        )
        with _pending_lock:
            _pending[message.message_id] = {
                'torrent_id': torrent.id,
                'file_hash': file_hash,
                'name': torrent.name,
                'size': torrent.total_size,
                'ts': time.time(),
                'chat_id': message.chat.id,
                'menu_msg_id': sent.message_id,
            }
        audit(message.from_user, 'TORRENT_ADD', f'{torrent.name} ({size_str})')

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
    if _still_active(file_hash):
        bot.reply_to(
            message,
            '⚠️ Этот торрент уже загружается.\n'
            'Если это не так — выполни /clear_downloads и отправь снова.',
        )
        return
    try:
        torrent = tr.add_magnet(magnet)
        sent = bot.reply_to(
            message,
            f'🧲 {torrent.name or "Магнет"}\n\nКуда скачать?',
            reply_markup=_folder_keyboard(message.message_id),
        )
        with _pending_lock:
            _pending[message.message_id] = {
                'torrent_id': torrent.id,
                'file_hash': file_hash,
                'name': torrent.name or 'magnet',
                'size': 0,
                'ts': time.time(),
                'chat_id': message.chat.id,
                'menu_msg_id': sent.message_id,
            }
        audit(message.from_user, 'TORRENT_ADD', f'magnet: {torrent.name or magnet[:60]}')
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
        with _pending_lock:
            pending = _pending.get(msg_id)
        if not pending:
            bot.answer_callback_query(call.id, 'Запрос устарел')
            bot.delete_message(call.message.chat.id, call.message.message_id)
            return
        if idx < 0 or idx >= len(config.ALLOWED_FOLDERS):
            bot.answer_callback_query(call.id, 'Папка не найдена')
            return

        folder = config.ALLOWED_FOLDERS[idx]
        location = os.path.join(config.SHARED_FOLDER, folder)
        pending.update({'folder': folder, 'location': location, 'ts': time.time()})

        try:
            tr.set_location(pending['torrent_id'], location)
        except Exception as e:
            bot.answer_callback_query(call.id, f'Ошибка: {e}')
            return

        # Многофайловый торрент (сезон сериала) — предложить выбрать серии
        files = tr.get_files(pending['torrent_id'])
        if len(files) > 1:
            pending['files'] = files
            pending['wanted'] = {f['id'] for f in files}
            pending['page'] = 0
            bot.edit_message_text(
                f"📥 {pending['name']}\n📁 Папка: {folder}\n\n"
                f"Что скачивать? Сними галочки с ненужного:",
                call.message.chat.id, call.message.message_id,
                reply_markup=_files_keyboard(msg_id, pending),
            )
            return

        _start_download(bot, call, msg_id)

    elif data.startswith('fsel_'):
        msg_id, file_id = _parse_two(data[len('fsel_'):])
        pending = _peek(bot, call, msg_id)
        if pending is None:
            return
        wanted = pending.setdefault('wanted', set())
        wanted.symmetric_difference_update({file_id})  # toggle
        pending['ts'] = time.time()
        _refresh_files_kb(bot, call, msg_id, pending)

    elif data.startswith('fall_'):
        msg_id = int(data[len('fall_'):])
        pending = _peek(bot, call, msg_id)
        if pending is None:
            return
        pending['wanted'] = {f['id'] for f in pending['files']}
        pending['ts'] = time.time()
        _refresh_files_kb(bot, call, msg_id, pending)

    elif data.startswith('fnone_'):
        msg_id = int(data[len('fnone_'):])
        pending = _peek(bot, call, msg_id)
        if pending is None:
            return
        pending['wanted'] = set()
        pending['ts'] = time.time()
        _refresh_files_kb(bot, call, msg_id, pending)

    elif data.startswith('fpg_'):
        msg_id, page = _parse_two(data[len('fpg_'):])
        pending = _peek(bot, call, msg_id)
        if pending is None:
            return
        pending['page'] = page
        pending['ts'] = time.time()
        _refresh_files_kb(bot, call, msg_id, pending)

    elif data.startswith('fgo_'):
        _start_download(bot, call, int(data[len('fgo_'):]))

    elif data.startswith('reject_'):
        msg_id = int(data[7:])
        with _pending_lock:
            pending = _pending.pop(msg_id, None)
        if pending:
            try:
                tr.remove_torrent(pending['torrent_id'], delete_data=False)
            except Exception:
                pass
            audit(call.from_user, 'TORRENT_CANCEL', pending['name'])
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


# ── file selection ────────────────────────────────────────────────────────────

_FILES_PER_PAGE = 15


def _still_active(file_hash: str) -> bool:
    """Правда ли торрент ещё качается.

    Реестр активных живёт в памяти и может разойтись с Transmission (например,
    торренты удалили). Если в Transmission пусто — реестр устарел, чистим его,
    чтобы файл можно было добавить заново.
    """
    if not tr.is_active(file_hash):
        return False
    try:
        if not tr.get_all_torrents():
            tr.clear_active()
            return False
    except Exception:
        pass  # Transmission недоступен — доверяем реестру
    return tr.is_active(file_hash)


def _parse_two(raw: str) -> tuple[int, int]:
    """'<msg_id>_<n>' → (msg_id, n)."""
    a, _, b = raw.rpartition('_')
    return int(a), int(b)


def _peek(bot, call, msg_id: int) -> dict | None:
    """Получить pending-запрос или сообщить, что он устарел."""
    with _pending_lock:
        pending = _pending.get(msg_id)
    if not pending or 'files' not in pending:
        bot.answer_callback_query(call.id, 'Запрос устарел')
        return None
    return pending


def _refresh_files_kb(bot, call, msg_id: int, pending: dict) -> None:
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=_files_keyboard(msg_id, pending),
        )
    except Exception:
        pass  # Telegram ругается, если клавиатура не изменилась
    bot.answer_callback_query(call.id)


def _short(name: str, limit: int = 32) -> str:
    base = os.path.basename(name)
    return base if len(base) <= limit else base[:limit - 1] + '…'


def _files_keyboard(msg_id: int, pending: dict) -> InlineKeyboardMarkup:
    """Клавиатура выбора файлов с галочками и постраничным листанием."""
    files = pending['files']
    wanted = pending.get('wanted', set())
    pages = max(1, (len(files) + _FILES_PER_PAGE - 1) // _FILES_PER_PAGE)
    page = max(0, min(pending.get('page', 0), pages - 1))

    kb = InlineKeyboardMarkup()
    for f in files[page * _FILES_PER_PAGE:(page + 1) * _FILES_PER_PAGE]:
        mark = '✅' if f['id'] in wanted else '⬜'
        kb.add(InlineKeyboardButton(
            f"{mark} {_short(f['name'])} · {_fmt(f['size'])}",
            callback_data=f"torrent:fsel_{msg_id}_{f['id']}",
        ))

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton('⬅️', callback_data=f'torrent:fpg_{msg_id}_{page - 1}'))
        nav.append(InlineKeyboardButton(f'{page + 1}/{pages}', callback_data=f'torrent:fpg_{msg_id}_{page}'))
        if page < pages - 1:
            nav.append(InlineKeyboardButton('➡️', callback_data=f'torrent:fpg_{msg_id}_{page + 1}'))
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton('✅ Все', callback_data=f'torrent:fall_{msg_id}'),
        InlineKeyboardButton('⬜ Снять все', callback_data=f'torrent:fnone_{msg_id}'),
    )
    total = sum(f['size'] for f in files if f['id'] in wanted)
    kb.add(InlineKeyboardButton(
        f'▶️ Скачать {len(wanted)} из {len(files)} · {_fmt(total)}',
        callback_data=f'torrent:fgo_{msg_id}',
    ))
    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'torrent:reject_{msg_id}'))
    return kb


def _start_download(bot, call, msg_id: int) -> None:
    """Применить выбор файлов, проверить место и запустить загрузку."""
    with _pending_lock:
        pending = _pending.get(msg_id)
    if not pending or 'location' not in pending:
        bot.answer_callback_query(call.id, 'Запрос устарел')
        return

    files = pending.get('files')
    wanted = pending.get('wanted')
    folder = pending['folder']
    location = pending['location']

    if files:
        if not wanted:
            bot.answer_callback_query(call.id, 'Не выбран ни один файл', show_alert=True)
            return
        need = sum(f['size'] for f in files if f['id'] in wanted)
        picked = f"\n🎬 Файлов: {len(wanted)} из {len(files)}"
    else:
        need = pending['size']
        picked = ''

    # Место проверяем на диске выбранной папки — папки могут быть на разных дисках
    try:
        if need and os.path.isdir(location):
            free = psutil.disk_usage(location).free
            if free < need:
                bot.answer_callback_query(
                    call.id,
                    f'❌ В «{folder}» мало места: свободно {_fmt(free)}, нужно {_fmt(need)}',
                    show_alert=True,
                )
                return
    except OSError:
        pass

    try:
        if files:
            unwanted = [f['id'] for f in files if f['id'] not in wanted]
            tr.set_files_wanted(pending['torrent_id'], sorted(wanted), unwanted)
        tr.mark_active(pending['file_hash'])
        tr.resume_torrent(pending['torrent_id'])
    except Exception as e:
        bot.answer_callback_query(call.id, f'Ошибка: {e}')
        return

    with _pending_lock:
        _pending.pop(msg_id, None)

    detail = f"{pending['name']} → {folder}"
    if files:
        detail += f" ({len(wanted)}/{len(files)} файлов)"
    audit(call.from_user, 'TORRENT_START', detail)

    stats = ''
    try:
        if os.path.isdir(location):
            stats = (f"\n📁 В «{folder}» уже {_fmt(_dir_size(location))}"
                     f" · свободно {_fmt(psutil.disk_usage(location).free)}")
    except OSError:
        pass

    try:
        sent = bot.edit_message_text(
            f"⏳ Загружается: {pending['name']}\n📁 Папка: {folder}{picked}{stats}\n⏳ 0%",
            call.message.chat.id, call.message.message_id,
        )
        tr.start_monitoring(
            bot, call.message.chat.id, sent.message_id,
            pending['torrent_id'], pending['file_hash'],
        )
    except Exception as e:
        bot.answer_callback_query(call.id, f'Ошибка: {e}')


# ── helpers ───────────────────────────────────────────────────────────────────

def _folder_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    """Keyboard to choose the destination folder, with free space per folder."""
    kb = InlineKeyboardMarkup()
    for i, folder in enumerate(config.ALLOWED_FOLDERS):
        path = os.path.join(config.SHARED_FOLDER, folder)
        try:
            free = psutil.disk_usage(path).free
            label = f'📁 {folder} · {_free_h(free)} своб.'
        except OSError:
            label = f'📁 {folder}'
        kb.add(InlineKeyboardButton(label, callback_data=f'torrent:dest_{msg_id}_{i}'))
    kb.add(InlineKeyboardButton('❌ Отмена', callback_data=f'torrent:reject_{msg_id}'))
    return kb


def _free_h(n: int) -> str:
    gb = n / 1024 ** 3
    return f'{gb / 1024:.1f} ТБ' if gb >= 1024 else f'{gb:.0f} ГБ'


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


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
