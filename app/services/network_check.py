"""On-demand reachability checks for a point's networking gear, triggered
by a dispatcher/admin from the points directory: "Пинг" (ICMP, no port —
is the device on the network at all) and "Веб" (TCP connect to the point's
configured port — is whatever service listens there actually up).

Both retry a few times before declaring a device unreachable — a single
lost ICMP packet (ARP resolution delay for a device the switch hasn't
seen recently, a momentary Wi-Fi hiccup, …) is normal on a real LAN and
must not read as "offline" on the first try. But each attempt is its own
single probe, tried one at a time: a good connection answers on the very
first attempt and returns immediately, without waiting out the rest of a
fixed multi-packet run — only an actual failure pays for a retry. That's
also why the per-attempt timeout below is short: it barely matters for a
working device (which replies in milliseconds either way) and mostly
controls how fast a genuinely dead one gets declared unreachable.
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import struct
import time
from typing import Optional

_PING_ATTEMPTS = 3
_PING_TIMEOUT = 1        # seconds — whole seconds only, ping -W wants an int
_PING_RETRY_DELAY = 0.3
_PORT_ATTEMPTS = 3
_PORT_TIMEOUT = 1.0      # seconds per TCP connect attempt
_PORT_RETRY_DELAY = 0.3

_RTT_AVG_RE = re.compile(r"=\s*[\d.]+/([\d.]+)/")  # "rtt min/avg/max/mdev = a/b/c/d ms" — want avg
_LATENCY_RE = re.compile(r"time[=<]([\d.]+)\s*ms")  # fallback: first packet's own "time=" if no summary line


_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_icmp_usable: Optional[bool] = None       # None — ещё не пробовали; True — пинг внутри приложения работает; False — сокет недоступен, пингуем внешней утилитой, как раньше


class _IcmpUnavailable(Exception):
    """ICMP-сокет создать нельзя (нет прав, не Linux, не тот цикл событий) — нужен запасной путь через утилиту ping."""


def _icmp_packet(ident: int, seq: int) -> bytes:
    """ICMP echo request (тип 8) с контрольной суммой. На непривилегированном сокете ядро само подставляет идентификатор, но сумма считается от пакета целиком."""
    head = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
    payload = b"lan-calendar-chk"          # 16 байт: чётная длина, выравнивать для суммы не нужно
    data = head + payload
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return struct.pack("!BBHHH", 8, 0, ~total & 0xFFFF, ident, seq) + payload


async def _icmp_once(ip: str) -> dict:
    """Один ICMP echo прямо из приложения, без запуска `ping`: на каждую проверку запуск процесса стоил около трети процессорного времени приложения (тысячи комплексов в круге).
    Сокет SOCK_DGRAM/IPPROTO_ICMP работает без root, если разрешён ping_group_range (в Docker он открыт для всех); один сокет на один пинг — ядро доставляет ответы только своему сокету.
    _IcmpUnavailable — сокета нет, вызывающий переходит на утилиту ping."""
    loop = asyncio.get_running_loop()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except (OSError, AttributeError) as exc:
        raise _IcmpUnavailable(str(exc)) from exc
    reader_added = False
    try:
        sock.setblocking(False)
        seq = int.from_bytes(os.urandom(2), "big")
        done: asyncio.Future = loop.create_future()

        def on_readable() -> None:
            # Ответ приходит в сокет без IP-заголовка: тип 0 (echo reply) и наш номер — то, что ждали; чужое и мусор пропускаем и ждём дальше.
            try:
                data = sock.recv(1024)
            except BlockingIOError:
                return
            except OSError as exc:                                  # ICMP-ошибка от сети (хост недостижим и т.п.) — для проверки это «не отвечает»
                if not done.done():
                    done.set_exception(exc)
                return
            if len(data) >= 8 and data[0] == 0 and struct.unpack("!H", data[6:8])[0] == seq and not done.done():
                done.set_result(True)

        try:                                # add_reader есть и в uvloop (на нём работает приложение), и в обычном цикле; sock_sendto в uvloop нет
            loop.add_reader(sock.fileno(), on_readable)
            reader_added = True
        except (NotImplementedError, AttributeError) as exc:       # цикл без add_reader (Windows/Proactor)
            raise _IcmpUnavailable(str(exc)) from exc
        started = time.monotonic()
        try:
            sock.sendto(_icmp_packet(os.getpid() & 0xFFFF, seq), (ip, 0))      # пакет в 32 байта: неблокирующая отправка не ждёт
        except PermissionError as exc:
            raise _IcmpUnavailable(str(exc)) from exc
        except OSError:                                             # нет маршрута, сеть недоступна — «не отвечает»
            return {"ok": False}
        try:
            await asyncio.wait_for(done, timeout=_PING_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            return {"ok": False}
        return {"ok": True, "latency_ms": (time.monotonic() - started) * 1000}
    finally:
        if reader_added:
            loop.remove_reader(sock.fileno())
        sock.close()


async def _ping_once(ip: str) -> Optional[dict]:
    """Один пинг: сначала внутри приложения (ICMP-сокет, только для IPv4-адресов), если сокета нет — внешней утилитой ping, как раньше. None — «утилиты ping на сервере нет» (и сокета тоже)."""
    global _icmp_usable
    if _icmp_usable is not False and _IPV4_RE.match(ip):
        try:
            result = await _icmp_once(ip)
        except _IcmpUnavailable:
            _icmp_usable = False                 # дальше не пробуем: каждый раз создавать сокет, которого нет, незачем
        else:
            _icmp_usable = True
            return result
    return await _ping_once_subprocess(ip)


async def _ping_once_subprocess(ip: str) -> Optional[dict]:
    """One ICMP echo (`ping -c 1`). Returns None to mean "no ping binary on
    this server" (checked once per call site, not worth retrying), a dict
    otherwise."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(_PING_TIMEOUT), ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PING_TIMEOUT + 1)
    except asyncio.TimeoutError:
        # ping мог завершиться сам ровно в момент таймаута — kill() тогда падает с ProcessLookupError
        # (в журнале это выглядело как «Traceback» в проверке доступности точки).
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return {"ok": False}

    if proc.returncode != 0:
        return {"ok": False}
    text = stdout.decode(errors="ignore")
    m = _RTT_AVG_RE.search(text) or _LATENCY_RE.search(text)
    return {"ok": True, "latency_ms": float(m.group(1)) if m else None}


