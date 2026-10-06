"""Подписки на сериалы: бот сам качает новые серии (LostFilm через Jackett).

Как работает:
  • /follow Silo — ищем сериал на LostFilm, запоминаем последнюю вышедшую серию;
  • раз в SERIES_CHECK_MINUTES смотрим ленту новинок LostFilm в Jackett;
  • серия новее запомненной и в нужном качестве → качаем в SERIES_FOLDER.

Индексатор LostFilm в Jackett отдаёт названия вида
  «Silo - S3E2 - Название серии - rus 1080p WEBDL (LostFilm)»  — серия;
  «Silo - S3 - rus 1080p WEBDL (LostFilm)»                     — сезон целиком
(номера без ведущих нулей). Каждое качество — отдельной раздачей. Поиск по
сериалу отдаёт сезонный пак, если сезон уже вышел целиком, иначе — серии;
лента новинок — всегда отдельные серии.
"""
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from core import config
from services import jackett
from services import transmission as tr

logger = logging.getLogger(__name__)

_FILE = Path(config.HISTORY_FILE).parent / 'subscriptions.json'
_lock = threading.Lock()
_check_lock = threading.Lock()  # не запускать две проверки одновременно

# LostFilm иногда выкладывает SD раньше 1080p. Ждём нужное качество столько
# проверок, потом берём лучшее из доступного.
_QUALITY_WAIT_CHECKS = 6
_waited: dict[tuple, int] = {}
_fail_notified: set[tuple] = set()  # об ошибке скачивания серии сообщаем один раз

# «Show - S3E2 - …» или «Show - S3 - …» (сезонный пак); номера могут быть с нулями
_EP_RE = re.compile(
    r'^(?P<show>.+?)\s+-\s+S(?P<s>\d{1,2})(?:E(?P<e>\d{1,3}))?(?=\s+-\s+|\s*$)(?:\s+-\s+(?P<rest>.*))?$',
    re.I,
)
PACK = 999  # «номер серии» для сезона целиком: всё в этом сезоне уже вышло


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


