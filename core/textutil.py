"""Текстовые утилиты для Telegram."""

TG_LIMIT = 4000  # у Telegram лимит 4096 символов на сообщение — берём с запасом


def chunks(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Разбить длинный текст на части по строкам, не превышая лимит Telegram."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ''
    for line in text.split('\n'):
        while len(line) > limit:  # одна строка длиннее лимита — режем жёстко
            if current:
                parts.append(current)
                current = ''
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f'{current}\n{line}' if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def send_long(bot, chat_id: int, text: str, reply_to: int | None = None) -> None:
    """Отправить текст любой длины (первая часть — ответом на сообщение)."""
    for i, part in enumerate(chunks(text)):
        bot.send_message(chat_id, part, reply_to_message_id=reply_to if i == 0 else None)
