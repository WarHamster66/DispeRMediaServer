"""LostFilm.tv напрямую: поиск сериалов, список серий, .torrent-файлы.

  • поиск и список серий — открытые страницы сайта, вход не нужен;
  • ссылки на раздачи — только после входа. Вход по e-mail и паролю из .env
    (LOSTFILM_EMAIL / LOSTFILM_PASSWORD) + капча: бот присылает картинку
    в Telegram (/lostfilm), ты отвечаешь кодом. Сессия живёт долго и хранится
    в data/lostfilm_session.json.

Если основной домен недоступен, берём первое рабочее зеркало. Раздачи,
закрытые для РФ, пробуем открыть ещё раз через PROXY_URL (если он задан).
"""
import html as htmllib
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import requests

from core import config

logger = logging.getLogger(__name__)

PACK = 999  # «серия» 999 у LostFilm — весь сезон целиком

MIRRORS = (
    'https://www.lostfilm.tv/',
    'https://www.lostfilm.today/',
    'https://www.lostfilmtv5.site/',
    'https://www.lostfilm.download/',
    'https://www.lostfilm.run/',
)
_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/140.0 Safari/537.36')
_TIMEOUT = 30
_SESSION_FILE = Path(config.HISTORY_FILE).parent / 'lostfilm_session.json'
_GEO_BLOCK = 'недоступен на территории'
_LOGIN_TTL = 600        # сколько живёт показанная капча
_SITE_TTL = 6 * 3600    # как часто перепроверять, какое зеркало работает

# PlayEpisode('733003010') → сериал 733, сезон 3, серия 10 (999 — весь сезон)
_PLAY_RE = re.compile(r"PlayEpisode\('(\d+?)(\d{3})(\d{3})'\)")
_SIZE_RE = re.compile(r'Размер:\s*([\d.,]+)\s*([ТГМК]Б|[TGMK]B)', re.I)
_UNITS = {'т': 1024 ** 4, 'г': 1024 ** 3, 'м': 1024 ** 2, 'к': 1024,
          't': 1024 ** 4, 'g': 1024 ** 3, 'm': 1024 ** 2, 'k': 1024}

_lock = threading.Lock()
_auth: dict = {}   # сессия после входа: {base, proxy, cookies, name, since}
_login: dict = {}  # начатый вход (показана капча): {session, base, proxy, ts}
_site: dict = {}   # рабочее зеркало для запросов без входа: {base, proxy, ts}


class LostFilmError(Exception):
    pass


class NeedLogin(LostFilmError):
    """Нет входа или сессия истекла — нужен /lostfilm."""


class BadCaptcha(LostFilmError):
    pass


class BadCredentials(LostFilmError):
    pass


# ── session storage ───────────────────────────────────────────────────────────

def _load_auth() -> None:
    try:
        data = json.loads(_SESSION_FILE.read_text(encoding='utf-8'))
        if data.get('base') and data.get('cookies'):
            _auth.update(data)
    except Exception:
        pass


def _save_auth(data: dict) -> None:
    tmp = _SESSION_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)  # куки входа — почти как пароль
    tmp.replace(_SESSION_FILE)


def _drop_auth(reason: str) -> None:
    with _lock:
        _auth.clear()
        try:
            _SESSION_FILE.unlink()
        except FileNotFoundError:
            pass
    logger.warning(f'LostFilm: {reason}')


_load_auth()


def is_configured() -> bool:
    return bool(config.LOSTFILM_EMAIL and config.LOSTFILM_PASSWORD)


def forget_session() -> None:
    """Забыть вход — например, при смене аккаунта."""
    if _auth:
        _drop_auth('вход сброшен')


def logged_in_as() -> str | None:
    """Имя на сайте, если вход выполнен."""
    with _lock:
        return (_auth.get('name') or config.LOSTFILM_EMAIL or '?') if _auth else None


# ── http ──────────────────────────────────────────────────────────────────────

