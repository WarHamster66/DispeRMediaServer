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
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import config
from services import lostfilm
from services import transmission as tr

logger = logging.getLogger(__name__)

PACK = lostfilm.PACK
SPECIALS = 999  # «сезон» 999 на LostFilm — доп. материалы

_FILE = Path(config.HISTORY_FILE).parent / 'subscriptions.json'
_lock = threading.Lock()
_check_lock = threading.Lock()  # не запускать две проверки одновременно

# LostFilm иногда выкладывает SD раньше 1080p. Ждём нужное качество столько
# проверок, потом берём лучшее из доступного.
_QUALITY_WAIT_CHECKS = 6
_waited: dict[tuple, int] = {}
_fail_notified: set[tuple] = set()  # об ошибке скачивания серии сообщаем один раз
_login_alert_sent = False           # «нужно войти в LostFilm» — тоже один раз

# Сайт недоступен целиком (домен сессии заблокирован и т.п.): считаем подряд
# идущие проверки, где не открылась ни одна страница сериала, и один раз просим
# перелогиниться — куки входа привязаны к домену, само на зеркало не переедет.
_SITE_DOWN_ALERT_CHECKS = 3
_site_down_checks = 0
_site_alert_sent = False


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
    # Ключ — адрес сериала на LostFilm, а не название: у «Dark Matter» 2015
    # и «Dark Matter» 2024 английское название одно и то же.
    link = _link_key(show['link'])
    key = link
    with _lock:
        subs = _load()
        if any(_link_key(s.get('link', '')) == link or s['key'] == key for s in subs):
            return False
        subs.append({'show': show['show'], 'title_ru': show.get('title_ru', ''), 'key': key,
                     'link': show['link'], 'lf_id': show.get('lf_id', ''),
                     'season': season, 'episode': episode, 'chat_id': chat_id, 'by': by,
                     'added': datetime.now().isoformat(timespec='seconds')})
        _save(subs)
    return True


def is_subscribed(show: dict) -> bool:
    link = _link_key(show['link'])
    return any(_link_key(s.get('link', '')) == link for s in list_subs())


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


def _link_key(link: str) -> str:
    """'/series/Dark_Matter_2024/' → 'series/dark_matter_2024' — уникальный id сериала."""
    return (link or '').strip().strip('/').lower()


def code(season: int, episode: int) -> str:
    """S03E02 или «сезон 3» для сезона целиком."""
    if season == SPECIALS:
        return 'доп. материалы' if episode >= PACK else f'доп. материалы, {episode}'
    return f'сезон {season}' if episode >= PACK else f'S{season:02d}E{episode:02d}'


def display(s: dict) -> str:
    """«Бункер (Silo)» — или просто Silo, если русского названия нет."""
    ru = (s.get('title_ru') or '').strip()
    return f"{ru} ({s['show']})" if ru and ru.lower() != s['show'].lower() else s['show']


def _quality_ok(item: dict) -> bool:
    q = config.SERIES_QUALITY.strip().lower()
    return not q or q in f"{item['label']} {item['desc']}".lower()


QUALITIES = ('2160p', '1080p', '720p', 'SD')  # от лучшего к худшему


def quality_of(item: dict) -> str:
    t = f"{item['label']} {item['desc']}".lower()
    if '2160' in t or '4k' in t:
        return '2160p'
    if '1080' in t:
        return '1080p'
    if '720' in t or 'mp4' in t or item['label'].strip().lower() == 'hd':
        return '720p'
    return 'SD'


def _choose(items: list[dict], quality: str = '') -> dict:
    """Выбранное качество, иначе ближайшее к нему (сначала ниже). Без выбора —
    качество из настроек, иначе лучшее из доступных."""
    if quality in QUALITIES:
        want = QUALITIES.index(quality)
        return min(items, key=lambda i: (abs(QUALITIES.index(quality_of(i)) - want),
                                         QUALITIES.index(quality_of(i)) < want))
    preferred = [i for i in items if _quality_ok(i)]
    return preferred[0] if preferred else min(items, key=lambda i: QUALITIES.index(quality_of(i)))


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
    episodes = [e for e in info['episodes'] if e['season'] != SPECIALS]
    if not episodes:
        return None
    last = max(episodes, key=lambda e: (e['season'], e['episode']))
    return {**last, 'pack': last['season'] in info['packs']}


