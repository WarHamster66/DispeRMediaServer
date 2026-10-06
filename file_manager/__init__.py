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
