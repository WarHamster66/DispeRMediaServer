"""RuTracker: поиск раздач и скачивание .torrent.

  • сайт закрыт проверкой Cloudflare. Раз в долгое время её проходит
    FlareSolverr (настоящий Chrome в Docker), а бот потом ходит обычными
    быстрыми запросами с выданным пропуском (cookie cf_clearance + тот же
    User-Agent). Всё это — в data/rutracker_session.json;
  • поиск и скачивание — только после входа: логин и пароль из .env
    (RUTRACKER_USER / RUTRACKER_PASSWORD), капчу (если сайт попросит) бот
    присылает картинкой — команда /rutracker;
  • в РФ сайт заблокирован — запросы идут через PROXY_URL.

Страницы сайта в windows-1251, и запросы туда — тоже в windows-1251.
"""
import html as htmllib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

import requests

from core import config
from services import flaresolverr

logger = logging.getLogger(__name__)

_ENC = 'cp1251'
_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')
_TIMEOUT = 40
_SESSION_FILE = Path(config.HISTORY_FILE).parent / 'rutracker_session.json'
_LOGIN_TTL = 600
_LOGGED_IN = 'id="logged-in-username"'

_lock = threading.Lock()
_solve_lock = threading.Lock()
_state: dict = {}   # {ua, cookies: [{name, value, domain, path}], name, since, solved_at, sort_seeders}
_login: dict = {}   # начатый вход: {cap_sid, cap_field, ts}


class RuTrackerError(Exception):
    pass


class NeedLogin(RuTrackerError):
    """Нет входа или сессия истекла — нужен /rutracker."""


class BadCaptcha(RuTrackerError):
    pass


class BadCredentials(RuTrackerError):
    pass


# ── state ─────────────────────────────────────────────────────────────────────

def _load() -> None:
    try:
        _state.update(json.loads(_SESSION_FILE.read_text(encoding='utf-8')))
    except Exception:
        pass


def _persist() -> None:
    tmp = _SESSION_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(_state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)  # куки входа — почти как пароль
    tmp.replace(_SESSION_FILE)


_load()


def is_configured() -> bool:
    return bool(config.RUTRACKER_USER and config.RUTRACKER_PASSWORD)


def logged_in_as() -> str | None:
    with _lock:
        has_session = any(c['name'] == 'bb_session' for c in _state.get('cookies', []))
        return (_state.get('name') or config.RUTRACKER_USER or '?') if has_session else None


def forget_session() -> None:
    """Забыть вход (пропуск Cloudflare оставляем — он к аккаунту не привязан)."""
    with _lock:
        _state['cookies'] = [c for c in _state.get('cookies', []) if c['name'] != 'bb_session']
        _state.pop('name', None)
        _persist()


# ── http ──────────────────────────────────────────────────────────────────────

def _base() -> str:
    url = config.RUTRACKER_URL.strip() or 'https://rutracker.org/'
    return url if url.endswith('/') else url + '/'


def _session() -> requests.Session:
    s = requests.Session()
    with _lock:
        s.headers.update({'User-Agent': _state.get('ua') or _UA,
                          'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8'})
        for c in _state.get('cookies', []):
            s.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))
    if config.PROXY_URL:
        s.proxies = {'http': config.PROXY_URL, 'https': config.PROXY_URL}
    return s


