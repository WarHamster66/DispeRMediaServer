"""Подписки на сериалы: /follow, /series."""
import hashlib
import logging
import secrets
import threading
import time

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit
from core.auth import is_authorized
from services import jackett, series

logger = logging.getLogger(__name__)

# token → {shows, ts, chat_id}: варианты, когда по запросу нашлось несколько сериалов
_picks: dict[str, dict] = {}
_lock = threading.Lock()
_TTL = 3600


def register(bot) -> None:
    bot.message_handler(commands=['follow'])(lambda m: _cmd_follow(bot, m))
    bot.message_handler(commands=['series'])(lambda m: _cmd_series(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('ser:'))(
        lambda c: _callback(bot, c)
    )


# ── /follow ───────────────────────────────────────────────────────────────────

def _cmd_follow(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        ask = bot.reply_to(message, 'На какой сериал подписаться? Напиши название '
                                    '(например: Silo или Бункер).\n'
                                    'Новые серии с LostFilm будут скачиваться сами.')
        # Следующее сообщение — название сериала, а не общий поиск по трекерам
        bot.register_next_step_handler(ask, lambda m: _follow_from_reply(bot, m))
        return
    _start_follow(bot, message, parts[1].strip())


def _follow_from_reply(bot, message) -> None:
    text = (message.text or '').strip()
    if not text:
        return
    if text.startswith('/'):
        bot.process_new_messages([message])  # прислали другую команду — выполнить её
        return
    _start_follow(bot, message, text)


def _start_follow(bot, message, query: str) -> None:
    if not jackett.is_configured():
        bot.reply_to(message, '📺 Подписки работают через Jackett, а он не настроен.\n'
                              'На сервере: sudo python3 setup_jackett.py')
        return
    status = bot.reply_to(message, f'📺 Ищу «{query}» на LostFilm… (до минуты)')
    threading.Thread(target=_run_follow, args=(bot, message, status, query),
                     name='SeriesFollow', daemon=True).start()


def _run_follow(bot, message, status, query: str) -> None:
    chat_id = message.chat.id
    try:
        shows = series.find_shows(query)
    except Exception as e:
        _edit(bot, chat_id, status.message_id, f'⚠️ Не удалось поискать на LostFilm: {e}')
        return
    if not shows:
        _edit(bot, chat_id, status.message_id,
              f'🤷 На LostFilm не нашёл «{query}». Попробуй английское название.')
        return

    token = secrets.token_hex(4)
    with _lock:
        now = time.time()
        for k, v in list(_picks.items()):
            if now - v['ts'] > _TTL:
                _picks.pop(k, None)
        _picks[token] = {'shows': shows, 'ts': now}

    if len(shows) == 1:
        _do_subscribe(bot, chat_id, status.message_id, message.from_user, token, 0)
        return

    kb = InlineKeyboardMarkup()
    for i, s in enumerate(shows[:10]):
        kb.add(InlineKeyboardButton(f"📺 {s['show']}", callback_data=f'ser:pick_{token}_{i}'))
    kb.add(InlineKeyboardButton('✖️ Отмена', callback_data='ser:close'))
    _edit(bot, chat_id, status.message_id, 'Нашлось несколько сериалов — какой?', kb)


def _do_subscribe(bot, chat_id: int, message_id: int, user, token: str, i: int) -> None:
    with _lock:
        pick = _picks.get(token)
    if not pick or i >= len(pick['shows']):
        _edit(bot, chat_id, message_id, '⌛ Запрос устарел — повтори /follow')
        return
    s = pick['shows'][i]
    label = series.code(s['season'], s['episode'])
    who = getattr(user, 'username', None) or str(user.id)

    if not series.subscribe(s['show'], chat_id, s['season'], s['episode'], who):
        _edit(bot, chat_id, message_id, f"ℹ️ Подписка на «{s['show']}» уже есть. Список: /series")
        return
    audit(user, 'SERIES_FOLLOW', s['show'])

    size = s['result'].get('size') or 0
    size_txt = f' ({size / 1024 ** 3:.1f} ГБ)' if size else ''
    if s['pack']:
        latest = f"Последний вышедший — {label} (целиком)"
        btn = f'⬇️ Скачать {label} целиком{size_txt}'
        next_txt = f"Новые серии (с сезона {s['season'] + 1}) буду качать сам"
    else:
        latest = f"Последняя серия на LostFilm: {label}"
        btn = f'⬇️ Скачать {label} сейчас{size_txt}'
        next_txt = "Новые серии буду качать сам"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(btn, callback_data=f'ser:dl_{token}_{i}'))
    _edit(bot, chat_id, message_id,
          f"✅ Подписка на «{s['show']}»\n{latest}\n\n"
          f"{next_txt} в «{config.SERIES_FOLDER}» "
          f"(качество {config.SERIES_QUALITY or 'любое'}p) и сообщу сюда.",
          kb)


# ── /series ───────────────────────────────────────────────────────────────────

def _cmd_series(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    text, kb = _series_view()
    bot.reply_to(message, text, reply_markup=kb)


def _series_view() -> tuple[str, InlineKeyboardMarkup]:
    subs = series.list_subs()
    kb = InlineKeyboardMarkup()
    if not subs:
        return ('📺 Подписок пока нет.\nПодписаться: /follow Название сериала', kb)
    lines = ['📺 Подписки на сериалы (LostFilm):\n']
    for s in subs:
        lines.append(f"• {s['show']} — есть до {series.code(s['season'], s['episode'])}")
        kb.add(InlineKeyboardButton(f"❌ Отписаться: {s['show'][:40]}",
                                    callback_data=f"ser:un_{_short_key(s['key'])}"))
    lines.append(f"\nПроверяю новинки каждые {config.SERIES_CHECK_MINUTES} мин.")
    kb.add(InlineKeyboardButton('🔄 Проверить сейчас', callback_data='ser:check'))
    return '\n'.join(lines), kb


def _short_key(key: str) -> str:
    """Короткий id подписки для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    data = call.data[len('ser:'):]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == 'close':
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    elif data.startswith('pick_'):
        _, token, i = data.split('_', 2)
        bot.answer_callback_query(call.id)
        _do_subscribe(bot, chat_id, msg_id, call.from_user, token, int(i))

    elif data.startswith('dl_'):
        _, token, i = data.split('_', 2)
        with _lock:
            pick = _picks.get(token)
        if not pick or int(i) >= len(pick['shows']):
            bot.answer_callback_query(call.id, 'Запрос устарел', show_alert=True)
            return
        s = pick['shows'][int(i)]
        bot.answer_callback_query(call.id, 'Качаю…')
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        threading.Thread(target=series.download_episode,
                         args=(bot, chat_id, s['show'], s, s['result']),
                         name='SeriesDownload', daemon=True).start()

    elif data.startswith('un_'):
        short = data[len('un_'):]
        target = next((s for s in series.list_subs() if _short_key(s['key']) == short), None)
        if not target:
            bot.answer_callback_query(call.id, 'Уже отписан')
        else:
            series.unsubscribe(target['key'])
            audit(call.from_user, 'SERIES_UNFOLLOW', target['show'])
            bot.answer_callback_query(call.id, f"Отписался от «{target['show']}»")
        text, kb = _series_view()
        _edit(bot, chat_id, msg_id, text, kb)

    elif data == 'check':
        bot.answer_callback_query(call.id, 'Проверяю LostFilm…')
        threading.Thread(target=_run_check, args=(bot, chat_id),
                         name='SeriesCheck', daemon=True).start()

    else:
        bot.answer_callback_query(call.id, 'Неизвестное действие')


def _run_check(bot, chat_id: int) -> None:
    try:
        n = series.check_new(bot)
        bot.send_message(chat_id, f'🔄 Готово: новых серий — {n}.' if n
                         else '🔄 Новых серий пока нет.')
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Проверка не удалась: {e}')


def _edit(bot, chat_id: int, message_id: int, text: str, kb=None) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    except Exception as e:
        if 'message is not modified' not in str(e):
            logger.warning(f'edit_message_text failed: {e}')
