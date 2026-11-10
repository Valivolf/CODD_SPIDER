"""Что за компьютер: процессор, память, видеокарты — и какую из карт стоит использовать для модели.

Каждая видеокарта получает вердикт (classify_gpu):
  use    — сильная: отдельная карта с видеопамятью не меньше MIN_VRAM_MB и не из старых серий — модель целиком на карте;
  hybrid — средняя/слабая отдельная карта (от 2 ГБ видеопамяти, но мало памяти или старая модель): работает ВМЕСТЕ с процессором — если модель помещается в видеопамять, на карте и на
           процессоре запускаются два экземпляра модели и считают разные запросы одновременно; не помещается — часть слоёв на карте, остальное на процессоре;
  weak   — не используется: встроенная графика (делит память с процессором, пользы нет) или видеопамяти меньше 2 ГБ;
  none   — не годится (виртуальный адаптер, «базовый драйвер»).
В настройках агента можно принудительно выключить карту или включить любую (на свой риск). Всё определяется средствами самой Windows (реестр, nvidia-smi, WinAPI) — без установки
дополнительных программ и без администратора."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import subprocess
import sys
from typing import Any, Optional

MIN_VRAM_MB = 6000                       # меньше — карта средняя/слабая (GTX 1050 4 ГБ, GTX 1650 4 ГБ, RX 570 4 ГБ…): работает вместе с процессором
HYBRID_MIN_VRAM_MB = 2000                # меньше — карта бесполезна даже в паре с процессором

_CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Команда без мигающего окна; любой сбой — (-1, '')."""
    try:
        flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return -1, ""


# ---------------------------------------------------------------- процессор и память
def _feature(code: int) -> bool:
    try:
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(code))        # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def cpu_info() -> dict[str, Any]:
    logical = os.cpu_count() or 1
    name = platform.processor() or "Процессор"
    physical = 0
    if sys.platform == "win32":
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip() or name
        except OSError:
            pass
        rc, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"], 12)
        if rc == 0 and out.strip().isdigit():
            physical = int(out.strip())
    if not physical:
        physical = max(1, logical // 2) if logical >= 4 else logical
    return {"name": " ".join(name.split()), "physical": min(physical, logical), "logical": logical, "avx2": _feature(40), "avx512": _feature(41)}


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory_gb() -> tuple[float, float]:
    """(всего, свободно сейчас) в ГБ."""
    try:
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))              # type: ignore[attr-defined]
        return round(st.ullTotalPhys / 2**30, 1), round(st.ullAvailPhys / 2**30, 1)
    except (AttributeError, OSError):
        return 8.0, 4.0


# ---------------------------------------------------------------- видеокарты
def _vendor(name: str) -> str:
    n = name.lower()
    if re.search(r"nvidia|geforce|rtx|gtx|quadro|tesla", n):
        return "NVIDIA"
    if re.search(r"amd|radeon|\bati\b", n):
        return "AMD"
    if "intel" in n:
        return "Intel"
    return "Другая"


_VIRTUAL = re.compile(r"basic render|basic display|hyper-v|vmware|virtualbox|parallels|citrix|remote display|microsoft remote|qxl|virtio|parsec|spice|rdp", re.I)
_INTEGRATED = re.compile(r"intel\(r\)\s*(uhd|hd|iris)|intel (uhd|hd|iris)|\buhd graphics|\bhd graphics|iris\(r\)|iris xe|radeon\(tm\)\s*(vega\s*\d+\s*)?graphics|radeon\s+graphics|radeon vega|vega \d+ graphics|amd radeon\(tm\) r\d graphics", re.I)
_OLD_OR_LOW = re.compile(
    r"\bGT\s?\d{3,4}\b|geforce\s+(gtx\s+)?(2\d\d|4\d\d|5\d\d|6\d\d|7\d\d|8\d\d|9\d\d)\b|\bGTX\s?(10[0-4]0|16[0-5]0)\b|\bMX\s?\d{3}\b|quadro\s+(k|m|p[0-9]{3}\b|nvs)|tesla\s+k"
    r"|radeon\s+(r[579]\s*\d{3}|hd\s*\d{4}|rx\s*(4\d{2}|5[0-4]\d)\b)|\brx\s?5[0-4]0\b", re.I)


def classify_gpu(name: str, vram_mb: int) -> dict[str, str]:
    """Вердикт по карте (см. описание модуля). Возвращает {verdict, text}."""
    if _VIRTUAL.search(name):
        return {"verdict": "none", "text": "виртуальный адаптер — не годится"}
    if _INTEGRATED.search(name):
        return {"verdict": "weak", "text": "встроенная графика делит память с процессором — пользы нет, не использую"}
    gb = round(vram_mb / 1024, 1)
    if not vram_mb:
        return {"verdict": "weak", "text": "объём видеопамяти определить не удалось — не использую"}
    if vram_mb < HYBRID_MIN_VRAM_MB:
        return {"verdict": "weak", "text": f"видеопамяти слишком мало ({gb} ГБ) — не использую"}
    if vram_mb < MIN_VRAM_MB:
        return {"verdict": "hybrid", "text": f"средняя ({gb} ГБ видеопамяти): считает вместе с процессором — часть работы на карте, часть на процессоре"}
    if _OLD_OR_LOW.search(name):
        return {"verdict": "hybrid", "text": f"старая или бюджетная модель ({gb} ГБ): считает вместе с процессором"}
    return {"verdict": "use", "text": f"сильная ({gb} ГБ видеопамяти) — использую"}


