"""Поиск по RuTracker: обычный текст в личке или /find, и вход на сайт (/rutracker)."""
import html
import io
import logging
import secrets
import threading
import time
from datetime import datetime

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit
from core.auth import is_admin, is_authorized
from handlers import credentials, torrent
from services import rutracker

logger = logging.getLogger(__name__)

# token → {query, results, ts}
_searches: dict[str, dict] = {}
_lock = threading.Lock()
_PER_PAGE = 8
_TTL = 2 * 3600
_CAPTCHA_TRIES = 3


def register(bot) -> None:
    """Регистрировать ПОСЛЕДНИМ: обычный текст в личке здесь = поиск, поэтому
    magnet-ссылки, команды и ответы на вопросы бота должны поймать раньше."""
    bot.message_handler(commands=['find'])(lambda m: _cmd_find(bot, m))
    bot.message_handler(commands=['rutracker'])(lambda m: _cmd_rutracker(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('rt:'))(lambda c: _callback(bot, c))
    bot.message_handler(func=_is_search_text, content_types=['text'])(lambda m: _start_search(bot, m, m.text))


def _is_search_text(m) -> bool:
    text = (m.text or '').strip()
    return (m.chat.type == 'private' and bool(text) and not text.startswith(('/', 'magnet:'))
            and rutracker.is_configured() and is_authorized(m.from_user.id))


def _thread(target, *args, name: str = 'RuTracker') -> None:
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


def _need_login_text() -> str:
    if not rutracker.is_configured():
        return ('🔎 Поиск по RuTracker ещё не настроен.\n'
                'Администратор: /rutracker — указать аккаунт (в личке с ботом).')
    return '🔑 Нужно войти на RuTracker: /rutracker'


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
    _start_search(bot, message, parts[1])


def _start_search(bot, message, query: str) -> None:
    query = ' '.join(query.split())[:100]
    if not rutracker.logged_in_as():
        bot.reply_to(message, _need_login_text())
        return
    status = bot.reply_to(message, f'🔎 Ищу «{query}» на RuTracker…')
    _thread(_run_search, bot, message.chat.id, status.message_id, query, name='RuTrackerSearch')


def _run_search(bot, chat_id: int, message_id: int, query: str) -> None:
    try:
        results = rutracker.search(query)
    except rutracker.NeedLogin:
        _edit(bot, chat_id, message_id, _need_login_text())
        return
    except Exception as e:
        logger.warning(f'RuTracker search failed: {e}')
        _edit(bot, chat_id, message_id, f'⚠️ Поиск на RuTracker не удался: {e}')
        return
    if not results:
        _edit(bot, chat_id, message_id, f'🤷 На RuTracker ничего не нашлось по «{query}».\n'
                                        'Попробуй короче или по-английски.')
        return
    token = secrets.token_hex(4)
    with _lock:
        now = time.time()
        for k, v in list(_searches.items()):
            if now - v['ts'] > _TTL:
                _searches.pop(k, None)
        _searches[token] = {'query': query, 'results': results, 'ts': now}
    _show_page(bot, chat_id, message_id, token, 0)


def _show_page(bot, chat_id: int, message_id: int, token: str, page: int) -> None:
    with _lock:
        found = _searches.get(token)
    if not found:
        _edit(bot, chat_id, message_id, '⌛ Результаты устарели — повтори поиск.')
        return
    results = found['results']
    pages = (len(results) + _PER_PAGE - 1) // _PER_PAGE
    page = max(0, min(page, pages - 1))
    first = page * _PER_PAGE
    chunk = results[first:first + _PER_PAGE]

    lines = [f'🔎 <b>{html.escape(found["query"])}</b> — RuTracker, {_plural(len(results), "раздача", "раздачи", "раздач")}',
             '<i>Сверху — где больше раздающих: там скачается быстрее.</i>', '']
    for i, r in enumerate(chunk, start=first + 1):
        lines += [_format(i, r), '']
    lines.append('Нажми номер — скачаю торрент и спрошу, в какую папку.')

    kb = InlineKeyboardMarkup(row_width=4)
    kb.add(*[InlineKeyboardButton(f'⬇️ {i}', callback_data=f'rt:d_{token}_{i - 1}')
             for i in range(first + 1, first + len(chunk) + 1)])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton('◀️', callback_data=f'rt:p_{token}_{page - 1}'))
        nav.append(InlineKeyboardButton(f'стр. {page + 1}/{pages}', callback_data='rt:n'))
        if page < pages - 1:
            nav.append(InlineKeyboardButton('▶️', callback_data=f'rt:p_{token}_{page + 1}'))
        kb.row(*nav)
    kb.row(InlineKeyboardButton('✖️ Закрыть', callback_data='rt:x'))
    _edit(bot, chat_id, message_id, '\n'.join(lines), kb, html_mode=True)


