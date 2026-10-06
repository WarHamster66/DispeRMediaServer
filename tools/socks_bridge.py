#!/usr/bin/env python3
"""Локальный SOCKS5-мост: без пароля → внешний прокси с паролем.

Зачем: FlareSolverr (обход проверки «я не робот» для Jackett/RuTracker)
управляет Chrome, а Chrome не умеет SOCKS5-прокси с логином и паролем.
Мост слушает ТОЛЬКО 127.0.0.1 (снаружи недоступен), принимает соединения
без пароля и пробрасывает их в PROXY_URL из .env — уже с паролем.

Поддерживается только CONNECT — его используют браузер и Jackett.
Запускается systemd-сервисом socks-bridge (ставит setup_flaresolverr.py).
"""
import logging
import os
import select
import socket
import struct
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import socks  # PySocks — уже в requirements.txt

LISTEN_HOST = os.environ.get('BRIDGE_HOST', '127.0.0.1')
LISTEN_PORT = int(os.environ.get('BRIDGE_PORT', '1081'))
ENV_FILE = Path(__file__).resolve().parent.parent / '.env'
IDLE_TIMEOUT = 300  # закрыть соединение после 5 минут тишины

_KINDS = {'socks5': socks.SOCKS5, 'socks5h': socks.SOCKS5,
          'socks4': socks.SOCKS4, 'socks4a': socks.SOCKS4, 'http': socks.HTTP}


def load_upstream():
    """Внешний прокси из окружения или .env. None — ходить напрямую."""
    url = os.environ.get('UPSTREAM_PROXY', '')
    if not url and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith('PROXY_URL='):
                url = line.split('=', 1)[1].strip()
    if not url:
        return None
    p = urlparse(url)
    if p.scheme not in _KINDS:
        raise SystemExit(f'Неподдерживаемый тип прокси: {p.scheme}')
    return {
        'proxy_type': _KINDS[p.scheme],
        'proxy_addr': p.hostname,
        'proxy_port': p.port,
        'proxy_rdns': True,  # имена сайтов резолвит удалённый прокси
        'proxy_username': unquote(p.username) if p.username else None,
        'proxy_password': unquote(p.password) if p.password else None,
    }


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('client closed')
        buf += chunk
    return buf


def _reply(client: socket.socket, code: int) -> None:
    # VER, REP, RSV, ATYP=IPv4, BND.ADDR=0.0.0.0, BND.PORT=0
    client.sendall(struct.pack('!BBBB4sH', 5, code, 0, 1, b'\0\0\0\0', 0))


def _relay(a: socket.socket, b: socket.socket) -> None:
    a.settimeout(None)
    b.settimeout(None)
    pair = [a, b]
    while True:
        readable, _, broken = select.select(pair, [], pair, IDLE_TIMEOUT)
        if broken or not readable:
            return
        for s in readable:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


def handle(client: socket.socket, upstream: dict | None) -> None:
    remote = None
    try:
        client.settimeout(30)
        ver, nmethods = _recv_exact(client, 2)
        if ver != 5:
            return
        _recv_exact(client, nmethods)
        client.sendall(b'\x05\x00')  # без аутентификации (мост слушает только localhost)

        ver, cmd, _, atyp = _recv_exact(client, 4)
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(client, 4))
        elif atyp == 3:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode('idna')
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            _reply(client, 8)  # address type not supported
            return
        port = struct.unpack('!H', _recv_exact(client, 2))[0]
        if cmd != 1:
            _reply(client, 7)  # command not supported (только CONNECT)
            return

        try:
            if upstream:
                remote = socks.create_connection((host, port), timeout=30, **upstream)
            else:
                remote = socket.create_connection((host, port), timeout=30)
        except Exception as e:
            logging.warning(f'connect {host}:{port} failed: {e}')
            _reply(client, 5)  # connection refused
            return

        _reply(client, 0)
        _relay(client, remote)
    except Exception as e:
        logging.debug(f'connection error: {e}')
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    upstream = load_upstream()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    target = f"{upstream['proxy_addr']}:{upstream['proxy_port']}" if upstream else 'напрямую'
    logging.info(f'SOCKS5 bridge {LISTEN_HOST}:{LISTEN_PORT} → {target}')
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client, upstream), daemon=True).start()


if __name__ == '__main__':
    main()
