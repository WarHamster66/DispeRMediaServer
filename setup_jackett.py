#!/usr/bin/env python3
"""
DispeR Media Server — установка Jackett (поиск по трекерам + подписки на сериалы).
Запуск от root:  sudo python3 setup_jackett.py

Что делает:
  1. Скачивает Jackett (официальный релиз с GitHub) в /opt/Jackett.
  2. Ставит его systemd-сервисом от имени пользователя бота.
  3. Включает доступ из локальной сети и прописывает прокси из .env (PROXY_URL),
     если он задан: в РФ RuTracker, Kinozal и др. заблокированы.
  4. Записывает API-ключ Jackett в .env бота (JACKETT_API_KEY) и перезапускает бота.

Логины от трекеров в бот НЕ попадают — их вводят в веб-интерфейсе Jackett.
"""
import json
import os
import platform
import pwd
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

R = '\033[91m'; G = '\033[92m'; Y = '\033[93m'; B = '\033[94m'; RS = '\033[0m'; BD = '\033[1m'

PROJECT_DIR = Path(__file__).parent.resolve()
ENV_PATH = PROJECT_DIR / '.env'
JACKETT_DIR = Path('/opt/Jackett')
RELEASES = 'https://github.com/Jackett/Jackett/releases/latest/download/'


def ok(m): print(f"  {G}✓{RS}  {m}")
def info(m): print(f"  {B}→{RS}  {m}")
def warn(m): print(f"  {Y}⚠{RS}  {m}")
def err(m): print(f"  {R}✗{RS}  {m}")


def run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, _, v = line.partition('=')
                env[k.strip()] = v.strip()
    return env


