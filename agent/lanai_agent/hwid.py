"""Отпечаток железа: по нему сервер привязывает компьютер (а не по адресу в сети — адрес меняется, оборудование нет).

Берётся то, что прошито в самом компьютере: UUID системной платы и серийные номера платы и системы из таблиц SMBIOS (их отдаёт Windows через GetSystemFirmwareTable, права
администратора не нужны). Заглушки производителей («To be filled by O.E.M.», «Default string», нули, одинаковые у всех плат строки) отбрасываются. Если ничего надёжного
не нашлось (виртуальная машина без SMBIOS, не Windows) — берётся идентификатор установки системы (MachineGuid), а в крайнем случае адрес сетевой карты. Процессор, диски и видеокарту
в отпечаток НЕ входят: их замена (апгрейд) не отвязывает компьютер, а замена системной платы — отвязывает: тогда подключают заново новым ключом.

Результат — 64 шестнадцатеричных знака (SHA-256), по нему ничего нельзя восстановить; `short()` — первые 8 знаков для показа человеку (то же самое видно в списке на сервере)."""
from __future__ import annotations

import ctypes
import hashlib
import re
import sys
import uuid
from typing import Optional

_PLACEHOLDER = re.compile(
    r"to be filled|default string|not specified|not applicable|system serial|serial number|base board|baseboard|o\.e\.m|oem|n/a|unknown|none|empty|"
    r"chassis|type1|your name|invalid", re.I)


def _usable(value: Optional[str]) -> bool:
    v = (value or "").strip()
    if len(v) < 4:
        return False
    if re.fullmatch(r"[0\-\s_.]+|[fF\-\s_.]+", v):
        return False
    if len(set(v.lower().replace("-", ""))) < 3:                  # «aaaaaaaa», «12121212»
        return False
    if re.fullmatch(r"0?123456789\d?", v):                         # «123456789», «0123456789» — заглушка, но только целиком (в настоящем номере такая цепочка может встретиться)
        return False
    return not _PLACEHOLDER.search(v)


def _smbios_table() -> bytes:
    k32 = ctypes.windll.kernel32                                  # type: ignore[attr-defined]
    sig = 0x52534D42                                              # 'RSMB'
    size = k32.GetSystemFirmwareTable(sig, 0, None, 0)
    if not size:
        return b""
    buf = ctypes.create_string_buffer(size)
    if k32.GetSystemFirmwareTable(sig, 0, buf, size) != size:
        return b""
    data = buf.raw
    length = int.from_bytes(data[4:8], "little")                  # RawSMBIOSData: 4 байта заголовка, длина, затем сами таблицы
    return data[8:8 + length]


def parse_smbios(table: bytes) -> dict[str, str]:
    """Из «сырых» таблиц SMBIOS: UUID и серийные номера системы (тип 1) и платы (тип 2)."""
    out: dict[str, str] = {}
    i = 0
    while i + 4 <= len(table):
        kind, size = table[i], table[i + 1]
        if size < 4 or i + size > len(table):
            break
        body = table[i:i + size]
        j = i + size
        strings: list[str] = []
        k = j
        while k < len(table) and table[k] != 0:
            end = table.find(b"\x00", k)
            if end < 0:
                break
            strings.append(table[k:end].decode("ascii", errors="replace").strip())
            k = end + 1
        nxt = k + 1 if strings else j + 2

        def text(offset: int) -> str:
            idx = body[offset] if offset < len(body) else 0
            return strings[idx - 1] if 1 <= idx <= len(strings) else ""

        if kind == 1 and size >= 0x19:
            raw = bytes(body[8:24])
            if raw not in (b"\x00" * 16, b"\xff" * 16):
                out["uuid"] = str(uuid.UUID(bytes_le=raw))
            out["system_serial"] = text(7)
        elif kind == 2 and size >= 8:
            out["board_serial"] = text(7)
        elif kind == 127:                                         # конец таблиц
            break
        i = nxt
    return out


def _machine_guid() -> str:
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
    except (OSError, ImportError):
        return ""


def parts() -> list[str]:
    """Надёжные признаки железа этого компьютера (для проверки и показа; в отпечаток идут их хеши)."""
    found: list[str] = []
    if sys.platform == "win32":
        try:
            sm = parse_smbios(_smbios_table())
        except (OSError, AttributeError, ValueError):
            sm = {}
        for key in ("uuid", "board_serial", "system_serial"):
            if _usable(sm.get(key)):
                found.append(f"{key}:{sm[key].strip().lower()}")
        if not found:
            guid = _machine_guid()
            if _usable(guid):
                found.append("machine_guid:" + guid.lower())
    else:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                text = open(path, encoding="ascii").read().strip()
            except OSError:
                continue
            if _usable(text):
                found.append("machine_id:" + text.lower())
                break
    if not found:
        found.append("mac:%012x" % uuid.getnode())
    return found


def make_id(items: list[str]) -> str:
    return hashlib.sha256(("lanai-hw-v1|" + "|".join(sorted(items))).encode("utf-8")).hexdigest()


_cached: Optional[str] = None


def hardware_id() -> str:
    global _cached
    if _cached is None:
        _cached = make_id(parts())
    return _cached


def short(hw_id: str) -> str:
    return (hw_id or "")[:8].upper()


if __name__ == "__main__":
    print("признаки:", [p.split(":")[0] for p in parts()])
    print("отпечаток:", hardware_id(), "| короткий код:", short(hardware_id()))
