"""Поиск раздач по трекерам (через Jackett) и скачивание в один клик.

Использование:
  • в личке с ботом просто напиши название: «Титаник»;
  • или командой: /find Титаник (работает и в группах).
Нажал номер раздачи — дальше обычный путь: выбор папки, выбор серий, загрузка.
"""
import logging
import secrets
import threading
import time

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core.audit import audit
from core.auth import is_authorized
from services import jackett

logger = logging.getLogger(__name__)

_PER_PAGE = 10
_MAX_RESULTS = 300
_TTL = 2 * 3600  # результаты поиска живут 2 часа

# sid → {query, results, total, indexers, ts}
_searches: dict[str, dict] = {}
_lock = threading.Lock()


def register(bot) -> None:
    bot.message_handler(commands=['find'])(lambda m: _cmd_find(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('find:'))(
        lambda c: _callback(bot, c)
    )
    # Обычный текст в личке = поиск. Регистрируется ПОСЛЕДНИМ, чтобы не
    # перехватывать magnet-ссылки и команды. Ввод при переименовании файлов и
    # т.п. не пострадает: такие сообщения забирает пошаговый обработчик telebot.
    bot.message_handler(
        func=lambda m: (m.chat.type == 'private' and bool(m.text)
                        and not m.text.startswith('/')
                        and not m.text.strip().startswith('magnet:')),
        content_types=['text'],
    )(lambda m: _start_search(bot, m, m.text))


def _cmd_find(bot, message) -> None:
    query = (message.text or '').split(maxsplit=1)
    if len(query) < 2:
        if is_authorized(message.from_user.id):
            bot.reply_to(message, 'Что ищем? Например: /find Титаник\n'
                                  '(в личке можно просто написать название)')
        else:
            bot.reply_to(message, 'Нет доступа.')
        return
    _start_search(bot, message, query[1])


def _start_search(bot, message, query: str) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    query = query.strip()
    if len(query) < 2:
        return
    if not jackett.is_configured():
        bot.reply_to(message, '🔎 Поиск не настроен: нужен Jackett.\n'
                              'На сервере: sudo python3 setup_jackett.py')
        return
    status = bot.reply_to(message, f'🔎 Ищу «{query}» по трекерам… (до минуты)')
    # Поиск по нескольким трекерам через прокси может занять десятки секунд —
    # выполняем в отдельном потоке, чтобы бот не «зависал» для остальных.
    threading.Thread(target=_run_search, args=(bot, message, status, query),
                     name='JackettSearch', daemon=True).start()


def _run_search(bot, message, status, query: str) -> None:
    chat_id = message.chat.id
    try:
        results, indexers = jackett.search(query)
    except jackett.JackettError as e:
        _edit(bot, chat_id, status.message_id, f'⚠️ {e}')
        return
    except Exception as e:
        logger.error(f'Search failed: {e}', exc_info=True)
        _edit(bot, chat_id, status.message_id, f'⚠️ Ошибка поиска: {e}')
        return

    audit(message.from_user, 'SEARCH', f'«{query}» → {len(results)}')

    if not results:
        _edit(bot, chat_id, status.message_id,
              f'🤷 По запросу «{query}» ничего не нашлось.{_indexers_note(indexers)}')
        return

    # Сначала живые раздачи (есть сиды), внутри — самые скачиваемые
    results.sort(key=lambda r: (r['seeders'] > 0, r['grabs'], r['seeders']), reverse=True)
    sid = secrets.token_hex(4)
    now = time.time()
    with _lock:
        for k, v in list(_searches.items()):
            if now - v['ts'] > _TTL:
                _searches.pop(k, None)
        _searches[sid] = {'query': query, 'results': results[:_MAX_RESULTS],
                          'total': len(results), 'indexers': indexers, 'ts': now}
    _show_page(bot, chat_id, status.message_id, sid, 0)


# ── rendering ─────────────────────────────────────────────────────────────────

def _show_page(bot, chat_id: int, message_id: int, sid: str, page: int) -> None:
    with _lock:
        s = _searches.get(sid)
    if not s:
        _edit(bot, chat_id, message_id, '⌛ Результаты поиска устарели — поищи заново.')
        return

    results = s['results']
    pages = max(1, (len(results) + _PER_PAGE - 1) // _PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * _PER_PAGE

    lines = [f"🔎 «{s['query']}» — найдено {s['total']} (сначала популярные)\n"]
    for i, r in enumerate(results[start:start + _PER_PAGE], start=start + 1):
        title = r['title'] if len(r['title']) <= 180 else r['title'][:179] + '…'
        seeds = '🌱' if r['seeders'] > 0 else '💀'
        meta = ' · '.join(x for x in (
            r['category'], _size(r['size']), r['date'],
            f"💾 {_count(r['grabs'])}" if r['grabs'] else '',
            f"{seeds} {r['seeders']}/{r['leechers']}",
        ) if x)
        lines.append(f"{i}. [{r['tracker']}] {title}\n    {meta}\n")
    text = '\n'.join(lines) + _indexers_note(s['indexers'])

    kb = InlineKeyboardMarkup(row_width=5)
    nums = [InlineKeyboardButton(f'⬇️ {i}', callback_data=f'find:d_{sid}_{i - 1}')
            for i in range(start + 1, min(start + _PER_PAGE, len(results)) + 1)]
    for row in range(0, len(nums), 5):
        kb.row(*nums[row:row + 5])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f'find:p_{sid}_{page - 1}'))
    nav.append(InlineKeyboardButton(f'стр. {page + 1}/{pages}', callback_data=f'find:p_{sid}_{page}'))
    if page < pages - 1:
        nav.append(InlineKeyboardButton('▶️', callback_data=f'find:p_{sid}_{page + 1}'))
    kb.row(*nav)
    kb.row(InlineKeyboardButton('✖️ Закрыть', callback_data='find:close'))

    _edit(bot, chat_id, message_id, text, kb)


