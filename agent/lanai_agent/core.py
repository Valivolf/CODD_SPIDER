"""Ядро агента: подготовка (железо → модель → llama.cpp), запуск модели, связь с сервером, выполнение заданий. Работает в своих потоках и не зависит от окна: окно (gui.py) только
читает `snapshot()` и меняет настройки через `apply()`; ту же работу делает консольный режим (`--headless`).

Порядок работы (`_supervise`, повторяется при любой смене настроек или сбое):
  1. определить компьютер (hardware.detect) и выбрать видеокарту (слабую не берём) и модель (catalog.recommend), рассчитать параметры (catalog.make_plan);
  2. скачать llama.cpp (cpu или vulkan — по плану) и модель, если их ещё нет (с докачкой);
  3. подключиться к серверу (регистрация по ключу при первом запуске);
  4. запустить llama-server с пониженным приоритетом и замерить скорость; не запустилась на видеокарте — один раз переключиться на процессор;
  5. работать: heartbeat раз в 10 секунд, по потоку-«ячейке» на каждый слот модели — длинный опрос заданий и их выполнение.
Пауза — пока владелец за компьютером (если включено «только в простое»): агент не берёт задания, но остаётся на связи."""
from __future__ import annotations

import collections
import json
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from . import VERSION, catalog, config, download, hardware, hwid, paths, protocol, runtime, system


class _Stop(Exception):
    pass


class _Restart(Exception):
    pass


class _Declined(Exception):
    """Пользователь не разрешил скачивание (или разрешить некому — консольный режим без --yes-download)."""


def fmt_size(n: float) -> str:
    return f"{n / 1e9:.1f} ГБ".replace(".", ",") if n >= 1e9 else f"{max(1, round(n / 1e6))} МБ"


def _model_from_path(path: str) -> catalog.Model:
    """Своя модель из файла (для проверок и опытных пользователей): размер и «миллиарды параметров» — по файлу и имени."""
    import re

    p = Path(path)
    m = re.search(r"(\d+(?:\.\d+)?)\s*[bB]\b|-(\d+(?:\.\d+)?)[bB]-", p.name)
    params = float(m.group(1) or m.group(2)) if m else 3.0
    size = round(p.stat().st_size / 2**30, 2) if p.exists() else 2.0
    kv_kb = max(60.0, params * 37.0)           # кэш контекста по файлу не узнать: берём с запасом (у Qwen3 4B — 147 КБ на токен), иначе на видеокарту отдалось бы слишком много слоёв
    return catalog.Model("custom", p.stem, params, size, 36, kv_kb, "", p.name, "своя модель")