def _ensure_link(sub: dict) -> dict:
    """Подписки, сделанные через Jackett, не знают адрес сериала — находим его."""
    if sub.get('link'):
        return sub
    same = [x for x in lostfilm.search(sub['show']) if _key(x['title']) == sub['key']]
    if not same:
        raise lostfilm.LostFilmError(f"не нашёл «{sub['show']}» на LostFilm")
    # Несколько сериалов с одним названием (Dark Matter 2015 и 2024) — берём
    # новый: у него больше id на LostFilm, и именно он, скорее всего, ещё выходит
    x = max(same, key=lambda s: int(s['id']) if s['id'].isdigit() else 0)
    if len(same) > 1:
        logger.warning(f"Series: «{sub['show']}» — {len(same)} сериала с таким названием, беру {x['link']}")
    fields = {'link': x['link'], 'lf_id': x['id'], 'title_ru': x['title_ru']}
    _update(sub['key'], **fields)
    return {**sub, **fields}


# ── download ──────────────────────────────────────────────────────────────────

def show_folder(s: dict) -> str:
    """Имя папки сериала внутри SERIES_FOLDER: русское название с LostFilm (иначе английское)."""
    name = (s.get('title_ru') or s.get('show') or '').strip()
    name = re.sub(r'[\\/:*?"<>|]+', ' ', name)
    return ' '.join(name.split()).strip(' .')


def season_episodes(show: dict, season: int) -> list[dict]:
    """Все вышедшие серии сезона по порядку."""
    info = lostfilm.series_info(show['link'])
    return sorted((e for e in info['episodes'] if e['season'] == season), key=lambda e: e['episode'])


_SE_RE = re.compile(r'(?<![A-Za-z0-9])S(\d{1,3})E(\d{1,3})(?!\d)', re.I)
_VIDEO = ('.mkv', '.mp4', '.avi', '.m4v', '.ts')


def parse_se(name: str) -> tuple[int, int] | None:
    """'Silo.S02E10.1080p.mkv' → (2, 10)."""
    m = _SE_RE.search(os.path.basename(name))
    return (int(m[1]), int(m[2])) if m else None


def _norm(name: str) -> str:
    return ' '.join(re.sub(r'[^0-9a-zа-яё]+', ' ', name.lower()).split())


def on_disk(show: dict) -> set[tuple[int, int]]:
    """Серии, которые уже скачаны: в папке сериала и (от старых версий бота,
    качавших без папок) прямо в SERIES_FOLDER."""
    root = os.path.join(config.SHARED_FOLDER, config.SERIES_FOLDER)
    found = set()
    folder = show_folder(show)
    if folder:
        for _, _, files in os.walk(os.path.join(root, folder)):
            found.update(parse_se(f) for f in files if f.lower().endswith(_VIDEO))
    names = {_norm(show['show']), _norm(show.get('link', '').strip('/').split('/')[-1])}
    try:
        loose = os.listdir(root)
    except OSError:
        loose = []
    for f in loose:
        m = _SE_RE.search(f)
        if m and f.lower().endswith(_VIDEO) and _norm(f[:m.start()]) in names:
            found.add(parse_se(f))
    found.discard(None)
    return found


def jobs_for(info: dict, have: set, season: int = 0, skip_have: bool = True) -> list[dict]:
    """Что качать: сезон (season=0 — весь сериал, без доп. материалов).

    Сезон, которого совсем нет, — одной раздачей, если она есть; иначе по сериям.
    """
    jobs = []
    seasons = [season] if season else sorted({e['season'] for e in info['episodes']} - {SPECIALS})
    for n in seasons:
        eps = sorted((e for e in info['episodes'] if e['season'] == n), key=lambda e: e['episode'])
        todo = [e for e in eps if not skip_have or (n, e['episode']) not in have]
        if not todo:
            continue
        if n in info['packs'] and len(todo) == len(eps):
            jobs.append({**eps[0], 'episode': PACK, 'name': ''})
        else:
            jobs += todo
    return jobs


def size_options(info: dict, jobs: list[dict]) -> tuple[dict[str, int], bool]:
    """Качества и примерный общий размер: ({'1080p': байты}, размер точный?).

    Размер берём по одной раздаче на сезон, остальные серии считаем такими же.
    """
    by_season: dict[int, list[dict]] = {}
    for j in jobs:
        by_season.setdefault(j['season'], []).append(j)

    def sample(season: int):
        j = by_season[season][0]
        try:
            return season, lostfilm.releases(j['lf_id'], j['season'], j['episode'])
        except lostfilm.NeedLogin:
            raise
        except Exception as e:
            logger.warning(f"LostFilm sizes: {code(j['season'], j['episode'])}: {e}")
            return season, []

    with ThreadPoolExecutor(max_workers=3) as pool:
        samples = dict(pool.map(sample, by_season))
    if not any(samples.values()):
        raise lostfilm.LostFilmError('LostFilm не отдал ни одной раздачи — попробуй позже')
    exact = all(samples.values())
    found = {quality_of(i) for items in samples.values() for i in items}
    sizes = {q: 0 for q in QUALITIES if q in found}
    for season, items in samples.items():
        if not items:
            continue
        todo = by_season[season]
        exact = exact and all(i['size'] for i in items)
        for q in sizes:
            item = _choose(items, q)
            if todo[0]['episode'] >= PACK:
                sizes[q] += item['size']
            elif item.get('whole'):
                total = sum(e['season'] == season for e in info['episodes']) or 1
                sizes[q] += item['size'] * len(todo) // total
                exact = False
            else:
                sizes[q] += item['size'] * len(todo)
                exact = exact and len(todo) == 1
    return sizes, exact


