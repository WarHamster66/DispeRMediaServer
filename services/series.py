"""Подписки на сериалы: бот сам качает новые серии с LostFilm.

Как работает:
  • /follow Бункер — ищем сериал на LostFilm, запоминаем последнюю вышедшую серию;
  • раз в SERIES_CHECK_MINUTES открываем страницу каждого сериала из подписок;
  • вышла серия новее запомненной → качаем в SERIES_FOLDER в нужном качестве.

Сам сайт — в services/lostfilm.py.
"""
import hashlib
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from core import config
from services import lostfilm
from services import transmission as tr

logger = logging.getLogger(__name__)

PACK = lostfilm.PACK

_FILE = Path(config.HISTORY_FILE).parent / 'subscriptions.json'
_lock = threading.Lock()
_check_lock = threading.Lock()  # не запускать две проверки одновременно

# LostFilm иногда выкладывает SD раньше 1080p. Ждём нужное качество столько
# проверок, потом берём лучшее из доступного.
_QUALITY_WAIT_CHECKS = 6
_waited: dict[tuple, int] = {}
_fail_notified: set[tuple] = set()  # об ошибке скачивания серии сообщаем один раз
_login_alert_sent = False           # «нужно войти в LostFilm» — тоже один раз


# ── storage ───────────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        return json.loads(_FILE.read_text(encoding='utf-8')).get('subs', [])
    except Exception:
        return []