class AgentCore:
    def __init__(self, cfg: Optional[config.Config] = None) -> None:
        self.cfg = cfg or config.load()
        self.hw: Optional[dict[str, Any]] = None
        self.hw_id = hwid.hardware_id()                  # отпечаток железа: по нему сервер привязывает этот компьютер (не по адресу в сети)
        self.server_models: dict[str, dict[str, Any]] = {}      # модели, лежащие на сервере: имя файла → {size, sha256, …} (скачиваются оттуда, а не из интернета)
        self.interactive = True                          # есть окно, которое может спросить разрешение на скачивание (консольный режим — нет)
        self.consent: Optional[dict[str, Any]] = None    # что сейчас ждёт разрешения: {id, what, size, source, note}
        self.notice = ""                                 # последнее замечание для окна (например, сервер не принял ключ) — не затирается сменой этапов
        self._consent_event = threading.Event()
        self._consent_answer: Optional[bool] = None
        self.log_lines: collections.deque[str] = collections.deque(maxlen=400)
        self.log_total = 0
        self.phase = "idle"
        self.text = "Запускаюсь…"
        self.progress: Optional[tuple[int, int, str]] = None
        self.tps = 0.0
        self.prompt_tps = 0.0
        self.jobs_done = 0
        self.jobs_failed = 0
        self.last_job = ""
        self.active = 0
        self.paused = False
        self.connected = False
        self.plan: Optional[catalog.Plan] = None
        self.gpu_failed = False
        self.server_info: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session_stop = threading.Event()
        self._auth_lost = threading.Event()
        self._runtimes: list[runtime.LlamaRuntime] = []
        self._hw_sent = 0.0
        self._on_change: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------- журнал и состояние
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.log_lines.append(line)
        self.log_total += 1
        try:
            with open(paths.logs_dir() / "agent.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _set(self, phase: str, text: str) -> None:
        if (phase, text) != (self.phase, self.text):
            self.phase, self.text = phase, text
            self.log(text)

    def snapshot(self) -> dict[str, Any]:
        plan = self.plan
        return {
            "phase": self.phase, "text": self.text, "progress": self.progress, "tps": self.tps, "prompt_tps": self.prompt_tps, "jobs_done": self.jobs_done, "jobs_failed": self.jobs_failed,
            "last_job": self.last_job, "active": self.active, "paused": self.paused, "connected": self.connected, "log": list(self.log_lines)[-120:],
            "consent": self.consent, "notice": self.notice,
            "plan": None if not plan else {"model": plan.model.title, "backend": plan.backend, "threads": plan.threads, "slots": plan.slots, "gpu": plan.gpu["name"] if plan.gpu else "",
                                           "est_tps": plan.est_tps, "notes": plan.notes, "instances": [{"kind": i.kind, "ngl": i.n_gpu_layers, "threads": i.threads, "est_tps": i.est_tps} for i in plan.instances]},
            "running": self._thread is not None and self._thread.is_alive() and self.cfg.run,
        }

    # ------------------------------------------------------------- управление
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervise, name="agent-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._session_stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=20)

    def apply(self, **changes: Any) -> None:
        """Изменить настройки и применить: сохраняются в файл, работа перезапускается с новыми параметрами."""
        with self._lock:
            for k, v in changes.items():
                if hasattr(self.cfg, k):
                    setattr(self.cfg, k, v)
            self.cfg.clean()
            config.save(self.cfg)
        self._session_stop.set()
        self._wake.set()

    def answer_consent(self, allow: bool) -> None:
        """Ответ пользователя на вопрос «скачать?» (из окна)."""
        self._consent_answer = bool(allow)
        self._consent_event.set()

    def _ask_download(self, key: str, what: str, size_bytes: float, source: str, note: str = "") -> None:
        """Любое скачивание — только с разрешения: в окне спрашивается, что именно, сколько и откуда. Разрешённое один раз (по ключу источника) повторно не спрашивается;
        ответ «нет» — агент ничего не качает и ждёт. Консольный режим без --yes-download спросить не может — отказ."""
        cfg = self.cfg
        if key in cfg.approved_downloads or cfg.auto_download:
            return
        if not self.interactive:
            raise _Declined(f"{what} — в консольном режиме разрешите скачивание ключом --yes-download")
        self.consent = {"id": key, "what": what, "size": size_bytes, "source": source, "note": note}
        self._consent_answer = None
        self._consent_event.clear()
        self._set("consent", f"Жду вашего разрешения: скачать {what} ({fmt_size(size_bytes)}) — {source}")
        try:
            while not self._consent_event.wait(0.5):
                self._check()
        finally:
            answer, self.consent = self._consent_answer, None
        if not answer:
            raise _Declined(what)
        with self._lock:
            if key not in cfg.approved_downloads:
                cfg.approved_downloads.append(key)
            config.save(cfg)

    def hardware_info(self) -> Optional[dict[str, Any]]:
        return self.hw

    def models_view(self) -> dict[str, Any]:
        """Данные для списка моделей в окне: оценка каждой под этот компьютер и рекомендация."""
        if not self.hw:
            return {"ready": False}
        gpu = None if self.gpu_failed else hardware.pick_gpu(self.hw, self.cfg.gpu_mode)
        best, rows = catalog.recommend(self.hw, gpu, self.cfg.limit_pct, self.cfg.ctx_per_slot)
        return {"ready": True, "recommended": best.id, "gpu": gpu["name"] if gpu else "", "rows": [
            {"id": r["model"].id, "title": r["model"].title, "params_b": r["model"].params_b, "size_gb": r["model"].size_gb, "verdict": r["verdict"], "est_tps": r["est_tps"],
             "need_ram_gb": r["need_ram_gb"], "note": r["model"].note, "local": (paths.models_dir() / r["model"].filename).exists(),
             "on_server": r["model"].filename in self.server_models, "family": r["model"].family, "experimental": r["model"].experimental} for r in rows]}

    # ------------------------------------------------------------- вспомогательное
    def _cancelled(self) -> bool:
        return self._stop.is_set() or self._session_stop.is_set()

    def _check(self) -> None:
        if self._stop.is_set():
            raise _Stop()
        if self._session_stop.is_set() or self._wake.is_set():
            raise _Restart()

    def _wait_wake(self, seconds: float) -> None:
        self._wake.wait(seconds)
        if self._stop.is_set():
            raise _Stop()

    def _client(self) -> protocol.ServerClient:
        cfg = self.cfg
        ca = cfg.ca_file or (str(paths.ca_file()) if paths.ca_file().exists() else "")
        return protocol.ServerClient(cfg.server_url, cfg.token, ca, cfg.allow_http, VERSION, self.hw_id)

    # ------------------------------------------------------------- главный цикл
    def _supervise(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self._session_stop = threading.Event()
            self._auth_lost.clear()
            cfg = self.cfg
            try:
                if not cfg.run:
                    self.connected = False
                    self._set("stopped", "Остановлено. Нажмите «Запустить»")
                    self._wait_wake(3600)
                    continue
                if self.hw is None:
                    self._set("hardware", "Определяю процессор, память и видеокарту…")
                    self.hw = hardware.detect()
                    self.log(hardware.describe(self.hw))
                if not cfg.server_url:
                    self._set("need_server", "Вставьте строку подключения из веб-панели (Настройки → ИИ → Ключи подключения)")
                    self._wait_wake(3600)
                    continue
                try:
                    client = self._client()
                except ValueError as exc:
                    self._set("need_server", str(exc))
                    self._wait_wake(3600)
                    continue
                if cfg.enroll_key:                              # вставили новую строку подключения: прежний доступ (если был) остаётся, пока новый ключ не принят
                    self._register(client)
                    cfg = self.cfg
                    client = self._client()
                if not cfg.token:
                    if not cfg.enroll_key and self.phase != "rejected":
                        self._set("need_key", "Нужна строка подключения (её создаёт администратор: «Настройки → ИИ → Ключи подключения»)")
                    self._wait_wake(3600)
                    continue
                self._fetch_server_models(client)
                beat = self._start_prep_beat(client)             # пока готовим (скачиваем, спрашиваем разрешение) — сервер видит компьютер «на связи, готовится»
                try:
                    plan = self._make_plan()
                    self.plan = plan
                    exe = self._ensure_llama(plan)
                    model_path = self._ensure_model(plan)
                finally:
                    beat.set()
                self._session(client, plan, exe, model_path)
            except _Stop:
                break
            except _Restart:
                self.log("Применяю новые настройки…")
                continue
            except protocol.AuthLost:
                self.cfg.token = ""
                self.cfg.agent_id = ""
                config.save(self.cfg)
                self.connected = False
                self._set("auth_lost", "Администратор отвязал этот компьютер от сервера. Чтобы подключиться снова, вставьте новую строку подключения")
                self._wait_wake(3600)
            except protocol.Rejected as exc:
                self.connected = False
                self._set("rejected", f"Сервер отказал: {exc}")
                self._wait_wake(60)
            except protocol.CertError as exc:
                self.connected = False
                self._set("cert", str(exc))
                self._wait_wake(60)
            except _Declined as exc:
                self._set("declined", f"Скачивание не разрешено: {exc}. Без него агент работать не может. Нажмите «Применить настройки», чтобы спросить снова")
                self._wait_wake(3600)
            except download.Cancelled:
                continue
            except Exception as exc:  # noqa: BLE001 — любой сбой: сообщить и повторить через время, а не умирать
                self.connected = False
                self._set("error", f"Сбой: {exc}")
                self.log(traceback.format_exc(limit=3))
                try:
                    self._wait_wake(20)
                except _Stop:
                    break
        self._shutdown_runtime()
        self._set("stopped", "Остановлено")

    def _register(self, client: protocol.ServerClient) -> bool:
        """Привязка по ключу. Отказ сервера (ключ использован, просрочен, неверный, режим выключен…) — окончательный: ключ забывается и повторно НЕ предъявляется (иначе агент
        каждую минуту стучался бы с негодным ключом); прежний доступ, если он был, остаётся. Нет связи — ошибка уходит наверх и привязка повторится."""
        self._set("register", "Подключаюсь к серверу…")
        try:
            res = client.register(self.cfg.enroll_key, self.cfg.name, self.hw or {})
        except protocol.Rejected as exc:
            with self._lock:
                self.cfg.enroll_key = ""
                config.save(self.cfg)
            self.notice = f"Сервер не принял строку подключения: {exc}"
            self.log(self.notice)
            if self.cfg.token:
                self.log("Прежний доступ этого компьютера сохранён — продолжаю с ним")
            else:
                self.connected = False
                self._set("rejected", self.notice + " Вставьте новую строку подключения.")
            return False
        with self._lock:
            self.cfg.token, self.cfg.agent_id, self.cfg.enroll_key = res["token"], res["agent_id"], ""
            if res.get("name"):
                self.cfg.name = res["name"]
            config.save(self.cfg)
        self.notice = ""
        self.log(f"Подключён к серверу как «{self.cfg.name}»")
        return True

    def _start_prep_beat(self, client: protocol.ServerClient) -> threading.Event:
        """Сигналы серверу, пока агент готовится (скачивает, ждёт разрешения): «на связи, модель ещё не готова». Без них сервер считал бы компьютер «не отвечает», а окно
        показывало «нет связи» сразу после успешной привязки."""
        stop = threading.Event()
        beat_client = client.clone()

        def state(first: bool) -> dict[str, Any]:
            plan = self.plan
            st: dict[str, Any] = {"ready": False, "paused": False, "busy": False, "active": 0, "tps": 0, "prompt_tps": 0, "slots": 1, "limit_pct": self.cfg.limit_pct,
                                  "status": self.text[:180], "version": VERSION, "model": plan.model.title if plan else "", "params_b": plan.model.params_b if plan else 0}
            if first:
                st["hardware"] = self.hw or {}
            return st

        def loop() -> None:
            first = True
            while True:
                try:
                    self.server_info = beat_client.heartbeat(state(first))
                    self.connected = True
                    first = False
                except protocol.AuthLost:
                    self._session_stop.set()
                    return
                except protocol.Rejected as exc:
                    self.log(f"Сервер: {exc}")
                except protocol.NetworkError:
                    self.connected = False
                if stop.wait(10.0):
                    return

        threading.Thread(target=loop, name="agent-prep-beat", daemon=True).start()
        return stop

    def _fetch_server_models(self, client: protocol.ServerClient) -> None:
        """Какие модели есть на сервере (администратор кладёт туда средние модели): их скачиваем оттуда — быстро и без интернета. Не получилось узнать — не страшно."""
        try:
            files = client.models()
        except (protocol.NetworkError, protocol.Rejected, protocol.AuthLost) as exc:
            self.log(f"Список моделей на сервере не получен: {exc}")
            return
        self.server_models = {f["file"]: f for f in files if isinstance(f, dict) and f.get("file")}
        if self.server_models:
            self.log("Модели на сервере: " + ", ".join(sorted(self.server_models)))

    def _make_plan(self) -> catalog.Plan:
        cfg, hw = self.cfg, self.hw or hardware.detect()
        gpu = None if self.gpu_failed else hardware.pick_gpu(hw, cfg.gpu_mode)
        if cfg.model_path and Path(cfg.model_path).exists():
            model = _model_from_path(cfg.model_path)
        elif cfg.model_id != "auto" and cfg.model_id in catalog.BY_ID:
            model = catalog.BY_ID[cfg.model_id]
        else:
            model = catalog.recommend(hw, gpu, cfg.limit_pct, cfg.ctx_per_slot)[0]
        plan = catalog.make_plan(hw, cfg.limit_pct, gpu, model, cfg.ctx_per_slot)
        parts = [f"{ {'gpu': 'видеокарта', 'cpu': 'процессор', 'hybrid': 'видеокарта+процессор'}[i.kind] } (потоков {i.threads}, ≈{i.est_tps} ток/с)" for i in plan.instances]
        self.log(f"План: {model.title}; " + " + ".join(parts) + f"; ячеек {plan.slots}" + (("; " + "; ".join(plan.notes)) if plan.notes else ""))
        if gpu is None and any(g["verdict"] == "weak" for g in hw.get("gpus", [])) and cfg.gpu_mode == "auto":
            self.log("Подходящей видеокарты нет (встроенная графика или совсем мало видеопамяти) — считаю на процессоре")
        return plan

    def _progress(self, label: str) -> Callable[[int, int], None]:
        def cb(done: int, total: int) -> None:
            self.progress = (done, total, label)
        return cb

    def _ensure_llama(self, plan: catalog.Plan) -> Path:
        cfg = self.cfg
        if cfg.llama_exe and Path(cfg.llama_exe).exists():
            return Path(cfg.llama_exe)
        tag = cfg.llama_tag if catalog.valid_tag(cfg.llama_tag) else catalog.BUNDLED_LLAMA_TAG
        target = paths.bin_dir() / f"{plan.backend}-{tag}"
        found = download.find_file(target, "llama-server.exe") if target.exists() else None
        if found:
            return found
        bundled = paths.resource_dir() / "llama" / catalog.asset_name(tag, plan.backend)
        if tag == catalog.BUNDLED_LLAMA_TAG and bundled.exists():          # llama.cpp зашит в программу: сети и разрешений не нужно — просто распаковываем
            self._set("unpack_llama", f"Распаковываю встроенный llama.cpp ({'для видеокарты' if plan.backend == 'vulkan' else 'для процессора'})…")
            try:
                download.extract_zip(bundled, target)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)               # недораспакованное не оставляем
                raise
            found = download.find_file(target, "llama-server.exe")
            if not found:
                raise RuntimeError("Во встроенном llama.cpp нет llama-server.exe")
            self.log("llama.cpp распакован из самой программы (без скачивания)")
            return found
        self._ask_download(f"llama:{tag}:{plan.backend}", "llama.cpp — программу запуска модели", 37e6 if plan.backend == "vulkan" else 24e6, "github.com/ggml-org/llama.cpp (интернет)",
                           "без неё модель на этом компьютере не запустить")
        self._set("download_llama", f"Скачиваю llama.cpp ({'для видеокарты' if plan.backend == 'vulkan' else 'для процессора'}, около 25–40 МБ)…")
        zip_path = paths.bin_dir() / catalog.asset_name(tag, plan.backend)
        try:
            download.download(catalog.asset_url(tag, plan.backend), zip_path, progress=self._progress("llama.cpp"), stop=self._session_stop)
            self.progress = None
            self._set("unpack_llama", "Распаковываю llama.cpp…")
            download.extract_zip(zip_path, target)
        except Exception:
            if plan.backend == "vulkan":
                raise
            raise
        finally:
            self.progress = None
        found = download.find_file(target, "llama-server.exe")
        if not found:
            raise RuntimeError("В скачанном llama.cpp нет llama-server.exe")
        try:
            zip_path.unlink()
        except OSError:
            pass
        return found

    def _ensure_model(self, plan: catalog.Plan) -> Path:
        cfg = self.cfg
        if cfg.model_path and Path(cfg.model_path).exists():
            return Path(cfg.model_path)
        m = plan.model
        dest = paths.models_dir() / m.filename
        on_server = self.server_models.get(m.filename)
        expected = int((on_server or {}).get("size") or 0) or int(m.size_gb * 1e9)            # размеры в каталоге — в ГБ по 10⁹ байт (как у Hugging Face)
        if dest.exists() and dest.stat().st_size >= expected * 0.97:
            return dest
        if on_server:
            try:
                self._ask_download(f"model-server:{m.filename}", f"модель «{m.title}»", float(on_server.get("size") or m.size_gb * 1e9), "с вашего сервера (не из интернета)")
                return self._download_from_server(m, on_server, dest)
            except (download.Cancelled, _Declined, _Restart, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — сервер не отдал: пробуем интернет (с отдельным разрешением)
                self.log(f"С сервера модель не скачалась ({exc}) — можно попробовать из интернета")
        self._ask_download(f"model-hf:{m.filename}", f"модель «{m.title}»", m.size_gb * 1e9, "huggingface.co (интернет)", m.note)
        self._set("download_model", f"Скачиваю модель «{m.title}» ({m.size_gb:.1f} ГБ) из интернета — это один раз…")
        try:
            download.download(m.url, dest, expected_size=expected, progress=self._progress(m.title), stop=self._session_stop)
        finally:
            self.progress = None
        return dest

    def _download_from_server(self, m: catalog.Model, info: dict[str, Any], dest: Path) -> Path:
        """Модель с сервера организации: по токену и железу этого компьютера, с докачкой; целостность — по размеру и SHA-256 (если сервер его посчитал)."""
        size = int(info.get("size") or 0)
        self._set("download_model", f"Скачиваю модель «{m.title}» ({size / 1e9:.1f} ГБ) с сервера — это один раз…")
        url, headers, ctx = self._client().model_download_args(m.filename)
        try:
            download.download(url, dest, expected_size=size, progress=self._progress(m.title), stop=self._session_stop, headers=headers, verify=ctx)
        finally:
            self.progress = None
        got_size = dest.stat().st_size
        if size and got_size != size:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"размер файла {got_size} не совпал с серверным {size}")
        want = (info.get("sha256") or "").lower()
        if want:
            self._set("verify_model", f"Проверяю целостность модели «{m.title}»…")
            try:
                got = download.sha256_file(dest, progress=self._progress("проверка"), stop=self._session_stop)
            finally:
                self.progress = None
            if got != want:
                dest.unlink(missing_ok=True)
                raise RuntimeError("контрольная сумма файла не совпала с серверной")
        self.log(f"Модель «{m.title}» получена с сервера")
        return dest

    def _shutdown_runtime(self) -> None:
        rts, self._runtimes = self._runtimes, []
        for rt in rts:
            rt.stop()

    # ------------------------------------------------------------- сеанс работы
    def _start_instance(self, exe: Path, model_path: Path, plan: catalog.Plan, inst: catalog.Instance) -> runtime.LlamaRuntime:
        """Запустить один экземпляр llama-server; неизвестный параметр новой версии — повтор с минимальным набором."""
        rt = runtime.LlamaRuntime(exe, model_path, plan, inst, self.log)
        rt.start()
        time.sleep(2.0)
        if not rt.alive():
            self.log("llama-server не принял параметры, пробую с минимальным набором: " + rt.log_tail(3))
            rt.stop()
            rt = runtime.LlamaRuntime(exe, model_path, plan, inst, self.log)
            rt.minimal = True
            rt.start()
        return rt

    def _session(self, client: protocol.ServerClient, plan: catalog.Plan, exe: Path, model_path: Path) -> None:
        stop = self._session_stop                                # событие ЭТОГО сеанса: потоки следующего сеанса его не «оживят»
        rts: list[runtime.LlamaRuntime] = []
        self._runtimes = rts
        try:
            self._set("loading", f"Загружаю модель «{plan.model.title}» в память…")
            started: list[runtime.LlamaRuntime] = []
            for inst in plan.instances:
                started.append(self._start_instance(exe, model_path, plan, inst))
            self._runtimes = started
            for rt in list(started):
                try:
                    rt.wait_ready(900.0, self._cancelled)
                except runtime.LlamaStartError as exc:
                    if self._cancelled():
                        raise _Restart() from None
                    rt.stop()
                    started.remove(rt)
                    if rt.inst.kind in ("gpu", "hybrid"):
                        self.gpu_failed = True
                        self.log(f"На видеокарте модель не запустилась ({exc})" + (" — продолжаю на процессоре" if started else " — перехожу на процессор"))
                        if not started:
                            raise _Restart() from None
                    elif not started:
                        raise RuntimeError(str(exc)) from None
                    else:
                        self.log(f"Экземпляр на процессоре не запустился ({exc}) — работаю на остальных")
            rts = started
            self._runtimes = rts
            self._check()
            self._set("benchmark", "Замеряю скорость…")
            speeds = []
            for rt in rts:
                rt.benchmark()
                speeds.append(rt.tps)
                self.log(f"Скорость ({ {'gpu': 'видеокарта', 'cpu': 'процессор', 'hybrid': 'видеокарта+процессор'}.get(rt.inst.kind, '') }): {rt.tps} ток/с (чтение подсказки {rt.prompt_tps} ток/с)")
            self.tps, self.prompt_tps = max(speeds), max(rt.prompt_tps for rt in rts)
            client = client.clone()
            first = client.heartbeat(self._state(rts, plan, with_hardware=True))
            self.connected = True
            self.server_info = first
            threads = [threading.Thread(target=self._heartbeat_loop, args=(client.clone(), rts, plan, stop), daemon=True, name="agent-heartbeat")]
            n = 0
            for rt in rts:
                for _ in range(rt.inst.slots):
                    threads.append(threading.Thread(target=self._worker, args=(client.clone(), rt, stop), daemon=True, name=f"agent-worker-{n}"))
                    n += 1
            for t in threads:
                t.start()
            where = " + ".join({"gpu": "видеокарта", "cpu": "процессор", "hybrid": "видеокарта и процессор вместе"}[rt.inst.kind] for rt in rts)
            self._set("ready", f"Работаю: {plan.model.title} ({where}), до ≈{self.tps} ток/с. Жду задания от сервера")
            while True:
                if self._stop.is_set():
                    raise _Stop()
                if self._wake.is_set() or stop.is_set():
                    raise _Restart()
                if self._auth_lost.is_set():
                    raise protocol.AuthLost()
                dead = [rt for rt in rts if not rt.alive()]
                if dead:
                    if any(rt.inst.kind in ("gpu", "hybrid") for rt in dead):
                        self.gpu_failed = True
                    raise RuntimeError("llama-server остановился: " + dead[0].log_tail(3))
                self._update_pause()
                time.sleep(1.0)
        finally:
            stop.set()
            was_connected, self.connected, self.active = self.connected, False, 0
            for rt in {id(r): r for r in [*rts, *self._runtimes]}.values():
                rt.stop()
            self._runtimes = []
            if was_connected:                                    # попрощаться с сервером: он сразу перестанет отдавать этому компьютеру задания (иначе — через ~полминуты тишины)
                try:
                    bye = client.clone()
                    bye._client.timeout = 5.0
                    bye.heartbeat({"bye": True})
                    bye.close()
                except Exception:  # noqa: BLE001
                    pass

    def _update_pause(self) -> None:
        cfg = self.cfg
        paused = bool(cfg.only_idle and system.idle_seconds() < cfg.idle_minutes * 60)
        if paused != self.paused:
            self.paused = paused
            self._set("paused" if paused else "ready", "Пауза: вы за компьютером — задания не беру" if paused else f"Работаю, жду задания (≈{self.tps} ток/с)")

    def _state(self, rts: list[runtime.LlamaRuntime], plan: catalog.Plan, with_hardware: bool = False) -> dict[str, Any]:
        st: dict[str, Any] = {
            "ready": any(rt.alive() for rt in rts), "paused": self.paused, "busy": self.active > 0, "active": self.active, "tps": self.tps, "prompt_tps": self.prompt_tps,
            "params_b": plan.model.params_b, "slots": sum(rt.inst.slots for rt in rts), "model": plan.model.title, "gpu_used": any(rt.inst.kind in ("gpu", "hybrid") for rt in rts),
            "gpu": plan.gpu["name"] if plan.gpu and any(rt.inst.kind in ("gpu", "hybrid") for rt in rts) else "", "limit_pct": self.cfg.limit_pct, "status": self.text[:180], "version": VERSION,
        }
        if with_hardware or time.monotonic() - self._hw_sent > 600:
            st["hardware"] = self.hw or {}
            self._hw_sent = time.monotonic()
        return st

    def _heartbeat_loop(self, client: protocol.ServerClient, rts: list[runtime.LlamaRuntime], plan: catalog.Plan, stop: threading.Event) -> None:
        while not stop.wait(10.0):
            try:
                self.server_info = client.heartbeat(self._state(rts, plan))
                self.connected = True
            except protocol.AuthLost:
                self._auth_lost.set()
                return
            except protocol.Rejected as exc:
                self.log(f"Сервер: {exc}")
            except protocol.NetworkError as exc:
                if self.connected:
                    self.log(f"Связь с сервером потеряна: {exc}")
                self.connected = False

    def _worker(self, client: protocol.ServerClient, rt: runtime.LlamaRuntime, stop: threading.Event) -> None:
        backoff = 3.0
        while not stop.is_set():
            if self.paused:
                time.sleep(1.0)
                continue
            try:
                job = client.next_job(20.0)
                backoff = 3.0
            except protocol.AuthLost:
                self._auth_lost.set()
                return
            except protocol.Rejected:
                time.sleep(10.0)                                      # режим выключен на сервере — ждём, остаёмся на связи
                continue
            except protocol.NetworkError:
                time.sleep(backoff)
                backoff = min(30.0, backoff * 1.7)
                continue
            if not job:
                continue
            if stop.is_set():                                          # сеанс уже завершён — задание отдаём обратно серверу ошибкой, он передаст другому
                try:
                    client.result(job["job_id"], {"error": "агент перезапускается"})
                except Exception:  # noqa: BLE001
                    pass
                return
            self._run_job(client, rt, job, stop)

    def _run_job(self, client: protocol.ServerClient, rt: runtime.LlamaRuntime, job: dict[str, Any], stop: threading.Event) -> None:
        jid = job["job_id"]
        started = time.monotonic()
        self.active += 1
        result: dict[str, Any]
        try:
            if stop.is_set() or not rt.alive():
                result = {"error": "агент перезапускается"}
            else:
                payload = runtime.sanitize(job["payload"])
                status, body, text = rt.chat(payload)
                result = {"status": status, "body": body, "text": text, "timings": (body or {}).get("timings")}
                if status < 500:
                    t = (body or {}).get("timings") or {}
                    if t.get("predicted_per_second"):
                        rt.tps = round(float(t["predicted_per_second"]) * 0.5 + rt.tps * 0.5, 1)
                    self.jobs_done += 1
                else:
                    self.jobs_failed += 1
        except Exception as exc:  # noqa: BLE001 — любой сбой задания: сообщить серверу, он отдаст другому
            self.jobs_failed += 1
            result = {"error": f"{exc.__class__.__name__}: {exc}"[:380]}
        finally:
            self.active = max(0, self.active - 1)
        self.last_job = f"{time.strftime('%H:%M:%S')} — {'ошибка: ' + result['error'] if result.get('error') else 'готово за ' + str(round(time.monotonic() - started, 1)) + ' с'}"
        try:
            client.result(jid, result)
        except protocol.AuthLost:
            self._auth_lost.set()
        except (protocol.NetworkError, protocol.Rejected) as exc:
            self.log(f"Результат задания не доставлен: {exc}")