def _registry_adapters() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if sys.platform != "win32":
        return out
    try:
        import winreg  # noqa: PLC0415

        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                sub = winreg.EnumKey(k, i)
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(k, sub) as s:
                        desc = str(winreg.QueryValueEx(s, "DriverDesc")[0]).strip()
                        mem = 0
                        for value in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                            try:
                                raw, kind = winreg.QueryValueEx(s, value)
                            except OSError:
                                continue
                            mem = int.from_bytes(raw[:8], "little") if isinstance(raw, (bytes, bytearray)) else int(raw)
                            if mem:
                                break
                        out.append({"name": desc, "vram_mb": int(mem // 2**20)})
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _nvidia_smi() -> list[dict[str, Any]]:
    for exe in ("nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
        rc, out = _run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"], 8)
        if rc == 0 and out.strip():
            res = []
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2 and parts[1].replace(".", "").isdigit():
                    res.append({"name": parts[0], "vram_mb": int(float(parts[1])), "driver": parts[2] if len(parts) > 2 else ""})
            if res:
                return res
    return []


def gpu_list() -> list[dict[str, Any]]:
    """Все видеокарты: из реестра Windows, для NVIDIA — с точным объёмом памяти от nvidia-smi. Каждая с вердиктом."""
    adapters = _registry_adapters()
    smi = _nvidia_smi()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in adapters:
        key = a["name"].lower()
        if not a["name"] or key in seen:
            continue
        seen.add(key)
        vram = a["vram_mb"]
        if _vendor(a["name"]) == "NVIDIA":
            match = next((s for s in smi if s["name"].lower() in key or key in s["name"].lower()), None)
            if match:
                vram = match["vram_mb"] or vram
        v = classify_gpu(a["name"], vram)
        out.append({"name": a["name"], "vendor": _vendor(a["name"]), "vram_mb": vram, "verdict": v["verdict"], "verdict_text": v["text"]})
    for s in smi:                                           # карта есть у nvidia-smi, но реестр её не показал
        if s["name"].lower() not in seen and not any(s["name"].lower() in g["name"].lower() for g in out):
            v = classify_gpu(s["name"], s["vram_mb"])
            out.append({"name": s["name"], "vendor": "NVIDIA", "vram_mb": s["vram_mb"], "verdict": v["verdict"], "verdict_text": v["text"]})
    out.sort(key=lambda g: ({"use": 0, "hybrid": 1, "weak": 2, "none": 3}[g["verdict"]], -g["vram_mb"]))
    return out


def detect() -> dict[str, Any]:
    total, free = memory_gb()
    return {"cpu": cpu_info(), "ram_gb": total, "ram_free_gb": free, "gpus": gpu_list(), "os": f"{platform.system()} {platform.release()}"}


def pick_gpu(hw: dict[str, Any], mode: str) -> Optional[dict[str, Any]]:
    """Какую карту использовать: auto — лучшую из сильных и средних (use / hybrid; встроенные и совсем слабые не берём); off — никакую; force — самую большую по памяти,
    какой бы слабой она ни была (кроме виртуальных)."""
    gpus = hw.get("gpus") or []
    if mode == "off":
        return None
    if mode == "force":
        real = [g for g in gpus if g["verdict"] != "none"]
        return max(real, key=lambda g: g["vram_mb"]) if real else None
    good = [g for g in gpus if g["verdict"] in ("use", "hybrid")]
    return good[0] if good else None


def describe(hw: dict[str, Any]) -> str:
    c = hw["cpu"]
    lines = [f"Процессор: {c['name']} — {c['physical']} ядер / {c['logical']} потоков{', AVX2' if c.get('avx2') else ', без AVX2 (будет медленно)'}",
             f"Память: {hw['ram_gb']} ГБ (свободно сейчас {hw['ram_free_gb']} ГБ)"]
    if not hw["gpus"]:
        lines.append("Видеокарта: не найдена — работаю на процессоре")
    for g in hw["gpus"]:
        lines.append(f"Видеокарта: {g['name']}, {round(g['vram_mb'] / 1024, 1)} ГБ — {g['verdict_text']}")
    return "\n".join(lines)


if __name__ == "__main__":
    h = detect()
    print(json.dumps(h, ensure_ascii=False, indent=2))
    print(describe(h))
