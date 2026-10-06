"""FlareSolverr — настоящий Chrome в Docker, который проходит проверку Cloudflare.

Бот зовёт его редко: только когда сайт снова показал «Just a moment…».
Пропуск (cookie cf_clearance) живёт долго, и дальше бот ходит обычными
быстрыми запросами с тем же User-Agent. Ставит: sudo python3 setup_rutracker.py
"""
import logging

import requests

from core import config
from services import socks_bridge

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.trust_env = False  # FlareSolverr на localhost — не через системный прокси


class FlareSolverrError(Exception):
    pass


def solve(url: str, timeout_ms: int = 60000) -> dict:
    """Открыть страницу в браузере. Вернёт {'cookies': [...], 'user_agent': str}."""
    payload = {'cmd': 'request.get', 'url': url, 'maxTimeout': timeout_ms}
    proxy = socks_bridge.browser_proxy()
    if proxy:
        payload['proxy'] = proxy
    try:
        r = _session.post(config.FLARESOLVERR_URL.rstrip('/') + '/v1', json=payload,
                          timeout=timeout_ms / 1000 + 30)
    except requests.ConnectionError:
        raise FlareSolverrError('FlareSolverr не запущен — на сервере: '
                                'sudo python3 setup_rutracker.py') from None
    except requests.RequestException as e:
        raise FlareSolverrError(f'FlareSolverr не ответил: {e.__class__.__name__}') from None
    try:
        data = r.json()
    except ValueError:
        raise FlareSolverrError(f'FlareSolverr ответил не JSON (HTTP {r.status_code})') from None
    if data.get('status') != 'ok':
        raise FlareSolverrError(f"FlareSolverr: {str(data.get('message'))[:200]}")
    solution = data.get('solution') or {}
    return {'cookies': solution.get('cookies') or [], 'user_agent': solution.get('userAgent') or ''}