def subscribe(show: str, chat_id: int, season: int, episode: int, by: str) -> bool:
    """False — если уже подписаны."""
    key = _key(show)
    with _lock:
        subs = _load()
        if any(s['key'] == key for s in subs):
            return False
        subs.append({'show': show, 'key': key, 'season': season, 'episode': episode,
                     'chat_id': chat_id, 'by': by,
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


def _set_last(key: str, season: int, episode: int) -> None:
    with _lock:
        subs = _load()
        for s in subs:
            if s['key'] == key and (season, episode) > (s['season'], s['episode']):
                s['season'], s['episode'] = season, episode
        _save(subs)


# ── parsing / search ──────────────────────────────────────────────────────────

def _key(show: str) -> str:
    return ' '.join(show.lower().split())


def parse(title: str) -> dict | None:
    """Разобрать название LostFilm. Для сезонного пака episode == PACK, pack == True."""
    m = _EP_RE.match((title or '').strip())
    if not m:
        return None
    rest = m['rest'] or ''
    pack = m['e'] is None
    # «Название серии - rus 1080p WEBDL (LostFilm)» → название до последнего « - »
    ep_name = rest.rsplit(' - ', 1)[0] if (' - ' in rest and not pack) else ''
    return {'show': m['show'].strip(), 'season': int(m['s']),
            'episode': PACK if pack else int(m['e']),
            'pack': pack, 'ep_name': ep_name.strip()}


def code(season: int, episode: int) -> str:
    """S03E02 или «сезон 3» для пака."""
    return f'сезон {season}' if episode >= PACK else f'S{season:02d}E{episode:02d}'


def _quality_ok(title: str) -> bool:
    q = config.SERIES_QUALITY.strip().lower()
    return not q or q in title.lower()


def _best_variant(variants: list[dict]) -> dict:
    """Из нескольких качеств одной серии выбрать предпочитаемое."""
    for v in variants:
        if _quality_ok(v['title']):
            return v
    return variants[0]


def find_shows(query: str) -> list[dict]:
    """Найти сериалы на LostFilm: [{show, season, episode, ep_name, result}] (последняя серия)."""
    results, _ = jackett.search(query, indexer=config.SERIES_INDEXER)
    shows: dict[str, dict] = {}
    for r in results:
        p = parse(r['title'])
        if not p:
            continue
        k = _key(p['show'])
        cur = shows.get(k)
        se = (p['season'], p['episode'])
        if cur is None or se > (cur['season'], cur['episode']):
            shows[k] = {**p, 'variants': [r]}
        elif se == (cur['season'], cur['episode']):
            cur['variants'].append(r)
    out = []
    for s in shows.values():
        s['result'] = _best_variant(s.pop('variants'))
        out.append(s)
    return sorted(out, key=lambda s: s['show'])


# ── download ──────────────────────────────────────────────────────────────────

def download_episode(bot, chat_id: int, show: str, ep: dict, result: dict,
                     quiet_errors: bool = False) -> bool:
    """Скачать серию (или сезон целиком) в SERIES_FOLDER и следить за загрузкой."""
    label = code(ep['season'], ep['episode'])
    location = os.path.join(config.SHARED_FOLDER, config.SERIES_FOLDER)
    try:
        kind, payload = jackett.fetch(result)
        torrent = tr.add_and_start(payload, location)
    except tr.InsufficientSpaceError as e:
        if not quiet_errors:
            _send(bot, chat_id, f"❌ {show} {label}: не хватает места в «{config.SERIES_FOLDER}» "
                                f"(нужно {e.required / 1024 ** 3:.1f} ГБ, свободно {e.available / 1024 ** 3:.1f} ГБ)")
        return False
    except Exception as e:
        logger.error(f'Series download failed ({show} {label}): {e}')
        if not quiet_errors:
            _send(bot, chat_id, f"⚠️ Не удалось скачать {show} {label}: {e}\nПопробую ещё раз позже.")
        return False

    raw = payload if isinstance(payload, bytes) else payload.encode()
    file_hash = hashlib.sha256(raw).hexdigest()
    tr.mark_active(file_hash)
    if ep.get('pack'):
        text = f"⬇️ {show} — {label} целиком, качаю в «{config.SERIES_FOLDER}»"
    else:
        name = f" «{ep['ep_name']}»" if ep.get('ep_name') else ''
        text = f"🆕 {show} — {label}{name}\nВышла на LostFilm, качаю в «{config.SERIES_FOLDER}»"
    msg = _send(bot, chat_id, text)
    tr.start_monitoring(bot, chat_id, msg.message_id if msg else None, torrent.id, file_hash)
    logger.info(f'Series download: {show} {label}')
    return True


def check_new(bot) -> int:
    """Проверить ленту LostFilm и скачать новые серии. Возвращает число запущенных загрузок."""
    subs = list_subs()
    if not subs or not jackett.is_configured():
        return 0
    if not _check_lock.acquire(blocking=False):
        return 0
    try:
        results, _ = jackett.search('', indexer=config.SERIES_INDEXER)
        by_key = {s['key']: s for s in subs}

        # (key, season, episode) → варианты качества
        found: dict[tuple, dict] = {}
        for r in results:
            p = parse(r['title'])
            if not p or p['pack']:
                continue  # сезонные паки не качаем автоматически — серии уже пришли по одной
            sub = by_key.get(_key(p['show']))
            if not sub or (p['season'], p['episode']) <= (sub['season'], sub['episode']):
                continue
            item = found.setdefault((sub['key'], p['season'], p['episode']), {'ep': p, 'variants': []})
            item['variants'].append(r)

        started = 0
        blocked: set[str] = set()  # сериалы, где текущая серия ещё не скачана
        for ep_id in sorted(found):  # по порядку серий
            key, season, episode = ep_id
            if key in blocked:
                continue  # не перепрыгиваем через серию этого сериала
            sub = by_key[key]
            item = found[ep_id]

            preferred = [v for v in item['variants'] if _quality_ok(v['title'])]
            if not preferred:
                _waited[ep_id] = _waited.get(ep_id, 0) + 1
                if _waited[ep_id] < _QUALITY_WAIT_CHECKS:
                    blocked.add(key)  # подождём, пока выложат нужное качество
                    continue
            variant = preferred[0] if preferred else item['variants'][0]

            ok = download_episode(bot, sub['chat_id'], sub['show'], item['ep'], variant,
                                  quiet_errors=ep_id in _fail_notified)
            if ok:
                _set_last(key, season, episode)
                _waited.pop(ep_id, None)
                _fail_notified.discard(ep_id)
                started += 1
            else:
                _fail_notified.add(ep_id)
                blocked.add(key)  # повторим эту серию в следующую проверку
        return started
    finally:
        _check_lock.release()


def _send(bot, chat_id: int, text: str):
    try:
        return bot.send_message(chat_id, text)
    except Exception as e:
        logger.warning(f'send_message failed: {e}')
        return None
