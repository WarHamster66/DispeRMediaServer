"""Поиск по названию: обычный текст в личке или /find — LostFilm и RuTracker сразу.

Ищем одновременно там, где выполнен вход, и показываем один экран: сериалы с
LostFilm и кнопка с раздачами RuTracker. Подключён один сайт или нашлось только
на одном — лишнего вопроса «где искать» нет. Сериал с LostFilm: сезон → серия
(или сезон / весь сериал) → качество с размером.
"""
import html
import logging
import secrets
import threading
import time

import psutil
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit
from core.auth import is_authorized
from handlers import rutracker as rt_ui
from handlers import series as series_ui
from services import lostfilm, rutracker, series

logger = logging.getLogger(__name__)

# token → {query, shows, info: {i: series_info}, rt: None|[..]|'ошибка', view, offer, ts, lock}
_sessions: dict[str, dict] = {}
_lock = threading.Lock()
_TTL = 2 * 3600
_MAX_SHOWS = 8
_MAX_BUTTONS = 90  # у Telegram не больше 100 кнопок в сообщении

_STALE = '⌛ Результаты устарели — повтори поиск.'


def register(bot) -> None:
    """Регистрировать ПОСЛЕДНИМ: обычный текст в личке здесь = поиск, поэтому
    magnet-ссылки, команды и ответы на вопросы бота должны поймать раньше."""
    bot.message_handler(commands=['find'])(lambda m: _cmd_find(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('sr:'))(lambda c: _callback(bot, c))
    bot.message_handler(func=_is_search_text, content_types=['text'])(lambda m: _start(bot, m, m.text))


def _is_search_text(m) -> bool:
    text = (m.text or '').strip()
    return (m.chat.type == 'private' and bool(text) and not text.startswith(('/', 'magnet:'))
            and (rutracker.is_configured() or lostfilm.is_configured())
            and is_authorized(m.from_user.id))


def _thread(target, *args, name: str = 'Search') -> threading.Thread:
    t = threading.Thread(target=target, args=args, name=name, daemon=True)
    t.start()
    return t


# ── поиск ─────────────────────────────────────────────────────────────────────

def _cmd_find(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, 'Что найти? Например: /find Титаник\n'
                              'В личке с ботом можно просто написать название.')
        return
    _start(bot, message, parts[1])


def _not_ready_text() -> str:
    if rutracker.is_configured() and lostfilm.is_configured():
        return '🔑 Нужно войти хотя бы на один сайт: /lostfilm или /rutracker'
    if rutracker.is_configured():
        return rt_ui.need_login_text()
    if lostfilm.is_configured():
        return series.login_hint()
    return ('🔎 Поиск ещё не настроен. Администратор (в личке с ботом):\n'
            '/lostfilm — сериалы с LostFilm\n/rutracker — фильмы и сериалы с RuTracker')


def _start(bot, message, query: str) -> None:
    query = ' '.join(query.split())[:100]
    use_lf = bool(lostfilm.logged_in_as())
    use_rt = bool(rutracker.logged_in_as())
    if not use_lf and not use_rt:
        bot.reply_to(message, _not_ready_text())
        return
    where = ' и '.join(n for n, on in (('LostFilm', use_lf), ('RuTracker', use_rt)) if on)
    status = bot.reply_to(message, f'🔎 Ищу «{query}» — {where}…')
    _thread(_run, bot, message.chat.id, status.message_id, query, use_lf, use_rt)


def _run(bot, chat_id: int, msg_id: int, query: str, use_lf: bool, use_rt: bool) -> None:
    token = secrets.token_hex(4)
    s = {'query': query, 'shows': [], 'info': {}, 'rt': None if use_rt else [], 'use_rt': use_rt,
         'use_lf': use_lf, 'note': '', 'rt_token': '', 'view': 'wait', 'offer': None,
         'ts': time.time(), 'lock': threading.RLock()}
    # вход настроен, но слетел — подскажем, не мешая искать на другом сайте
    if not use_lf and lostfilm.is_configured():
        s['note'] = '🔑 На LostFilm не искал: вход не выполнен — /lostfilm'
    elif not use_rt and rutracker.is_configured():
        s['note'] = '🔑 На RuTracker не искал: вход не выполнен — /rutracker'
    with _lock:
        for k, v in list(_sessions.items()):
            if time.time() - v['ts'] > _TTL:
                _sessions.pop(k, None)
        _sessions[token] = s

    rt_thread = _thread(_search_rt, s, name='RuTrackerSearch') if use_rt else None
    if use_lf:
        try:
            s['shows'] = series.find_shows(query)[:_MAX_SHOWS]
        except Exception as e:
            logger.warning(f'LostFilm search failed: {e}')
            s['note'] = f'⚠️ LostFilm: {e}'
    if s['shows'] and rt_thread:
        # RuTracker может думать долго (проверка Cloudflare) — сериалы показываем
        # сразу, а строку RuTracker обновим, если человек ещё на этом экране
        rt_thread.join(1.5)
        if rt_thread.is_alive():
            _show_home(bot, chat_id, msg_id, token, s)
            rt_thread.join()
            with s['lock']:
                if s['view'] == 'home':
                    _show_home(bot, chat_id, msg_id, token, s)
            return
    elif rt_thread:
        rt_thread.join()
    _show_first(bot, chat_id, msg_id, token, s)


def _search_rt(s: dict) -> None:
    try:
        s['rt'] = rutracker.search(s['query'])
    except rutracker.NeedLogin:
        s['rt'] = 'нужно войти — /rutracker'
    except Exception as e:
        logger.warning(f'RuTracker search failed: {e}')
        s['rt'] = str(e) or 'не ответил'


def _show_first(bot, chat_id: int, msg_id: int, token: str, s: dict) -> None:
    """Первый экран: если выбирать не из чего — сразу результат."""
    shows, rt = s['shows'], s['rt']
    if shows and rt:
        _show_home(bot, chat_id, msg_id, token, s)
    elif len(shows) == 1:
        _show_show(bot, chat_id, msg_id, token, s, 0)
    elif shows:
        _show_home(bot, chat_id, msg_id, token, s)
    elif rt and not isinstance(rt, str):
        _show_rt(bot, chat_id, msg_id, token, s, back=False)
    else:
        lines = [f'🤷 По «{s["query"]}» ничего не нашлось.']
        if isinstance(rt, str):
            lines = [f'⚠️ RuTracker: {rt}']
        if s['note']:
            lines.append(s['note'])
        if not isinstance(rt, str):
            lines.append('Попробуй короче или по-английски.')
        _edit(bot, chat_id, msg_id, '\n'.join(lines))


# ── экраны ────────────────────────────────────────────────────────────────────

def _home_button(token: str, text: str = '↩️ К результатам') -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f'sr:h_{token}')


