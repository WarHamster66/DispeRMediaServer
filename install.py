#!/usr/bin/env python3
"""
DispeR Media Server — Установщик
Запускай от root на сервере Ubuntu:  sudo python3 install.py
"""

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── ANSI цвета ─────────────────────────────────────────────────────────────────
R  = '\033[91m'; G  = '\033[92m'; Y  = '\033[93m'; B  = '\033[94m'
M  = '\033[95m'; C  = '\033[96m'; RS = '\033[0m';  BD = '\033[1m'

PROJECT_DIR  = Path(__file__).parent.resolve()
VENV_DIR     = PROJECT_DIR / 'venv'
VENV_PYTHON  = VENV_DIR / 'bin' / 'python'
VENV_PIP     = VENV_DIR / 'bin' / 'pip'
SERVICE_NAME = 'media-server'
SERVICE_FILE = Path(f'/etc/systemd/system/{SERVICE_NAME}.service')
TR_SETTINGS  = Path('/etc/transmission-daemon/settings.json')
SAMBA_CONF   = Path('/etc/samba/smb.conf')
MEDIA_DIRS   = ['Сериалы', 'Мультсериалы', 'Films', 'Torrent']
TOTAL        = 10


# ── UI helpers ─────────────────────────────────────────────────────────────────

def banner():
    print(f"""
{B}╔══════════════════════════════════════════════════════════╗
║        DispeR Media Server — Установщик v1.0            ║
║   Telegram · Transmission · Samba · Plex · Python       ║
╚══════════════════════════════════════════════════════════╝{RS}
  Проект: {PROJECT_DIR}
  Python:  {sys.version.split()[0]}
""")


def hdr(n: int, title: str):
    print(f"\n{BD}{B}[{n}/{TOTAL}] {title}{RS}")
    print("─" * 60)


def ok(msg: str):    print(f"  {G}✓{RS}  {msg}")
def info(msg: str):  print(f"  {C}→{RS}  {msg}")
def warn(msg: str):  print(f"  {Y}⚠{RS}  {msg}")
def err(msg: str):   print(f"  {R}✗{RS}  {msg}")


def ask(prompt: str, default: str = '') -> str:
    dflt = f'  [{default}]' if default else ''
    try:
        v = input(f"  {M}?{RS}  {prompt}{dflt}: ").strip()
        return v if v else default
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено."); sys.exit(0)


def ask_secret(prompt: str, default: str = '') -> str:
    try:
        v = getpass.getpass(f"  {M}?{RS}  {prompt}: ").strip()
        return v if v else default
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено."); sys.exit(0)


def ask_bool(prompt: str, default: bool = True) -> bool:
    dflt = 'Y/n' if default else 'y/N'
    v = ask(f"{prompt} ({dflt})", '').lower()
    if not v:
        return default
    return v.startswith('y')


def run(cmd: list, check: bool = True, capture: bool = False, **kw):
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    return subprocess.run(cmd, check=check, **kw)


# ── 0. Предварительные проверки ────────────────────────────────────────────────

def preflight():
    if os.geteuid() != 0:
        print(f"\n{R}Запустите от root:{RS}  sudo python3 install.py\n")
        sys.exit(1)
    try:
        rel = Path('/etc/os-release').read_text().lower()
        if 'ubuntu' not in rel and 'debian' not in rel:
            warn("Скрипт оптимизирован для Ubuntu/Debian. Продолжайте осторожно.")
    except FileNotFoundError:
        warn("Не удалось определить ОС.")


# ── 1. Системные пакеты ────────────────────────────────────────────────────────