async def ping_host(ip: str) -> dict:
    ip = (ip or "").strip()
    if not ip:
        return {"ok": False, "detail": "IP-адрес не указан"}

    for attempt in range(1, _PING_ATTEMPTS + 1):
        result = await _ping_once(ip)
        if result is None:
            return {"ok": False, "detail": "На сервере не установлена утилита ping"}
        if result["ok"]:
            return {"ok": True, "latency_ms": result["latency_ms"], "detail": "Доступен"}
        if attempt < _PING_ATTEMPTS:
            await asyncio.sleep(_PING_RETRY_DELAY)

    return {"ok": False, "detail": f"Нет ответа ({_PING_ATTEMPTS} попытки)"}


async def check_tcp_port(ip: str, port: str) -> dict:
    ip = (ip or "").strip()
    port_str = (port or "").strip()
    if not ip:
        return {"ok": False, "detail": "IP-адрес не указан"}
    if not port_str:
        return {"ok": False, "detail": "Порт не указан"}
    try:
        port_num = int(port_str)
    except ValueError:
        return {"ok": False, "detail": "Некорректный порт"}

    for attempt in range(1, _PORT_ATTEMPTS + 1):
        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port_num), timeout=_PORT_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            if attempt < _PORT_ATTEMPTS:
                await asyncio.sleep(_PORT_RETRY_DELAY)
            continue

        latency = (time.monotonic() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return {"ok": True, "latency_ms": latency, "detail": "Веб-интерфейс отвечает"}

    return {"ok": False, "detail": f"Порт недоступен ({_PORT_ATTEMPTS} попытки)"}