def _close_button() -> InlineKeyboardButton:
    return InlineKeyboardButton('✖️ Закрыть', callback_data='sr:x')


def _show_home(bot, chat_id: int, msg_id: int, token: str, s: dict) -> None:
    with s['lock']:
        s['view'] = 'home'
        lines = [f'🔎 <b>{html.escape(s["query"])}</b>']
        kb = InlineKeyboardMarkup()
        if s['shows']:
            lines += ['', '📺 <b>LostFilm</b> — выбрать сезон, серию или скачать целиком']
        for i, show in enumerate(s['shows']):
            kb.add(InlineKeyboardButton(f'📺 {series.display(show)}'[:60], callback_data=f'sr:o_{token}_{i}'))
        rt = s['rt']
        if s['use_rt']:
            if rt is None:
                lines += ['', '🧲 <b>RuTracker</b> — ⏳ ещё ищу…']
            elif isinstance(rt, str):
                lines += ['', f'🧲 <b>RuTracker</b> — ⚠️ {html.escape(rt)}']
            elif rt:
                n = rt_ui._plural(len(rt), 'раздача', 'раздачи', 'раздач')
                lines += ['', f'🧲 <b>RuTracker</b> — {n}, от разных релизёров']
                kb.add(InlineKeyboardButton(f'🧲 RuTracker: {n}', callback_data=f'sr:r_{token}'))
            else:
                lines += ['', '🧲 <b>RuTracker</b> — ничего не нашлось']
        if s['note']:
            lines += ['', html.escape(s['note'])]
        kb.add(_close_button())
        _edit(bot, chat_id, msg_id, '\n'.join(lines), kb)