def _session(proxy: bool = False, cookies: list | None = None) -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': _UA, 'Accept-Language': 'ru-RU,ru;q=0.9'})
    if proxy and config.PROXY_URL:
        s.proxies = {'http': config.PROXY_URL, 'https': config.PROXY_URL}
    for c in cookies or []:
        # с доменом: куки LostFilm не уходят на другие сайты (трекер, зеркала)
        s.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))
    return s


def _request(s: requests.Session, method: str, url: str, **kw) -> requests.Response:
    """Запрос с одной повторной попыткой: сеть до сайта бывает нестабильной."""
    kw.setdefault('timeout', _TIMEOUT)
    last = ''
    for attempt in range(2):
        if attempt:
            time.sleep(3)
        try:
            r = s.request(method, url, **kw)
        except requests.RequestException as e:
            last = e.__class__.__name__
            continue
        if r.status_code >= 500:
            last = f'HTTP {r.status_code}'
            continue
        return r
    raise LostFilmError(f'LostFilm не отвечает ({last})')


def _probe(base: str, proxy: bool) -> bool:
    try:
        r = _session(proxy).get(base, timeout=10)
        return r.status_code == 200 and 'lostfilm' in r.text.lower()
    except requests.RequestException:
        return False


def _pick_site(force: bool = False) -> tuple[str, bool]:
    """Рабочий адрес сайта: (base, через_прокси)."""
    with _lock:
        if not force and _site and time.time() - _site['ts'] < _SITE_TTL:
            return _site['base'], _site['proxy']
    custom = config.LOSTFILM_URL.strip()
    if custom and not custom.endswith('/'):
        custom += '/'
    bases = list(dict.fromkeys(([custom] if custom else []) + list(MIRRORS)))
    options = [(b, False) for b in bases]
    if config.PROXY_URL:
        options += [(b, True) for b in bases[:2]]
    for base, proxy in options:
        if _probe(base, proxy):
            with _lock:
                _site.update(base=base, proxy=proxy, ts=time.time())
            if (base, proxy) != (bases[0], False):
                logger.info(f'LostFilm: использую {base}{" через прокси" if proxy else ""}')
            return base, proxy
    raise LostFilmError('LostFilm недоступен: ни одно зеркало не ответило')


def _where() -> tuple[str, bool]:
    """После входа ходим туда же, где входили (куки привязаны к домену)."""
    with _lock:
        if _auth:
            return _auth['base'], _auth.get('proxy', False)
    return _pick_site()


def _find(pattern: str, text: str) -> str:
    m = re.search(pattern, text or '', re.S | re.I)
    return m.group(1) if m else ''


def _text(fragment: str) -> str:
    """HTML-кусок → чистый текст в одну строку."""
    return ' '.join(htmllib.unescape(re.sub(r'<[^>]+>', ' ', fragment or '')).split())


def _size(desc: str) -> int:
    m = _SIZE_RE.search(desc or '')
    if not m:
        return 0
    try:
        return int(float(m[1].replace(',', '.')) * _UNITS[m[2][0].lower()])
    except (ValueError, KeyError):
        return 0


# ── search / episodes (без входа) ─────────────────────────────────────────────

def search(query: str) -> list[dict]:
    """Сериалы по названию (рус. или англ.): [{id, title, title_ru, link}]."""
    base, proxy = _where()
    r = _request(_session(proxy), 'POST', base + 'ajaxik.php',
                 data={'act': 'common', 'type': 'search', 'val': query})
    try:
        j = r.json()
    except ValueError:
        raise LostFilmError('LostFilm ответил на поиск не JSON') from None
    data = j.get('data') if isinstance(j, dict) else None
    out = []
    for x in (data.get('series') or []) if isinstance(data, dict) else []:
        link = (x.get('link') or '').strip().rstrip('/')
        if not link.startswith('/series/'):
            continue  # фильмы и актёры не нужны
        out.append({'id': str(x.get('id') or ''),
                    'title': (x.get('title_orig') or x.get('title') or '').strip(),
                    'title_ru': (x.get('title') or '').strip(),
                    'link': link})
    return out


