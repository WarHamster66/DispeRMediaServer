"""Internet speed test.

Two backends, tried in order:
  1. Official Ookla CLI (`speedtest`) if installed — measures both download AND
     upload via nearby servers (works in regions where direct uploads to global
     CDNs get reset). Install: https://www.speedtest.net/apps/cli
  2. Cloudflare (speed.cloudflare.com) fallback — no install needed, but upload
     is best-effort (often blocked by DPI) so usually download-only.

The test always runs over the direct connection (home internet), never the proxy.
"""
import json
import logging
import shutil
import subprocess
import time

import requests

logger = logging.getLogger(__name__)

_DOWN = 'https://speed.cloudflare.com/__down'
_UP = 'https://speed.cloudflare.com/__up'
_DOWN_BYTES = 20_000_000   # ~20 MB
_UP_BYTES = 5_000_000      # ~5 MB


def get_internet_speed() -> str:
    return _ookla() or _cloudflare()


def _ookla() -> str | None:
    """Полный замер через официальный Ookla CLI, если он установлен."""
    exe = shutil.which('speedtest')
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, '-f', 'json', '--accept-license', '--accept-gdpr'],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0 or not r.stdout.strip():
            logger.warning(f"Ookla speedtest failed: {r.stderr.strip()[:200]}")
            return None
        d = json.loads(r.stdout)
        dl = d['download']['bandwidth'] * 8 / 1_000_000  # bandwidth в байт/с
        ul = d['upload']['bandwidth'] * 8 / 1_000_000
        ping = d.get('ping', {}).get('latency')
        tail = f"  📶 {ping:.0f} ms" if ping else ""
        return f"📈 Скорость: ▼ {dl:.1f} Mbps  ▲ {ul:.1f} Mbps{tail}"
    except Exception as e:
        logger.warning(f"Ookla speedtest error: {e}")
        return None


def _cloudflare() -> str:
    """Запасной замер через Cloudflare. Скачивание надёжно, отдача — как получится."""
    try:
        t0 = time.time()
        r = requests.get(_DOWN, params={'bytes': _DOWN_BYTES}, timeout=30, stream=True)
        r.raise_for_status()
        received = sum(len(chunk) for chunk in r.iter_content(65536))
        dt = time.time() - t0
        dl = (received * 8) / dt / 1_000_000 if dt > 0 else 0
    except Exception as e:
        logger.error(f"Speedtest download error: {e}")
        return f"⚠️ Speedtest недоступен: {e}"

    try:
        t1 = time.time()
        requests.post(_UP, data=b'\0' * _UP_BYTES, timeout=30)
        ut = time.time() - t1
        ul = (_UP_BYTES * 8) / ut / 1_000_000 if ut > 0 else 0
        return f"📈 Скорость: ▼ {dl:.1f} Mbps  ▲ {ul:.1f} Mbps"
    except Exception as e:
        logger.warning(f"Speedtest upload skipped: {e}")
        return f"📈 Скорость: ▼ {dl:.1f} Mbps  (отдача недоступна)"