def download_season(bot, chat_id: int, show: dict, episodes: list[dict],
                    quality: str = '', manual: bool = False) -> int:
    """Скачать несколько серий / сезонов по очереди. Вернёт, сколько запущено."""
    queue = list(episodes)
    started = failed = 0
    while queue:
        ep = queue.pop(0)
        items, more = None, []
        try:
            items = lostfilm.releases(ep['lf_id'], ep['season'], ep['episode'])
        except Exception:
            pass  # download_episode повторит запрос и сам сообщит об ошибке
        if items and ep['episode'] < PACK and items[0].get('whole'):
            # отдельных серий нет — берём все нужные файлы из раздачи сезона сразу
            more = [e['episode'] for e in queue if e['season'] == ep['season']]
            queue = [e for e in queue if e['season'] != ep['season']]
        if download_episode(bot, chat_id, display(show), ep, items=items, folder=show_folder(show),
                            quality=quality, manual=manual, more=more):
            started += 1
            failed = 0
            continue
        failed += 1
        if queue and (failed >= 3 or not lostfilm.logged_in_as()):
            _send(bot, chat_id, f'⏹ {display(show)}: остановился, осталось раздач в очереди: {len(queue)}.')
            break
    return started


_download_lock = threading.Lock()  # две серии из одной раздачи сезона — строго по очереди


def download_episode(bot, chat_id: int, show_name: str, ep: dict,
                     items: list[dict] | None = None, quiet_errors: bool = False,
                     folder: str = '', quality: str = '', manual: bool = False,
                     more: list[int] = ()) -> bool:
    """Скачать серию (ep['episode'] == PACK — сезон целиком) в SERIES_FOLDER/<folder>.

    Если сайт на серию отдаёт раздачу сезона, качаем из неё только эту серию
    и серии из more. manual — просил пользователь (а не подписка).
    """
    label = code(ep['season'], ep['episode'])
    if more:
        label += f' и ещё {len(more)}'
    where = '/'.join(x for x in (config.SERIES_FOLDER, folder) if x)
    location = os.path.join(config.SHARED_FOLDER, config.SERIES_FOLDER, folder)
    retry = '' if manual else '\nПопробую ещё раз позже.'
    extended = False
    try:
        if items is None:
            items = lostfilm.releases(ep['lf_id'], ep['season'], ep['episode'])
        item = _choose(items, quality)
        data = lostfilm.download(item)
        if not os.path.isdir(location):
            os.makedirs(location, exist_ok=True)
            os.chmod(location, 0o2775)  # Transmission (группа media) должен писать сюда
        file_hash = hashlib.sha256(data).hexdigest()
        want = None
        if item.get('whole') and ep['episode'] < PACK:
            numbers = {(ep['season'], n) for n in (ep['episode'], *more)}

            def want(name):
                return parse_se(name) in numbers
        # раздача уже в Transmission: что из неё докачать (сезон целиком — всё остальное)
        want_more = want
        if manual and ep['episode'] >= PACK:
            def want_more(name):
                return True
        with _download_lock:
            watched = tr.is_active(file_hash)
            if watched and not want_more:
                # Transmission молча вернул бы уже добавленный торрент, и мы бы
                # следили за ним второй раз. Значит, сайт отдал не ту раздачу
                # (или её уже скачивают).
                if manual:
                    _send(bot, chat_id, f'ℹ️ {show_name} {label}: эта раздача уже качается. '
                                        'Список загрузок: /torrents')
                    return False
                raise lostfilm.LostFilmError('LostFilm отдал раздачу, которая уже качается')
            try:
                torrent = tr.add_and_start(data, location, want)
            except tr.DuplicateTorrentError as e:
                # раздача сезона уже в Transmission: нужна ещё серия из неё
                if not want_more:
                    raise
                if not tr.want_more_files(e.torrent, want_more):
                    if manual:
                        _send(bot, chat_id, f'ℹ️ {show_name} {label}: уже скачано или качается. '
                                            'Список загрузок: /torrents')
                    return not manual  # подписке эта серия больше не нужна
                torrent, extended = e.torrent, True
            if not watched:
                tr.mark_active(file_hash)
    except lostfilm.NeedLogin:
        if not quiet_errors:
            _send(bot, chat_id, f'❌ {show_name} {label}: не скачать без входа.\n{login_hint()}')
        return False
    except tr.DuplicateTorrentError:
        if not quiet_errors:
            _send(bot, chat_id, f'ℹ️ {show_name} {label}: этот торрент уже есть в Transmission — '
                                'второй раз не добавляю. Список загрузок: /torrents')
        return False
    except tr.InsufficientSpaceError as e:
        if not quiet_errors:
            _send(bot, chat_id, f"❌ {show_name} {label}: не хватает места в «{where}» "
                                f"(нужно {e.required / 1024 ** 3:.1f} ГБ, свободно {e.available / 1024 ** 3:.1f} ГБ)")
        return False
    except Exception as e:
        logger.error(f'Series download failed ({show_name} {label}): {e}')
        if not quiet_errors:
            _send(bot, chat_id, f'⚠️ Не удалось скачать {show_name} {label}: {e}{retry}')
        return False

    q = quality_of(item)
    name = f" «{ep['name']}»" if ep.get('name') and ep['episode'] < PACK and not more else ''
    if watched and extended:
        _send(bot, chat_id, f'➕ {show_name} — {label}{name}: добавил к раздаче, которая уже качается.')
    else:
        if manual:
            whole = ' целиком' if ep['episode'] >= PACK else ''
            text = f'⬇️ {show_name} — {label}{whole}{name}, {q}\nКачаю в «{where}»'
        elif ep['episode'] >= PACK:
            text = f"⬇️ {show_name} — {label} целиком, качаю в «{where}»"
        else:
            text = f"🆕 {show_name} — {label}{name}\nВышла на LostFilm, качаю в «{where}»"
        msg = _send(bot, chat_id, text)
        tr.start_monitoring(bot, chat_id, msg.message_id if msg else None, torrent.id, file_hash)
    logger.info(f'Series download: {show_name} {label} ({q})')
    return True


