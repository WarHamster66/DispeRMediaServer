"""Подписки на сериалы: /follow, /series и вход на LostFilm (/lostfilm)."""
import hashlib
import io
import logging
import secrets
import threading
import time

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.audit import audit
from core.auth import is_admin, is_authorized
from handlers import credentials
from services import lostfilm, series

logger = logging.getLogger(__name__)

# token → {shows, dl: {i: серия для кнопки «скачать»}, ts}
_picks: dict[str, dict] = {}
_lock = threading.Lock()
_TTL = 3600
_CAPTCHA_TRIES = 3


def register(bot) -> None:
    bot.message_handler(commands=['follow'])(lambda m: _cmd_follow(bot, m))
    bot.message_handler(commands=['series'])(lambda m: _cmd_series(bot, m))
    bot.message_handler(commands=['lostfilm'])(lambda m: _cmd_lostfilm(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('ser:'))(
        lambda c: _callback(bot, c)
    )


def start_login_reminder(bot) -> None:
    """После запуска (например, сразу после установки): аккаунт указан, а входа
    нет — прислать админам кнопку «Войти». Саму капчу не шлём: бот принял бы
    следующее сообщение (magnet, файл) за код с картинки."""
    _thread(_remind_login, bot, name='LostFilmReminder')


def _remind_login(bot) -> None:
    time.sleep(20)  # даём подняться сети
    if not lostfilm.is_configured() or lostfilm.logged_in_as():
        return
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton('🔑 Войти на LostFilm', callback_data='ser:login'))
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(admin_id, f'📺 LostFilm: аккаунт {config.LOSTFILM_EMAIL} указан, но вход '
                                       'не выполнен — без него сериалы не скачаются.', reply_markup=kb)
        except Exception as e:
            logger.warning(f'LostFilm login reminder to {admin_id} failed: {e}')


def _thread(target, *args, name: str = 'Series') -> None:
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


# ── /follow ───────────────────────────────────────────────────────────────────

def _cmd_follow(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) < 2:
        ask = bot.reply_to(message, 'На какой сериал подписаться? Напиши название — '
                                    'можно по-русски (например: Бункер или Silo).\n'
                                    'Новые серии с LostFilm будут скачиваться сами.')
        bot.clear_step_handler_by_chat_id(message.chat.id)
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
    status = bot.reply_to(message, f'📺 Ищу «{query}» на LostFilm…')
    _thread(_run_follow, bot, message, status, query, name='SeriesFollow')


def _run_follow(bot, message, status, query: str) -> None:
    chat_id = message.chat.id
    try:
        shows = series.find_shows(query)
    except Exception as e:
        _edit(bot, chat_id, status.message_id, f'⚠️ Не удалось поискать на LostFilm: {e}')
        return
    if not shows:
        _edit(bot, chat_id, status.message_id,
              f'🤷 На LostFilm нет «{query}». Попробуй другое название — русское или английское.')
        return

    token = secrets.token_hex(4)
    with _lock:
        now = time.time()
        for k, v in list(_picks.items()):
            if now - v['ts'] > _TTL:
                _picks.pop(k, None)
        _picks[token] = {'shows': shows, 'dl': {}, 'ts': now}

    if len(shows) == 1:
        _subscribe(bot, chat_id, status.message_id, message.from_user, token, 0)
        return

    kb = InlineKeyboardMarkup()
    for i, s in enumerate(shows[:10]):
        kb.add(InlineKeyboardButton(f'📺 {series.display(s)}'[:60], callback_data=f'ser:pick_{token}_{i}'))
    kb.add(InlineKeyboardButton('✖️ Отмена', callback_data='ser:close'))
    _edit(bot, chat_id, status.message_id, 'Нашлось несколько сериалов — какой?', kb)


def _quality_text() -> str:
    q = config.SERIES_QUALITY.strip()
    return 'любое' if not q else f'{q}p' if q.isdigit() else q