def _save(subs: list[dict]) -> None:
    tmp = _FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps({'subs': subs}, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(_FILE)


def list_subs() -> list[dict]:
    with _lock:
        return _load()


def subscribe(show: dict, chat_id: int, season: int, episode: int, by: str) -> bool:
    """show — из find_shows(). False — если уже подписаны."""
    key = _key(show['show'])
    with _lock:
        subs = _load()
        if any(s['key'] == key for s in subs):
            return False
        subs.append({'show': show['show'], 'title_ru': show.get('title_ru', ''), 'key': key,
                     'link': show['link'], 'lf_id': show.get('lf_id', ''),
                     'season': season, 'episode': episode, 'chat_id': chat_id, 'by': by,
                     'added': datetime.now().isoformat(timespec='seconds')})
        _save(subs)
    return True


def unsubscribe(key: str) -> str | None:
    """Вернёт название отписанного сериала или None."""
    with _lock:
        subs = _load()
        for s in subs:
            if s['key'] == key:
                subs.remove(s)
                _save(subs)
                return s['show']
    return None


def _update(key: str, **fields) -> None:
    with _lock:
        subs = _load()
        for s in subs:
            if s['key'] == key:
                s.update(fields)
        _save(subs)


def _set_last(key: str, season: int, episode: int) -> None:
    with _lock:
        subs = _load()
        for s in subs:
            if s['key'] == key and (season, episode) > (s['season'], s['episode']):
                s['season'], s['episode'] = season, episode
        _save(subs)


# ── helpers ───────────────────────────────────────────────────────────────────

def _key(show: str) -> str:
    return ' '.join(show.lower().split())


def code(season: int, episode: int) -> str:
    """S03E02 или «сезон 3» для сезона целиком."""
    return f'сезон {season}' if episode >= PACK else f'S{season:02d}E{episode:02d}'


def display(s: dict) -> str:
    """«Бункер (Silo)» — или просто Silo, если русского названия нет."""
    ru = (s.get('title_ru') or '').strip()
    return f"{ru} ({s['show']})" if ru and ru.lower() != s['show'].lower() else s['show']


def _quality_ok(item: dict) -> bool:
    q = config.SERIES_QUALITY.strip().lower()
    return not q or q in f"{item['label']} {item['desc']}".lower()


def _rank(item: dict) -> int:
    t = f"{item['label']} {item['desc']}".lower()
    return 3 if '1080' in t else 2 if ('720' in t or 'mp4' in t) else 1


def _choose(items: list[dict]) -> dict:
    """Предпочитаемое качество, иначе лучшее из доступных."""
    preferred = [i for i in items if _quality_ok(i)]
    return preferred[0] if preferred else max(items, key=_rank)


def login_hint() -> str:
    if not lostfilm.is_configured():
        return '🔑 Чтобы бот мог качать с LostFilm, укажи аккаунт: /lostfilm'
    return '🔑 Нужно войти в LostFilm: отправь /lostfilm и введи код с картинки.'


# ── search ────────────────────────────────────────────────────────────────────

def find_shows(query: str) -> list[dict]:
    """Сериалы на LostFilm (рус. или англ. название): [{show, title_ru, link, lf_id}]."""
    return [{'show': x['title'], 'title_ru': x['title_ru'], 'link': x['link'], 'lf_id': x['id']}
            for x in lostfilm.search(query)]


def latest(show: dict) -> dict | None:
    """Последняя вышедшая серия: {season, episode, name, lf_id, pack}.

    pack=True — её сезон уже можно скачать целиком одной раздачей.
    """
    info = lostfilm.series_info(show['link'])
    if not info['episodes']:
        return None
    last = max(info['episodes'], key=lambda e: (e['season'], e['episode']))
    return {**last, 'pack': last['season'] in info['packs']}


def _ensure_link(sub: dict) -> dict:
    """Подписки, сделанные через Jackett, не знают адрес сериала — находим его."""
    if sub.get('link'):
        return sub
    for x in lostfilm.search(sub['show']):
        if _key(x['title']) == sub['key']:
            fields = {'link': x['link'], 'lf_id': x['id'], 'title_ru': x['title_ru']}
            _update(sub['key'], **fields)
            return {**sub, **fields}
    raise lostfilm.LostFilmError(f"не нашёл «{sub['show']}» на LostFilm")


# ── download ──────────────────────────────────────────────────────────────────

def download_episode(bot, chat_id: int, show_name: str, ep: dict,
                     items: list[dict] | None = None, quiet_errors: bool = False) -> bool:
    """Скачать серию (ep['episode'] == PACK — сезон целиком) в SERIES_FOLDER."""
    label = code(ep['season'], ep['episode'])
    location = os.path.join(config.SHARED_FOLDER, config.SERIES_FOLDER)
    try:
        if items is None:
            items = lostfilm.releases(ep['lf_id'], ep['season'], ep['episode'])
        data = lostfilm.download(_choose(items))
        file_hash = hashlib.sha256(data).hexdigest()
        if tr.is_active(file_hash):
            # Transmission молча вернул бы уже добавленный торрент, и мы бы
            # следили за ним второй раз. Значит, сайт отдал не ту раздачу.
            raise lostfilm.LostFilmError('LostFilm отдал раздачу, которая уже качается')
        torrent = tr.add_and_start(data, location)
    except lostfilm.NeedLogin:
        if not quiet_errors:
            _send(bot, chat_id, f'❌ {show_name} {label}: не скачать без входа.\n{login_hint()}')
        return False
    except tr.InsufficientSpaceError as e:
        if not quiet_errors:
            _send(bot, chat_id, f"❌ {show_name} {label}: не хватает места в «{config.SERIES_FOLDER}» "
                                f"(нужно {e.required / 1024 ** 3:.1f} ГБ, свободно {e.available / 1024 ** 3:.1f} ГБ)")
        return False
    except Exception as e:
        logger.error(f'Series download failed ({show_name} {label}): {e}')
        if not quiet_errors:
            _send(bot, chat_id, f'⚠️ Не удалось скачать {show_name} {label}: {e}\nПопробую ещё раз позже.')
        return False

    tr.mark_active(file_hash)
    if ep['episode'] >= PACK:
        text = f"⬇️ {show_name} — {label} целиком, качаю в «{config.SERIES_FOLDER}»"
    else:
        name = f" «{ep['name']}»" if ep.get('name') else ''
        text = f"🆕 {show_name} — {label}{name}\nВышла на LostFilm, качаю в «{config.SERIES_FOLDER}»"
    msg = _send(bot, chat_id, text)
    tr.start_monitoring(bot, chat_id, msg.message_id if msg else None, torrent.id, file_hash)
    logger.info(f'Series download: {show_name} {label}')
    return True


def check_new(bot) -> int:
    """Проверить сериалы из подписок и скачать новые серии. Возвращает число запущенных загрузок."""
    global _login_alert_sent
    subs = list_subs()
    if not subs or not _check_lock.acquire(blocking=False):
        return 0
    try:
        if lostfilm.logged_in_as():
            _login_alert_sent = False
        started = 0
        need_login = False
        for sub in subs:
            try:
                sub = _ensure_link(sub)
                info = lostfilm.series_info(sub['link'])
            except Exception as e:
                logger.warning(f"Series check: {sub['show']}: {e}")
                continue
            new = sorted((e for e in info['episodes']
                          if (e['season'], e['episode']) > (sub['season'], sub['episode'])),
                         key=lambda e: (e['season'], e['episode']))
            for ep in new:  # по порядку; не перепрыгиваем через несскачанную серию
                if not lostfilm.logged_in_as():
                    need_login = True
                    break
                ep_id = (sub['key'], ep['season'], ep['episode'])
                try:
                    items = lostfilm.releases(ep['lf_id'], ep['season'], ep['episode'])
                except lostfilm.NeedLogin:
                    need_login = True
                    break
                except Exception as e:
                    logger.warning(f"Series check: {sub['show']} {code(ep['season'], ep['episode'])}: {e}")
                    break  # повторим в следующую проверку

                if not any(_quality_ok(i) for i in items):
                    _waited[ep_id] = _waited.get(ep_id, 0) + 1
                    if _waited[ep_id] < _QUALITY_WAIT_CHECKS:
                        break  # подождём, пока выложат нужное качество

                if download_episode(bot, sub['chat_id'], display(sub), ep, items=items,
                                    quiet_errors=ep_id in _fail_notified):
                    _set_last(sub['key'], ep['season'], ep['episode'])
                    _waited.pop(ep_id, None)
                    _fail_notified.discard(ep_id)
                    started += 1
                else:
                    _fail_notified.add(ep_id)
                    break  # повторим эту серию в следующую проверку
            if need_login:
                break

        if need_login and not _login_alert_sent:
            _login_alert_sent = True
            for chat_id in {s['chat_id'] for s in subs}:
                _send(bot, chat_id, '📺 Вышли новые серии, но скачать их пока не могу.\n'
                                    f'{login_hint()}\nСкачаю сразу после входа.')
        return started
    finally:
        _check_lock.release()


def _send(bot, chat_id: int, text: str):
    try:
        return bot.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f'send_message failed: {e}')
        return None
