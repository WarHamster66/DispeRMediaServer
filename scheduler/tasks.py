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


def _send_scheduled_report(bot) -> None:
    from handlers.reports import build_report
    try:
        text = f'🕐 Ежедневный отчёт:\n\n{build_report()}'
        bot.send_message(config.CHAT_ID, text)
        logger.info('Daily report sent')
    except Exception as e:
        logger.error(f'Could not send daily report: {e}')


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