def step_packages():
    hdr(1, "Системные пакеты")

    # Без этих пакетов бот не заработает — ставим строго.
    required = [
        'python3', 'python3-pip', 'python3-venv',
        'transmission-daemon',
        'samba', 'samba-common-bin',
        'curl', 'gnupg',
    ]
    # Для мониторинга (температуры, wifi, сеть) — желательно, но не критично.
    # Имена пакетов отличаются между версиями Ubuntu, поэтому ставим по одному
    # и не роняем установку, если какого-то нет.
    optional = ['lm-sensors', 'smartmontools', 'net-tools', 'iw', 'wireless-tools']

    info("apt-get update…")
    run(['apt-get', 'update', '-qq'])

    info(f"Установка обязательных: {' '.join(required)}")
    run(['apt-get', 'install', '-y', '-qq'] + required)
    ok("Обязательные пакеты установлены")

    info("Установка пакетов мониторинга (необязательные)…")
    for pkg in optional:
        r = run(['apt-get', 'install', '-y', '-qq', pkg], check=False, capture=True)
        if r.returncode == 0:
            ok(f"  {pkg}")
        else:
            warn(f"  {pkg} недоступен в этой версии ОС — пропущен")


# ── 2. Plex Media Server ───────────────────────────────────────────────────────

def step_plex():
    hdr(2, "Plex Media Server")
    if Path('/usr/lib/plexmediaserver').exists():
        ok("Plex уже установлен — пропускаем")
        return
    if not ask_bool("Установить Plex Media Server?"):
        info("Plex пропущен (можно установить позже вручную)")
        return

    info("Добавляю официальный репозиторий Plex…")
    try:
        run(['bash', '-c',
             'curl -fsSL https://downloads.plex.tv/plex-keys/PlexSign.key'
             ' | gpg --dearmor > /usr/share/keyrings/plex-archive-keyring.gpg'])
        run(['bash', '-c',
             'echo "deb [signed-by=/usr/share/keyrings/plex-archive-keyring.gpg]'
             ' https://downloads.plex.tv/repo/deb public main"'
             ' > /etc/apt/sources.list.d/plexmediaserver.list'])
        run(['apt-get', 'update', '-qq'])
        run(['apt-get', 'install', '-y', '-qq', 'plexmediaserver'])
        run(['systemctl', 'enable', '--now', 'plexmediaserver'])
        ok("Plex установлен и запущен")
        info("Веб-интерфейс Plex: http://localhost:32400/web")
    except subprocess.CalledProcessError as e:
        err(f"Не удалось установить Plex: {e}")
        info("Попробуй вручную: https://www.plex.tv/media-server-downloads/")


# ── 3. Диски и директории ──────────────────────────────────────────────────────

_PSEUDO_FS = {
    'proc', 'sysfs', 'tmpfs', 'devtmpfs', 'devpts', 'cgroup', 'cgroup2',
    'overlay', 'squashfs', 'autofs', 'mqueue', 'debugfs', 'tracefs',
    'securityfs', 'pstore', 'bpf', 'configfs', 'fusectl', 'hugetlbfs',
    'ramfs', 'efivarfs', 'binfmt_misc', 'nsfs', 'fuse.snapfuse',
}


