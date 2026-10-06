#!/usr/bin/env python3
"""
DispeR Media Server — FlareSolverr для RuTracker (и других сайтов с проверкой «я не робот»).
Запуск от root ПОСЛЕ setup_jackett.py:  sudo python3 setup_flaresolverr.py

Проблема: RuTracker показывает JS-проверку (как «Just a moment…» у Cloudflare),
браузер её проходит, а Jackett — нет. FlareSolverr проходит её настоящим Chrome.
Но Chrome не умеет SOCKS5-прокси с паролем, поэтому ставим локальный мост:

  Jackett + FlareSolverr ─► мост 127.0.0.1:1081 (без пароля)
                            ─► PROXY_URL из .env (с паролем) ─► трекеры

Что делает скрипт:
  1. Ставит Xvfb (нужен FlareSolverr) и FlareSolverr в /opt/flaresolverr.
  2. Запускает сервисы socks-bridge и flaresolverr (оба слушают только 127.0.0.1).
  3. Переключает Jackett на мост и подключает к нему FlareSolverr.
  4. Проверяет, что FlareSolverr открывает RuTracker.
На внешнем прокси/VPN-сервере ничего настраивать не нужно.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from setup_jackett import (B, BD, G, PROJECT_DIR, R, RS, bot_user, config_path,
                           err, info, ok, read_env, run, warn)

FS_DIR = Path('/opt/flaresolverr')
FS_URL = ('https://github.com/FlareSolverr/FlareSolverr/releases/latest/download/'
          'flaresolverr_linux_x64.tar.gz')
BRIDGE_PORT = 1081
FS_PORT = 8191


def write_unit(name: str, body: str) -> None:
    Path(f'/etc/systemd/system/{name}.service').write_text(body)


def install_packages() -> None:
    info("Ставлю Xvfb (виртуальный экран для Chrome)…")
    run(['apt-get', 'install', '-y', '-qq', 'xvfb'])
    ok("Xvfb установлен")


def install_flaresolverr(proxy: str) -> None:
    if (FS_DIR / 'flaresolverr').exists():
        ok("FlareSolverr уже скачан — пропускаю")
        return
    tarball = Path('/tmp/flaresolverr_linux_x64.tar.gz')
    info("Скачиваю FlareSolverr (~260 МБ, внутри свой Chrome)…")
    if run(['curl', '-fL', '--retry', '2', '-o', str(tarball), FS_URL], check=False).returncode != 0:
        if not proxy:
            raise SystemExit('Не удалось скачать FlareSolverr с GitHub.')
        warn("Напрямую не скачалось — пробую через прокси из .env")
        run(['curl', '-fL', '--retry', '2', '--proxy', proxy, '-o', str(tarball), FS_URL])
    run(['tar', '-xzf', str(tarball), '-C', '/opt'])
    tarball.unlink(missing_ok=True)
    ok("FlareSolverr распакован в /opt/flaresolverr")


def install_services(user: str, use_bridge: bool) -> None:
    python = PROJECT_DIR / 'venv' / 'bin' / 'python'
    if use_bridge:
        write_unit('socks-bridge', f"""[Unit]
Description=Local SOCKS5 bridge (no auth) to upstream proxy for FlareSolverr/Jackett
After=network-online.target
Wants=network-online.target