def _subscribe(bot, chat_id: int, message_id: int, user, token: str, i: int) -> None:
    with _lock:
        pick = _picks.get(token)
    if not pick or i >= len(pick['shows']):
        _edit(bot, chat_id, message_id, '⌛ Запрос устарел — повтори /follow')
        return
    s = pick['shows'][i]
    name = series.display(s)
    try:
        last = series.latest(s)
    except Exception as e:
        _edit(bot, chat_id, message_id, f'⚠️ Не удалось открыть «{name}» на LostFilm: {e}')
        return

    season, episode = (last['season'], last['episode']) if last else (0, 0)
    who = getattr(user, 'username', None) or str(user.id)
    is_new = series.subscribe(s, chat_id, season, episode, who)
    if is_new:
        audit(user, 'SERIES_FOLLOW', s['show'])
        lines = [f'✅ Подписка на «{name}»']
    else:
        # подписка уже есть — всё равно даём кнопку «скачать»: так можно
        # забрать последний сезон целиком, не отписываясь
        lines = [f'ℹ️ Подписка на «{name}» уже есть.']
    kb = None
    if last:
        label = series.code(last['season'], last['episode'])
        ep_name = f" «{last['name']}»" if last.get('name') else ''
        lines.append(f'Последняя серия на LostFilm: {label}{ep_name}')
        if last['pack']:
            dl = {**last, 'episode': series.PACK, 'name': ''}
            btn = f"⬇️ Скачать сезон {last['season']} целиком"
        else:
            dl = last
            btn = f'⬇️ Скачать {label} сейчас'
        with _lock:
            pick['dl'][i] = dl
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(btn, callback_data=f'ser:dl_{token}_{i}'))
    elif is_new:
        lines.append('Серий пока нет — скачаю первую, как только выйдет.')
    else:
        lines.append('Вышедших серий пока нет.')
    if is_new:
        lines.append(f'\nНовые серии буду качать сам в «{config.SERIES_FOLDER}» '
                     f'(качество {_quality_text()}) и сообщу сюда.')
    else:
        lines.append('\nСписок подписок: /series')
    if not lostfilm.logged_in_as():
        lines.append('\n' + series.login_hint())
    _edit(bot, chat_id, message_id, '\n'.join(lines), kb)


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
    who = lostfilm.logged_in_as()
    account = f'🔑 LostFilm: вход выполнен ({who})' if who else '⚠️ LostFilm: вход не выполнен — /lostfilm'
    if not subs:
        return (f'📺 Подписок пока нет.\nПодписаться: /follow Название сериала\n\n{account}', kb)
    lines = ['📺 Подписки на сериалы (LostFilm):\n']
    for s in subs:
        have = series.code(s['season'], s['episode']) if s['season'] else 'пока ничего'
        lines.append(f'• {series.display(s)} — есть до {have}')
        kb.add(InlineKeyboardButton(f"❌ Отписаться: {s['show'][:40]}",
                                    callback_data=f"ser:un_{_short_key(s['key'])}"))
    lines.append(f'\nПроверяю новинки каждые {config.SERIES_CHECK_MINUTES} мин.\n{account}')
    kb.add(InlineKeyboardButton('🔄 Проверить сейчас', callback_data='ser:check'))
    return '\n'.join(lines), kb


def _short_key(key: str) -> str:
    """Короткий id подписки для callback_data (лимит Telegram — 64 байта)."""
    return hashlib.md5(key.encode()).hexdigest()[:10]


# ── /lostfilm: аккаунт и вход с капчей ────────────────────────────────────────

def _cmd_lostfilm(bot, message) -> None:
    uid = message.from_user.id
    if not is_authorized(uid):
        bot.reply_to(message, 'Нет доступа.')
        return
    if not lostfilm.is_configured():
        if is_admin(uid):
            _ask_account(bot, message.chat.id, message.chat.type, uid)
        else:
            bot.reply_to(message, '🔑 Аккаунт LostFilm ещё не указан — это делает администратор: /lostfilm')
        return
    kb = InlineKeyboardMarkup()
    if is_admin(uid):
        kb.add(InlineKeyboardButton('✏️ Сменить аккаунт', callback_data='ser:acc'))
    who = lostfilm.logged_in_as()
    if who:
        kb.add(InlineKeyboardButton('🔄 Войти заново', callback_data='ser:login'))
        bot.reply_to(message, f'✅ LostFilm: вход выполнен ({who}).\nАккаунт: {config.LOSTFILM_EMAIL}',
                     reply_markup=kb)
        return
    if is_admin(uid):
        bot.reply_to(message, f'Аккаунт LostFilm: {config.LOSTFILM_EMAIL}', reply_markup=kb)
    _thread(_send_captcha, bot, message.chat.id, 1, name='LostFilmLogin')