def _human(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _list_disks() -> list:
    """Real mounted filesystems with free space, using stdlib only (psutil not yet installed)."""
    disks = []
    seen = set()
    try:
        lines = Path('/proc/mounts').read_text().splitlines()
    except FileNotFoundError:
        return disks
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace('\\040', ' '), parts[2]
        if fstype in _PSEUDO_FS or not dev.startswith('/dev/'):
            continue
        if mnt in seen:
            continue
        try:
            u = shutil.disk_usage(mnt)
        except OSError:
            continue
        seen.add(mnt)
        disks.append({'dev': dev, 'mnt': mnt, 'fstype': fstype,
                      'total': u.total, 'free': u.free})
    # Most free space first
    disks.sort(key=lambda d: d['free'], reverse=True)
    return disks


def step_disk() -> dict:
    hdr(3, "Медиадиск и директории")

    disks = _list_disks()
    shared = ''

    if disks:
        info("Обнаруженные диски (отсортированы по свободному месту):")
        print()
        print(f"       {'#':<3}{'Точка монтирования':<28}{'Своб.':>10}{'Всего':>10}{'  ФС':<8}")
        print(f"       {'─' * 64}")
        for i, d in enumerate(disks, 1):
            print(f"       {i:<3}{d['mnt']:<28}{_human(d['free']):>10}"
                  f"{_human(d['total']):>10}  {d['fstype']}")
        print()

        # Рекомендуем диск с наибольшим свободным местом, кроме корневого если есть выбор
        recommended = disks[0]
        non_root = [d for d in disks if d['mnt'] != '/']
        if non_root and non_root[0]['free'] > recommended['free'] * 0.5:
            recommended = non_root[0]
        info(f"Рекомендуется: {recommended['mnt']} "
             f"({_human(recommended['free'])} свободно)")
        print()

        choice = ask(
            "Выбери диск по номеру или впиши свой путь",
            recommended['mnt'],
        )
        # Если ввели число — берём из списка
        if choice.isdigit() and 1 <= int(choice) <= len(disks):
            shared = disks[int(choice) - 1]['mnt']
        else:
            shared = choice
        # Если выбрали голую точку монтирования диска — кладём медиа в подпапку
        if shared in {d['mnt'] for d in disks} and shared != '/':
            sub = ask("Подпапка на этом диске для медиа", 'Media')
            if sub.strip():
                shared = str(Path(shared) / sub)
    else:
        warn("Не удалось автоматически определить диски — введите путь вручную.")
        shared = ask("Путь к медиапапке (корень шары)", '/media/media-server')

    # Не создаём медиапапки прямо в корне ФС
    if shared.rstrip('/') == '':
        warn("Корень файловой системы не годится для медиа.")
        shared = ask("Путь к медиапапке (корень шары)", '/media/media-server')

    dl_dir = ask("Папка для торрентов", f'{shared.rstrip("/")}/Torrent')

    # Предупреждаем, если на выбранном диске мало места
    try:
        free = shutil.disk_usage(shared if Path(shared).exists()
                                 else str(Path(shared).parent)).free
        if free < 5 * 1024 ** 3:
            warn(f"На выбранном диске мало места: {_human(free)} свободно")
    except OSError:
        pass

    # Создаём директории
    Path(shared).mkdir(parents=True, exist_ok=True)
    ok(f"Создана корневая папка: {shared}")
    for d in MEDIA_DIRS:
        p = Path(shared) / d
        p.mkdir(exist_ok=True)
        ok(f"  {p}")

    # Общие права на всю медиапапку (см. _setup_media_permissions)
    _setup_media_permissions(shared, dl_dir)

    return {'SHARED_FOLDER': shared, 'DISK_PATH': shared, 'DOWNLOAD_DIR': dl_dir}


def _setup_media_permissions(shared: str, download_dir: str) -> None:
    """Единые права на медиапапку через общую группу 'media'.

    Transmission (debian-transmission), Plex (plex) и пользователь бота должны
    делить доступ: Transmission пишет торренты в любую из папок, Plex читает,
    бот переименовывает/удаляет. Делаем их членами группы 'media', а на дерево
    ставим setgid (2775) — тогда новые файлы наследуют группу и доступны всем троим.
    """
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    run(['groupadd', '-f', 'media'], check=False)
    for user in ('debian-transmission', 'plex', os.environ.get('SUDO_USER', '')):
        if user:
            run(['usermod', '-aG', 'media', user], check=False)
    run(['chown', '-R', 'root:media', shared], check=False)
    run(['chmod', '-R', '2775', shared], check=False)
    ok("Права настроены: группа 'media' (Transmission пишет, Plex читает, бот управляет)")


# ── 4. Transmission ────────────────────────────────────────────────────────────

def _find_apparmor_profile() -> Path | None:
    """Найти файл профиля AppArmor для transmission-daemon.

    Имя файла отличается между версиями Ubuntu: раньше было
    'usr.bin.transmission-daemon', в новых версиях — просто 'transmission-daemon'.
    Поэтому ищем по содержимому, а не по фиксированному имени.
    """
    base = Path('/etc/apparmor.d')
    if not base.exists():
        return None
    for name in ('transmission-daemon', 'usr.bin.transmission-daemon'):
        p = base / name
        if p.exists():
            return p
    try:
        for p in base.glob('*transmission*'):
            if p.is_file() and 'profile transmission-daemon' in p.read_text(errors='ignore'):
                return p
    except Exception:
        pass
    return None


def _configure_transmission_apparmor(disk: dict) -> None:
    """Разрешить transmission-daemon писать в медиапапку через AppArmor local-override.

    На Ubuntu профиль AppArmor разрешает запись только в стандартные каталоги и
    блокирует /media, /srv и т.п. Без этой правки торренты добавляются, но не
    качаются (apparmor="DENIED" в dmesg).
    """
    profile = _find_apparmor_profile()
    if profile is None:
        return  # transmission не под AppArmor на этой системе — ничего не делаем

    base = profile.name  # имя файла = имя local-override
    local = Path('/etc/apparmor.d/local') / base
    paths = {disk['SHARED_FOLDER'].rstrip('/'), disk['DOWNLOAD_DIR'].rstrip('/')}
    rules = '\n'.join(f"{p}/ rw,\n{p}/** rwk," for p in sorted(paths) if p)

    try:
        local.parent.mkdir(parents=True, exist_ok=True)
        existing = local.read_text() if local.exists() else ''
        if rules not in existing:
            local.write_text(
                existing.rstrip()
                + "\n\n# DispeR Media Server — доступ Transmission к медиапапке\n"
                + rules + "\n"
            )

        # Профиль обычно подключает local-override; если нет — добавим include
        prof_text = profile.read_text()
        if f'local/{base}' not in prof_text:
            idx = prof_text.rstrip().rfind('}')
            if idx != -1:
                prof_text = (prof_text[:idx]
                             + f'  include <local/{base}>\n'
                             + prof_text[idx:])
                profile.write_text(prof_text)

        run(['apparmor_parser', '-r', str(profile)], check=False)
        ok(f"AppArmor: запись в медиапапку разрешена (профиль {base})")
    except Exception as e:
        warn(f"AppArmor настроить не удалось: {e}")
        info("Если торренты встанут на паузу — см. раздел про AppArmor в README")


def step_transmission(disk: dict) -> dict:
    hdr(4, "Transmission RPC")

    tr_user = ask("Логин Transmission RPC", 'admin')
    tr_pass = ask_secret("Пароль Transmission RPC")
    if not tr_pass:
        tr_pass = 'changeme911'
        warn(f"Пароль не введён — используется: {tr_pass}")

    run(['systemctl', 'stop', 'transmission-daemon'], check=False)
    time.sleep(1)

    # Загружаем существующие настройки или создаём новые
    existing: dict = {}
    if TR_SETTINGS.exists():
        try:
            existing = json.loads(TR_SETTINGS.read_text())
        except Exception:
            pass

    existing.update({
        'download-dir':               disk['DOWNLOAD_DIR'],
        'incomplete-dir':             disk['DOWNLOAD_DIR'] + '/.incomplete',
        'incomplete-dir-enabled':     True,
        'rpc-enabled':                True,
        'rpc-port':                   9091,
        'rpc-authentication-required': True,
        'rpc-username':               tr_user,
        'rpc-password':               tr_pass,   # Transmission захэширует сам при старте
        'rpc-whitelist-enabled':      False,
        'rpc-bind-address':           '0.0.0.0',
        'rpc-host-whitelist-enabled': False,
        'ratio-limit-enabled':        False,
        'umask':                      2,
    })

    TR_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    TR_SETTINGS.write_text(json.dumps(existing, indent=4))
    run(['chown', 'debian-transmission:debian-transmission', str(TR_SETTINGS)], check=False)

    # Ubuntu ограничивает transmission-daemon профилем AppArmor, который запрещает
    # запись вне стандартных папок. Без этого торренты встают на паузу
    # (apparmor="DENIED" в dmesg). Разрешаем запись в выбранную медиапапку.
    _configure_transmission_apparmor(disk)

    run(['systemctl', 'enable', 'transmission-daemon'])
    run(['systemctl', 'start',  'transmission-daemon'])
    time.sleep(2)

    ok(f"Transmission запущен: RPC {tr_user}@localhost:9091")
    return {'TRANSMISSION_HOST': '127.0.0.1', 'TRANSMISSION_PORT': '9091',
            'TRANSMISSION_USER': tr_user, 'TRANSMISSION_PASSWORD': tr_pass}


# ── 5. Samba ───────────────────────────────────────────────────────────────────

def step_samba(disk: dict) -> dict:
    hdr(5, "Samba (сетевая шара)")

    share_name = ask("Имя шары в сети", 'Media')
    shared = disk['SHARED_FOLDER']

    # ── Пользователь для доступа ──
    # Гостевой доступ блокируется Windows 11 и многими ТВ, поэтому заводим
    # отдельного пользователя с паролем.
    sudo_user = os.environ.get('SUDO_USER', '')
    smb_user = ask("Логин для доступа к шаре (с него зайдёшь с ПК/ТВ)", sudo_user or 'media')
    smb_pass = ask_secret("Пароль для доступа к шаре")
    if not smb_pass:
        smb_pass = 'media1234'
        warn(f"Пароль не введён — используется: {smb_pass}")

    # Системный аккаунт нужен, чтобы Samba могла создать пользователя.
    # Если такого нет — создаём без права входа в систему.
    if run(['id', smb_user], check=False, capture=True).returncode != 0:
        run(['useradd', '--no-create-home', '--shell', '/usr/sbin/nologin', smb_user], check=False)
        info(f"Создан системный пользователь {smb_user}")

    # Заводим/обновляем Samba-пароль неинтерактивно и включаем пользователя.
    subprocess.run(['smbpasswd', '-a', '-s', smb_user],
                   input=f"{smb_pass}\n{smb_pass}\n", text=True, check=False)
    run(['smbpasswd', '-e', smb_user], check=False)
    ok(f"Samba-пользователь готов: {smb_user}")

    # Убираем старый блок с тем же именем если он уже есть
    conf_text = SAMBA_CONF.read_text() if SAMBA_CONF.exists() else ''
    conf_text = re.sub(
        rf'\[{re.escape(share_name)}\][^\[]*', '', conf_text, flags=re.DOTALL
    ).rstrip() + '\n'

    new_block = f"""
[{share_name}]
   comment = DispeR Media Server
   path = {shared}
   browseable = yes
   read only = no
   guest ok = no
   valid users = {smb_user}
   force user = nobody
   create mask = 0777
   directory mask = 0777
"""
    SAMBA_CONF.write_text(conf_text + new_block)

    run(['systemctl', 'enable', 'smbd', 'nmbd'])
    run(['systemctl', 'restart', 'smbd', 'nmbd'])

    ip = _local_ip()
    ok(f"Samba: \\\\{ip}\\{share_name}  →  {shared}")
    return {'share_name': share_name, 'smb_user': smb_user, 'smb_pass': smb_pass}


# ── 6. Python venv ─────────────────────────────────────────────────────────────

def step_python_env():
    hdr(6, "Python окружение")

    if not VENV_DIR.exists():
        info("Создаю virtualenv…")
        run([sys.executable, '-m', 'venv', str(VENV_DIR)])
        ok("venv создан")
    else:
        ok("venv уже существует")

    info("Устанавливаю зависимости из requirements.txt…")
    run([str(VENV_PIP), 'install', '--quiet', '--upgrade', 'pip'])
    req = PROJECT_DIR / 'requirements.txt'
    if req.exists():
        run([str(VENV_PIP), 'install', '--quiet', '-r', str(req)])
        ok("Все Python-пакеты установлены")
    else:
        warn("requirements.txt не найден — установи пакеты вручную")


# ── 7. .env файл ───────────────────────────────────────────────────────────────

def step_env(disk: dict, tr: dict) -> dict:
    hdr(7, "Конфигурация (.env)")

    # Загружаем существующий .env если есть
    env_path = PROJECT_DIR / '.env'
    cur: dict = {}
    if env_path.exists():
        warn(".env уже существует — существующие значения используются как подсказки")
        for line in env_path.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, _, v = line.partition('=')
                cur[k.strip()] = v.strip()

    print(f"\n  {BD}Telegram{RS}")
    print("  Токен — у @BotFather  |  Chat ID и User ID — у @userinfobot\n")

    token       = ask("Bot Token",                       cur.get('TELEGRAM_TOKEN', ''))
    chat_id     = ask("Chat ID (куда слать отчёты)",    cur.get('TELEGRAM_CHAT_ID', ''))
    creators    = ask("Creator ID через запятую",       cur.get('CREATOR_IDS', chat_id))

    print(f"\n  {BD}Погода{RS}")
    print("  Без API-ключа (Open-Meteo). Просто укажи свой город.\n")
    # Текущий город берём из config.json как подсказку
    cfg_path = PROJECT_DIR / 'config.json'
    cur_city = 'Москва'
    try:
        cur_city = json.loads(cfg_path.read_text(encoding='utf-8')).get('WEATHER_CITY', cur_city)
    except Exception:
        pass
    city = ask("Город для погоды", cur_city)

    print(f"\n  {BD}Прокси (необязательно){RS}")
    proxy       = ask("SOCKS5 URL (Enter — без прокси)", cur.get('PROXY_URL', ''))

    cfg = {
        'TELEGRAM_TOKEN':      token,
        'TELEGRAM_CHAT_ID':    chat_id,
        'CREATOR_IDS':         creators,
        'TRANSMISSION_HOST':   tr['TRANSMISSION_HOST'],
        'TRANSMISSION_PORT':   tr['TRANSMISSION_PORT'],
        'TRANSMISSION_USER':   tr['TRANSMISSION_USER'],
        'TRANSMISSION_PASSWORD': tr['TRANSMISSION_PASSWORD'],
        'PROXY_URL':           proxy,
    }

    content = '\n'.join([
        '# Telegram',
        f'TELEGRAM_TOKEN={cfg["TELEGRAM_TOKEN"]}',
        f'TELEGRAM_CHAT_ID={cfg["TELEGRAM_CHAT_ID"]}',
        f'CREATOR_IDS={cfg["CREATOR_IDS"]}',
        '',
        '# Transmission RPC',
        f'TRANSMISSION_HOST={cfg["TRANSMISSION_HOST"]}',
        f'TRANSMISSION_PORT={cfg["TRANSMISSION_PORT"]}',
        f'TRANSMISSION_USER={cfg["TRANSMISSION_USER"]}',
        f'TRANSMISSION_PASSWORD={cfg["TRANSMISSION_PASSWORD"]}',
        '',
        '# SOCKS5 proxy (leave empty if not needed)',
        f'PROXY_URL={cfg["PROXY_URL"]}',
        '',
    ])
    env_path.write_text(content)
    env_path.chmod(0o600)   # только owner может читать
    ok(f".env записан (права 600): {env_path}")

    # Обновляем config.json путями к диску и городом
    if cfg_path.exists():
        raw = json.loads(cfg_path.read_text(encoding='utf-8'))
        raw['DISK_PATH']     = disk['DISK_PATH']
        raw['SHARED_FOLDER'] = disk['SHARED_FOLDER']
        raw['DOWNLOAD_DIR']  = disk['DOWNLOAD_DIR']
        raw['WEATHER_CITY']  = city
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=4))
        ok("config.json обновлён")

    return cfg


