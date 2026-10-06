#!/usr/bin/env python3
"""Поиск по RuTracker: ставит FlareSolverr — настоящий Chrome в Docker, которым
бот проходит проверку Cloudflare на RuTracker.

    sudo python3 setup_rutracker.py

  • ставит Docker, если его нет (пакет docker.io);
  • запускает контейнер flaresolverr: автозапуск, доступен только с этого
    сервера (127.0.0.1:8191), в простое ~60 МБ памяти;
  • повторный запуск обновляет FlareSolverr до последней версии.

Логин и пароль RuTracker вводятся в боте: /rutracker (или их спросит install.py).
Только стандартная библиотека — запускается системным python3.
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

IMAGE = 'ghcr.io/flaresolverr/flaresolverr:latest'
NAME = 'flaresolverr'
PORT = 8191
OLD_UNIT = Path('/etc/systemd/system/flaresolverr.service')  # от прежней установки с Jackett
ENV_FILE = Path(__file__).resolve().parent / '.env'

G, Y, R, B, RS = '\033[92m', '\033[93m', '\033[91m', '\033[1m', '\033[0m'


def ok(msg: str):   print(f'  {G}✓{RS}  {msg}')
def warn(msg: str): print(f'  {Y}⚠{RS}  {msg}')


def run(cmd: list, check: bool = True):
    print(f'  $ {" ".join(cmd)}')
    return subprocess.run(cmd, check=check)


def ensure_docker() -> None:
    if shutil.which('docker'):
        ok('Docker уже установлен')
    else:
        run(['apt-get', 'update', '-q'])
        run(['apt-get', 'install', '-y', 'docker.io'])
        ok('Docker установлен')
    run(['systemctl', 'enable', '--now', 'docker'], check=False)


def remove_old_service() -> None:
    """FlareSolverr без Docker (ставился вместе с Jackett) занимает тот же порт."""
    if OLD_UNIT.exists():
        run(['systemctl', 'disable', '--now', 'flaresolverr'], check=False)
        OLD_UNIT.unlink()
        run(['systemctl', 'daemon-reload'], check=False)
        ok('Старая служба flaresolverr (без Docker) удалена')


def start_container() -> None:
    run(['docker', 'pull', IMAGE])
    subprocess.run(['docker', 'rm', '-f', NAME], capture_output=True)  # прежний — заменить свежим
    run(['docker', 'run', '-d', '--name', NAME, '--restart', 'unless-stopped',
         # сеть хоста: браузер ходит через SOCKS-мост бота на 127.0.0.1;
         # HOST=127.0.0.1 — снаружи сервера FlareSolverr недоступен
         '--network', 'host', '-e', 'HOST=127.0.0.1', '-e', f'PORT={PORT}',
         '-e', 'LOG_LEVEL=info', IMAGE])


def wait_ready(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/', timeout=5) as r:
                if b'FlareSolverr is ready' in r.read():
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def has_proxy() -> bool:
    try:
        return any(line.startswith('PROXY_URL=') and line.split('=', 1)[1].strip()
                   for line in ENV_FILE.read_text().splitlines())
    except OSError:
        return False


def install() -> bool:
    """Поставить/обновить FlareSolverr. True — запущен и отвечает."""
    ensure_docker()
    remove_old_service()
    start_container()
    if not wait_ready():
        warn(f'FlareSolverr не ответил — посмотри: docker logs {NAME}')
        return False
    ok(f'FlareSolverr работает: 127.0.0.1:{PORT} (контейнер {NAME}, автозапуск)')
    if not has_proxy():
        warn('В России RuTracker заблокирован — нужен зарубежный прокси: PROXY_URL в .env')
    return True


def main() -> None:
    if os.geteuid() != 0:
        print(f'\n{R}Запусти от root:{RS}  sudo python3 setup_rutracker.py\n')
        sys.exit(1)
    print(f'\n{B}Поиск по RuTracker — установка FlareSolverr (Docker){RS}\n')
    success = install()
    print(f"""
  {B}Дальше — в Telegram:{RS}
    /rutracker — логин и пароль RuTracker (в личке с ботом, только админ)
    потом просто напиши боту название фильма или сериала
""")
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
