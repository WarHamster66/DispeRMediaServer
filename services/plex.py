"""Plex integration — trigger a library scan after downloads finish.

Plex allows unauthenticated API access from localhost by default, so a token is
usually not required. If your setup needs one, set PLEX_TOKEN in config.json.
"""
import logging

import requests

from core import config

logger = logging.getLogger(__name__)


def _params() -> dict:
    return {'X-Plex-Token': config.PLEX_TOKEN} if config.PLEX_TOKEN else {}


def refresh_libraries() -> int:
    """Trigger a scan of all Plex libraries. Returns how many were refreshed."""
    base = config.PLEX_URL.rstrip('/')
    headers = {'Accept': 'application/json'}
    r = requests.get(f'{base}/library/sections', params=_params(), headers=headers, timeout=10)
    r.raise_for_status()
    sections = r.json().get('MediaContainer', {}).get('Directory', [])

    count = 0
    for sec in sections:
        key = sec.get('key')
        if not key:
            continue
        try:
            requests.get(f'{base}/library/sections/{key}/refresh', params=_params(), timeout=10)
            count += 1
        except Exception as e:
            logger.warning(f"Plex refresh failed for section {key}: {e}")
    logger.info(f"Plex: refresh triggered for {count} libraries")
    return count


def refresh_libraries_safe() -> None:
    """Same as refresh_libraries but never raises (for use in completion hooks)."""
    try:
        refresh_libraries()
    except Exception as e:
        logger.warning(f"Plex refresh skipped: {e}")
