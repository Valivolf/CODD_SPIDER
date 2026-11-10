"""Окно агента (tkinter — входит в Python и в exe, ничего дополнительного ставить не нужно).

Окно только показывает состояние ядра (`AgentCore.snapshot()`, обновление дважды в секунду) и передаёт изменения настроек (`AgentCore.apply`) — всё тяжёлое делают потоки ядра."""
from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from typing import Any

from . import APP_TITLE, VERSION, catalog, hardware, hwid, paths, protocol, system, textmenu
from .core import AgentCore

GPU_LABELS = {"auto": "Автоматически (подходящую карту — вместе с процессором)", "off": "Не использовать видеокарту", "force": "Использовать любую (на свой риск)"}
GPU_BY_LABEL = {v: k for k, v in GPU_LABELS.items()}
AUTO_LABEL = "Подобрать автоматически (рекомендуется)"
PHASE_COLOR = {"ready": "#15803d", "paused": "#b45309", "error": "#b91c1c", "rejected": "#b91c1c", "auth_lost": "#b91c1c", "stopped": "#64748b"}


class AgentWindow:
    def __init__(self, core: AgentCore, minimized: bool = False) -> None:
        self.core = core
        self.root = tk.Tk()
        textmenu.install(self.root)                      # правая кнопка и Ctrl+V/C/X/A при любой раскладке — во всех полях окна
        self.root.title(f"{APP_TITLE} — {VERSION}")
        self.root.geometry("780x860")
        self.root.minsize(720, 700)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.model_rows: list[dict[str, Any]] = []
        self.models_shown_for = ""
        self._cert_asked = False
        self._consent_shown = ""
        self._build()
        if minimized:
            self.root.iconify()
        self.root.after(300, self.tick)

    # ------------------------------------------------------------- разметка
    def _build(self) -> None:
        r = self.root
        pad = {"padx": 10, "pady": 4}
        ttk.Style().configure("Big.TLabel", font=("Segoe UI", 11, "bold"))
        cfg = self.core.cfg

        box = ttk.LabelFrame(r, text=" Подключение к серверу ")
        box.pack(fill="x", **pad)
        self.var_conn, self.var_name = tk.StringVar(value=cfg.enroll_key and f"{cfg.server_url}  {cfg.enroll_key}".strip() or ""), tk.StringVar(value=cfg.name)
        ttk.Label(box, text="Строка подключения").grid(row=0, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(box, textvariable=self.var_conn, width=52).grid(row=0, column=1, sticky="we", padx=8, pady=3)
        ttk.Button(box, text="Вставить из буфера", command=self.on_paste).grid(row=0, column=2, padx=(0, 8), pady=3)
        ttk.Label(box, text="Имя этого компьютера").grid(row=1, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(box, textvariable=self.var_name, width=52).grid(row=1, column=1, sticky="we", padx=8, pady=3)
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="Администратор создаёт её в веб-панели: Настройки → ИИ → «Создать ключ» — одна строка вида lcai://адрес-сервера/ключ, адрес уже внутри. Ключ одноразовый: "
                  "после подключения он больше не действует, а компьютер привязывается к своему оборудованию (не к адресу в сети) и остаётся в списке сервера, пока администратор сам его не отвяжет. "
                  "Вставить: правая кнопка мыши → «Вставить», кнопка справа или Ctrl+V (работает и на русской раскладке).",
                  foreground="#64748b", wraplength=720, justify="left").grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 2))
        btns = ttk.Frame(box)
        btns.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Button(btns, text="Подключиться", command=self.on_connect).pack(side="left")
        ttk.Button(btns, text="Доверять сертификату сервера…", command=self.on_trust).pack(side="left", padx=8)
        self.lbl_conn = ttk.Label(box, text="", foreground="#64748b", wraplength=720, justify="left")
        self.lbl_conn.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        self._offer_clipboard()

        hw = ttk.LabelFrame(r, text=" Этот компьютер ")
        hw.pack(fill="x", **pad)
        self.lbl_hw = ttk.Label(hw, text="Определяю…", justify="left", wraplength=700)
        self.lbl_hw.pack(anchor="w", padx=8, pady=6)

        res = ttk.LabelFrame(r, text=" Сколько ресурсов отдавать ")
        res.pack(fill="x", **pad)
        row = ttk.Frame(res)
        row.pack(fill="x", padx=8, pady=4)
        self.var_pct = tk.IntVar(value=cfg.limit_pct)
        ttk.Label(row, text="Доля процессора, памяти и видеопамяти:").pack(side="left")
        self.lbl_pct = ttk.Label(row, text=f"{cfg.limit_pct} %", style="Big.TLabel", width=6)
        self.lbl_pct.pack(side="right")
        ttk.Scale(row, from_=10, to=100, orient="horizontal", variable=self.var_pct, command=lambda v: self.lbl_pct.config(text=f"{int(float(v))} %")).pack(side="right", fill="x", expand=True, padx=8)
        row2 = ttk.Frame(res)
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Label(row2, text="Видеокарта:").pack(side="left")
        self.var_gpu = tk.StringVar(value=GPU_LABELS.get(cfg.gpu_mode, GPU_LABELS["auto"]))
        ttk.Combobox(row2, textvariable=self.var_gpu, values=list(GPU_LABELS.values()), state="readonly", width=62).pack(side="left", padx=8)
        row3 = ttk.Frame(res)
        row3.pack(fill="x", padx=8, pady=2)
        self.var_idle = tk.BooleanVar(value=cfg.only_idle)
        ttk.Checkbutton(row3, text="Работать, только пока я не за компьютером — простой не меньше", variable=self.var_idle).pack(side="left")
        self.var_idle_min = tk.IntVar(value=cfg.idle_minutes)
        ttk.Spinbox(row3, from_=1, to=240, width=4, textvariable=self.var_idle_min).pack(side="left", padx=4)
        ttk.Label(row3, text="мин.").pack(side="left")
        self.var_auto = tk.BooleanVar(value=system.autostart_enabled())
        ttk.Checkbutton(res, text="Запускать при входе в Windows (свёрнутым)", variable=self.var_auto).pack(anchor="w", padx=8, pady=2)
        ttk.Label(res, text="Нагрузка идёт с пониженным приоритетом — ваша работа не должна тормозить. Изменения применяются кнопкой «Применить» (модель перезапускается).",
                  foreground="#64748b", wraplength=700, justify="left").pack(anchor="w", padx=8, pady=(0, 6))

        mod = ttk.LabelFrame(r, text=" Модель ")
        mod.pack(fill="x", **pad)
        self.var_model = tk.StringVar()
        self.cmb_model = ttk.Combobox(mod, textvariable=self.var_model, state="readonly", width=92)
        self.cmb_model.pack(fill="x", padx=8, pady=4)
        self.lbl_model = ttk.Label(mod, text="", foreground="#64748b", wraplength=700, justify="left")
        self.lbl_model.pack(anchor="w", padx=8, pady=(0, 4))
        ttk.Button(mod, text="Применить настройки", command=self.on_apply).pack(anchor="w", padx=8, pady=(0, 6))

        foot = ttk.Frame(r)
        foot.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        self.btn_run = ttk.Button(foot, text="Остановить", command=self.on_toggle)
        self.btn_run.pack(side="left")
        ttk.Button(foot, text="Свернуть", command=self.root.iconify).pack(side="left", padx=8)
        ttk.Button(foot, text="Выйти из программы", command=self.on_quit).pack(side="right")

        st = ttk.LabelFrame(r, text=" Состояние ")
        st.pack(fill="both", expand=True, **pad)
        self.lbl_state = ttk.Label(st, text="", style="Big.TLabel", wraplength=700, justify="left")
        self.lbl_state.pack(anchor="w", padx=8, pady=(6, 2))
        self.bar = ttk.Progressbar(st, mode="determinate", maximum=100)
        self.lbl_prog = ttk.Label(st, text="", foreground="#64748b")
        self.lbl_stats = ttk.Label(st, text="", foreground="#334155")
        self.lbl_stats.pack(anchor="w", padx=8)
        self.txt = tk.Text(st, height=6, wrap="word", state="disabled", font=("Consolas", 9), background="#f8fafc")
        self.txt.pack(fill="both", expand=True, padx=8, pady=6)

    # ------------------------------------------------------------- действия
    def _offer_clipboard(self) -> None:
        """Не подключены, а в буфере обмена уже лежит строка подключения (администратор прислал и человек её скопировал) — подставляем сами."""
        cfg = self.core.cfg
        if cfg.token or self.var_conn.get().strip():
            return
        try:
            text = self.root.clipboard_get()
            url, key = protocol.parse_connection(text)
        except (tk.TclError, ValueError):
            return
        if key and url:
            self.var_conn.set(text.strip())

    def on_paste(self) -> None:
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showinfo(APP_TITLE, "В буфере обмена нет текста. Скопируйте строку подключения из веб-панели и повторите.")
            return
        self.var_conn.set(text.strip())

    def _trust_dialog(self, url: str) -> bool:
        """Сервер с собственным сертификатом организации: показать отпечаток и спросить, доверять ли ему (администратор сверяет отпечаток в браузере)."""
        try:
            path, fp = protocol.fetch_server_ca(url)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Не удалось получить сертификат сервера: {exc}")
            return False
        pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
        if messagebox.askyesno(APP_TITLE, "Сервер использует собственный сертификат организации.\n\nОтпечаток SHA-256:\n" + pretty + "\n\nСверьте его с отпечатком, который показывает "
                               "администратор сервера (в браузере: замок → Сертификат → Отпечатки). Доверять этому сертификату?"):
            self.core.apply(ca_file=str(path))
            return True
        try:
            path.unlink()
        except OSError:
            pass
        return False

    def _server_url(self) -> str:
        """Адрес сервера: из строки подключения в поле или уже сохранённый."""
        try:
            url, _ = protocol.parse_connection(self.var_conn.get())
        except ValueError:
            url = ""
        return protocol.normalize_url(url or self.core.cfg.server_url, self.core.cfg.allow_http)

    def on_connect(self) -> None:
        cfg = self.core.cfg
        text = self.var_conn.get().strip()
        changes: dict[str, Any] = {"name": self.var_name.get()}
        if text:
            try:
                url, key = protocol.parse_connection(text)
                url = protocol.normalize_url(url or cfg.server_url, cfg.allow_http)
            except ValueError as exc:
                messagebox.showwarning(APP_TITLE, str(exc))
                return
            ca = cfg.ca_file or (str(paths.ca_file()) if paths.ca_file().exists() else "")
            self.root.config(cursor="watch")
            self.root.update_idletasks()
            try:
                need_trust = protocol.needs_trust(url, ca) if url.startswith("https://") else False
            finally:
                self.root.config(cursor="")
            if need_trust and not self._trust_dialog(url):
                return
            changes.update(server_url=url, enroll_key=key)                  # прежний доступ остаётся, пока новый ключ не принят; другой сервер — прежний токен ему не нужен
            if url != cfg.server_url:
                changes.update(token="", agent_id="")
        elif not cfg.token:
            messagebox.showwarning(APP_TITLE, "Вставьте строку подключения из веб-панели (кнопка «Вставить из буфера» или правая кнопка мыши → «Вставить»).")
            return
        self.core.apply(**changes)
        self.var_conn.set("")
        self._cert_asked = False

    def _ask_consent(self, c: dict[str, Any]) -> None:
        """Разрешение на скачивание: что, сколько и откуда. Ничего не качается, пока не нажато «Да»."""
        from .core import fmt_size  # noqa: PLC0415

        text = (f"Программе нужно скачать:\n\n  {c['what']}\n  размер: {fmt_size(c['size'])}\n  откуда: {c['source']}\n\n"
                + (f"{c['note']}\n\n" if c.get("note") else "") + "Скачивается один раз и хранится на этом компьютере. Разрешить?")
        self.core.answer_consent(messagebox.askyesno(APP_TITLE, text))

    def on_trust(self) -> None:
        try:
            url = self._server_url()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc) or "Сначала вставьте строку подключения — в ней адрес сервера.")
            return
        self._trust_dialog(url)

    def on_apply(self) -> None:
        label = self.var_model.get()
        model_id = "auto"
        if label and label != AUTO_LABEL:
            idx = self.cmb_model.current()
            if 1 <= idx <= len(self.model_rows):
                model_id = self.model_rows[idx - 1]["id"]
        system.set_autostart(bool(self.var_auto.get()))
        self.core.apply(limit_pct=int(self.var_pct.get()), gpu_mode=GPU_BY_LABEL.get(self.var_gpu.get(), "auto"), only_idle=bool(self.var_idle.get()),
                        idle_minutes=int(self.var_idle_min.get() or 5), autostart=bool(self.var_auto.get()), model_id=model_id, name=self.var_name.get())

    def on_toggle(self) -> None:
        self.core.apply(run=not self.core.cfg.run)

    def on_close(self) -> None:
        if messagebox.askyesno(APP_TITLE, "Закрыть программу? Тогда этот компьютер перестанет помогать серверу.\n\n(Чтобы оставить её работать, нажмите «Свернуть».)"):
            self.on_quit()

    def on_quit(self) -> None:
        self.core.stop()
        self.root.destroy()

    # ------------------------------------------------------------- обновление окна
    def _fill_models(self) -> None:
        view = self.core.models_view()
        if not view.get("ready"):
            return
        sig = f"{self.core.cfg.limit_pct}|{self.core.cfg.gpu_mode}|{self.core.gpu_failed}|{len(self.core.server_models)}"
        if sig == self.models_shown_for:
            return
        self.models_shown_for = sig
        self.model_rows = view["rows"]
        values = [AUTO_LABEL]
        for r in view["rows"]:
            mark = catalog.VERDICT_TEXT.get(r["verdict"], r["verdict"])
            where = "уже скачана" if r["local"] else ("скачается с сервера" if r.get("on_server") else "скачается из интернета")
            values.append(f"{r['title']}  ·  {mark}  ·  ≈{r['est_tps']} ток/с  ·  файл {r['size_gb']} ГБ  ·  {where}{'  ·  новая, не проверена' if r.get('experimental') else ''}")
        keep = self.cmb_model.current()
        self.cmb_model["values"] = values
        cur = self.core.cfg.model_id
        if cur != "auto" and any(r["id"] == cur for r in view["rows"]):
            self.cmb_model.current(1 + [r["id"] for r in view["rows"]].index(cur))
        elif keep < 0 or self.core.cfg.model_id == "auto":
            self.cmb_model.current(0)
        rec = next((r for r in view["rows"] if r["id"] == view["recommended"]), None)
        gpu_text = f"; видеокарта: {view['gpu']}" if view["gpu"] else "; на процессоре"
        self.lbl_model.config(text=(f"Для этого компьютера рекомендуем: {rec['title']}{gpu_text}. Модели больше 3 млрд параметров сервер доверяет и вопросы людей, меньшие — только фоновые задачи. "
                                    "Модель скачивается один раз и хранится на этом компьютере.") if rec else "")

    def tick(self) -> None:
        core = self.core
        try:
            snap = core.snapshot()
            if core.hw is not None and self.lbl_hw.cget("text").startswith("Определяю"):
                self.lbl_hw.config(text=hardware.describe(core.hw) + f"\nПривязка к оборудованию: код {hwid.short(core.hw_id)} (то же видно в списке компьютеров на сервере)")
            self._fill_models()
            self.lbl_state.config(text=snap["text"], foreground=PHASE_COLOR.get(snap["phase"], "#0f172a"))
            self.btn_run.config(text="Остановить" if core.cfg.run else "Запустить")
            where = f"   ·   сервер: {core.cfg.server_url}" if core.cfg.server_url else ""
            consent = snap.get("consent")
            if consent and self._consent_shown != consent["id"]:
                self._consent_shown = consent["id"]
                self._ask_consent(consent)
            elif not consent:
                self._consent_shown = ""
            self.lbl_conn.config(text=(("● подключён к серверу" if snap["connected"] else "● нет связи с сервером") if core.cfg.token else "● не подключён: вставьте строку подключения") + where
                                 + (f"\n⚠ {snap['notice']}" if snap.get("notice") else ""),
                                 foreground="#15803d" if snap["connected"] and not snap.get("notice") else "#b91c1c")
            if snap["phase"] == "cert" and not self._cert_asked:
                self._cert_asked = True
                try:
                    self._trust_dialog(protocol.normalize_url(core.cfg.server_url, core.cfg.allow_http))
                except ValueError:
                    pass
            elif snap["phase"] != "cert":
                self._cert_asked = False
            if snap["progress"]:
                done, total, label = snap["progress"]
                if not self.bar.winfo_ismapped():
                    self.bar.pack(fill="x", padx=8, pady=2, before=self.lbl_stats)
                    self.lbl_prog.pack(anchor="w", padx=8, before=self.lbl_stats)
                self.bar["value"] = (done / total * 100) if total else 0
                self.lbl_prog.config(text=f"{label}: {done // 2**20} МБ из {total // 2**20 if total else '?'} МБ")
            elif self.bar.winfo_ismapped():
                self.bar.pack_forget()
                self.lbl_prog.pack_forget()
            p = snap["plan"]
            where = " + ".join({"gpu": "видеокарта", "cpu": "процессор", "hybrid": "видеокарта и процессор вместе"}.get(i["kind"], i["kind"]) for i in p["instances"]) if p else ""
            self.lbl_stats.config(text=(f"Модель: {p['model']} · {where} · ячеек {p['slots']}   |   " if p else "")
                                  + f"скорость {snap['tps']} ток/с · выполнено {snap['jobs_done']} · ошибок {snap['jobs_failed']}" + (" · сейчас занят" if snap["active"] else "")
                                  + (f"\nПоследнее задание: {snap['last_job']}" if snap["last_job"] else ""))
            log = "\n".join(snap["log"][-60:])
            if log != getattr(self, "_log", ""):
                self._log = log
                self.txt.config(state="normal")
                self.txt.delete("1.0", "end")
                self.txt.insert("end", log)
                self.txt.see("end")
                self.txt.config(state="disabled")
        except tk.TclError:
            return
        self.root.after(500, self.tick)

    def run(self) -> None:
        self.root.mainloop()