def _show_rt(bot, chat_id: int, msg_id: int, token: str, s: dict, back: bool) -> None:
    rt = s['rt']
    if not rt or isinstance(rt, str):
        return
    with s['lock']:
        s['view'] = 'rt'
        if not s['rt_token']:
            s['rt_token'] = rt_ui.add_results(s['query'], rt, f'sr:h_{token}' if back else '', s['note'])
    rt_ui.show_page(bot, chat_id, msg_id, s['rt_token'], 0)


def _info(s: dict, i: int) -> dict:
    info = s['info'].get(i)
    if not info:
        info = s['info'][i] = lostfilm.series_info(s['shows'][i]['link'])
    return info


def _season_eps(info: dict, season: int) -> list[dict]:
    return sorted((e for e in info['episodes'] if e['season'] == season), key=lambda e: e['episode'])


def _season_name(season: int) -> str:
    return 'Доп. материалы' if season == series.SPECIALS else f'Сезон {season}'


def _eps(n: int) -> str:
    return rt_ui._plural(n, 'серия', 'серии', 'серий')


def _show_show(bot, chat_id: int, msg_id: int, token: str, s: dict, i: int) -> None:
    show = s['shows'][i]
    name = series.display(show)
    with s['lock']:
        s['view'] = 'show'
    kb = InlineKeyboardMarkup(row_width=2)
    home = len(s['shows']) > 1 or bool(s['use_rt'] and s['rt'] != [])
    tail = [_home_button(token)] if home else []
    try:
        info = _info(s, i)
    except Exception as e:
        kb.row(*tail, _close_button())
        _edit(bot, chat_id, msg_id, html.escape(f'⚠️ Не удалось открыть «{name}» на LostFilm: {e}'), kb)
        return
    have = series.on_disk(show)
    seasons = sorted({e['season'] for e in info['episodes']})
    real = [n for n in seasons if n != series.SPECIALS]
    lines = [f'📺 <b>{html.escape(name)}</b>']
    if not seasons:
        lines.append('Вышедших серий пока нет.')
    else:
        total = [e for e in info['episodes'] if e['season'] != series.SPECIALS]
        got = sum((e['season'], e['episode']) in have for e in total)
        lines.append(f"Сезонов: {len(real)} · {_eps(len(total))}" + (f' · на диске {got}' if got else ''))
        lines += ['', 'Выбери сезон — там можно взять и одну серию.']
        buttons = []
        for n in reversed(seasons):  # новые сезоны сверху
            eps = _season_eps(info, n)
            got_n = sum((n, e['episode']) in have for e in eps)
            mark = ' ✅' if got_n == len(eps) else f' · есть {got_n}/{len(eps)}' if got_n else f' · {len(eps)}'
            buttons.append(InlineKeyboardButton(_season_name(n) + mark, callback_data=f'sr:s_{token}_{i}_{n}'))
        kb.add(*buttons[:_MAX_BUTTONS])
        if len(real) > 1 and got < len(total):
            text = (f'⬇️ Весь сериал — {_eps(len(total))}' if not got
                    else f'⬇️ Докачать недостающее — {_eps(len(total) - got)}')
            kb.row(InlineKeyboardButton(text, callback_data=f'sr:a_{token}_{i}_0_m'))
    if series.is_subscribed(show):
        lines.append('🔔 Подписка есть: новые серии скачаются сами.')
    else:
        kb.row(series_ui.subscribe_button(show, '🔔 Подписаться на новые серии'))
    kb.row(*tail, _close_button())
    _edit(bot, chat_id, msg_id, '\n'.join(lines), kb)