def _remember_cookies(s: requests.Session) -> None:
    with _lock:
        known = {(c['name'], c.get('domain', '')): c for c in _state.get('cookies', [])}
        for c in s.cookies:
            known[(c.name, c.domain)] = {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
        _state['cookies'] = list(known.values())
        _persist()


def _is_challenge(r: requests.Response) -> bool:
    return (r.status_code in (403, 503)
            and (r.headers.get('cf-mitigated') == 'challenge' or b'Just a moment' in r.content[:3000]))


def _solve_challenge(url: str) -> None:
    """Пройти проверку браузером на той самой странице, которую она закрыла
    (главную Cloudflare не проверяет — там пропуск не выдают)."""
    with _solve_lock:
        if time.time() - _state.get('solved_at', 0) < 60:
            return  # другой поток только что прошёл проверку
        sol = flaresolverr.solve(url)
        if not any(c['name'] == 'cf_clearance' for c in sol['cookies']):
            logger.warning('RuTracker: FlareSolverr не получил пропуск cf_clearance')
        with _lock:
            fresh = {c['name'] for c in sol['cookies']}
            cookies = [c for c in _state.get('cookies', []) if c['name'] not in fresh]
            cookies += [{'name': c['name'], 'value': c['value'], 'domain': c.get('domain', ''),
                         'path': c.get('path', '/')} for c in sol['cookies']]
            _state.update(cookies=cookies, ua=sol['user_agent'] or _state.get('ua') or _UA,
                          solved_at=time.time())
            _persist()
        logger.info('RuTracker: проверка Cloudflare пройдена (FlareSolverr)')


def _request(method: str, path: str, **kw) -> requests.Response:
    """Запрос к сайту: при проверке Cloudflare — пройти её и повторить."""
    url = urljoin(_base(), path)
    kw.setdefault('timeout', _TIMEOUT)
    solved = False
    last = ''
    for attempt in range(3):
        s = _session()
        try:
            r = s.request(method, url, **kw)
        except requests.RequestException as e:
            last = e.__class__.__name__
            time.sleep(3)
            continue
        if _is_challenge(r):
            if solved:
                raise RuTrackerError('Cloudflare не пускает даже после проверки браузером')
            try:
                # браузер умеет только GET — для отправки формы проходим проверку на её странице
                _solve_challenge(url if method == 'GET' else urljoin(_base(), 'forum/login.php'))
            except flaresolverr.FlareSolverrError as e:
                raise RuTrackerError(str(e)) from None
            solved = True
            continue
        if r.status_code >= 500:
            last = f'HTTP {r.status_code}'
            time.sleep(3)
            continue
        _remember_cookies(s)
        return r
    raise RuTrackerError(f'RuTracker не отвечает ({last or "?"})')


def _page(r: requests.Response) -> str:
    return r.content.decode(_ENC, errors='replace')


def _find(pattern: str, text: str) -> str:
    m = re.search(pattern, text or '', re.S | re.I)
    return m.group(1) if m else ''


def _text(fragment: str) -> str:
    return ' '.join(htmllib.unescape(re.sub(r'<[^>]+>', ' ', fragment or '')).split())


def _input_value(page: str, name: str) -> str:
    tag = _find(rf'(<input[^>]*name="{re.escape(name)}"[^>]*>)', page)
    return htmllib.unescape(_find(r'value="([^"]*)"', tag))


# ── login ─────────────────────────────────────────────────────────────────────

def _captcha_url(page: str) -> str:
    return _find(r'<img[^>]+src="([^"]*/captcha/[^"]+)"', page)


def start_login() -> bytes | None:
    """Шаг 1: открыть страницу входа. Вернёт картинку капчи или None (капча не нужна)."""
    if not is_configured():
        raise RuTrackerError('в .env нет RUTRACKER_USER / RUTRACKER_PASSWORD')
    page = _page(_request('GET', 'forum/login.php'))
    img = _captcha_url(page)
    pending = {'ts': time.time()}
    image = None
    if img:
        pending.update(cap_sid=_input_value(page, 'cap_sid'),
                       cap_field=_find(r'name="(cap_code_[^"]+)"', page))
        image = _request('GET', urljoin(_base() + 'forum/', htmllib.unescape(img))).content
    with _lock:
        _login.clear()
        _login.update(pending)
    return image


def finish_login(code: str = '') -> str:
    """Шаг 2: войти (с кодом капчи, если она была). Вернёт имя на сайте."""
    with _lock:
        pending = dict(_login)
        _login.clear()  # капча одноразовая
    if not pending or time.time() - pending['ts'] > _LOGIN_TTL:
        raise RuTrackerError('вход устарел — начни заново: /rutracker')
    form = {'login_username': config.RUTRACKER_USER, 'login_password': config.RUTRACKER_PASSWORD,
            'login': 'вход'}
    if pending.get('cap_sid') and pending.get('cap_field'):
        form.update({'cap_sid': pending['cap_sid'], pending['cap_field']: code.strip()})
    r = _request('POST', 'forum/login.php', data=urlencode(form, encoding=_ENC),
                 headers={'Content-Type': 'application/x-www-form-urlencoded',
                          'Referer': _base() + 'forum/login.php'})
    page = _page(r)
    if _LOGGED_IN in page:
        name = _text(_find(r'id="logged-in-username"[^>]*>(.*?)</a>', page)) or config.RUTRACKER_USER
        with _lock:
            _state.update(name=name, since=datetime.now().isoformat(timespec='seconds'))
            _persist()
        logger.info(f'RuTracker: вход выполнен ({name})')
        return name

    message = (_text(_find(r'<h4[^>]*warnColor1[^>]*>(.*?)</h4>', page))
               or _text(_find(r'<div[^>]*class="msg-main"[^>]*>(.*?)</div>', page)))
    low = message.lower()
    if 'парол' in low or 'имя пользовател' in low:
        raise BadCredentials(message or 'неверный логин или пароль')
    if _captcha_url(page):
        raise BadCaptcha(message or ('неверный код с картинки' if pending.get('cap_sid')
                                     else 'RuTracker просит ввести код с картинки'))
    raise RuTrackerError(message or 'RuTracker не пустил (неизвестная причина)')


# ── search ────────────────────────────────────────────────────────────────────

def search(query: str) -> list[dict]:
    """Раздачи по запросу, сначала — где больше раздающих (до 50 шт.)."""
    if not logged_in_as():
        raise NeedLogin('нужно войти на RuTracker')
    words = ' '.join(re.sub(r'[^0-9A-Za-zА-Яа-яЁё]+', ' ', query).split())
    if not words:
        return []
    sort = _state.get('sort_seeders', '10')
    page = _page(_request('GET', f"forum/tracker.php?nm={quote(words, encoding=_ENC)}&o={sort}&s=2"))
    if _LOGGED_IN not in page:
        forget_session()
        raise NeedLogin('сессия RuTracker истекла')
    _check_sort_param(page, sort)
    results = _parse_results(page)
    if not results and 'tor-tbl' in page and 'tCenter' in page and 'Не найдено' not in page:
        logger.warning(f'RuTracker: таблица есть, но разобрать не вышло: {_text(page)[:300]!r}')
    results.sort(key=lambda x: (x['seeders'], x['grabs']), reverse=True)
    return results


def _check_sort_param(page: str, used: str) -> None:
    """Номер сортировки «по сидам» берём из формы на самой странице поиска."""
    value = _find(r'<option[^>]*value="(\d+)"[^>]*>\s*Сиды', page)
    if value and value != used:
        logger.info(f'RuTracker: сортировка по сидам — o={value}')
        with _lock:
            _state['sort_seeders'] = value
            _persist()


def _int(text: str) -> int:
    digits = re.sub(r'\D', '', text or '')
    return int(digits) if digits else 0


def _parse_results(page: str) -> list[dict]:
    body = _find(r'id="tor-tbl".*?<tbody[^>]*>(.*?)</tbody>', page)
    out = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body, re.S):
        topic = _find(r'viewtopic\.php\?t=(\d+)', row)
        if not topic or 'tr-dl' not in row:
            continue  # пустая строка или раздача ещё на проверке — скачать нельзя
        size_td = _find(r'(<td[^>]*tor-size[^>]*>)', row)
        stamps = re.findall(r'<td[^>]*data-ts_text="(\d{9,11})"', row)
        out.append({
            'id': topic,
            'title': _text(_find(r'<a[^>]*\btLink\b[^>]*>(.*?)</a>', row)),
            'forum': _text(_find(r'f-name[^>]*>\s*<a[^>]*>(.*?)</a>', row)),
            'size': _int(_find(r'data-ts_text="(\d+)"', size_td)),
            'seeders': _int(_find(r'class="seedmed"[^>]*>(.*?)<', row)),
            'leechers': _int(_find(r'leechmed[^>]*>(?:\s*<b>)?\s*(\d+)', row)),
            'grabs': _int(_find(r'number-format[^>]*>([^<]*)<', row)),
            'date': int(stamps[-1]) if stamps else 0,
        })
    return out


