"""Network traffic tracking with proper restart handling."""
import json
import logging
import os
from datetime import datetime, timedelta

import psutil

from core import config

logger = logging.getLogger(__name__)

# Runtime state — both fields must be initialised from the saved file on startup
# so the first diff after restart is correct.
_state: dict = {
    'last_bytes_recv': 0,
    'last_bytes_sent': 0,
    'total_bytes_recv': 0,
    'total_bytes_sent': 0,
}

_MAX_HISTORY = 720  # ~30 days at 1-hour saves


def _load_initial_state() -> None:
    """Restore totals AND last counters from the most recent saved entry."""
    if not os.path.exists(config.NETWORK_FILE):
        return
    try:
        with open(config.NETWORK_FILE, 'r') as f:
            history = json.load(f)
        if not history:
            return
        last = history[-1]
        # Seed last_* with the OS counters as they stand right now so the first
        # save_network_data() call produces a zero-diff instead of adding boot traffic.
        net = psutil.net_io_counters()
        _state['last_bytes_recv'] = net.bytes_recv
        _state['last_bytes_sent'] = net.bytes_sent
        _state['total_bytes_recv'] = last['bytes_recv']
        _state['total_bytes_sent'] = last['bytes_sent']
    except Exception as e:
        logger.warning(f"Could not load network history: {e}")


_load_initial_state()


def save_network_data() -> None:
    try:
        net = psutil.net_io_counters()

        diff_recv = net.bytes_recv - _state['last_bytes_recv']
        diff_sent = net.bytes_sent - _state['last_bytes_sent']

        # Handle counter reset (reboot)
        if diff_recv < 0:
            diff_recv = net.bytes_recv
        if diff_sent < 0:
            diff_sent = net.bytes_sent

        _state['total_bytes_recv'] += diff_recv
        _state['total_bytes_sent'] += diff_sent
        _state['last_bytes_recv'] = net.bytes_recv
        _state['last_bytes_sent'] = net.bytes_sent

        entry = {
            'timestamp': datetime.now().isoformat(),
            'bytes_recv': _state['total_bytes_recv'],
            'bytes_sent': _state['total_bytes_sent'],
        }

        history: list = []
        if os.path.exists(config.NETWORK_FILE):
            with open(config.NETWORK_FILE, 'r') as f:
                history = json.load(f)

        history.append(entry)
        with open(config.NETWORK_FILE, 'w') as f:
            json.dump(history[-_MAX_HISTORY:], f)

    except Exception as e:
        logger.error(f"Error saving network data: {e}")


def get_network_usage() -> str:
    try:
        if not os.path.exists(config.NETWORK_FILE):
            return "📶 Данные о трафике ещё не собраны"

        with open(config.NETWORK_FILE, 'r') as f:
            history = json.load(f)
        if not history:
            return "📶 Нет данных о трафике"

        current = history[-1]
        now = datetime.fromisoformat(current['timestamp'])

        day_entry = next(
            (e for e in reversed(history) if datetime.fromisoformat(e['timestamp']) <= now - timedelta(days=1)),
            None,
        )
        month_entry = next(
            (e for e in reversed(history) if datetime.fromisoformat(e['timestamp']) <= now - timedelta(days=30)),
            None,
        )

        def gb(v: int) -> str:
            return f"{v / 1024 ** 3:.2f} GB"

        recv_day = current['bytes_recv'] - (day_entry['bytes_recv'] if day_entry else 0)
        recv_month = current['bytes_recv'] - (month_entry['bytes_recv'] if month_entry else 0)

        return (
            f"📊 Трафик:\n"
            f"┌ Сессия: {gb(current['bytes_recv'])} ▼ / {gb(current['bytes_sent'])} ▲\n"
            f"├ 24 ч:   {gb(recv_day)} ▼\n"
            f"└ 30 д:   {gb(recv_month)} ▼"
        )
    except Exception as e:
        logger.error(f"Error reading network usage: {e}")
        return "⚠️ Ошибка при расчёте трафика"