def _ask_account(bot, chat_id: int, chat_type: str, user_id: int) -> None:
    """Спросить почту и пароль LostFilm и сохранить их в .env (только в личке)."""
    credentials.ask(
        bot, chat_id, chat_type, user_id, site='LostFilm',
        login_prompt='📧 Почта (логин) на LostFilm?',
        keys=('LOSTFILM_EMAIL', 'LOSTFILM_PASSWORD'),
        check=lambda s: None if '@' in s and ' ' not in s else 'Нужна почта вида name@mail.ru — пришли ещё раз:',
        comment='LostFilm — автоскачивание сериалов по подписке (вход в боте: /lostfilm)',
        on_saved=lambda m, login, changed, note: _account_saved(bot, m, login, changed, note))


def _account_saved(bot, message, email: str, changed: bool, note: str) -> None:
    chat_id = message.chat.id
    if changed:
        lostfilm.forget_session()  # прежний вход был под другим аккаунтом
    audit(message.from_user, 'LOSTFILM_ACCOUNT', email)
    if lostfilm.logged_in_as():
        bot.send_message(chat_id, f'✅ Аккаунт {email} сохранён. Вход уже выполнен.{note}')
        return
    bot.send_message(chat_id, f'✅ Аккаунт {email} сохранён в .env. Осталось ввести код с картинки.{note}')
    _thread(_send_captcha, bot, chat_id, 1, name='LostFilmLogin')


def _send_captcha(bot, chat_id: int, attempt: int) -> None:
    try:
        image = lostfilm.start_login()
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ LostFilm: {e}')
        return
    msg = bot.send_photo(chat_id, io.BytesIO(image),
                         caption='🔑 Вход в LostFilm: напиши в ответ код с картинки.')
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
        bot.reply_to(message, 'Нужен код с картинки текстом. Начать заново: /lostfilm')
        return
    try:
        name = lostfilm.finish_login(text)
    except lostfilm.BadCaptcha:
        if attempt < _CAPTCHA_TRIES:
            bot.reply_to(message, '❌ Код не подошёл — вот новая картинка.')
            _thread(_send_captcha, bot, message.chat.id, attempt + 1, name='LostFilmLogin')
        else:
            bot.reply_to(message, '❌ Код снова не подошёл. Попробуй позже: /lostfilm')
        return
    except lostfilm.BadCredentials:
        kb = None
        if is_admin(message.from_user.id):
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton('✏️ Ввести почту и пароль заново', callback_data='ser:acc'))
        bot.reply_to(message, f'❌ LostFilm: неверная почта или пароль ({config.LOSTFILM_EMAIL}).',
                     reply_markup=kb)
        return
    except Exception as e:
        bot.reply_to(message, f'⚠️ Не получилось войти в LostFilm: {e}')
        return

    audit(message.from_user, 'LOSTFILM_LOGIN', name)
    if series.list_subs():
        bot.reply_to(message, f'✅ Вошёл в LostFilm ({name}). Проверяю новые серии…')
        _thread(_run_check, bot, message.chat.id, name='SeriesCheck')
    else:
        bot.reply_to(message, f'✅ Вошёл в LostFilm ({name}). Подписаться на сериал: /follow')


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
        bot.answer_callback_query(call.id, 'Открываю сериал…')
        _thread(_subscribe, bot, chat_id, msg_id, call.from_user, token, int(i), name='SeriesFollow')

    elif data.startswith('dl_'):
        _, token, i = data.split('_', 2)
        with _lock:
            pick = _picks.get(token)
            ep = pick['dl'].get(int(i)) if pick else None
        if not ep:
            bot.answer_callback_query(call.id, 'Запрос устарел', show_alert=True)
            return
        bot.answer_callback_query(call.id, 'Качаю…')
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        _thread(series.download_episode, bot, chat_id, series.display(pick['shows'][int(i)]), ep,
                name='SeriesDownload')

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
        _thread(_run_check, bot, chat_id, name='SeriesCheck')

    elif data == 'login':
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
        except Exception:
            pass
        _thread(_send_captcha, bot, chat_id, 1, name='LostFilmLogin')

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


def _run_check(bot, chat_id: int) -> None:
    try:
        n = series.check_new(bot)
        if n:
            bot.send_message(chat_id, f'🔄 Готово: новых серий — {n}.')
        elif not lostfilm.logged_in_as():
            bot.send_message(chat_id, f'🔄 Проверил, но без входа ничего не скачаю.\n{series.login_hint()}')
        else:
            bot.send_message(chat_id, '🔄 Новых серий пока нет.')
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ Проверка не удалась: {e}')


def _edit(bot, chat_id: int, message_id: int, text: str, kb=None) -> None:
    try:
        bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
    except Exception as e:
        if 'message is not modified' not in str(e):
            logger.warning(f'edit_message_text failed: {e}')