# ── download ──────────────────────────────────────────────────────────────────

def download(topic_id: str) -> bytes:
    """.torrent раздачи (нужен вход)."""
    if not logged_in_as():
        raise NeedLogin('нужно войти на RuTracker')
    r = _request('GET', f'forum/dl.php?t={topic_id}',
                 headers={'Referer': f'{_base()}forum/viewtopic.php?t={topic_id}'})
    if r.content.startswith(b'd'):
        return r.content
    if _LOGGED_IN not in _page(r) and 'login' in r.url:
        forget_session()
        raise NeedLogin('сессия RuTracker истекла')
    raise RuTrackerError('вместо .torrent-файла пришло что-то другое')


def magnet(topic_id: str) -> str:
    """Magnet-ссылка со страницы раздачи — запасной путь, если .torrent не скачался."""
    link = _find(r'href="(magnet:\?xt=urn:btih:[^"]+)"', _page(_request('GET', f'forum/viewtopic.php?t={topic_id}')))
    if not link:
        raise RuTrackerError('на странице раздачи нет magnet-ссылки')
    return htmllib.unescape(link)


# ── title → качество ──────────────────────────────────────────────────────────

_SOURCES = (  # от лучшего к худшему; первое совпадение
    (r'remux', 'Remux'), (r'blu-?ray\s*(?:disc|cee)?\b(?!rip)', 'Blu-ray'),
    (r'bd-?rip', 'BDRip'), (r'web-?dl(?!rip)', 'WEB-DL'), (r'web-?dlrip', 'WEB-DLRip'),
    (r'web-?rip', 'WEBRip'), (r'hdrip', 'HDRip'), (r'hdtv-?rip', 'HDTVRip'), (r'hdtv', 'HDTV'),
    (r'dvd-?rip', 'DVDRip'), (r'dvd\s*-?\s*[59]\b', 'DVD'), (r'sat-?rip', 'SATRip'), (r'tv-?rip', 'TVRip'),
    (r'\b(?:cam-?rip|ts|telesync|tc)\b', 'экранка'),
)
_AUDIO = ((r'\bdub\b|дубляж', 'дубляж'), (r'\bmvo\b|многоголос', 'многоголосый'),
          (r'\bdvo\b|двухголос', 'двухголосый'), (r'\bavo\b|авторск', 'авторский'),
          (r'\bvo\b|одноголос', 'одноголосый'))


