"""System metrics: CPU, RAM, disk, temperature, network, services, logs."""
import logging
import os
import re
import subprocess

import psutil

from core import config

logger = logging.getLogger(__name__)


def get_uptime() -> str:
    return f"⏱️ Время работы: {subprocess.getoutput('uptime -p')}"


def get_cpu_load() -> str:
    load = os.getloadavg()
    pct = load[0] * 100 / os.cpu_count()
    return f"💻 CPU: {pct:.1f}%"


def get_memory_usage() -> str:
    m = psutil.virtual_memory()
    return f"💾 RAM: {m.percent}% ({_fmt(m.used)} / {_fmt(m.total)})"


def get_disk_usage() -> str:
    main = psutil.disk_usage('/')
    ext = psutil.disk_usage(config.DISK_PATH)
    return (
        f"📂 Системный диск: {main.percent}% ({_fmt(main.used)} / {_fmt(main.total)})\n"
        f"📂 Медиадиск: {ext.percent}% ({_fmt(ext.used)} / {_fmt(ext.total)}, "
        f"свободно {_fmt(ext.free)})"
    )


def get_cpu_temperature() -> str:
    try:
        output = subprocess.check_output(['sensors'], stderr=subprocess.DEVNULL).decode()
        for line in output.splitlines():
            if 'temp1' in line:
                temp = float(line.split(':')[1].split()[0].replace('+', '').replace('°C', ''))
                if temp <= 50:
                    status = '✅ OK'
                elif temp <= 70:
                    status = '⚠️ WARNING'
                else:
                    status = '🔥 CRITICAL'
                return f"🌡️ CPU: {temp}°C ({status})"
        return "🌡️ CPU температура: недоступна"
    except Exception as e:
        return f"🌡️ CPU температура: ошибка ({e})"


def get_hdd_temperature() -> str:
    """Температура накопителя через lm-sensors (без root).

    Для NVMe в выводе `sensors` обычно есть строка 'Composite'. Для SATA-дисков
    с hwmon — 'temp1'. smartctl не используем, т.к. он требует root.
    """
    try:
        output = subprocess.check_output(['sensors'], stderr=subprocess.DEVNULL).decode()
    except Exception:
        return "💿 Температура диска: недоступна"

    for key, label in (('composite', 'NVMe'), ('drivetemp', 'HDD')):
        for line in output.splitlines():
            if key in line.lower() and '°c' in line.lower():
                m = re.search(r'([+-]?\d+\.?\d*)\s*°C', line)
                if m:
                    return f"💿 {label}: {float(m.group(1)):.0f}°C"
    return "💿 Температура диска: недоступна"


def get_wifi_signal() -> str:
    """Состояние сетевого подключения — WiFi или проводное (Ethernet)."""
    iface = subprocess.getoutput("ip route show default 2>/dev/null | awk '{print $5; exit}'").strip()
    if not iface:
        return "🌐 Сеть: нет подключения"

    # WiFi-интерфейс имеет каталог /sys/class/net/<iface>/wireless
    if os.path.isdir(f'/sys/class/net/{iface}/wireless'):
        raw = subprocess.getoutput(
            f"iwconfig {iface} 2>/dev/null | grep -i 'link quality' | awk -F'=' '{{print $2}}'"
        ).strip()
        return f"📶 WiFi ({iface}): {raw}" if raw else f"📶 WiFi: {iface}"

    speed = subprocess.getoutput(f"cat /sys/class/net/{iface}/speed 2>/dev/null").strip()
    state = subprocess.getoutput(f"cat /sys/class/net/{iface}/operstate 2>/dev/null").strip()
    if speed.lstrip('-').isdigit() and int(speed) > 0:
        return f"🔌 Сеть: проводное ({iface}), {speed} Мбит/с"
    return f"🔌 Сеть: {iface} ({state or 'активно'})"


def get_internal_ip() -> str:
    try:
        ip = subprocess.getoutput('hostname -I').split()[0]
        return f"🏠 Внутренний IP: {ip}"
    except Exception as e:
        return f"🏠 Внутренний IP: ошибка ({e})"


def get_external_ip() -> str:
    try:
        import requests
        ip = requests.get('https://api.ipify.org', timeout=10).text
        return f"🌍 Внешний IP: {ip}"
    except Exception as e:
        return f"🌍 Внешний IP: ошибка ({e})"


def get_last_logs(lines: int = 8) -> str:
    """Последние события самого бота (загрузки, ошибки) из его лог-файла."""
    try:
        with open(config.LOG_FILE, encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-lines:]
        return ''.join(tail).strip() or 'Лог пуст'
    except FileNotFoundError:
        return 'Лог-файл ещё не создан'
    except Exception as e:
        return f'Лог недоступен: {e}'


def get_services_status() -> str:
    raw = subprocess.getoutput(
        "systemctl list-units --type=service --state=running | awk '{print $1 \" - \" $3}'"
    )
    lines = [l for l in raw.splitlines() if any(s in l for s in config.CUSTOM_SERVICES)]
    return '\n'.join(lines) if lines else 'Нет запущенных отслеживаемых служб'


def get_media_stats() -> str:
    """Count files and total size per media category."""
    parts = []
    for folder_name in config.ALLOWED_FOLDERS:
        folder_path = os.path.join(config.SHARED_FOLDER, folder_name)
        if not os.path.isdir(folder_path):
            continue
        count = 0
        total = 0
        for root, _, files in os.walk(folder_path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                    count += 1
                except OSError:
                    pass
        parts.append(f"📁 {folder_name}: {count} файлов, {_fmt(total)}")
    return '\n'.join(parts) if parts else 'Нет данных'


def _fmt(b: int) -> str:
    gb = b / 1024 ** 3
    if gb >= 1024:
        return f"{gb / 1024:.1f} ТБ"
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    mb = b / 1024 ** 2
    return f"{mb:.0f} МБ"
