"""Сборка LanCalendarAIAgent.exe (один файл, без консоли) средствами PyInstaller:  python build_exe.py [папка_результата]
Нужен Python 3.10+ с установленными httpx и pyinstaller (pip install httpx pyinstaller). Результат по умолчанию — agent/dist/LanCalendarAIAgent.exe.
Подписи у файла нет: Windows (SmartScreen / Защитник) при первом запуске может предупредить «неизвестный издатель» — это нормально для программы без платного сертификата;
запустите через «Подробнее → Выполнить в любом случае» или добавьте файл в исключения защитника."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "dist"
    work = HERE / "build"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole", "--name", "LanCalendarAIAgent", "--distpath", str(out), "--workpath", str(work),
           "--specpath", str(work), "--paths", str(HERE), "--collect-submodules", "lanai_agent",
           "--add-data", f"{HERE / 'resources'}{os.pathsep}resources",            # сборки llama.cpp (cpu, vulkan) и лицензия — внутри exe
           str(HERE / "run_agent.py")]
    print(" ".join(cmd))
    return subprocess.call(cmd, cwd=str(HERE))


if __name__ == "__main__":
    sys.exit(main())