def set_env_value(key: str, value: str) -> None:
    """Заменить/добавить KEY=value в .env, сохранив остальное, владельца и права."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, done = [], False
    for line in lines:
        if line.split('=', 1)[0].strip() == key and not line.lstrip().startswith('#'):
            out.append(f'{key}={value}')
            done = True
        else:
            out.append(line)
    if not done:
        out += ['', '# Jackett (поиск по трекерам)', f'{key}={value}']
    st = ENV_PATH.stat() if ENV_PATH.exists() else None
    ENV_PATH.write_text('\n'.join(out) + '\n')
    ENV_PATH.chmod(0o600)
    if st:
        os.chown(ENV_PATH, st.st_uid, st.st_gid)


def bot_user() -> str:
    """Пользователь бота = владелец папки проекта (так его выставляет install.py)."""
    owner = pwd.getpwuid(PROJECT_DIR.stat().st_uid).pw_name
    if owner != 'root':
        return owner
    return os.environ.get('SUDO_USER') or 'root'


def release_asset() -> str:
    arch = platform.machine().lower()
    if arch in ('x86_64', 'amd64'):
        return 'Jackett.Binaries.LinuxAMDx64.tar.gz'
    if arch in ('aarch64', 'arm64'):
        return 'Jackett.Binaries.LinuxARM64.tar.gz'
    if arch.startswith('arm'):
        return 'Jackett.Binaries.LinuxARM32.tar.gz'
    raise SystemExit(f'Неподдерживаемая архитектура: {arch}')


def download(proxy: str) -> None:
    tarball = Path('/tmp') / release_asset()
    url = RELEASES + release_asset()
    info(f"Скачиваю Jackett ({release_asset()})…")
    if run(['curl', '-fL', '--retry', '2', '-o', str(tarball), url], check=False).returncode != 0:
        if not proxy:
            raise SystemExit('Не удалось скачать Jackett с GitHub.')
        warn("Напрямую не скачалось — пробую через прокси из .env")
        run(['curl', '-fL', '--retry', '2', '--proxy', proxy, '-o', str(tarball), url])
    run(['tar', '-xzf', str(tarball), '-C', '/opt'])
    tarball.unlink(missing_ok=True)
    ok("Jackett распакован в /opt/Jackett")


def config_path(user: str) -> Path:
    return Path(pwd.getpwnam(user).pw_dir) / '.config' / 'Jackett' / 'ServerConfig.json'


def wait_for_config(path: Path, seconds: int = 90) -> dict:
    info("Жду первого запуска Jackett (он создаёт конфиг и API-ключ)…")
    for _ in range(seconds):
        try:
            cfg = json.loads(path.read_text(encoding='utf-8-sig'))
            if cfg.get('APIKey'):
                return cfg
        except Exception:
            pass
        time.sleep(1)
    raise SystemExit(f'Jackett не создал конфиг за {seconds} сек: {path}')


def apply_settings(path: Path, proxy: str) -> None:
    run(['systemctl', 'stop', 'jackett'], check=False)
    time.sleep(2)
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    cfg['AllowExternal'] = True  # открыть веб-интерфейс с ПК в локальной сети
    if proxy:
        p = urlparse(proxy)
        cfg['ProxyType'] = 2 if p.scheme.startswith('socks5') else (1 if p.scheme == 'socks4' else 0)
        cfg['ProxyUrl'] = p.hostname
        cfg['ProxyPort'] = p.port
        cfg['ProxyUsername'] = p.username or ''
        cfg['ProxyPassword'] = p.password or ''
        ok(f"Прокси для трекеров: {p.hostname}:{p.port} ({p.scheme})")
    st = path.stat()
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chown(path, st.st_uid, st.st_gid)
    run(['systemctl', 'start', 'jackett'])
    ok("Jackett перезапущен с новыми настройками")


def local_ip() -> str:
    r = run(['hostname', '-I'], check=False, capture=True)
    parts = r.stdout.split()
    return parts[0] if parts else '<IP-сервера>'


def main():
    if os.geteuid() != 0:
        print(f"\n{R}Запусти от root:{RS}  sudo python3 setup_jackett.py\n")
        sys.exit(1)

    print(f"\n{B}{BD}DispeR Media Server — установка Jackett (поиск по трекерам){RS}\n")
    env = read_env()
    proxy = env.get('PROXY_URL', '')
    user = bot_user()
    info(f"Jackett будет работать от пользователя: {user}")

    if (JACKETT_DIR / 'jackett').exists():
        ok("Jackett уже установлен — пропускаю скачивание")
    else:
        download(proxy)

    run(['chown', '-R', f'{user}:{user}', str(JACKETT_DIR)])
    if not Path('/etc/systemd/system/jackett.service').exists():
        # Скрипт Jackett запускает сервис от имени владельца файлов (не root)
        subprocess.run(['./install_service_systemd.sh'], cwd=JACKETT_DIR, check=True)
    else:
        run(['systemctl', 'restart', 'jackett'], check=False)
    ok("Сервис jackett установлен и запущен")

    cfg_file = config_path(user)
    cfg = wait_for_config(cfg_file)
    apply_settings(cfg_file, proxy)

    set_env_value('JACKETT_API_KEY', cfg['APIKey'])
    ok("JACKETT_API_KEY записан в .env бота")
    run(['systemctl', 'restart', 'media-server'], check=False)
    ok("Бот перезапущен")

    ip = local_ip()
    print(f"""
{G}{'═' * 62}{RS}
{BD}{G}  ✅  Jackett установлен. Осталось добавить трекеры (один раз):{RS}
{G}{'═' * 62}{RS}

  1. Открой на ПК: {B}http://{ip}:9117{RS}
  2. Сразу задай пароль на вход: внизу страницы «Admin password» → Set.
     (в Jackett хранятся логины от трекеров — без пароля их увидит любой в сети)
  3. Нажми «+ Add indexer» и добавь:
       • {BD}RuTracker.org{RS}  — логин и пароль от RuTracker
       • {BD}LostFilm.tv{RS}    — e-mail и пароль от LostFilm (нужно для подписок)
       • {BD}RuTor{RS}          — публичный, без логина
       • {BD}Kinozal{RS}, {BD}NoNaMe Club{RS} — если есть аккаунты
  4. У каждого трекера нажми 🔧 → «Test», должно быть зелёным.

  Потом в боте просто напиши название фильма — например «Титаник».
  Подписка на сериал: /follow Silo
""")


if __name__ == '__main__':
    main()
