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


class DuplicateTorrentError(Exception):
    """Torrent with the same infohash is already in Transmission.

    Transmission answers "torrent-duplicate" and transmission-rpc silently
    returns the EXISTING torrent, so without this check the caller would treat
    someone else's running download as its own (move it, cancel it, watch it twice).
    """

    def __init__(self, torrent):
        self.torrent = torrent
        super().__init__('torrent is already in Transmission')


# snapshot of hashes + add must be atomic, or two simultaneous adds of the same
# torrent would both look "new"
_add_lock = threading.Lock()


def _known_hashes(tc) -> set[str]:
    return {_hash_of(t) for t in tc.get_torrents(arguments=['id', 'hashString'])}


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


# ── seeding after completion ──────────────────────────────────────────────────
# {hashString: {'name', 'until'}} — когда убрать торрент из Transmission.
# На диске, чтобы срок не сбрасывался после перезапуска бота.
_SEED_FILE = Path(config.HISTORY_FILE).parent / 'seeding.json'


def _seed_load() -> dict:
    try:
        return json.loads(_SEED_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _seed_save(data: dict) -> None:
    try:
        tmp = _SEED_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(_SEED_FILE)
    except Exception as e:
        logger.warning(f'Could not save seeding list: {e}')


def _seed_add(hs: str, name: str, hours: float) -> None:
    with _watch_lock:
        data = _seed_load()
        data[hs] = {'name': name, 'until': time.time() + hours * 3600}
        _seed_save(data)


def cleanup_seeding() -> int:
    """Убрать из Transmission торренты, у которых вышел срок раздачи (файлы остаются)."""
    with _watch_lock:
        data = _seed_load()
    due = [hs for hs, v in data.items() if v.get('until', 0) <= time.time()]
    if not due:
        return 0
    tc = get_client()
    present = {_hash_of(t) for t in tc.get_torrents(arguments=['id', 'hashString'])}
    removed = 0
    for hs in due:
        if hs in present:
            try:
                tc.remove_torrent(hs, delete_data=False)
                removed += 1
                logger.info(f"Seeding finished, removed from Transmission: {data[hs].get('name', hs)}")
            except Exception as e:
                logger.warning(f'Could not remove seeded torrent {hs}: {e}')
                continue  # попробуем в следующий раз
        data.pop(hs, None)  # убран или его уже удалили вручную
    with _watch_lock:
        current = _seed_load()
        for hs in due:
            if hs not in data:
                current.pop(hs, None)
        _seed_save(current)
    return removed


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


def _left_bytes(torrent) -> int:
    """Сколько байт выбранных файлов ещё не скачано — точно, без округления."""
    try:
        return int(torrent.left_until_done)
    except (KeyError, AttributeError, TypeError, ValueError):
        return int(_wanted_size(torrent) * (1 - (torrent.percent_done or 0)))


def _local_error(torrent) -> str:
    """Local Transmission error text (disk full, no write permission...), else ''.

    Tracker warnings/errors (codes 1 and 2) are routine and don't stop the download.
    """
    try:
        return (torrent.error_string or 'unknown error') if torrent.error == 3 else ''
    except (KeyError, AttributeError):
        return ''


def _is_complete(torrent) -> bool:
    if torrent.status in ('seeding', 'seed pending'):  # seed pending — ждёт очереди раздач
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
    with _add_lock:
        known = _known_hashes(tc)
        with open(file_path, 'rb') as f:
            torrent = tc.add_torrent(f, paused=True, download_dir=config.DOWNLOAD_DIR)

    time.sleep(0.5)
    torrent = tc.get_torrent(torrent.id)
    if _hash_of(torrent) in known:
        raise DuplicateTorrentError(torrent)

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
    with _add_lock:
        known = _known_hashes(tc)
        torrent = tc.add_torrent(magnet_link, paused=True, download_dir=config.DOWNLOAD_DIR)
    if _hash_of(torrent) in known:
        raise DuplicateTorrentError(tc.get_torrent(torrent.id))
    return torrent


def add_and_start(source: bytes | str, location: str):
    """Добавить торрент (bytes .torrent или magnet) сразу в папку и запустить.

    Для автоскачивания без участия пользователя (подписки на сериалы).
    Место проверяется на диске целевой папки. Raises InsufficientSpaceError.
    """
    tc = get_client()
    with _add_lock:
        known = _known_hashes(tc)
        torrent = tc.add_torrent(source, paused=True, download_dir=location)
    time.sleep(0.5)
    torrent = tc.get_torrent(torrent.id)
    if _hash_of(torrent) in known:
        raise DuplicateTorrentError(torrent)
    size = torrent.total_size or 0
    if size > 0:
        free = psutil.disk_usage(location).free
        if free < size:
            tc.remove_torrent(torrent.id, delete_data=False)
            raise InsufficientSpaceError(size, free)
    tc.start_torrent(torrent.id)
    return torrent


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
    prev_left = None   # сколько байт оставалось на прошлой проверке
    conn_errors = 0
    stall_notified = False
    conn_notified = False
    error_notified = False
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

            err = _local_error(torrent)
            if err and not error_notified:
                error_notified = True
                _safe_send(bot, chat_id,
                           f"⚠️ {torrent.name}: Transmission остановил загрузку из-за ошибки:\n"
                           f"{err}\nПосле исправления возобнови: /resume")
            elif not err:
                error_notified = False

            size = _wanted_size(torrent)
            # Процент — из точных байтов: torrent.progress в transmission-rpc
            # округлён до сотых, и 99.996% у раздачи на 49 ГБ (ещё ~2 МБ)
            # превращался в «100%» — бот рано писал «сохраняю».
            left = _left_bytes(torrent)
            pct = 100.0 * (size - left) / size if size else torrent.percent_done * 100
            # Всё скачано, но Transmission ещё не закончил: проверяет данные или
            # переименовывает .part (обычно секунды)
            finishing = left == 0
            if finishing:
                progress = 'finishing'
                text = (
                    f"💾 {torrent.name}\n"
                    f"📦 Размер: {_fmt_size(size)}\n"
                    f"Скачано, завершаю…"
                )
            else:
                # 99.7% вместо «99%», который висит долго и выглядит как зависание
                progress = f'{int(pct * 10) / 10:.1f}' if pct >= 99 else str(int(pct))  # вниз: 99.97 → 99.9
                text = (
                    f"⏳ {torrent.name}\n"
                    f"📦 Размер: {_fmt_size(size)}\n"
                    f"⬇️ {_fmt_size(size - left)} / "
                    f"{_fmt_size(size)} ({progress}%)\n"
                    f"🚀 Загрузка: {_fmt_speed(torrent.rate_download)}  "
                    f"📤 Отдача: {_fmt_speed(torrent.rate_upload)}"
                )
                if pct >= 99:
                    text += "\nОстались последние куски — они часто приходят медленнее, это нормально."

            # «Есть прогресс» — по байтам: у больших раздач 1% — это сотни МБ,
            # и по целым процентам бот зря решал, что загрузка встала
            moved = prev_left is not None and left < prev_left
            prev_left = left

            changed = progress != prev_progress
            if changed and message_id is not None:
                try:
                    bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                except Exception:
                    pass
            prev_progress = progress

            if changed or moved:
                interval_idx = 0
                stall_count = 0
                stall_notified = False
            elif finishing:
                pass  # это не «нет прогресса» — не замедляемся, ждём завершения
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
            finishing = False

        time.sleep(15 if finishing else intervals[interval_idx])


def _on_complete(bot, chat_id: int, message_id: int | None, torrent, file_hash: str, tc) -> None:
    """Завершение: убрать из Transmission, записать в историю, обновить Plex, сообщить.

    Важные действия идут ДО уведомлений — если Telegram недоступен,
    история и Plex всё равно обновятся.
    """
    name = torrent.name
    size = _wanted_size(torrent)
    dl_dir = torrent.download_dir
    try:
        seed_note = ''
        if config.SEED_HOURS > 0 and _hash_of(torrent):
            # Оставляем на раздаче: другим тоже нужны последние куски. Уберёт
            # cleanup_seeding() из планировщика, когда выйдет срок.
            _seed_add(_hash_of(torrent), name, config.SEED_HOURS)
            seed_note = f'\n⬆️ Раздаю ещё {config.SEED_HOURS:g} ч, потом уберу из Transmission (файлы останутся)'
        else:
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
        _safe_send(bot, chat_id, f"🏁 {name} сохранён в {dl_dir}{stats}{seed_note}")
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