def _show_season(bot, chat_id: int, msg_id: int, token: str, s: dict, i: int, season: int) -> None:
    show = s['shows'][i]
    with s['lock']:
        s['view'] = 'season'
    info = _info(s, i)
    eps = _season_eps(info, season)
    have = series.on_disk(show)
    got = sum((season, e['episode']) in have for e in eps)
    lines = [f'📺 <b>{html.escape(series.display(show))}</b> — {_season_name(season).lower()}', '']
    used = 0
    for e in eps:
        mark = ' ✅' if (season, e['episode']) in have else ''
        line = f"{e['episode']}. {html.escape(e['name'][:60] or 'без названия')}{mark}"
        used += len(line) + 1
        if used > 3300:
            lines.append('…')
            break
        lines.append(line)
    lines += ['', 'Нажми номер серии или возьми сезон целиком.' + (' ✅ — уже на диске.' if got else '')]

    kb = InlineKeyboardMarkup(row_width=5)
    kb.add(*[InlineKeyboardButton(f"{e['episode']}{'✅' if (season, e['episode']) in have else ''}",
                                  callback_data=f"sr:a_{token}_{i}_{season}_{e['episode']}")
             for e in eps[:_MAX_BUTTONS]])
    if 0 < got < len(eps):
        kb.row(InlineKeyboardButton(f'⬇️ Недостающие — {_eps(len(eps) - got)}',
                                    callback_data=f'sr:a_{token}_{i}_{season}_m'))
    if len(eps) > 1:
        kb.row(InlineKeyboardButton(('⬇️ Всё' if season == series.SPECIALS else '⬇️ Весь сезон') + f' — {_eps(len(eps))}' + (' (уже есть)' if got == len(eps) else ''),
                                    callback_data=f'sr:a_{token}_{i}_{season}_p'))
    kb.row(InlineKeyboardButton('↩️ К сезонам', callback_data=f'sr:o_{token}_{i}'), _close_button())
    _edit(bot, chat_id, msg_id, '\n'.join(lines), kb)


def _jobs(s: dict, i: int, season: int, what: str) -> list[dict]:
    """what: номер серии, p — весь сезон, m — только недостающее (season 0 — весь сериал)."""
    info = _info(s, i)
    if what.isdigit():
        return [e for e in info['episodes'] if (e['season'], e['episode']) == (season, int(what))]
    have = series.on_disk(s['shows'][i]) if what == 'm' else set()
    return series.jobs_for(info, have, season)


def _what_text(season: int, what: str, jobs: list[dict]) -> str:
    if what.isdigit():
        ep = jobs[0]
        return series.code(season, ep['episode']) + (f" «{ep['name']}»" if ep.get('name') else '')
    if season:
        return _season_name(season).lower() + (' — недостающие серии' if what == 'm' else ' целиком')
    return 'весь сериал (чего ещё нет на диске)'


def _show_quality(bot, chat_id: int, msg_id: int, token: str, s: dict, i: int, season: int, what: str) -> None:
    show = s['shows'][i]
    with s['lock']:
        s['view'] = 'quality'
    kb = InlineKeyboardMarkup()
    back = (InlineKeyboardButton('↩️ К сезону', callback_data=f'sr:s_{token}_{i}_{season}') if season
            else InlineKeyboardButton('↩️ К сезонам', callback_data=f'sr:o_{token}_{i}'))
    title = f'📺 <b>{html.escape(series.display(show))}</b>'
    try:
        jobs = _jobs(s, i, season, what)
        if not jobs:
            kb.row(back, _close_button())
            _edit(bot, chat_id, msg_id, f'{title}\n✅ Всё это уже есть на диске.', kb)
            return
        what_text = html.escape(_what_text(season, what, jobs))
        _edit(bot, chat_id, msg_id, f'{title}\n{what_text}\n\n⏳ Смотрю, в каком качестве есть…')
        sizes, exact = series.size_options(_info(s, i), jobs)
    except lostfilm.NeedLogin:
        kb.row(InlineKeyboardButton('🔑 Войти на LostFilm', callback_data='ser:login'))
        kb.row(back, _close_button())
        _edit(bot, chat_id, msg_id, f'{title}\n🔑 Вход на LostFilm слетел — войди заново и нажми «Назад».', kb)
        return
    except Exception as e:
        logger.warning(f'LostFilm releases failed ({show["show"]}): {e}')
        kb.row(back, _close_button())
        _edit(bot, chat_id, msg_id, f'{title}\n⚠️ {html.escape(str(e))}', kb)
        return

    with s['lock']:
        s['offer'] = {'key': (i, season, what), 'jobs': jobs, 'sizes': sizes}
    wish = config.SERIES_QUALITY.strip().lower()
    preferred = next((q for q in sizes if wish and q.lower().startswith(wish)), '')
    for q, size in sizes.items():
        text = ('⭐ ' if q == preferred else '') + q
        if size:
            text += f" · {'' if exact else '≈ '}{rt_ui._size(size)}"
        kb.row(InlineKeyboardButton(text, callback_data=f'sr:g_{token}_{i}_{season}_{what}_{q}'))
    kb.row(back, _close_button())
    lines = [title, what_text, '', f'Какое качество? Свободно на диске: {rt_ui._size(_free())}']
    if len(jobs) > 1:
        lines.append(f'Раздач будет до {len(jobs)} — Transmission возьмёт их по очереди.')
    _edit(bot, chat_id, msg_id, '\n'.join(lines), kb)