def _indexers_note(indexers: list[dict]) -> str:
    failed = [i['name'] for i in indexers if not i['ok']]
    return f"\n⚠️ Не ответили: {', '.join(failed)}" if failed else ''


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return
    data = call.data[len('find:'):]
    chat_id = call.message.chat.id

    if data == 'close':
        bot.answer_callback_query(call.id)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        return

    try:
        kind, sid, n = data.split('_', 2)
        n = int(n)
    except ValueError:
        bot.answer_callback_query(call.id, 'Неверные данные')
        return

    if kind == 'p':
        bot.answer_callback_query(call.id)
        _show_page(bot, chat_id, call.message.message_id, sid, n)
        return

    if kind == 'd':
        with _lock:
            s = _searches.get(sid)
        if not s or n >= len(s['results']):
            bot.answer_callback_query(call.id, 'Результаты устарели — поищи заново', show_alert=True)
            return
        result = s['results'][n]
        bot.answer_callback_query(call.id, f'Беру раздачу №{n + 1}…')
        threading.Thread(target=_download, args=(bot, call, result),
                         name='JackettFetch', daemon=True).start()
        return

    bot.answer_callback_query(call.id, 'Неизвестное действие')


def _download(bot, call, result: dict) -> None:
    """Скачать .torrent/magnet с трекера и передать в обычный поток добавления."""
    from handlers import torrent as torrent_handlers  # локально: избегаем циклического импорта
    chat_id = call.message.chat.id
    try:
        kind, payload = jackett.fetch(result)
    except Exception as e:
        bot.send_message(chat_id, f'⚠️ {e}\n{result["title"][:120]}')
        return
    try:
        if kind == 'torrent':
            torrent_handlers.offer_torrent_bytes(bot, chat_id, call.from_user, payload)
        else:
            torrent_handlers.offer_magnet(bot, chat_id, call.from_user, payload)
    except Exception as e:
        logger.error(f'Offer after search failed: {e}', exc_info=True)
        bot.send_message(chat_id, f'⚠️ Не удалось добавить торрент: {e}')


# ── helpers ───────────────────────────────────────────────────────────────────

def _edit(bot, chat_id: int, message_id: int, text: str, kb=None) -> None:
    try:
        bot.edit_message_text(text[:4000], chat_id, message_id, reply_markup=kb,
                              disable_web_page_preview=True)
    except Exception as e:
        if 'message is not modified' not in str(e):
            logger.warning(f'edit_message_text failed: {e}')


def _size(b: int) -> str:
    if b >= 1024 ** 3:
        return f'{b / 1024 ** 3:.2f} GB'
    if b >= 1024 ** 2:
        return f'{b / 1024 ** 2:.0f} MB'
    return f'{b / 1024:.0f} KB' if b else ''


def _count(n: int) -> str:
    return f'{n / 1000:.1f}K' if n >= 1000 else str(n)
