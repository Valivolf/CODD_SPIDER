"""In-memory login throttling.

A small, single-process guard against brute-forcing a user's password —
not distributed, which is fine for an app designed to run as one
instance on the LAN. Two independent limits:
  • per username (normalized, so a locked-out account stays locked regardless of which client
    IP is trying it): MAX_ATTEMPTS per WINDOW_SECONDS;
  • per client address: IP_MAX_FAILURES failed logins per WINDOW_SECONDS — stops «один пароль
    на много логинов» (password spraying), которое лимит по логину не видит.
"""
from __future__ import annotations

import time

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 5 * 60
IP_MAX_FAILURES = 30

_failures: dict[str, list[float]] = {}
_ip_failures: dict[str, list[float]] = {}


def _recent(key: str, now: float) -> list[float]:
    attempts = [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]
    _failures[key] = attempts
    return attempts


def check_and_reserve(key: str) -> bool:
    """Atomically checks the lock and reserves an attempt slot in one
    synchronous call, with no `await` in between. Callers must call this
    BEFORE the (slow) password check, not check is_locked() first and
    register a failure only after — otherwise a burst of concurrent
    requests for the same username can all pass the check before any of
    them has registered a failure, since Python only ever runs one
    coroutine's synchronous code at a time: doing the check-and-reserve
    as a single synchronous step is what makes it race-free.

    Returns True if the caller may proceed, False if locked out. On a
    successful login, call clear() to release the reserved slots.
    """
    now = time.monotonic()
    attempts = _recent(key, now)
    if len(attempts) >= MAX_ATTEMPTS:
        return False
    attempts.append(now)
    return True


def clear(key: str) -> None:
    _failures.pop(key, None)


def ip_locked(ip: str) -> bool:
    """Слишком много неудачных входов с этого адреса за последние минуты."""
    if not ip:
        return False
    now = time.monotonic()
    recent = [t for t in _ip_failures.get(ip, []) if now - t < WINDOW_SECONDS]
    _ip_failures[ip] = recent
    return len(recent) >= IP_MAX_FAILURES


def ip_register_failure(ip: str) -> None:
    if ip:
        _ip_failures.setdefault(ip, []).append(time.monotonic())


# Подключение ИИ-агентов — ОТДЕЛЬНЫЙ счётчик (не тот, что у входа в веб-панель): сервер и агент нередко на одном адресе (ноутбук с локальным сервером), и неверные ключи агента
# не должны закрывать вход в панель. Считаются только ПЕРЕБОР неизвестных ключей; использованный или просроченный ключ, выключенный режим и т. п. — не перебор.
AGENT_MAX_FAILURES = 20
_agent_failures: dict[str, list[float]] = {}


def agent_register_locked(ip: str) -> int:
    """Сколько секунд ещё действует блокировка подключения агентов с этого адреса (0 — не заблокирован)."""
    if not ip:
        return 0
    now = time.monotonic()
    recent = [t for t in _agent_failures.get(ip, []) if now - t < WINDOW_SECONDS]
    _agent_failures[ip] = recent
    if len(recent) < AGENT_MAX_FAILURES:
        return 0
    return max(1, int(WINDOW_SECONDS - (now - recent[0])))


def agent_register_failure(ip: str) -> None:
    if ip:
        _agent_failures.setdefault(ip, []).append(time.monotonic())


def agent_register_clear(ip: str) -> None:
    _agent_failures.pop(ip, None)
