"""Background tasks: daily report, network monitor, disk alert, weekly backup."""
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil
import pytz

from core import config

logger = logging.getLogger(__name__)

_next_report: datetime | None = None
_disk_alert_sent = False

_BACKUP_INTERVAL = 7 * 24 * 3600  # раз в неделю
_BACKUP_STAMP = Path(config.NETWORK_FILE).parent / '.last_backup'


def start_background_tasks(bot) -> None:
    logger.info('Starting background tasks…')
    _calc_next_report()

    for target, name in (
        (_report_loop, 'ReportScheduler'),
        (_network_loop, 'NetworkMonitor'),
        (_disk_alert_loop, 'DiskAlertMonitor'),
        (_backup_loop, 'WeeklyBackup'),
        (_resume_torrents, 'ResumeTorrents'),
        (_series_loop, 'SeriesWatcher'),
        (_seeding_loop, 'SeedingCleanup'),
    ):
        t = threading.Thread(target=target, args=(bot,), name=name, daemon=True)
        t.start()
        logger.info(f'  {name} started')


def reschedule_report() -> None:
    """Called when the user changes the report time via /configure."""
    _calc_next_report()


# ── internal ──────────────────────────────────────────────────────────────────

def _resolve_tz():
    """Часовой пояс для планировщика.

    Если TIMEZONE пуст или 'system'/'auto'/'local' — используем время сервера.
    Иначе — заданную зону (например 'Asia/Yekaterinburg').
    """
    name = (config.TIMEZONE or '').strip()
    if not name or name.lower() in ('system', 'auto', 'local'):
        return None  # время сервера
    try:
        return pytz.timezone(name)
    except Exception:
        logger.warning(f"Неизвестный TIMEZONE '{name}', использую время сервера")
        return None


def _calc_next_report() -> None:
    global _next_report
    from handlers.reports import get_report_config
    cfg = get_report_config()
    report_time = datetime.strptime(cfg['REPORT_TIME'], '%H:%M').time()
    tz = _resolve_tz()
    if tz is None:
        now = datetime.now().astimezone()  # время сервера (с локальной зоной)
        candidate = datetime.combine(now.date(), report_time).astimezone()
    else:
        now = datetime.now(tz)
        candidate = tz.localize(datetime.combine(now.date(), report_time))
    if candidate <= now:
        candidate += timedelta(days=1)
    _next_report = candidate
    logger.info(f'Next scheduled report: {_next_report.strftime("%Y-%m-%d %H:%M %Z")}')


def _report_loop(bot) -> None:
    global _next_report
    while True:
        try:
            # Сравнение по абсолютному времени — зона now не важна
            now = datetime.now().astimezone()
            if _next_report and now >= _next_report:
                _send_scheduled_report(bot)
                _calc_next_report()
        except Exception as e:
            logger.error(f'Report scheduler error: {e}', exc_info=True)
        time.sleep(60)


_REPORT_RETRIES = 6        # если Telegram недоступен — до 6 попыток…
_REPORT_RETRY_DELAY = 300  # …раз в 5 минут


def _send_scheduled_report(bot) -> None:
    from core.textutil import chunks
    from handlers.reports import build_report
    try:
        text = f'🕐 Ежедневный отчёт:\n\n{build_report()}'
    except Exception as e:
        logger.error(f'Could not build daily report: {e}')
        return
    # Каждую часть отправляем со своими повторами: если сеть моргнула на второй
    # части, первая не уйдёт дважды
    for part in chunks(text):
        for attempt in range(1, _REPORT_RETRIES + 1):
            try:
                bot.send_message(config.CHAT_ID, part)
                break
            except Exception as e:
                if attempt == _REPORT_RETRIES:
                    logger.error(f'Daily report not sent after {attempt} attempts: {e}')
                    return
                logger.warning(f'Daily report attempt {attempt} failed, retry in '
                               f'{_REPORT_RETRY_DELAY // 60} min: {e}')
                time.sleep(_REPORT_RETRY_DELAY)
    logger.info('Daily report sent')


def _seeding_loop(bot) -> None:
    """Убирать из Transmission торренты, которые отраздавали своё (SEED_HOURS)."""
    from services import transmission as tr
    time.sleep(60)
    while True:
        try:
            tr.cleanup_seeding()
        except Exception as e:
            logger.warning(f'Seeding cleanup error: {e}')
        time.sleep(600)


def _network_loop(bot) -> None:
    from services.network_tracker import save_network_data
    while True:
        try:
            save_network_data()
        except Exception as e:
            logger.error(f'Network monitor error: {e}', exc_info=True)
        time.sleep(900)  # every 15 minutes


def _disk_alert_loop(bot) -> None:
    global _disk_alert_sent
    while True:
        try:
            usage = psutil.disk_usage(config.DISK_PATH)
            free_pct = 100 - usage.percent
            if free_pct < config.DISK_ALERT_THRESHOLD:
                if not _disk_alert_sent:
                    free_gb = usage.free / 1024 ** 3
                    bot.send_message(
                        config.CHAT_ID,
                        f'⚠️ Мало места на диске!\n'
                        f'Свободно: {free_gb:.1f} GB ({free_pct:.1f}%)\n'
                        f'Используй /dir2 для удаления лишнего.',
                    )
                    _disk_alert_sent = True
                    logger.warning(f'Disk alert sent: {free_pct:.1f}% free')
            else:
                _disk_alert_sent = False  # reset so next breach triggers new alert
        except Exception as e:
            logger.error(f'Disk alert error: {e}', exc_info=True)
        time.sleep(3600)  # check every hour


def _series_loop(bot) -> None:
    """Подписки на сериалы: проверять LostFilm на новые серии."""
    from services import series
    time.sleep(120)  # после старта сначала даём подняться сети
    while True:
        try:
            n = series.check_new(bot)
            if n:
                logger.info(f'Series watcher: started {n} new episode(s)')
        except Exception as e:
            logger.error(f'Series watcher error: {e}')
        time.sleep(max(5, config.SERIES_CHECK_MINUTES) * 60)


def _resume_torrents(bot) -> None:
    """Продолжить следить за торрентами, которые качались до перезапуска/перезагрузки."""
    time.sleep(10)  # даём сети и Transmission подняться после загрузки сервера
    try:
        from services import transmission as tr
        n = tr.resume_monitoring(bot)
        if n:
            logger.info(f'Resumed monitoring for {n} torrent(s)')
    except Exception as e:
        logger.error(f'Could not resume torrent monitoring: {e}', exc_info=True)


def _backup_loop(bot) -> None:
    """Раз в неделю сохраняет config.json + data/ в папку бэкапов на сервере.
    Проверяет раз в 6 часов."""
    from services.backup import save_backup
    while True:
        try:
            last = 0.0
            if _BACKUP_STAMP.exists():
                last = float(_BACKUP_STAMP.read_text().strip() or 0)
            if time.time() - last >= _BACKUP_INTERVAL:
                path = save_backup()
                _BACKUP_STAMP.write_text(str(time.time()))
                logger.info(f'Weekly backup saved: {path}')
        except Exception as e:
            logger.error(f'Backup error: {e}', exc_info=True)
        time.sleep(6 * 3600)
