"""Запуск агента:  LanCalendarAIAgent.exe            — окно (по умолчанию)
                  LanCalendarAIAgent.exe --minimized — окно свёрнуто (так запускается автозапуск Windows)
                  --headless                         — без окна, в консоли (для проверок и серверов)
                  --selftest [файл]                  — определить компьютер и подобрать модель, вывести JSON и выйти (без подключения)
                  --version"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import APP_TITLE, VERSION, catalog, config, hardware
from .core import AgentCore


def selftest() -> dict:
    hw = hardware.detect()
    cfg = config.load()
    gpu = hardware.pick_gpu(hw, cfg.gpu_mode)
    best, rows = catalog.recommend(hw, gpu, cfg.limit_pct, cfg.ctx_per_slot)
    plan = catalog.make_plan(hw, cfg.limit_pct, gpu, best, cfg.ctx_per_slot)
    return {"version": VERSION, "hardware": hw, "gpu_used": gpu["name"] if gpu else None, "recommended": best.id,
            "plan": {"backend": plan.backend, "slots": plan.slots, "est_tps": plan.est_tps, "notes": plan.notes,
                     "instances": [{"kind": i.kind, "ngl": i.n_gpu_layers, "threads": i.threads, "ctx": i.ctx, "est_tps": i.est_tps} for i in plan.instances]},
            "models": [{"id": r["model"].id, "verdict": r["verdict"], "est_tps": r["est_tps"], "need_ram_gb": r["need_ram_gb"]} for r in rows]}


def _safe_streams() -> None:
    """Вывод в консоль/файл/трубу не должен ронять программу: в перенаправленном выводе Windows кодировка — cp1251/cp866, и символ вроде «≈» (он есть в строках журнала) вызывал
    UnicodeEncodeError в главном потоке — режим --headless умирал на первой же строке плана. Пишем в UTF-8, а что не выйдет — заменяем."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:                               # exe без консоли: потока вывода нет
            continue
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            stream.reconfigure(encoding="utf-8" if enc != "utf8" else None, errors="replace")
        except Exception:  # noqa: BLE001 — не получилось перенастроить: пусть пишет как есть, печать ниже защищена
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_streams()
    ap = argparse.ArgumentParser(prog="LanCalendarAIAgent", description=APP_TITLE)
    ap.add_argument("--headless", action="store_true", help="без окна")
    ap.add_argument("--minimized", action="store_true", help="окно свёрнуто")
    ap.add_argument("--selftest", nargs="?", const="-", help="определить компьютер, подобрать модель, вывести JSON (в файл или на экран)")
    ap.add_argument("--yes-download", action="store_true", help="консольный режим: скачивать llama.cpp и модель без вопроса (в окне программа всегда спрашивает разрешение)")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(VERSION)
        return 0
    if args.selftest is not None:
        text = json.dumps(selftest(), ensure_ascii=False, indent=2)
        if args.selftest == "-":
            print(text)
        else:
            with open(args.selftest, "w", encoding="utf-8") as f:
                f.write(text)
        return 0
    core = AgentCore()
    if args.headless:
        core.interactive = False                         # спросить некому: скачивание только с --yes-download
        core.cfg.auto_download = bool(args.yes_download)
    core.start()
    if args.headless:
        print(f"{APP_TITLE} {VERSION}: работаю без окна (Ctrl+C — выход)")
        seen = 0
        try:
            while True:
                time.sleep(1.0)
                new = core.log_total - seen
                if new > 0:
                    for line in list(core.log_lines)[-min(new, len(core.log_lines)):]:
                        try:
                            print(line, flush=True)
                        except (OSError, ValueError, UnicodeError):      # вывод закрыт или не принимает символ — агент от этого работать не перестаёт
                            break
                    seen = core.log_total
        except KeyboardInterrupt:
            pass
        core.stop()
        return 0
    from .gui import AgentWindow  # noqa: PLC0415

    AgentWindow(core, minimized=args.minimized).run()
    core.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