def _free() -> int:
    try:
        return psutil.disk_usage(config.SHARED_FOLDER).free
    except OSError:
        return 0


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    kind, *args = call.data[len('sr:'):].split('_')
    if kind == 'x':
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        return

    token = args[0] if args else ''
    with _lock:
        s = _sessions.get(token)
    if not s:
        bot.answer_callback_query(call.id, _STALE, show_alert=True)
        return
    try:
        nums = [int(a) for a in args[1:3]]
        if nums:
            s['shows'][nums[0]]
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, _STALE, show_alert=True)
        return

    if kind == 'g':
        _go(bot, call, s, token, nums[0], nums[1], args[3], args[4])
        return
    bot.answer_callback_query(call.id)
    with s['lock']:
        s['view'] = kind  # чтобы досчитавшийся поиск RuTracker не перерисовал экран
    if kind == 'h':
        _show_home(bot, chat_id, msg_id, token, s)
    elif kind == 'r':
        _show_rt(bot, chat_id, msg_id, token, s, back=True)
    elif kind == 'o':
        _thread(_guard, _show_show, bot, chat_id, msg_id, token, s, nums[0])
    elif kind == 's':
        _thread(_guard, _show_season, bot, chat_id, msg_id, token, s, nums[0], nums[1])
    elif kind == 'a':
        _thread(_guard, _show_quality, bot, chat_id, msg_id, token, s, nums[0], nums[1], args[3])


def _guard(screen, bot, chat_id: int, msg_id: int, token: str, s: dict, *args) -> None:
    try:
        screen(bot, chat_id, msg_id, token, s, *args)
    except Exception as e:
        logger.warning(f'Search screen failed: {e}')
        kb = InlineKeyboardMarkup()
        kb.row(_home_button(token), _close_button())
        _edit(bot, chat_id, msg_id, f'⚠️ {html.escape(str(e))}', kb)


def _go(bot, call, s: dict, token: str, i: int, season: int, what: str, quality: str) -> None:
    show = s['shows'][i]
    with s['lock']:
        offer = s['offer']
        if not offer or offer['key'] != (i, season, what):
            bot.answer_callback_query(call.id, 'Уже запустил')  # второе нажатие той же кнопки
            return
        size = offer['sizes'].get(quality, 0)
        free = _free()
        if size and free and size > free:
            bot.answer_callback_query(call.id, f'Не хватит места: нужно {rt_ui._size(size)}, '
                                               f'свободно {rt_ui._size(free)}', show_alert=True)
            return
        s['offer'] = None
        s['view'] = 'go'
    bot.answer_callback_query(call.id, 'Качаю…')
    what_text = _what_text(season, what, offer['jobs'])
    audit(call.from_user, 'LOSTFILM_DOWNLOAD', f"{show['show']}: {what_text}, {quality}")
    kb = InlineKeyboardMarkup()
    if season:
        kb.row(InlineKeyboardButton('↩️ Ещё из этого сезона', callback_data=f'sr:s_{token}_{i}_{season}'))
    kb.row(InlineKeyboardButton('↩️ К сезонам', callback_data=f'sr:o_{token}_{i}'), _close_button())
    _edit(bot, call.message.chat.id, call.message.message_id,
          f'📺 <b>{html.escape(series.display(show))}</b>\n'
          f'🚀 Ставлю на загрузку: {html.escape(what_text)}, {quality}', kb)
    _thread(lambda: series.download_season(bot, call.message.chat.id, show, offer['jobs'],
                                           quality=quality, manual=True), name='SeriesDownload')


def _edit(bot, chat_id: int, message_id: int, text: str, kb=None) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb, parse_mode='HTML')
    except Exception as e:
        if 'message is not modified' not in str(e):
            logger.warning(f'edit_message_text failed: {e}')