# ── 8. Plex библиотека ─────────────────────────────────────────────────────────

def step_plex_library(disk: dict):
    hdr(8, "Plex — настройка медиатеки")

    if not Path('/usr/lib/plexmediaserver').exists():
        info("Plex не установлен — пропускаем настройку библиотеки")
        return

    ip = _local_ip()
    print(f"""
  Plex запущен. Чтобы добавить медиатеку:
  1. Открой в браузере: {B}http://{ip}:32400/web{RS}
  2. Войди в аккаунт Plex (или создай бесплатный)
  3. Добавь библиотеки:
       Фильмы  →  {disk['SHARED_FOLDER']}/Films
       Сериалы →  {disk['SHARED_FOLDER']}/Сериалы
       Мульты  →  {disk['SHARED_FOLDER']}/Мультсериалы
""")

    # Права уже настроены в step_disk через общую группу 'media' (см.
    # _setup_media_permissions). Plex входит в эту группу и читает медиапапку —
    # отдельный chown здесь не нужен (он бы сломал запись Transmission).
    run(['usermod', '-aG', 'plugdev', 'plex'], check=False)
    ok("Plex добавлен в группу для чтения медиапапки")


# ── 9. Systemd сервис ──────────────────────────────────────────────────────────

def step_systemd(env: dict):
    hdr(9, "Systemd автозапуск")

    sudo_user = os.environ.get('SUDO_USER', '')
    run_user  = ask("Пользователь для запуска бота", sudo_user or 'ubuntu')

    svc = f"""[Unit]
Description=DispeR Media Server Bot
After=network-online.target transmission-daemon.service
Wants=network-online.target

[Service]
Type=simple
User={run_user}
WorkingDirectory={PROJECT_DIR}
ExecStart={VENV_PYTHON} {PROJECT_DIR / 'main.py'}
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""
    SERVICE_FILE.write_text(svc)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', SERVICE_NAME])

    # Разрешаем боту перезапускать себя без пароля — для команды /update
    systemctl = shutil.which('systemctl') or '/usr/bin/systemctl'
    sudoers = Path('/etc/sudoers.d/media-server')
    sudoers.write_text(
        f'{run_user} ALL=(root) NOPASSWD: {systemctl} restart {SERVICE_NAME}\n'
    )
    sudoers.chmod(0o440)
    ok("Боту разрешён перезапуск через /update (sudoers)")

    if env.get('TELEGRAM_TOKEN') and env.get('TELEGRAM_CHAT_ID'):
        run(['systemctl', 'restart', SERVICE_NAME])
        time.sleep(3)
        r = run(['systemctl', 'is-active', SERVICE_NAME], capture=True)
        if r.stdout.strip() == 'active':
            ok(f"Сервис {SERVICE_NAME} запущен и добавлен в автозапуск")
        else:
            warn(f"Сервис стартовал с проблемами.")
            info(f"Проверь: journalctl -u {SERVICE_NAME} -n 30")
    else:
        warn("Telegram токен не введён — сервис не запущен.")
        info(f"После заполнения .env запусти:  systemctl start {SERVICE_NAME}")


# ── 10. Статический IP ──────────────────────────────────────────────────────────

def _detect_network() -> dict:
    """Активный интерфейс, текущий IP/префикс и шлюз."""
    net = {'iface': '', 'ip': '', 'prefix': '24', 'gw': ''}
    r = run(['ip', 'route', 'show', 'default'], capture=True)
    if r.returncode == 0 and r.stdout.strip():
        parts = r.stdout.split()
        if 'via' in parts:
            net['gw'] = parts[parts.index('via') + 1]
        if 'dev' in parts:
            net['iface'] = parts[parts.index('dev') + 1]
    if net['iface']:
        r2 = run(['ip', '-o', '-4', 'addr', 'show', 'dev', net['iface']], capture=True)
        if r2.returncode == 0:
            for tok in r2.stdout.split():
                if '/' in tok and tok.count('.') == 3:
                    net['ip'], net['prefix'] = tok.split('/')
                    break
    return net


def _apply_static_nmcli(iface, ip_, prefix, gw, dns):
    r = run(['nmcli', '-t', '-f', 'NAME,DEVICE', 'connection', 'show', '--active'], capture=True)
    con = ''
    for line in r.stdout.splitlines():
        name, _, dev = line.partition(':')
        if dev == iface:
            con = name
            break
    if not con:
        raise RuntimeError(f"активное соединение для {iface} не найдено")
    run(['nmcli', 'con', 'mod', con,
         'ipv4.addresses', f'{ip_}/{prefix}',
         'ipv4.gateway', gw,
         'ipv4.dns', dns.replace(',', ' '),
         'ipv4.method', 'manual'])
    run(['nmcli', 'con', 'up', con], check=False)  # может оборвать SSH при смене IP


def _apply_static_netplan(iface, ip_, prefix, gw, dns):
    dns_list = ', '.join(d.strip() for d in dns.split(',') if d.strip())
    cfg = f"""network:
  version: 2
  renderer: networkd
  ethernets:
    {iface}:
      dhcp4: no
      addresses: [{ip_}/{prefix}]
      routes:
        - to: default
          via: {gw}
      nameservers:
        addresses: [{dns_list}]
