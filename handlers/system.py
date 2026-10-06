"""System info commands, /start, /update and /reboot."""
import json
import logging
import secrets
import subprocess
import threading
import time

import psutil
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit, tail as audit_tail
from core.auth import is_admin, is_authorized, require_admin
from services import system_monitor

logger = logging.getLogger(__name__)

SERVICE_NAME = 'media-server'
# Флажок «после перезапуска сообщить в чат». Имя файла прежнее — совместимость.
_RESTART_FLAG = config.BASE_DIR / 'data' / '.update_notify'

_REBOOT_TTL = 60  # сколько секунд действует кнопка подтверждения перезагрузки
_reboot_tokens: dict[str, float] = {}
_reboot_lock = threading.Lock()


def register(bot) -> None:
    bot.message_handler(commands=['start'])(lambda m: _cmd_start(bot, m))
    bot.message_handler(commands=['help'])(lambda m: _cmd_help(bot, m))
    bot.message_handler(commands=['uptime'])(lambda m: _simple(bot, m, system_monitor.get_uptime))
    bot.message_handler(commands=['wifi'])(lambda m: _simple(bot, m, system_monitor.get_wifi_signal))
    bot.message_handler(commands=['cpu'])(lambda m: _simple(bot, m, system_monitor.get_cpu_load))
    bot.message_handler(commands=['memory'])(lambda m: _simple(bot, m, system_monitor.get_memory_usage))
    bot.message_handler(commands=['disk'])(lambda m: _simple(bot, m, system_monitor.get_disk_usage))
    bot.message_handler(commands=['logs'])(lambda m: _simple(bot, m, system_monitor.get_last_logs))
    bot.message_handler(commands=['media'])(lambda m: _cmd_media(bot, m))
    bot.message_handler(commands=['export_logs'])(lambda m: _cmd_export_logs(bot, m))
    bot.message_handler(commands=['clear_logs'])(lambda m: _cmd_clear_logs(bot, m))
    bot.message_handler(commands=['backup'])(lambda m: _cmd_backup(bot, m))
    bot.message_handler(commands=['scan'])(lambda m: _cmd_scan(bot, m))
    bot.message_handler(commands=['audit'])(lambda m: _cmd_audit(bot, m))
    bot.message_handler(commands=['disks'])(lambda m: _cmd_disks(bot, m))
    bot.message_handler(commands=['update'])(lambda m: _do_update(bot, m.chat.id, m.from_user.id))
    bot.message_handler(commands=['reboot'])(lambda m: _ask_reboot(bot, m.chat.id, m.from_user))
    bot.callback_query_handler(func=lambda c: c.data.startswith('sys:'))(lambda c: _sys_callback(bot, c))

    # Сообщение «снова в строю» — в фоне, с повторами: после перезагрузки сервера
    # сеть и прокси поднимаются не мгновенно.
    threading.Thread(target=_notify_after_restart, args=(bot,),
                     name='RestartNotify', daemon=True).start()


# ── access helpers ────────────────────────────────────────────────────────────

def _admin_gate(bot, chat_id: int, user_id: int) -> bool:
    """Пропускает только админа; остальным — понятный отказ."""
    if not is_authorized(user_id):
        bot.send_message(chat_id, 'Нет доступа.')
        return False
    if not require_admin(user_id):
        bot.send_message(chat_id, '⛔ Эта команда доступна только администратору.')
        return False
    return True


# ── restart notification ──────────────────────────────────────────────────────

def _write_restart_flag(chat_id: int, kind: str) -> None:
    try:
        _RESTART_FLAG.write_text(json.dumps({'chat_id': chat_id, 'kind': kind}))
    except Exception as e:
        logger.warning(f'Could not write restart flag: {e}')


def _clear_restart_flag() -> None:
    try:
        _RESTART_FLAG.unlink()
    except OSError:
        pass