def _format(i: int, r: dict) -> str:
    """Одна раздача — 4 строки: название, качество, размер/сиды/скорость, дата/раздел."""
    d = rutracker.describe(r['title'])
    name = d['name']
    if d['year'] and d['year'] not in name:
        name += f" ({d['year']})"
    if len(name) > 80:
        name = name[:77] + '…'
    quality = ' · '.join(x for x in (d['res'], d['source'], d['hdr'], d['audio']) if x) or 'качество не указано'
    tv = ', '.join(x for x in (f"сезон {d['season']}" if d['season'] else '',
                               f"серии {d['episodes']}" if d['episodes'] else '') if x)
    date = datetime.fromtimestamp(r['date']).strftime('%d.%m.%Y') if r['date'] else '—'
    forum = r['forum'] if len(r['forum']) <= 45 else r['forum'][:44] + '…'
    return '\n'.join([
        f'<b>{i}. {html.escape(name)}</b>',
        f'🎞 {html.escape(quality)}' + (f'  📺 {html.escape(tv)}' if tv else ''),
        f"📦 {_size(r['size'])} · 🟢 {_plural(r['seeders'], 'сид', 'сида', 'сидов')} · {rutracker.speed(r['seeders'])}",
        f'📅 {date} · {html.escape(forum)}',
    ])


def _size(n: int) -> str:
    if n >= 1024 ** 3:
        return f'{n / 1024 ** 3:.1f} ГБ'
    return f'{n / 1024 ** 2:.0f} МБ' if n else '? ГБ'


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f'{n} {word}'


def _download(bot, chat_id: int, message_id: int, user, token: str, idx: int) -> None:
    with _lock:
        found = _searches.get(token)
    if not found or idx >= len(found['results']):
        bot.send_message(chat_id, '⌛ Результаты устарели — повтори поиск.')
        return
    r = found['results'][idx]
    try:
        data = rutracker.download(r['id'])
    except rutracker.NeedLogin:
        bot.send_message(chat_id, _need_login_text())
        return
    except Exception as e:
        logger.warning(f'RuTracker .torrent download failed ({r["id"]}): {e}')
        try:  # запасной путь — magnet со страницы раздачи
            torrent.offer_magnet(bot, chat_id, user, rutracker.magnet(r['id']), reply_to=message_id)
        except Exception:
            bot.send_message(chat_id, f'⚠️ Не удалось скачать торрент: {e}')
        return
    torrent.offer_torrent_bytes(bot, chat_id, user, data, reply_to=message_id)


# ── /rutracker: аккаунт и вход ────────────────────────────────────────────────

def start_login_reminder(bot) -> None:
    """Аккаунт указан, а входа нет (например, сразу после установки) — прислать
    админам кнопку «Войти». Капчу сами не шлём: бот принял бы следующее
    сообщение за код с картинки."""
    _thread(_remind_login, bot, name='RuTrackerReminder')


def _remind_login(bot) -> None:
    time.sleep(25)  # даём подняться сети
    if not rutracker.is_configured() or rutracker.logged_in_as():
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('🔑 Войти на RuTracker', callback_data='rt:login'))
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, f'🔎 RuTracker: аккаунт {config.RUTRACKER_USER} указан, но вход '
                                       'не выполнен — без него поиск не работает.', reply_markup=kb)
        except Exception as e:
            logger.warning(f'RuTracker login reminder to {admin_id} failed: {e}')


def _cmd_rutracker(bot, message) -> None:
    uid = message.from_user.id
    if not is_authorized(uid):
        bot.reply_to(message, 'Нет доступа.')
        return
    if not rutracker.is_configured():
        if is_admin(uid):
            _ask_account(bot, message.chat.id, message.chat.type, uid)
        else:
            bot.reply_to(message, '🔑 Аккаунт RuTracker ещё не указан — это делает администратор: /rutracker')
        return
    kb = InlineKeyboardMarkup()
    if is_admin(uid):
        kb.add(InlineKeyboardButton('✏️ Сменить аккаунт', callback_data='rt:acc'))
    who = rutracker.logged_in_as()
    if who:
        kb.add(InlineKeyboardButton('🔄 Войти заново', callback_data='rt:login'))
        bot.reply_to(message, f'✅ RuTracker: вход выполнен ({who}).\n'
                              'Для поиска просто напиши боту название фильма или сериала.', reply_markup=kb)
        return
    if is_admin(uid):
        bot.reply_to(message, f'Аккаунт RuTracker: {config.RUTRACKER_USER}. Вхожу…', reply_markup=kb)
    _thread(_login_flow, bot, message.chat.id, message.from_user, 1, name='RuTrackerLogin')