[Service]
User={user}
ExecStart={python} {PROJECT_DIR / 'tools' / 'socks_bridge.py'}
Environment=BRIDGE_HOST=127.0.0.1
Environment=BRIDGE_PORT={BRIDGE_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""")
    write_unit('flaresolverr', f"""[Unit]
Description=FlareSolverr (solves anti-bot challenges for Jackett)
After=network-online.target
Wants=network-online.target

[Service]
User={user}
WorkingDirectory={FS_DIR}
ExecStart={FS_DIR / 'flaresolverr'}
Environment=HOST=127.0.0.1
Environment=PORT={FS_PORT}
Environment=LOG_LEVEL=info
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
""")
    run(['chown', '-R', f'{user}:{user}', str(FS_DIR)])
    run(['systemctl', 'daemon-reload'])
    services = (['socks-bridge'] if use_bridge else []) + ['flaresolverr']
    for s in services:
        run(['systemctl', 'enable', '--now', s])
        run(['systemctl', 'restart', s])
    ok(f"Сервисы запущены: {', '.join(services)}")


def configure_jackett(user: str, use_bridge: bool) -> None:
    path = config_path(user)
    if not path.exists():
        raise SystemExit('Jackett не найден — сначала:  sudo python3 setup_jackett.py')
    run(['systemctl', 'stop', 'jackett'], check=False)
    time.sleep(2)
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if use_bridge:
        cfg.update({'ProxyType': 2, 'ProxyUrl': '127.0.0.1', 'ProxyPort': BRIDGE_PORT,
                    'ProxyUsername': '', 'ProxyPassword': ''})
    cfg['FlareSolverrUrl'] = f'http://127.0.0.1:{FS_PORT}'
    cfg['FlareSolverrMaxTimeout'] = 60000
    st = path.stat()
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chown(path, st.st_uid, st.st_gid)
    run(['systemctl', 'start', 'jackett'])
    ok("Jackett: прокси → мост, FlareSolverr подключён" if use_bridge
       else "Jackett: FlareSolverr подключён")


def wait_port(port: int, seconds: int = 90) -> bool:
    for _ in range(seconds):
        if run(['bash', '-c', f'</dev/tcp/127.0.0.1/{port}'], check=False, capture=True).returncode == 0:
            return True
        time.sleep(1)
    return False


def self_test(use_bridge: bool) -> None:
    if use_bridge:
        r = run(['curl', '-sS', '--max-time', '20', '--socks5-hostname', f'127.0.0.1:{BRIDGE_PORT}',
                 'https://api.ipify.org'], check=False, capture=True)
        if r.returncode == 0 and r.stdout.strip():
            ok(f"Мост работает, внешний IP: {r.stdout.strip()}")
        else:
            err(f"Мост не отвечает: {r.stderr.strip()[:200]}")
            info("Логи:  journalctl -u socks-bridge -n 30")
            return

    info("Жду запуска FlareSolverr…")
    if not wait_port(FS_PORT):
        err("FlareSolverr не запустился. Логи:  journalctl -u flaresolverr -n 50")
        return

    info("Проверяю: FlareSolverr открывает RuTracker (до минуты)…")
    payload = {'cmd': 'request.get', 'url': 'https://rutracker.org/forum/index.php',
               'maxTimeout': 60000}
    if use_bridge:
        payload['proxy'] = {'url': f'socks5://127.0.0.1:{BRIDGE_PORT}'}
    req = urllib.request.Request(f'http://127.0.0.1:{FS_PORT}/v1',
                                 data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
    except Exception as e:
        err(f"FlareSolverr не ответил: {e}")
        return
    status = data.get('solution', {}).get('status')
    if data.get('status') == 'ok' and status == 200:
        ok("RuTracker открывается через FlareSolverr — проверка «я не робот» пройдена")
    else:
        warn(f"Ответ FlareSolverr: {data.get('status')} / HTTP {status} — {data.get('message', '')[:200]}")
        info("Всё равно попробуй добавить RuTracker в Jackett; при ошибке пришли логи:")
        info("  journalctl -u flaresolverr -n 50")


def main():
    if os.geteuid() != 0:
        print(f"\n{R}Запусти от root:{RS}  sudo python3 setup_flaresolverr.py\n")
        sys.exit(1)
    print(f"\n{B}{BD}DispeR Media Server — FlareSolverr для RuTracker{RS}\n")

    proxy = read_env().get('PROXY_URL', '')
    use_bridge = bool(proxy)
    user = bot_user()
    info(f"Сервисы будут работать от пользователя: {user}")
    if use_bridge:
        info("Прокси из .env найден — ставлю локальный мост без пароля (только 127.0.0.1)")
    else:
        info("PROXY_URL не задан — FlareSolverr будет ходить напрямую")

    install_packages()
    install_flaresolverr(proxy)
    install_services(user, use_bridge)
    configure_jackett(user, use_bridge)
    self_test(use_bridge)

    print(f"""
{G}{'═' * 62}{RS}
{BD}{G}  ✅  Готово. Теперь добавь RuTracker в Jackett:{RS}
{G}{'═' * 62}{RS}
  1. Открой Jackett → «+ Add indexer» → RuTracker.org
  2. Введи логин и пароль от RuTracker.
     Если появится картинка капчи — введи текст с неё.
  3. OK → у индексатора нажми «Test», должно быть зелёным.

  Потом в боте: «Титаник» — в результатах появится [RuTracker].
""")


if __name__ == '__main__':
    main()
