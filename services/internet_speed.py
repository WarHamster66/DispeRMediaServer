"""Internet speed test via Cloudflare (speed.cloudflare.com).

More reliable than speedtest-cli and reachable in most regions. The test runs
over the direct connection (home internet), NOT through the proxy — that's the
whole point of measuring the real link speed.
"""
import logging
import time

import requests

logger = logging.getLogger(__name__)

_DOWN = 'https://speed.cloudflare.com/__down'
_UP = 'https://speed.cloudflare.com/__up'

_DOWN_BYTES = 20_000_000   # ~20 MB на скачивание
_UP_BYTES = 5_000_000      # ~5 MB на отдачу


def get_internet_speed() -> str:
    try:
        # ── Download ──
        t0 = time.time()
        r = requests.get(_DOWN, params={'bytes': _DOWN_BYTES}, timeout=30, stream=True)
        r.raise_for_status()
        received = 0
        for chunk in r.iter_content(65536):
            received += len(chunk)
        dt = time.time() - t0
        dl = (received * 8) / dt / 1_000_000 if dt > 0 else 0

        # ── Upload ──
        t1 = time.time()
        requests.post(_UP, data=b'\0' * _UP_BYTES, timeout=30)
        ut = time.time() - t1
        ul = (_UP_BYTES * 8) / ut / 1_000_000 if ut > 0 else 0

        return f"📈 Скорость: ▼ {dl:.1f} Mbps  ▲ {ul:.1f} Mbps"
    except Exception as e:
        logger.error(f"Speedtest error: {e}")
        return f"⚠️ Speedtest недоступен: {e}"
