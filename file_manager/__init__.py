import hashlib
import os

from core import config

_registry: dict[str, str] = {}


def path_to_id(path: str) -> str:
    uid = hashlib.md5(path.encode()).hexdigest()[:10]
    _registry[uid] = path
    return uid


def id_to_path(uid: str) -> str | None:
    return _registry.get(uid)


def is_inside_share(path: str) -> bool:
    """Путь лежит внутри SHARED_FOLDER (защита от выхода за пределы шары).

    Сравниваем по компонентам пути, а не строкой: иначе '/media/media-server2'
    «начинается с» '/media/media-server' и проходит проверку.
    """
    base = os.path.realpath(config.SHARED_FOLDER)
    target = os.path.realpath(path)
    try:
        return os.path.commonpath([base, target]) == base
    except ValueError:  # разные корни/диски
        return False


def is_allowed(path: str) -> bool:
    """Путь относится к одной из разрешённых медиапапок (ALLOWED_FOLDERS)."""
    if not config.USE_ALLOWED_FOLDERS:
        return True
    if not is_inside_share(path):
        return False
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(config.SHARED_FOLDER))
    return rel.split(os.path.sep)[0] in config.ALLOWED_FOLDERS


PAGE_SIZE = 40  # у Telegram лимит ~100 кнопок на сообщение — берём с запасом


def page_of(items: list, page: int, size: int = PAGE_SIZE) -> tuple[list, int, int]:
    """Срез списка для страницы: (элементы, номер страницы, всего страниц)."""
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size:(page + 1) * size], page, pages


def nav_buttons(make_cb, page: int, pages: int) -> list:
    """Кнопки ⬅️ 2/5 ➡️; make_cb(n) — callback_data для страницы n. Пусто, если страница одна."""
    from telebot.types import InlineKeyboardButton
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton('⬅️', callback_data=make_cb(page - 1)))
    row.append(InlineKeyboardButton(f'{page + 1}/{pages}', callback_data=make_cb(page)))
    if page < pages - 1:
        row.append(InlineKeyboardButton('➡️', callback_data=make_cb(page + 1)))
    return row


def listdir_allowed(path: str, dirs_only: bool = False) -> list[str]:
    """Отсортированное содержимое папки, только разрешённые пути."""
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(path, name)
        if dirs_only and not os.path.isdir(full):
            continue
        if config.USE_ALLOWED_FOLDERS and not is_allowed(full):
            continue
        out.append(name)
    return out


def is_valid_name(name: str) -> bool:
    """Имя файла/папки без разделителей пути и спецзначений."""
    return (
        bool(name)
        and name not in ('.', '..')
        and '/' not in name
        and '\\' not in name
        and '\0' not in name
        and len(name.encode()) <= 255
    )
