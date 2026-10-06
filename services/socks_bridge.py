"""Локальный SOCKS5-мост внутри бота: без пароля → PROXY_URL с паролем.

Нужен только FlareSolverr: его Chrome не умеет SOCKS5-прокси с логином и
паролем. Мост слушает только 127.0.0.1, поднимается при первой надобности
и живёт в фоновом потоке бота — отдельная служба не нужна.
"""
import logging
import select
import socket
import struct
import threading
from urllib.parse import unquote, urlparse

import socks  # PySocks — уже в requirements.txt

from core import config

logger = logging.getLogger(__name__)

IDLE_TIMEOUT = 300  # закрыть соединение после 5 минут тишины
_KINDS = {'socks5': socks.SOCKS5, 'socks5h': socks.SOCKS5,
          'socks4': socks.SOCKS4, 'socks4a': socks.SOCKS4,
          'http': socks.HTTP, 'https': socks.HTTP}

_lock = threading.Lock()
_port: int | None = None


def browser_proxy() -> dict | None:
    """Прокси для браузера FlareSolverr (формат его API) или None — напрямую."""
    if not config.PROXY_URL:
        return None
    p = urlparse(config.PROXY_URL)
    if p.scheme in ('http', 'https'):
        # HTTP-прокси с паролем FlareSolverr умеет сам
        proxy = {'url': f'{p.scheme}://{p.hostname}:{p.port}'}
        if p.username:
            proxy.update(username=unquote(p.username), password=unquote(p.password or ''))
        return proxy
    if not p.username:
        return {'url': f'socks5://{p.hostname}:{p.port}'}
    return {'url': f'socks5://127.0.0.1:{_ensure_running()}'}


def _upstream() -> dict:
    p = urlparse(config.PROXY_URL)
    if p.scheme not in _KINDS:
        raise ValueError(f'неподдерживаемый тип прокси: {p.scheme}')
    return {'proxy_type': _KINDS[p.scheme], 'proxy_addr': p.hostname, 'proxy_port': p.port,
            'proxy_rdns': True,  # имена сайтов резолвит удалённый прокси
            'proxy_username': unquote(p.username) if p.username else None,
            'proxy_password': unquote(p.password) if p.password else None}


def _ensure_running() -> int:
    global _port
    with _lock:
        if _port:
            return _port
        upstream = _upstream()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))  # свободный порт, только для самого сервера
        srv.listen(64)
        _port = srv.getsockname()[1]
        threading.Thread(target=_serve, args=(srv, upstream), name='SocksBridge', daemon=True).start()
        logger.info(f'SOCKS bridge 127.0.0.1:{_port} → {upstream["proxy_addr"]}:{upstream["proxy_port"]}')
        return _port


def _serve(srv: socket.socket, upstream: dict) -> None:
    while True:
        client, _ = srv.accept()
        threading.Thread(target=_handle, args=(client, upstream), daemon=True).start()


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


def _handle(client: socket.socket, upstream: dict) -> None:
    remote = None
    try:
        client.settimeout(30)
        ver, nmethods = _recv_exact(client, 2)
        if ver != 5:
            return
        _recv_exact(client, nmethods)
        client.sendall(b'\x05\x00')  # без пароля: мост слушает только localhost

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
            _reply(client, 7)  # только CONNECT — его использует браузер
            return
        try:
            remote = socks.create_connection((host, port), timeout=30, **upstream)
        except Exception as e:
            logger.debug(f'bridge connect {host}:{port} failed: {e}')
            _reply(client, 5)
            return
        _reply(client, 0)
        _relay(client, remote)
    except Exception as e:
        logger.debug(f'bridge connection error: {e}')
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