def _ask_account(bot, chat_id: int, chat_type: str, user_id: int) -> None:
    credentials.ask(
        bot, chat_id, chat_type, user_id, site='RuTracker',
        login_prompt='👤 Логин (имя пользователя) на RuTracker?',
        keys=('RUTRACKER_USER', 'RUTRACKER_PASSWORD'),
        comment='RuTracker — поиск раздач в боте (вход: /rutracker)',
        on_saved=lambda m, login, changed, note: _account_saved(bot, m, login, changed, note))


def _account_saved(bot, message, login: str, changed: bool, note: str) -> None:
    chat_id = message.chat.id
    if changed:
        rutracker.forget_session()  # прежний вход был под другим аккаунтом
    audit(message.from_user, 'RUTRACKER_ACCOUNT', login)
    if rutracker.logged_in_as():
        bot.send_message(chat_id, f'✅ Аккаунт {login} сохранён. Вход уже выполнен.{note}')
        return
    bot.send_message(chat_id, f'✅ Аккаунт {login} сохранён в .env. Вхожу на RuTracker…{note}')
    _thread(_login_flow, bot, chat_id, message.from_user, 1, name='RuTrackerLogin')


def _login_flow(bot, chat_id: int, user, attempt: int) -> None:
    """Открыть вход; если сайт просит капчу — прислать картинку и ждать код."""
    try:
        image = rutracker.start_login()
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ RuTracker: {e}')
        return
    if image is None:
        _finish_login(bot, chat_id, user, '', attempt)  # капча не нужна
        return
    msg = bot.send_photo(chat_id, io.BytesIO(image),
                         caption='🔑 Вход на RuTracker: напиши в ответ код с картинки.')
    bot.clear_step_handler_by_chat_id(chat_id)
    bot.register_next_step_handler(msg, lambda m: _captcha_reply(bot, m, attempt))


def _captcha_reply(bot, message, attempt: int) -> None:
    text = (message.text or '').strip()
    if text.startswith('/'):
        bot.process_new_messages([message])  # передумал и прислал команду
        return
    if not is_authorized(message.from_user.id):
        return
    if not text:
        bot.reply_to(message, 'Нужен код с картинки текстом. Начать заново: /rutracker')
        return
    _finish_login(bot, message.chat.id, message.from_user, text, attempt)


def _finish_login(bot, chat_id: int, user, code: str, attempt: int) -> None:
    try:
        name = rutracker.finish_login(code)
    except rutracker.BadCaptcha as e:
        if attempt < _CAPTCHA_TRIES:
            bot.send_message(chat_id, f'❌ {e} — вот новая картинка.')
            _thread(_login_flow, bot, chat_id, user, attempt + 1, name='RuTrackerLogin')
        else:
            bot.send_message(chat_id, '❌ Код снова не подошёл. Попробуй позже: /rutracker')
        return
    except rutracker.BadCredentials as e:
        kb = None
        if user and is_admin(user.id):
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton('✏️ Ввести логин и пароль заново', callback_data='rt:acc'))
        bot.send_message(chat_id, f'❌ RuTracker: {e} ({config.RUTRACKER_USER}).', reply_markup=kb)
        return
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Не получилось войти на RuTracker: {e}')
        return
    if user:
        audit(user, 'RUTRACKER_LOGIN', name)
    bot.send_message(chat_id, f'✅ Вошёл на RuTracker ({name}).\n'
                              'Теперь просто напиши боту название фильма или сериала — найду.')


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    data = call.data[len('rt:'):]
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data.startswith('d_'):
        _, token, idx = data.split('_', 2)
        bot.answer_callback_query(call.id, 'Скачиваю торрент…')
        _thread(_download, bot, chat_id, msg_id, call.from_user, token, int(idx), name='RuTrackerDownload')
    elif data.startswith('p_'):
        _, token, page = data.split('_', 2)
        bot.answer_callback_query(call.id)
        _show_page(bot, chat_id, msg_id, token, int(page))
    elif data == 'n':
        bot.answer_callback_query(call.id)
    elif data == 'x':
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    elif data == 'login':
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        _thread(_login_flow, bot, chat_id, call.from_user, 1, name='RuTrackerLogin')
    elif data == 'acc':
        if not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, 'Только для администратора', show_alert=True)
            return
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        _ask_account(bot, chat_id, call.message.chat.type, call.from_user.id)
    else:
        bot.answer_callback_query(call.id, 'Неизвестное действие')


def _edit(bot, chat_id: int, message_id: int, text: str, kb=None, html_mode: bool = False) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb,
                              parse_mode='HTML' if html_mode else None)
    except Exception as e:
        if 'message is not modified' not in str(e):
            logger.warning(f'edit_message_text failed: {e}')
