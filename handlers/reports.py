"""Report commands: /report, /configure, daily report builder."""
import json
import logging
import os
from datetime import datetime

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from core import config
from core.auth import is_authorized
from services import (
    internet_speed,
    network_tracker,
    system_monitor,
    weather,
)

logger = logging.getLogger(__name__)

_DEFAULT_SETTINGS = {
    'current_time': True,
    'weather': True,
    'uptime': True,
    'internet_speed': False,
    'internal_ip': True,
    'external_ip': True,
    'wifi_signal': False,
    'cpu_percent': True,
    'cpu_temperature': True,
    'hdd_temperature': True,
    'memory_percent': True,
    'disk_info': True,
    'services': True,
    'logs': False,
    'network_usage': True,
}

_DEFAULT_TIME = '10:00'

_cfg: dict = {}  # loaded on first use


def get_report_config() -> dict:
    global _cfg
    if _cfg:
        return _cfg
    if os.path.exists(config.REPORT_CONFIG_FILE):
        try:
            with open(config.REPORT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                _cfg = json.load(f)
            # Fill missing keys from defaults
            _cfg.setdefault('REPORT_SETTINGS', {})
            for k, v in _DEFAULT_SETTINGS.items():
                _cfg['REPORT_SETTINGS'].setdefault(k, v)
            _cfg.setdefault('REPORT_TIME', _DEFAULT_TIME)
            return _cfg
        except Exception:
            pass
    _cfg = {'REPORT_SETTINGS': dict(_DEFAULT_SETTINGS), 'REPORT_TIME': _DEFAULT_TIME}
    _save_config()
    return _cfg


def _save_config() -> None:
    try:
        with open(config.REPORT_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(_cfg, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f'Could not save report config: {e}')


def build_report() -> str:
    cfg = get_report_config()
    s = cfg['REPORT_SETTINGS']
    parts = []

    if s.get('current_time'):
        parts.append(f"📋 Отчёт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    if s.get('weather'):
        parts.append(f"\n🌦️ Погода:\n{weather.get_weather()}\n")
    if s.get('uptime'):
        parts.append(f"\n{system_monitor.get_uptime()}\n")
    if s.get('internet_speed'):
        parts.append(f"\n{internet_speed.get_internet_speed()}\n")
    if s.get('internal_ip'):
        parts.append(f"\n{system_monitor.get_internal_ip()}\n")
    if s.get('external_ip'):
        parts.append(f"\n{system_monitor.get_external_ip()}\n")
    if s.get('wifi_signal'):
        parts.append(f"\n{system_monitor.get_wifi_signal()}\n")
    if s.get('cpu_percent'):
        parts.append(f"\n{system_monitor.get_cpu_load()}\n")
    if s.get('cpu_temperature'):
        parts.append(f"\n{system_monitor.get_cpu_temperature()}\n")
    if s.get('hdd_temperature'):
        parts.append(f"\n{system_monitor.get_hdd_temperature()}\n")
    if s.get('memory_percent'):
        parts.append(f"\n{system_monitor.get_memory_usage()}\n")
    if s.get('disk_info'):
        parts.append(f"\n{system_monitor.get_disk_usage()}\n")
    if s.get('services'):
        parts.append(f"\n📝 Службы:\n{system_monitor.get_services_status()}\n")
    if s.get('logs'):
        parts.append(f"\n📰 Логи бота:\n{system_monitor.get_last_logs()}\n")
    if s.get('network_usage'):
        parts.append(f"\n{network_tracker.get_network_usage()}\n")

    return ''.join(parts)


def register(bot) -> None:
    bot.message_handler(commands=['report'])(lambda m: _cmd_report(bot, m))
    bot.message_handler(commands=['configure'])(lambda m: _cmd_configure(bot, m))
    bot.callback_query_handler(func=lambda c: c.data.startswith('report:'))(
        lambda c: _callback(bot, c)
    )


# ── commands ──────────────────────────────────────────────────────────────────

def _cmd_report(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.send_chat_action(message.chat.id, 'typing')
    text = build_report()
    # Escape MarkdownV2 reserved chars
    for ch in r'\-=_*[]()~`>#+=|{}.!':
        text = text.replace(ch, f'\\{ch}')
    try:
        bot.reply_to(message, text, parse_mode='MarkdownV2')
    except Exception:
        bot.reply_to(message, build_report())  # fallback without markdown


def _cmd_configure(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, 'Нет доступа.')
        return
    bot.reply_to(message, 'Настройки отчёта:', reply_markup=_configure_keyboard())


def _configure_keyboard() -> InlineKeyboardMarkup:
    cfg = get_report_config()
    s = cfg['REPORT_SETTINGS']
    kb = InlineKeyboardMarkup()
    labels = {
        'current_time': 'Время',
        'weather': 'Погода',
        'uptime': 'Аптайм',
        'internet_speed': 'Speedtest',
        'internal_ip': 'Внутр. IP',
        'external_ip': 'Внеш. IP',
        'wifi_signal': 'Сеть',
        'cpu_percent': 'CPU %',
        'cpu_temperature': 'CPU °C',
        'hdd_temperature': 'HDD °C',
        'memory_percent': 'RAM',
        'disk_info': 'Диски',
        'services': 'Службы',
        'logs': 'Логи',
        'network_usage': 'Трафик',
    }
    for key, label in labels.items():
        icon = '✅' if s.get(key) else '❌'
        kb.add(InlineKeyboardButton(f'{icon} {label}', callback_data=f'report:toggle_{key}'))
    kb.add(InlineKeyboardButton(
        f"🕒 Время отчёта: {cfg['REPORT_TIME']}",
        callback_data='report:set_time',
    ))
    return kb


# ── callbacks ─────────────────────────────────────────────────────────────────

def _callback(bot, call) -> None:
    if not is_authorized(call.from_user.id):
        bot.answer_callback_query(call.id, 'Нет доступа')
        return

    data = call.data[len('report:'):]
    cfg = get_report_config()

    if data.startswith('toggle_'):
        key = data[7:]
        if key in cfg['REPORT_SETTINGS']:
            cfg['REPORT_SETTINGS'][key] = not cfg['REPORT_SETTINGS'][key]
            _save_config()
        try:
            bot.edit_message_reply_markup(
                call.message.chat.id, call.message.message_id,
                reply_markup=_configure_keyboard(),
            )
        except Exception:
            pass

    elif data == 'set_time':
        bot.edit_message_text(
            'Введите время отчёта (HH:MM):',
            call.message.chat.id, call.message.message_id,
        )
        bot.register_next_step_handler(call.message, lambda m: _step_set_time(bot, m))


def _step_set_time(bot, message) -> None:
    if not is_authorized(message.from_user.id):
        return
    try:
        parsed = datetime.strptime(message.text.strip(), '%H:%M')
        cfg = get_report_config()
        cfg['REPORT_TIME'] = parsed.strftime('%H:%M')
        _save_config()
        bot.reply_to(message, f"✅ Время отчёта: {cfg['REPORT_TIME']}")
        # Notify scheduler to pick up new time
        from scheduler import tasks as sched
        sched.reschedule_report()
    except ValueError:
        bot.reply_to(message, '❌ Неверный формат. Введите время как HH:MM')
