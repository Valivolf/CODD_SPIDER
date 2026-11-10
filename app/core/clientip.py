"""Адрес компьютера, с которого пришёл запрос.

Приложение слушает только 127.0.0.1:8000 хоста — снаружи до него доходит лишь Caddy (docker/Caddyfile), который сам ставит X-Forwarded-For (приходящий от клиента заголовок
он отбрасывает). Поэтому заголовку верим только когда запрос пришёл с локального/внутреннего адреса (Caddy, docker-прокси); от любого другого соединения берётся его собственный
адрес — подделать его заголовком нельзя."""
from __future__ import annotations

import ipaddress

from fastapi import Request


def _valid(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return ""


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    try:
        trusted = bool(peer) and (ipaddress.ip_address(peer).is_loopback or ipaddress.ip_address(peer).is_private)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = _valid(forwarded.split(",")[0]) if forwarded else ""
        if first:
            return first
    return peer
