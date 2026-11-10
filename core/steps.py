"""Ожидание ответа на вопрос бота — с ограничением по времени.

Telegram-бот ждёт «следующее сообщение» бессрочно: нажал «Создать папку»,
не ответил, а через день написал «Матильда» для поиска — бот создал бы
папку «Матильда». Здесь ответ, пришедший позже STEP_TTL, не считается
ответом и обрабатывается как обычное сообщение (поиск, команда, magnet).
"""
import time

STEP_TTL = 300  # 5 минут


def wait_reply(bot, message, handler, ttl: int = STEP_TTL) -> None:
    """Как bot.register_next_step_handler, но с таймаутом.

    Прежние ожидания в этом чате сбрасываются: иначе следующее сообщение
    попало бы во все накопившиеся вопросы сразу.
    """
    asked = time.time()

    def step(reply):
        if time.time() - asked > ttl:
            bot.process_new_messages([reply])  # вопрос устарел — обычное сообщение
            return
        handler(reply)

    bot.clear_step_handler_by_chat_id(message.chat.id)
    bot.register_next_step_handler(message, step)
