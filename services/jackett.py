"""Поиск по торрент-трекерам через Jackett (https://github.com/Jackett/Jackett).

Jackett сам логинится на трекеры (RuTracker, Kinozal, RuTor, NNM-Club, LostFilm…),
ходит к ним через прокси и отдаёт единый JSON. Бот обращается к нему только по
локальному API, поэтому логины от трекеров хранятся в Jackett, а не в боте.
"""
import logging
from datetime import datetime

import requests

from core import config

logger = logging.getLogger(__name__)

# Jackett локальный — системные/переменные прокси не применяем
_session = requests.Session()
_session.trust_env = False


class JackettError(Exception):
    pass


def is_configured() -> bool:
    return bool(config.JACKETT_API_KEY)


def search(query: str, indexer: str = 'all', timeout: int = 90) -> tuple[list[dict], list[dict]]:
    """Искать раздачи. Возвращает (результаты, статусы индексаторов).

    Пустой query у конкретного индексатора = лента свежих релизов.
    """
    if not is_configured():
        raise JackettError('Jackett не настроен: нет JACKETT_API_KEY в .env')
    url = f"{config.JACKETT_URL.rstrip('/')}/api/v2.0/indexers/{indexer}/results"
    try:
        r = _session.get(url, params={'apikey': config.JACKETT_API_KEY, 'Query': query},
                         timeout=timeout)
    except requests.RequestException as e:
        raise JackettError(f'Jackett недоступен: {e}') from e
    if r.status_code in (401, 403):
        raise JackettError('Jackett отклонил API-ключ — проверь JACKETT_API_KEY')
    if r.status_code != 200:
        raise JackettError(f'Jackett ответил {r.status_code}')

    data = r.json()
    results = [_normalize(x) for x in data.get('Results', [])]
    indexers = [
        {'name': i.get('Name') or i.get('ID'),
         'ok': i.get('Status') == 2,
         'count': i.get('Results', 0),
         'error': i.get('Error')}
        for i in data.get('Indexers', [])
    ]
    return results, indexers


def _normalize(x: dict) -> dict:
    seeders = x.get('Seeders') or 0
    peers = x.get('Peers') or 0
    date = ''
    raw_date = x.get('PublishDate') or ''
    try:
        date = datetime.fromisoformat(raw_date.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except ValueError:
        date = raw_date[:10]
    return {
        'title': x.get('Title') or '',
        'tracker': x.get('Tracker') or x.get('TrackerId') or '?',
        'tracker_id': x.get('TrackerId') or '',
        'category': x.get('CategoryDesc') or '',
        'size': x.get('Size') or 0,
        'grabs': x.get('Grabs') or 0,
        'seeders': seeders,
        'leechers': max(0, peers - seeders),
        'date': date,
        'link': x.get('Link'),
        'magnet': x.get('MagnetUri'),
        'details': x.get('Details'),
    }


def fetch(result: dict) -> tuple[str, bytes | str]:
    """Получить раздачу: ('torrent', bytes) или ('magnet', 'magnet:?…')."""
    link = result.get('link')
    if link:
        try:
            # Jackett проксирует скачивание .torrent с трекера (с авторизацией).
            # Для некоторых трекеров он отвечает редиректом на magnet.
            r = _session.get(link, timeout=60, allow_redirects=False)
            location = r.headers.get('Location', '')
            if r.is_redirect and location.startswith('magnet:'):
                return 'magnet', location
            if r.is_redirect and location:
                r = _session.get(location, timeout=60)
            if r.status_code == 200 and r.content[:1] == b'd':  # bencode-словарь
                return 'torrent', r.content
            logger.warning(f'Unexpected download response {r.status_code} for {result["title"][:60]}')
        except requests.RequestException as e:
            logger.warning(f'Torrent download via Jackett failed: {e}')
    if result.get('magnet'):
        return 'magnet', result['magnet']
    raise JackettError('Не удалось получить торрент с трекера')
