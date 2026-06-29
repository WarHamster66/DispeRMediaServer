"""Weather via Open-Meteo — free, no API key required.

City name is resolved to coordinates via Open-Meteo's geocoding API, then the
forecast is fetched. Set the city in config.json (WEATHER_CITY).
"""
import logging

import requests

from core import config

logger = logging.getLogger(__name__)

_GEO = 'https://geocoding-api.open-meteo.com/v1/search'
_FORECAST = 'https://api.open-meteo.com/v1/forecast'

# WMO weather codes → краткое описание
_WMO = {
    0: 'ясно', 1: 'преим. ясно', 2: 'переменная облачность', 3: 'пасмурно',
    45: 'туман', 48: 'изморозь',
    51: 'морось', 53: 'морось', 55: 'сильная морось',
    56: 'ледяная морось', 57: 'ледяная морось',
    61: 'небольшой дождь', 63: 'дождь', 65: 'сильный дождь',
    66: 'ледяной дождь', 67: 'ледяной дождь',
    71: 'небольшой снег', 73: 'снег', 75: 'сильный снег', 77: 'снежная крупа',
    80: 'ливень', 81: 'ливень', 82: 'сильный ливень',
    85: 'снегопад', 86: 'сильный снегопад',
    95: 'гроза', 96: 'гроза с градом', 99: 'сильная гроза с градом',
}

_coords: tuple | None = None  # кэш: (lat, lon, name)


def _get_coords():
    global _coords
    if _coords:
        return _coords
    r = requests.get(
        _GEO,
        params={'name': config.WEATHER_CITY, 'count': 1, 'language': 'ru', 'format': 'json'},
        timeout=10,
    ).json()
    results = r.get('results')
    if not results:
        return None
    c = results[0]
    _coords = (c['latitude'], c['longitude'], c.get('name', config.WEATHER_CITY))
    return _coords


def get_weather() -> str:
    try:
        coords = _get_coords()
        if not coords:
            return f"🌦️ Город не найден: {config.WEATHER_CITY} (проверь WEATHER_CITY в config.json)"
        lat, lon, name = coords

        d = requests.get(
            _FORECAST,
            params={
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,weather_code',
                'daily': 'temperature_2m_max,temperature_2m_min,weather_code',
                'timezone': 'auto',
                'forecast_days': 1,
            },
            timeout=10,
        ).json()

        cur = d['current']
        temp_now = cur['temperature_2m']
        desc_now = _WMO.get(cur['weather_code'], '')

        daily = d['daily']
        t_max = daily['temperature_2m_max'][0]
        t_min = daily['temperature_2m_min'][0]
        desc_day = _WMO.get(daily['weather_code'][0], '')

        return (
            f"📍 {name}\n"
            f"🌡️ Сейчас: {temp_now:.0f}°C, {desc_now}\n"
            f"🌞 Днём: до {t_max:.0f}°C, {desc_day}\n"
            f"🌙 Ночью: {t_min:.0f}°C"
        )
    except Exception as e:
        logger.error(f"Weather error: {e}")
        return f"⚠️ Погода недоступна: {e}"