def series_info(link: str) -> dict:
    """Вышедшие серии сериала.

    {'title', 'title_ru', 'episodes': [{season, episode, name, lf_id}], 'packs': {сезоны целиком}}
    У ещё не вышедших серий нет кнопки просмотра — они сюда не попадают.
    """
    base, proxy = _where()
    page = _request(_session(proxy), 'GET', base + link.strip('/') + '/seasons/').text
    page = re.sub(r'<!--.*?-->', '', page, flags=re.S)  # на сайте закомментированы старые кнопки
    if 'serie-block' not in page:
        raise LostFilmError('Не смог разобрать страницу сериала (сайт изменился?)')
    info = {'title': _text(_find(r'<h2 class="title-en"[^>]*>(.*?)</h2>', page)),
            'title_ru': _text(_find(r'<h1 class="title-ru"[^>]*>(.*?)</h1>', page)),
            'episodes': [], 'packs': set()}
    seen = set()
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
        m = _PLAY_RE.search(row)
        if not m or int(m[3]) == PACK:
            continue
        se = (int(m[2]), int(m[3]))
        if se in seen:
            continue
        seen.add(se)
        name_cell = _find(r'<td class="gamma"[^>]*>(.*?)</td>', row)
        info['episodes'].append({'season': se[0], 'episode': se[1], 'lf_id': m[1],
                                 'name': _text(re.split(r'<br', name_cell, maxsplit=1)[0])})
    for m in _PLAY_RE.finditer(page):
        if int(m[3]) == PACK:
            info['packs'].add(int(m[2]))
    return info


# ── releases / download (нужен вход) ──────────────────────────────────────────

def _parse_items(page: str) -> list[dict]:
    """Страница раздач: по блоку на качество (SD, 1080, 720/MP4)."""
    items = []
    for block in page.split('inner-box--item')[1:]:
        link = (_find(r'inner-box--link.*?<a[^>]+href="([^"]+)"', block)
                or _find(r'<a[^>]+href="([^"]+)"', block))
        if not link:
            continue
        desc = _text(_find(r'inner-box--desc[^>]*>(.*?)</div>', block))
        items.append({'label': _text(_find(r'inner-box--label[^>]*>(.*?)</div>', block)),
                      'desc': desc, 'size': _size(desc), 'url': htmllib.unescape(link)})
    return items


def releases(lf_id: str, season: int, episode: int) -> list[dict]:
    """Раздачи серии (episode=PACK — сезон целиком): [{label, desc, size, url, proxy}]."""
    with _lock:
        auth = dict(_auth)
    if not auth:
        raise NeedLogin('нужно войти в LostFilm')
    s = _session(auth.get('proxy', False), auth['cookies'])
    r = _request(s, 'GET', auth['base'] + 'v_search.php',
                 params={'c': lf_id, 's': season, 'e': episode}, allow_redirects=False)
    location = r.headers.get('Location') or ''
    if (r.is_redirect and 'login' in location) or 'log in first' in r.text:
        _drop_auth('сессия истекла, нужен новый вход')
        raise NeedLogin('сессия LostFilm истекла')
    if r.is_redirect:
        target = urljoin(r.url, location)
    else:
        # страница-переходник: <meta http-equiv="refresh" content="0; url=…"> или location.replace
        link = (_find(r'url=["\']?([^"\'>\s]+)', r.text)
                or _find(r'location\.(?:replace|href)\s*[=(]\s*["\']([^"\']+)', r.text))
        if not link:
            logger.warning(f'LostFilm v_search: нет ссылки на раздачи: {r.text[:300]!r}')
            raise LostFilmError('LostFilm не дал ссылку на раздачи (сайт изменился?)')
        target = urljoin(r.url, htmllib.unescape(link))

    via_proxy = auth.get('proxy', False)
    page = _request(s, 'GET', target).text
    items = _parse_items(page)
    if not items and _GEO_BLOCK in page and config.PROXY_URL and not via_proxy:
        via_proxy = True
        page = _request(_session(True, auth['cookies']), 'GET', target).text
        items = _parse_items(page)
    if not items:
        if _GEO_BLOCK in page:
            raise LostFilmError('раздача закрыта для РФ' +
                                ('' if config.PROXY_URL else ' — нужен PROXY_URL в .env'))
        logger.warning(f'LostFilm: нет раздач на {target}: {_text(page)[:300]!r}')
        raise LostFilmError('на странице раздач пусто (ещё не выложили?)')
    for it in items:
        it['proxy'] = via_proxy
    return items


