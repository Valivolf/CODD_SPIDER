"""Мелочи Windows: простой компьютера (когда владелец ушёл от клавиатуры) и автозапуск при входе в систему."""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LanCalendarAIAgent"


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Сколько секунд владелец ничего не нажимал и не двигал мышью (0, если определить нельзя)."""
    try:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(_LastInput)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))             # type: ignore[attr-defined]
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0.0


def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" -m lanai_agent --minimized'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg  # noqa: PLC0415

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass
