"""Persistent download history stored in a JSON file."""
import json
import logging
import os
from datetime import datetime

from core import config

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 500


def record_download(name: str, size_bytes: int, download_dir: str) -> None:
    history = _load()
    history.append({
        'name': name,
        'size': size_bytes,
        'dir': download_dir,
        'completed_at': datetime.now().isoformat(),
    })
    _save(history[-_MAX_ENTRIES:])


def get_history(limit: int = 20) -> list[dict]:
    return _load()[-limit:]


def format_history(limit: int = 20) -> str:
    entries = get_history(limit)
    if not entries:
        return "История загрузок пуста."
    lines = []
    for e in reversed(entries):
        dt = datetime.fromisoformat(e['completed_at']).strftime('%d.%m %H:%M')
        size = _fmt(e['size'])
        lines.append(f"• {dt}  {size}  {e['name']}")
    return '\n'.join(lines)


def _load() -> list[dict]:
    if not os.path.exists(config.HISTORY_FILE):
        return []
    try:
        with open(config.HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save(data: list[dict]) -> None:
    try:
        with open(config.HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Could not save download history: {e}")


def _fmt(b: int) -> str:
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.0f} MB"
    return f"{b / 1024:.0f} KB"