def download(item: dict) -> bytes:
    """Скачать .torrent из releases()."""
    with _lock:
        cookies = list(_auth.get('cookies') or [])
    r = _request(_session(item.get('proxy', False), cookies), 'GET', item['url'])
    if not r.content.startswith(b'd'):
        raise LostFilmError('вместо .torrent-файла пришло что-то другое')
    return r.content


# ── login (капча через Telegram) ──────────────────────────────────────────────

def start_login() -> bytes:
    """Шаг 1: открыть страницу входа, вернуть картинку капчи."""
    if not is_configured():
        raise LostFilmError('в .env нет LOSTFILM_EMAIL / LOSTFILM_PASSWORD')
    base, proxy = _pick_site(force=True)
    s = _session(proxy)
    _request(s, 'GET', base + 'login')
    r = _request(s, 'GET', base + 'simple_captcha.php', params={'r': random.random()})
    if not r.content or not r.headers.get('Content-Type', '').startswith('image'):
        raise LostFilmError('LostFilm не показал капчу')
    with _lock:
        _login.clear()
        _login.update(session=s, base=base, proxy=proxy, ts=time.time())
    return r.content


def finish_login(code: str) -> str:
    """Шаг 2: войти с кодом капчи. Вернёт имя на сайте."""
    with _lock:
        pending = dict(_login)
        _login.clear()  # капча одноразовая
    if not pending or time.time() - pending['ts'] > _LOGIN_TTL:
        raise LostFilmError('капча устарела — начни заново: /lostfilm')
    s, base = pending['session'], pending['base']

    def js(value: str) -> str:
        # сайт сам прогоняет поля через encodeURIComponent — делаем так же,
        # иначе пароль с «+» или «%» сервер раскодирует неправильно
        return quote(value, safe="-_.!~*'()")

    r = _request(s, 'POST', base + 'ajaxik.users.php', data={
        'act': 'users', 'type': 'login',
        'mail': js(config.LOSTFILM_EMAIL), 'pass': js(config.LOSTFILM_PASSWORD),
        'need_captcha': '1', 'captcha': js(code.strip()), 'rem': '1',
    })
    try:
        res = r.json()
    except ValueError:
        raise LostFilmError('LostFilm ответил на вход не JSON') from None
    if not isinstance(res, dict):
        raise LostFilmError(f'LostFilm ответил на вход непонятно: {str(res)[:100]}')

    if res.get('success'):
        name = str(res.get('name') or config.LOSTFILM_EMAIL)
        data = {'base': base, 'proxy': pending['proxy'], 'name': name,
                'since': datetime.now().isoformat(timespec='seconds'),
                'cookies': [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
                            for c in s.cookies]}
        with _lock:
            _auth.clear()
            _auth.update(data)
            _save_auth(data)
        logger.info(f'LostFilm: вход выполнен ({name}, {base})')
        return name

    err = str(res.get('error') or '')
    if err == '3':
        raise BadCredentials('неверная почта или пароль')
    if res.get('need_captcha') or err in ('1', '2', '4'):
        raise BadCaptcha('неверный код с картинки')
    raise LostFilmError(f'LostFilm не пустил: {str(res)[:200]}')
