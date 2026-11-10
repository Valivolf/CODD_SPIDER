"""Список общих папок SMB-сервера (то, что показывает «net view \\\\сервер»): минимальный клиент службы srvsvc поверх именованного
канала IPC$\\srvsvc, без лишних библиотек (smbprotocol сам такого вызова не умеет).

Нужен, чтобы при «общая папка не найдена» показать, какие папки сервер реально отдаёт этому пользователю, и подставить
верное написание (регистр, пробелы, русские и латинские буквы-двойники). Любой сбой — не страшно: список просто не показывается.
"""
from __future__ import annotations

import struct
import uuid
from typing import Any

_SRVSVC = uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188")      # служба srvsvc, версия 3.0
_NDR32 = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")       # формат передачи NDR, версия 2
_OP_NET_SHARE_ENUM = 15
_MAX_FRAG = 4280


def _bind_pdu(call_id: int) -> bytes:
    ctx = struct.pack("<HBB", 0, 1, 0) + _SRVSVC.bytes_le + struct.pack("<HH", 3, 0) + _NDR32.bytes_le + struct.pack("<I", 2)
    body = struct.pack("<HHIBxxx", _MAX_FRAG, _MAX_FRAG, 0, 1) + ctx
    head = struct.pack("<BBBB4sHHI", 5, 0, 11, 0x03, b"\x10\x00\x00\x00", 16 + len(body), 0, call_id)
    return head + body


def _request_pdu(call_id: int, opnum: int, stub: bytes) -> bytes:
    body = struct.pack("<IHH", len(stub), 0, opnum) + stub
    head = struct.pack("<BBBB4sHHI", 5, 0, 0, 0x03, b"\x10\x00\x00\x00", 16 + len(body), 0, call_id)
    return head + body


def _share_enum_stub() -> bytes:
    # ServerName = NULL; InfoStruct = уровень 1 (имя, тип, комментарий) с пустым списком; PreferedMaximumLength = без ограничения;
    # ResumeHandle = NULL
    return struct.pack("<IIIIIII", 0, 1, 1, 0x00020000, 0, 0, 0xFFFFFFFF) + struct.pack("<I", 0)


def _read_pdu(pipe) -> tuple[int, int, bytes]:
    """Один пакет DCE/RPC: (тип, флаги, всё после 24-байтного заголовка / после 16 для bind_ack)."""
    data = b""
    while len(data) < 16:
        chunk = pipe.read(65536)
        if not chunk:
            raise OSError("канал srvsvc закрылся")
        data += chunk
    frag_len = struct.unpack_from("<H", data, 8)[0]
    while len(data) < frag_len:
        chunk = pipe.read(65536)
        if not chunk:
            raise OSError("канал srvsvc закрылся")
        data += chunk
    return data[2], data[3], data[:frag_len]


def _parse_enum(stub: bytes) -> list[dict[str, Any]]:
    pos = 0

    def u32() -> int:
        nonlocal pos
        pos = (pos + 3) & ~3
        val = struct.unpack_from("<I", stub, pos)[0]
        pos += 4
        return val

    def string() -> str:
        nonlocal pos
        u32()                                  # максимальная длина
        u32()                                  # смещение
        count = u32()                          # сколько знаков (с завершающим нулём)
        raw = stub[pos:pos + count * 2]
        pos += count * 2
        return raw.decode("utf-16-le").rstrip("\x00")

    u32()                                      # уровень
    u32()                                      # выбор варианта
    if not u32():                              # указатель на контейнер
        return []
    entries = u32()
    if not u32():                              # указатель на массив
        return []
    u32()                                      # размер массива
    items = []
    for _ in range(entries):
        name_ptr, kind, remark_ptr = u32(), u32(), u32()
        items.append([name_ptr, kind, remark_ptr])
    out: list[dict[str, Any]] = []
    for name_ptr, kind, remark_ptr in items:
        name = string() if name_ptr else ""
        remark = string() if remark_ptr else ""
        out.append({"name": name, "type": kind, "remark": remark})
    return out


def list_shares(server: str, smb, **kw) -> list[str]:
    """Имена обычных (дисковых, не служебных) общих папок сервера, доступных этому пользователю. smb — модуль smbclient."""
    pipe = smb.open_file(f"\\\\{server}\\IPC$\\srvsvc", mode="r+b", buffering=0, file_type="pipe", **kw)
    try:
        pipe.write(_bind_pdu(1))
        ptype, _flags, pdu = _read_pdu(pipe)
        if ptype != 12:
            raise OSError("сервер не принял запрос списка общих папок")
        # результат первого контекста: после заголовка (16), размеров (8) и вторичного адреса (длина + выравнивание)
        sec_len = struct.unpack_from("<H", pdu, 24)[0]
        pos = (26 + sec_len + 3) & ~3
        if struct.unpack_from("<H", pdu, pos + 4)[0] != 0:
            raise OSError("сервер не поддерживает запрос списка общих папок")
        pipe.write(_request_pdu(2, _OP_NET_SHARE_ENUM, _share_enum_stub()))
        stub = b""
        while True:
            ptype, flags, pdu = _read_pdu(pipe)
            if ptype == 3:
                raise OSError("сервер отказал в списке общих папок")
            if ptype != 2:
                raise OSError("неожиданный ответ сервера")
            stub += pdu[24:]
            if flags & 0x02:                   # последний фрагмент
                break
        shares = _parse_enum(stub)
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass
    names = []
    for s in shares:
        if (s["type"] & 0xFF) != 0 or (s["type"] & 0x80000000) or s["name"].endswith("$"):
            continue                           # принтеры, IPC, административные C$ и т. п.
        names.append(s["name"])
    return names
