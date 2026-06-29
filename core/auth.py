from core import config


def is_authorized(user_id: int) -> bool:
    return user_id in config.CREATOR_IDS
