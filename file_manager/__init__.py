import hashlib

_registry: dict[str, str] = {}


def path_to_id(path: str) -> str:
    uid = hashlib.md5(path.encode()).hexdigest()[:10]
    _registry[uid] = path
    return uid


def id_to_path(uid: str) -> str | None:
    return _registry.get(uid)
