"""Transmission RPC client wrapper with torrent monitoring.

Uses the maintained `transmission-rpc` library (Transmission 3.0 / 4.0 compatible).
"""
import logging
import threading
import time

import psutil
from transmission_rpc import Client

from core import config
from services.download_history import record_download

logger = logging.getLogger(__name__)

# Tracks in-progress torrents: {file_hash: True}
_active: dict[str, bool] = {}
_active_lock = threading.Lock()


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


def get_all_torrents() -> list:
    return get_client().get_torrents()


def pause_torrent(torrent_id: int) -> None:
    get_client().stop_torrent(torrent_id)


def resume_torrent(torrent_id: int) -> None:
    get_client().start_torrent(torrent_id)


def remove_torrent(torrent_id: int, delete_data: bool = False) -> None:
    get_client().remove_torrent(torrent_id, delete_data=delete_data)


def start_monitoring(bot, chat_id: int, message_id: int, torrent_id: int, file_hash: str) -> None:
    """Start a daemon thread that monitors torrent progress and edits the status message."""
    t = threading.Thread(
        target=_monitor_loop,
        args=(bot, chat_id, message_id, torrent_id, file_hash),
        daemon=True,
    )
    t.start()


def _monitor_loop(bot, chat_id: int, message_id: int, torrent_id: int, file_hash: str) -> None:
    """Monitor torrent using a while loop (no recursion — fixes stack overflow on long downloads)."""
    intervals = [10, 60, 300, 600]
    interval_idx = 0
    stall_count = 0
    max_stalls = 4
    prev_progress = -1

    while True:
        try:
            tc = get_client()
            torrent = tc.get_torrent(torrent_id)
        except Exception as e:
            logger.error(f"Failed to get torrent {torrent_id}: {e}")
            bot.send_message(chat_id, f"⚠️ Ошибка мониторинга: {e}")
            mark_done(file_hash)
            return

        try:
            if torrent.status == 'seeding':
                _on_complete(bot, chat_id, message_id, torrent, file_hash, tc)
                return

            progress = int(torrent.progress)
            text = (
                f"⏳ {torrent.name}\n"
                f"📦 Размер: {_fmt_size(torrent.total_size)}\n"
                f"⬇️ {_fmt_size(int(torrent.total_size * torrent.progress / 100))} / "
                f"{_fmt_size(torrent.total_size)} ({progress}%)\n"
                f"🚀 Загрузка: {_fmt_speed(torrent.rate_download)}  "
                f"📤 Отдача: {_fmt_speed(torrent.rate_upload)}"
            )

            if progress != prev_progress:
                try:
                    bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                except Exception:
                    pass
                prev_progress = progress
                interval_idx = 0
                stall_count = 0
            else:
                stall_count += 1
                if stall_count >= max_stalls:
                    if interval_idx < len(intervals) - 1:
                        interval_idx += 1
                        stall_count = 0
                        logger.info(f"[{torrent.name}] No progress — checking every {intervals[interval_idx]}s")
                    else:
                        bot.send_message(chat_id, f"❌ {torrent.name}: нет прогресса, мониторинг остановлен")
                        mark_done(file_hash)
                        return

        except Exception as e:
            logger.error(f"Error in monitor loop for torrent {torrent_id}: {e}")

        time.sleep(intervals[interval_idx])


def _on_complete(bot, chat_id: int, message_id: int, torrent, file_hash: str, tc) -> None:
    name = torrent.name
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"✅ Загружено: {name}\n"
                f"📦 Размер: {_fmt_size(torrent.total_size)}\n"
                f"⬆️ Отдано: {_fmt_size(torrent.uploaded_ever)}"
            ),
        )
        tc.remove_torrent(torrent.id, delete_data=False)
        bot.send_message(chat_id, f"🏁 {name} сохранён в {torrent.download_dir}")
        record_download(name, torrent.total_size, torrent.download_dir)
        logger.info(f"Torrent completed: {name}")

        # Просим Plex пересканировать библиотеку, чтобы файл сразу появился
        from services import plex
        plex.refresh_libraries_safe()
    except Exception as e:
        logger.error(f"Error handling completed torrent {name}: {e}")
        bot.send_message(chat_id, f"⚠️ Ошибка при завершении загрузки: {e}")
    finally:
        mark_done(file_hash)


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