def _notify_after_restart(bot) -> None:
    """Если перезапуск был вызван /update или /reboot — сообщить, что бот снова онлайн."""
    if not _RESTART_FLAG.exists():
        return
    try:
        info = json.loads(_RESTART_FLAG.read_text().strip())
        if isinstance(info, int):  # старый формат флажка: просто chat_id
            info = {'chat_id': info, 'kind': 'update'}
        chat_id = int(info['chat_id'])
    except Exception as e:
        logger.warning(f'Bad restart flag: {e}')
        _clear_restart_flag()
        return

    if info.get('kind') == 'reboot':
        text = f'✅ Сервер перезагружен и снова в строю.\n{system_monitor.get_uptime()}'
    else:
        commit = subprocess.run(
            ['git', '-C', str(config.BASE_DIR), 'log', '-1', '--format=%h %s'],
            capture_output=True, text=True,
        ).stdout.strip()
        text = f'✅ Обновление применено, бот снова в строю.\n{commit}'

    for attempt in range(12):  # до ~3 минут ждём сеть
        try:
            bot.send_message(chat_id, text)
            break
        except Exception as e:
            logger.warning(f'Restart notify attempt {attempt + 1} failed: {e}')
            time.sleep(15)
    _clear_restart_flag()


# ── simple commands ───────────────────────────────────────────────────────────

def _simple(bot, message, fn) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.reply_to(message, fn())


def _cmd_start(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    kb = None
    if is_admin(message.from_user.id):
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton('🔄 Обновить медиасервер', callback_data='sys:update'))
        kb.add(InlineKeyboardButton('♻️ Перезагрузить сервер', callback_data='sys:reboot'))
    bot.send_message(
        message.chat.id,
        '👋 Медиасервер онлайн. Используй /help для списка команд.',
        reply_markup=kb,
    )


