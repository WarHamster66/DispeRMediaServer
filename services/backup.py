"""Backup of config.json and the data/ folder into a local folder on the server.

Used by the weekly scheduler task and the /backup command so settings and
history can be restored after a reinstall. Backups land in config.BACKUP_DIR
(default ~/media-server-backups); only the latest _KEEP archives are kept.
"""
import logging
import zipfile
from datetime import datetime
from pathlib import Path

from core import config

logger = logging.getLogger(__name__)

_KEEP = 8  # сколько последних бэкапов хранить


def save_backup() -> Path:
    """Создать zip с config.json и data/ в папке бэкапов. Вернуть путь к файлу."""
    backup_dir = Path(config.BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    path = backup_dir / f"backup_{datetime.now():%Y%m%d_%H%M}.zip"
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        cfg = config.BASE_DIR / 'config.json'
        if cfg.exists():
            z.write(cfg, 'config.json')
        data_dir = config.BASE_DIR / 'data'
        if data_dir.is_dir():
            for f in data_dir.rglob('*'):
                if f.is_file():
                    z.write(f, str(f.relative_to(config.BASE_DIR)))

    _rotate(backup_dir)
    logger.info(f"Backup saved: {path}")
    return path


def _rotate(backup_dir: Path) -> None:
    """Удалить старые бэкапы, оставив только _KEEP последних."""
    backups = sorted(backup_dir.glob('backup_*.zip'))
    for old in backups[:-_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
