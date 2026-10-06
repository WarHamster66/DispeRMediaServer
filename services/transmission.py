"""Transmission RPC client wrapper with torrent monitoring.

Uses the maintained `transmission-rpc` library (Transmission 3.0 / 4.0 compatible).
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import psutil
from transmission_rpc import Client

from core import config
from services.download_history import record_download

logger = logging.getLogger(__name__)

# Tracks in-progress torrents: {file_hash: True}
_active: dict[str, bool] = {}
_active_lock = threading.Lock()

# Торренты, за которыми следит бот, сохраняются на диск — чтобы после
# перезагрузки сервера или /update продолжить следить и прислать «🏁 сохранён».
# Ключ — hashString (числовые id Transmission меняются после рестарта демона).
_WATCH_FILE = Path(config.HISTORY_FILE).parent / 'active_torrents.json'
_watch_lock = threading.Lock()


class InsufficientSpaceError(Exception):
    def __init__(self, required: int, available: int):
        self.required = required
        self.available = available
        super().__init__(f"Need {required} bytes, only {available} available")


def get_client() -> Client:
    return Client(
        host=config.TRANSMISSION_HOST,
        port=config.TRANSMISSION_PORT,
        username=config.TRANSMISSION_USER,
        password=config.TRANSMISSION_PASSWORD,
        timeout=60,  # при активной записи на диск демон может отвечать медленно
    )


def is_active(file_hash: str) -> bool:
    with _active_lock:
        return file_hash in _active


def mark_active(file_hash: str) -> None:
    with _active_lock:
        _active[file_hash] = True


def mark_done(file_hash: str) -> None:
    with _active_lock:
        _active.pop(file_hash, None)


def clear_active() -> None:
    """Сбросить реестр активных загрузок.

    Нужен после /clear_downloads: торренты удалены из Transmission, но реестр
    в памяти о них ещё «помнит» и не даёт добавить тот же файл заново.
    """
    with _active_lock:
        _active.clear()
    with _watch_lock:
        _save_watch({})


# ── persistent watch list ─────────────────────────────────────────────────────

def _load_watch() -> dict:
    try:
        return json.loads(_WATCH_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _save_watch(data: dict) -> None:
    try:
        tmp = _WATCH_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(_WATCH_FILE)  # атомарно — файл не побьётся при внезапном выключении
    except Exception as e:
        logger.warning(f'Could not save watch list: {e}')


def _watch_add(key: str, chat_id: int, file_hash: str, name: str) -> None:
    with _watch_lock:
        data = _load_watch()
        data[key] = {'chat_id': chat_id, 'file_hash': file_hash, 'name': name}
        _save_watch(data)


def _watch_remove(key) -> None:
    with _watch_lock:
        data = _load_watch()
        if data.pop(str(key), None) is not None:
            _save_watch(data)


def resume_monitoring(bot) -> int:
    """После старта бота продолжить следить за недокачанными торрентами."""
    with _watch_lock:
        watch = _load_watch()
    for key, info in watch.items():
        chat_id = info.get('chat_id') or config.CHAT_ID
        file_hash = info.get('file_hash') or key
        mark_active(file_hash)
        message_id = None
        try:
            sent = bot.send_message(chat_id, f"🔄 После перезапуска продолжаю следить: {info.get('name', key)}")
            message_id = sent.message_id
        except Exception as e:
            logger.warning(f'Resume notify failed: {e}')
        start_monitoring(bot, chat_id, message_id, key, file_hash)
    return len(watch)


def _hash_of(torrent) -> str:
    return getattr(torrent, 'hashString', None) or getattr(torrent, 'hash_string', '') or ''


def _wanted_size(torrent) -> int:
    """Размер выбранных файлов (с учётом выбора серий), иначе полный размер."""
    return getattr(torrent, 'size_when_done', 0) or torrent.total_size


def _is_complete(torrent) -> bool:
    if torrent.status == 'seeding':
        return True
    # Скачан целиком, но стоит на паузе (например, поставили паузу на 100%)
    done = getattr(torrent, 'percent_done', 0) or 0
    return torrent.status == 'stopped' and done >= 1.0 and _wanted_size(torrent) > 0


def _safe_send(bot, chat_id: int, text: str) -> None:
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f'send_message failed: {e}')


def add_torrent_file(file_path: str):
    """Add a .torrent file PAUSED and check disk space.

    The torrent is left paused so the caller can ask the user which folder to
    download into (see set_location) before starting it.
    Raises InsufficientSpaceError if there is not enough free space.
    """
    tc = get_client()
    with open(file_path, 'rb') as f:
        torrent = tc.add_torrent(f, paused=True, download_dir=config.DOWNLOAD_DIR)

    time.sleep(0.5)
    torrent = tc.get_torrent(torrent.id)

    if torrent.total_size > 0:
        free = psutil.disk_usage(config.DOWNLOAD_DIR).free
        if free < torrent.total_size:
            tc.remove_torrent(torrent.id, delete_data=False)
            raise InsufficientSpaceError(torrent.total_size, free)

    return torrent


def add_magnet(magnet_link: str):
    """Add a magnet link PAUSED. Size is unknown until metadata arrives, so disk
    space is not checked. Left paused so the caller can pick a folder first."""
    tc = get_client()
    return tc.add_torrent(magnet_link, paused=True, download_dir=config.DOWNLOAD_DIR)


def set_location(torrent_id: int, location: str) -> None:
    """Set the final download directory for a torrent (used for folder selection)."""
    get_client().move_torrent_data(torrent_id, location)


def get_files(torrent_id: int) -> list[dict]:
    """Files inside a torrent as [{id, name, size}].

    The list index IS the file id used by change_torrent() — see transmission-rpc
    docs ("the index of file object is the id of the file"). Returns [] for magnets
    whose metadata has not arrived yet.
    """
    try:
        files = get_client().get_torrent(torrent_id).get_files()
    except Exception as e:
        logger.warning(f"Could not read files of torrent {torrent_id}: {e}")
        return []
    return [
        {'id': i,
         'name': getattr(f, 'name', f'file {i}'),
         'size': getattr(f, 'size', 0)}
        for i, f in enumerate(files)
    ]


def set_files_wanted(torrent_id: int, wanted: list[int], unwanted: list[int]) -> None:
    """Pick which files of a torrent to download (ids are indices from get_files)."""
    get_client().change_torrent(
        torrent_id,
        files_wanted=list(wanted),
        files_unwanted=list(unwanted),
    )


def get_all_torrents() -> list:
    return get_client().get_torrents()


def pause_torrent(torrent_id: int) -> None:
    get_client().stop_torrent(torrent_id)


def resume_torrent(torrent_id: int) -> None:
    get_client().start_torrent(torrent_id)


def remove_torrent(torrent_id: int, delete_data: bool = False) -> None:
    get_client().remove_torrent(torrent_id, delete_data=delete_data)


def start_monitoring(bot, chat_id: int, message_id: int | None, torrent_id, file_hash: str) -> None:
    """Start a daemon thread that monitors torrent progress and edits the status message.

    torrent_id — числовой id или hashString. message_id может быть None
    (тогда прогресс не редактируется, приходят только итоговые сообщения).
    """
    t = threading.Thread(
        target=_monitor_loop,
        args=(bot, chat_id, message_id, torrent_id, file_hash),
        daemon=True,
    )
    t.start()


def _monitor_loop(bot, chat_id: int, message_id: int | None, torrent_id, file_hash: str) -> None:
    """Следит за торрентом, пока он не докачается или его не удалят.

    Мониторинг больше не «сдаётся»: при долгом отсутствии прогресса или
    недоступности Transmission он один раз предупреждает и продолжает
    проверять раз в 10 минут — чтобы гарантированно поймать завершение.
    """
    intervals = [10, 60, 300, 600]
    interval_idx = 0
    stall_count = 0
    max_stalls = 4
    prev_progress = -1
    conn_errors = 0
    stall_notified = False
    conn_notified = False
    key = torrent_id  # после первого ответа заменим на стабильный hashString

    while True:
        try:
            tc = get_client()
            torrent = tc.get_torrent(key)
            conn_errors = 0
        except Exception as e:
            # Торрент удалили (например, через /clear_downloads) — мониторить нечего
            if 'not found' in str(e).lower():
                logger.info(f"Torrent {key} removed — monitoring stopped")
                _watch_remove(key)
                mark_done(file_hash)
                return
            # Временный сбой RPC (демон занят записью на диск и т.п.) — ждём
            conn_errors += 1
            logger.warning(f"Monitor: RPC error #{conn_errors} for torrent {key}: {e}")
            if conn_errors == 10 and not conn_notified:
                conn_notified = True
                _safe_send(bot, chat_id,
                           "⚠️ Transmission не отвечает ~5 минут. Продолжаю следить "
                           "за загрузкой — проверь /torrents")
            time.sleep(30 if conn_errors < 10 else 300)
            continue

        hs = _hash_of(torrent)
        if hs and key != hs:
            key = hs
            _watch_add(hs, chat_id, file_hash, torrent.name)

        try:
            if _is_complete(torrent):
                _on_complete(bot, chat_id, message_id, torrent, file_hash, tc)
                return

            size = _wanted_size(torrent)
            progress = int(torrent.progress)
            text = (
                f"⏳ {torrent.name}\n"
                f"📦 Размер: {_fmt_size(size)}\n"
                f"⬇️ {_fmt_size(int(size * torrent.progress / 100))} / "
                f"{_fmt_size(size)} ({progress}%)\n"
                f"🚀 Загрузка: {_fmt_speed(torrent.rate_download)}  "
                f"📤 Отдача: {_fmt_speed(torrent.rate_upload)}"
            )

            if progress != prev_progress:
                if message_id is not None:
                    try:
                        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                    except Exception:
                        pass
                prev_progress = progress
                interval_idx = 0
                stall_count = 0
                stall_notified = False
            else:
                stall_count += 1
                if stall_count >= max_stalls:
                    stall_count = 0
                    if interval_idx < len(intervals) - 1:
                        interval_idx += 1
                        logger.info(f"[{torrent.name}] No progress — checking every {intervals[interval_idx]}s")
                    elif not stall_notified:
                        stall_notified = True
                        _safe_send(bot, chat_id,
                                   f"🐢 {torrent.name}: давно нет прогресса (мало раздающих "
                                   f"или нет сети). Продолжаю следить — напишу, когда докачается.")

        except Exception as e:
            logger.error(f"Error in monitor loop for torrent {key}: {e}")

        time.sleep(intervals[interval_idx])


def _on_complete(bot, chat_id: int, message_id: int | None, torrent, file_hash: str, tc) -> None:
    """Завершение: убрать из Transmission, записать в историю, обновить Plex, сообщить.

    Важные действия идут ДО уведомлений — если Telegram недоступен,
    история и Plex всё равно обновятся.
    """
    name = torrent.name
    size = _wanted_size(torrent)
    dl_dir = torrent.download_dir
    try:
        try:
            tc.remove_torrent(torrent.id, delete_data=False)
        except Exception as e:
            logger.warning(f"Could not remove completed torrent {name}: {e}")
        record_download(name, size, dl_dir)
        logger.info(f"Torrent completed: {name}")

        # Просим Plex пересканировать библиотеку, чтобы файл сразу появился
        from services import plex
        plex.refresh_libraries_safe()

        if message_id is not None:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"✅ Загружено: {name}\n"
                        f"📦 Размер: {_fmt_size(size)}\n"
                        f"⬆️ Отдано: {_fmt_size(torrent.uploaded_ever)}"
                    ),
                )
            except Exception:
                pass

        # Статистика папки: сколько в ней уже занято и сколько свободно на диске
        stats = ''
        try:
            used = _dir_size(dl_dir)
            free = psutil.disk_usage(dl_dir).free
            folder_name = os.path.basename(dl_dir.rstrip('/'))
            stats = f"\n📁 В «{folder_name}» теперь {_fmt_size(used)} · свободно {_fmt_size(free)}"
        except Exception:
            pass
        _safe_send(bot, chat_id, f"🏁 {name} сохранён в {dl_dir}{stats}")
    except Exception as e:
        logger.error(f"Error handling completed torrent {name}: {e}")
        _safe_send(bot, chat_id, f"⚠️ Ошибка при завершении загрузки: {e}")
    finally:
        _watch_remove(_hash_of(torrent))
        mark_done(file_hash)


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _fmt_size(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b} B"


def _fmt_speed(bps: int) -> str:
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps} B/s"