def _cmd_help(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    text = (
        '📖 Команды:\n\n'
        '📺 Сериалы (LostFilm)\n'
        '/follow — подписаться: новые серии качаются сами\n'
        '/series — мои подписки\n'
        '/lostfilm — аккаунт и вход на LostFilm (нужно для скачивания)\n\n'
        '🎬 Торренты\n'
        '/torrent — добавить торрент (файл или magnet)\n'
        '/torrents — список активных загрузок\n'
        '/pause — поставить на паузу\n'
        '/resume — возобновить\n'
        '/clear_downloads — очистить очередь Transmission\n\n'
        '📊 Система\n'
        '/report — полный отчёт о сервере\n'
        '/configure — настроить содержимое отчёта\n'
        '/uptime — время работы\n'
        '/cpu — загрузка CPU\n'
        '/memory — использование RAM\n'
        '/disk — состояние дисков\n'
        '/disks — подключённые диски и свободное место\n'
        '/wifi — состояние сети\n'
        '/logs — последние события бота\n'
        '/export_logs — скачать лог-файл\n'
        '/scan — пересканировать библиотеку Plex\n'
        '/backup — сохранить бэкап настроек на сервер\n\n'
        '📂 Файлы\n'
        '/dir — просмотр и удаление файлов\n'
        '/dir2 — управление папками\n'
        '/dir3 — переименование, перемещение, создание\n'
        '/media — статистика медиатеки\n\n'
        '📜 История\n'
        '/history — история загрузок\n'
    )
    if is_admin(message.from_user.id):
        text += (
            '\n👑 Администратор\n'
            '/reboot — перезагрузить сервер (с подтверждением)\n'
            '/update — обновить бота с GitHub и перезапустить\n'
            '/audit — журнал действий (кто что удалил/добавил)\n'
            '/clear_logs — очистить лог-файл\n'
        )
    bot.reply_to(message, text)


def _cmd_media(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.send_chat_action(message.chat.id, 'typing')
    bot.reply_to(message, f'📚 Медиатека:\n\n{system_monitor.get_media_stats()}')


def _cmd_export_logs(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        with open(config.LOG_FILE, 'rb') as f:
            bot.send_document(message.chat.id, f, caption='server.log')
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_clear_logs(bot, message) -> None:
    if not _admin_gate(bot, message.chat.id, message.from_user.id):
        return
    try:
        open(config.LOG_FILE, 'w').close()
        audit(message.from_user, 'CLEAR_LOGS')  # аудит-лог при этом сохраняется
        bot.reply_to(message, '✅ Лог-файл очищен.')
    except Exception as e:
        bot.reply_to(message, f'Ошибка: {e}')


def _cmd_backup(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        from services.backup import save_backup
        path = save_backup()
        audit(message.from_user, 'BACKUP', str(path))
        bot.reply_to(message, f'🗄 Бэкап сохранён на сервере:\n{path}')
    except Exception as e:
        bot.reply_to(message, f'Ошибка бэкапа: {e}')


def _cmd_scan(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    try:
        from services import plex
        n = plex.refresh_libraries()
        bot.reply_to(message, f'🔄 Plex: запущено сканирование {n} библиотек.')
    except Exception as e:
        bot.reply_to(message, f'⚠️ Не удалось обратиться к Plex: {e}')


def _cmd_audit(bot, message) -> None:
    """Последние записи журнала действий — кто что удалял/добавлял."""
    if not _admin_gate(bot, message.chat.id, message.from_user.id):
        return
    bot.reply_to(message, f'🕵️ Журнал действий (последние записи):\n\n{audit_tail(15)}')


def _cmd_disks(bot, message) -> None:
    """Все подключённые диски с местом — удобно после подключения нового."""
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    lines = []
    seen = set()
    for p in psutil.disk_partitions():
        # Только реальные диски: без snap-образов (loop/squashfs), служебных
        # разделов и bind-монтирований (один физический диск — одна строка)
        if not p.device.startswith('/dev/') or p.device in seen:
            continue
        if p.device.startswith('/dev/loop') or p.fstype == 'squashfs':
            continue
        if p.mountpoint.startswith(('/boot', '/snap')):
            continue
        seen.add(p.device)
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        gb = 1024 ** 3
        icon = '🖥' if p.mountpoint == '/' else '💽'
        name = 'Системный диск' if p.mountpoint == '/' else p.mountpoint
        lines.append(
            f'{icon} {name}\n'
            f'   {p.device} · {p.fstype} · свободно {u.free / gb:.1f} ГБ из {u.total / gb:.1f} ГБ ({u.percent}% занято)'
        )
    text = '\n\n'.join(lines) if lines else 'Диски не найдены.'
    text += '\n\n💡 Для переноса папок на новый диск:  sudo python3 migrate_disk.py'
    bot.reply_to(message, text)


# ── sys:* callbacks ───────────────────────────────────────────────────────────

def _sys_callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    data = call.data[len('sys:'):]
    chat_id = call.message.chat.id

    if data == 'update':
        bot.answer_callback_query(call.id)
        _do_update(bot, chat_id, call.from_user.id)
    elif data == 'reboot':
        bot.answer_callback_query(call.id)
        _ask_reboot(bot, chat_id, call.from_user)
    elif data.startswith('reboot_yes_'):
        _confirm_reboot(bot, call, data[len('reboot_yes_'):])
    elif data == 'reboot_no':
        bot.answer_callback_query(call.id, 'Отменено')
        try:
            bot.edit_message_text('❎ Перезагрузка отменена.', chat_id, call.message.message_id)
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, 'Неизвестное действие')


# ── /reboot ───────────────────────────────────────────────────────────────────

def _ask_reboot(bot, chat_id: int, user) -> None:
    """Шаг 1: спросить подтверждение. Кнопка одноразовая и живёт _REBOOT_TTL секунд."""
    if not _admin_gate(bot, chat_id, user.id):
        return

    token = secrets.token_hex(4)
    now = time.time()
    with _reboot_lock:
        for t, exp in list(_reboot_tokens.items()):
            if exp < now:
                _reboot_tokens.pop(t, None)
        _reboot_tokens[token] = now + _REBOOT_TTL

    warn = ''
    try:
        from services import transmission as tr
        n = sum(1 for t in tr.get_all_torrents() if t.status == 'downloading')
        if n:
            warn = f'\n⬇️ Сейчас качается: {n} — после перезагрузки загрузки продолжатся сами.'
    except Exception:
        pass

    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton('✅ Да, перезагрузить', callback_data=f'sys:reboot_yes_{token}'),
        InlineKeyboardButton('❌ Отмена', callback_data='sys:reboot_no'),
    )
    bot.send_message(
        chat_id,
        f'⚠️ Перезагрузить сервер?\n'
        f'Бот, Plex, Samba и Transmission будут недоступны 1–2 минуты.{warn}\n\n'
        f'Кнопка действует {_REBOOT_TTL} сек.',
        reply_markup=kb,
    )
    audit(user, 'REBOOT_REQUEST')


def _confirm_reboot(bot, call, token: str) -> None:
    """Шаг 2: админ подтвердил — перезагружаем."""
    chat_id = call.message.chat.id
    if not require_admin(call.from_user.id):
        bot.answer_callback_query(call.id, '⛔ Только для администратора', show_alert=True)
        return

    with _reboot_lock:
        exp = _reboot_tokens.pop(token, 0)
    if time.time() > exp:
        bot.answer_callback_query(call.id, 'Подтверждение устарело — запроси /reboot заново',
                                  show_alert=True)
        try:
            bot.edit_message_text('⌛ Подтверждение устарело.', chat_id, call.message.message_id)
        except Exception:
            pass
        return

    bot.answer_callback_query(call.id, 'Перезагружаю…')
    try:
        bot.edit_message_text('♻️ Перезагружаю сервер… Напишу, когда вернусь.',
                              chat_id, call.message.message_id)
    except Exception:
        pass
    audit(call.from_user, 'REBOOT')
    _write_restart_flag(chat_id, 'reboot')

    # Нужно правило sudoers NOPASSWD на `systemctl reboot` (ставит установщик)
    try:
        r = subprocess.run(['sudo', '-n', 'systemctl', 'reboot'],
                           capture_output=True, text=True, timeout=20)
        ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:
        ok, err = False, str(e)

    if not ok:
        _clear_restart_flag()
        logger.error(f'Reboot failed: {err}')
        bot.send_message(
            chat_id,
            f'❌ Не удалось перезагрузить: {err[:300]}\n\n'
            f'Похоже, нет разрешения sudo — см. README, раздел «Перезагрузка из бота».',
        )


# ── /update ───────────────────────────────────────────────────────────────────

# Файлы, которые лежат в репозитории как шаблон, но на каждом сервере свои
# (пути, город, токен Plex…). git pull не умеет обновляться поверх их
# локальных изменений, поэтому перед обновлением сохраняем свою версию,
# возвращаем файл к версии из репозитория, обновляемся и кладём свою обратно.
_LOCAL_FILES = ('config.json',)


def _pull_keeping_local(base: str) -> subprocess.CompletedProcess:
    def git(*args):
        return subprocess.run(['git', '-C', base, *args],
                              capture_output=True, text=True, timeout=120)

    saved: dict[str, bytes] = {}
    for name in _LOCAL_FILES:
        path = config.BASE_DIR / name
        if path.exists():
            saved[name] = path.read_bytes()
            git('update-index', '--no-skip-worktree', name)  # снять флаг, если ставили
            git('checkout', '--', name)
    try:
        return git('pull', '--ff-only')
    finally:
        for name, data in saved.items():
            (config.BASE_DIR / name).write_bytes(data)


def _do_update(bot, chat_id: int, user_id: int) -> None:
    """git pull + обновление зависимостей + перезапуск сервиса."""
    if not _admin_gate(bot, chat_id, user_id):
        return

    base = str(config.BASE_DIR)
    bot.send_message(chat_id, '🔄 Проверяю обновления…')

    # git pull, бережно обходя файлы с локальными настройками сервера
    try:
        r = _pull_keeping_local(base)
    except Exception as e:
        bot.send_message(chat_id, f'❌ Ошибка git pull: {e}')
        return

    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        bot.send_message(chat_id, f'❌ git pull не удался:\n{out[:600]}')
        return
    if 'up to date' in out.lower() or 'up-to-date' in out.lower():
        bot.send_message(chat_id, '✅ Уже последняя версия.')
        return

    # обновляем зависимости (вдруг появились новые)
    pip = config.BASE_DIR / 'venv' / 'bin' / 'pip'
    if pip.exists():
        try:
            subprocess.run([str(pip), 'install', '-q', '-r', str(config.BASE_DIR / 'requirements.txt')],
                           timeout=300)
        except Exception as e:
            logger.warning(f'pip update failed: {e}')

    commit = subprocess.run(['git', '-C', base, 'log', '-1', '--format=%h %s'],
                            capture_output=True, text=True).stdout.strip()
    audit(user_id, 'UPDATE', commit)
    bot.send_message(chat_id, f'✅ Обновлено до:\n{commit}\n\n♻️ Перезапускаюсь…')

    _write_restart_flag(chat_id, 'update')

    # Перезапуск выполняет systemd (наш процесс при этом завершится).
    # Требуется правило sudoers NOPASSWD на systemctl restart (ставит установщик).
    subprocess.Popen(['sudo', '-n', 'systemctl', 'restart', SERVICE_NAME])