def describe(title: str) -> dict:
    """Разобрать название раздачи: имя, год, разрешение, источник, HDR, перевод, сезон."""
    t = title or ''
    name = re.split(r'\s+[\(\[]', t, maxsplit=1)[0]
    # «Бункер / Silo / Сезон: 2 / Серии: 1-10 из 10» — сезон покажем отдельной строкой
    name = re.split(r'\s*/\s*(?:сезон|серии|выпуски)\b', name, maxsplit=1, flags=re.I)[0].strip()
    tags = ' '.join(re.findall(r'\[([^\]]*)\]', t))
    year = _find(r'\b((?:19|20)\d{2})\b', tags) or _find(r'\b((?:19|20)\d{2})\b', t)
    low = t.lower()
    if re.search(r'2160p|\b4k\b|\buhd\b', low):
        res = '4K'
    elif re.search(r'1080[pi]', low):
        res = '1080p'
    elif '720p' in low:
        res = '720p'
    else:
        res = ''
    source = next((label for pattern, label in _SOURCES if re.search(pattern, low)), '')
    if not res and source in ('DVDRip', 'DVD', 'SATRip', 'TVRip', 'экранка'):
        res = 'SD'
    hdr = ('Dolby Vision' if re.search(r'dolby\s*vision|\bdv\b', low)
           else 'HDR' if re.search(r'\bhdr(?:10\+?)?\b', low) else '')  # HDRip — не HDR
    audio = next((label for pattern, label in _AUDIO if re.search(pattern, low)), '')
    season = _find(r'сезон[:\s]*(\d+(?:\s*-\s*\d+)?)', t)
    episodes = _find(r'серии[:\s]*(\d+(?:\s*-\s*\d+)?(?:\s*из\s*\d+)?)', t)
    return {'name': name or t, 'year': year, 'res': res, 'source': source, 'hdr': hdr,
            'audio': audio, 'season': season, 'episodes': episodes}


def speed(seeders: int) -> str:
    """Насколько быстро скачается — по числу раздающих."""
    if seeders >= 100:
        return '⚡⚡⚡ очень быстро'
    if seeders >= 20:
        return '⚡⚡ быстро'
    if seeders >= 5:
        return '⚡ нормально'
    if seeders >= 1:
        return '🐢 медленно'
    return '⛔ нет раздающих'