def check_new(bot) -> int:
    """Проверить сериалы из подписок и скачать новые серии. Возвращает число запущенных загрузок."""
    global _login_alert_sent, _site_down_checks, _site_alert_sent
    subs = list_subs()
    if not subs or not _check_lock.acquire(blocking=False):
        return 0
    try:
        if lostfilm.logged_in_as():
            _login_alert_sent = False
        started = 0
        need_login = False
        opened = 0      # страницы сериалов, которые удалось открыть
        site_errors = 0  # сайт не отвечает совсем (сеть / блокировка домена)
        for sub in subs:
            try:
                sub = _ensure_link(sub)
                info = lostfilm.series_info(sub['link'])
                opened += 1
            except lostfilm.LostFilmError as e:
                logger.warning(f"Series check: {sub['show']}: {e}")
                if str(e).startswith(('LostFilm не отвечает', 'LostFilm недоступен')):
                    site_errors += 1
                continue
            except Exception as e:
                logger.warning(f"Series check: {sub['show']}: {e}")
                continue
            new = sorted((e for e in info['episodes']
                          if e['season'] != SPECIALS and (e['season'], e['episode']) > (sub['season'], sub['episode'])),
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

                if download_episode(bot, sub['chat_id'], display(sub), ep, items=items, folder=show_folder(sub),
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

        if site_errors and not opened:
            # не открылась ни одна страница: сайт не отвечает вообще.
            # Несколько проверок подряд — просим перелогиниться, один раз до восстановления.
            _site_down_checks += 1
            if _site_down_checks >= _SITE_DOWN_ALERT_CHECKS and not _site_alert_sent:
                _site_alert_sent = True
                # первая неудача — в момент T, N-я — через (N-1) интервалов
                mins = (_SITE_DOWN_ALERT_CHECKS - 1) * max(5, config.SERIES_CHECK_MINUTES)
                for chat_id in {s['chat_id'] for s in subs}:
                    _send(bot, chat_id,
                          f'📺 LostFilm не отвечает уже больше {mins} мин — похоже, адрес сайта опять заблокирован.\n'
                          'Новые серии пока не скачиваются. Сделай вход заново: /lostfilm — бот сам подберёт '
                          'рабочее зеркало, а после входа подтянет всё пропущенное.')
        else:
            if _site_alert_sent:
                for chat_id in {s['chat_id'] for s in subs}:
                    _send(bot, chat_id, '📺 LostFilm снова доступен — проверки серий восстановлены.')
            _site_down_checks = 0
            _site_alert_sent = False

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