"""
    path = Path('/etc/netplan/99-disper-static.yaml')
    path.write_text(cfg)
    path.chmod(0o600)
    run(['netplan', 'apply'], check=False)  # может оборвать SSH при смене IP


def step_static_ip():
    hdr(10, "Статический IP (рекомендуется)")
    net = _detect_network()
    if not net['iface'] or not net['ip']:
        warn("Не удалось определить сеть — пропускаю настройку IP")
        return

    info(f"Интерфейс:  {net['iface']}")
    info(f"Текущий IP: {net['ip']}/{net['prefix']}")
    info(f"Шлюз:       {net['gw'] or 'не определён'}")
    print()
    warn("Если зайти SSH'ом и сменить IP на ДРУГОЙ — соединение оборвётся.")
    warn("Тот же IP оставить безопасно (просто закрепим за сервером).")
    print()

    if not ask_bool("Закрепить статический IP?", True):
        info("Оставлено получение IP по DHCP")
        return

    new_ip = ask("IP адрес", net['ip'])
    prefix = ask("Префикс маски (24 = 255.255.255.0)", net['prefix'])
    gw     = ask("Шлюз (роутер)", net['gw'])
    dns    = ask("DNS через запятую", f"{gw},8.8.8.8" if gw else "8.8.8.8")

    nm = run(['systemctl', 'is-active', 'NetworkManager'], capture=True).stdout.strip() == 'active'
    try:
        if nm:
            info("Сеть управляется NetworkManager — применяю через nmcli")
            _apply_static_nmcli(net['iface'], new_ip, prefix, gw, dns)
        else:
            info("Применяю через netplan")
            _apply_static_netplan(net['iface'], new_ip, prefix, gw, dns)
        ok(f"Статический IP назначен: {new_ip}/{prefix}")
        if new_ip != net['ip']:
            warn(f"IP изменён на {new_ip} — переподключайся по новому адресу.")
    except Exception as e:
        err(f"Не удалось назначить статический IP: {e}")
        info("Можно настроить вручную или сделать DHCP-резервацию на роутере")


# ── Итог ───────────────────────────────────────────────────────────────────────

def summary(disk: dict, samba: dict | None = None):
    ip = _local_ip()
    samba = samba or {'share_name': 'Media', 'smb_user': '—', 'smb_pass': '—'}
    print(f"""
{G}{'═' * 60}{RS}
{BD}{G}  ✅  Установка завершена!{RS}
{G}{'═' * 60}{RS}

  {BD}Ссылки:{RS}
    Transmission веб:  http://{ip}:9091
    Plex веб:          http://{ip}:32400/web

  {BD}Сетевая папка (Windows / ТВ / телефон):{RS}
    Адрес (Windows):   \\\\{ip}\\{samba['share_name']}
    Адрес (ТВ/прочее): smb://{ip}/{samba['share_name']}
    Логин:             {BD}{samba['smb_user']}{RS}
    Пароль:            {BD}{samba['smb_pass']}{RS}

  {BD}Управление ботом:{RS}
    systemctl status  {SERVICE_NAME}
    systemctl restart {SERVICE_NAME}
    journalctl -u {SERVICE_NAME} -f

  {BD}Медиапапки:{RS}
    {disk['SHARED_FOLDER']}/Films
    {disk['SHARED_FOLDER']}/Сериалы
    {disk['SHARED_FOLDER']}/Мультсериалы
    {disk['SHARED_FOLDER']}/Torrent   ← сюда качает Transmission

  {BD}Если нужно поправить токен/пароль:{RS}
    nano {PROJECT_DIR}/.env
    systemctl restart {SERVICE_NAME}

""")


def _local_ip() -> str:
    r = run(['hostname', '-I'], capture=True)
    parts = r.stdout.strip().split() if r.returncode == 0 else []
    return parts[0] if parts else '<SERVER_IP>'


# ── Точка входа ────────────────────────────────────────────────────────────────

def main():
    banner()
    preflight()

    if not ask_bool("Начать установку?"):
        print("Отменено."); sys.exit(0)

    step_packages()
    step_plex()
    disk = step_disk()
    tr   = step_transmission(disk)
    samba = step_samba(disk)
    step_python_env()
    env  = step_env(disk, tr)
    step_plex_library(disk)
    step_systemd(env)
    step_static_ip()
    summary(disk, samba)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Y}Прервано.{RS}")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        err(f"Команда завершилась с ошибкой: {e.cmd}")
        err(f"Код: {e.returncode}")
        sys.exit(1)
