"""Каталог моделей, подбор модели под компьютер и расчёт параметров запуска llama.cpp (в том числе одновременная работа процессора и видеокарты).

Модели — в формате GGUF (Q4_K_M: хороший баланс качества и размера): в основном семейство Qwen (та же линейка, что на сервере, Qwen2.5-3B) и несколько других семейств для выбора
(Gemma, Ministral, Llama, Phi). Часть моделей средней величины (4–8 млрд) администратор кладёт на сервер — компьютеры скачивают их оттуда (быстро, без интернета; см. `hosted_files`),
остальные скачиваются из интернета (Hugging Face). Размеры сверены с
Hugging Face. Подбор (`recommend`) исходит из того, что у компьютера есть ГОД: сколько памяти разрешено занять (доля limit_pct от всей памяти минус запас для Windows и
других программ), есть ли сильная видеокарта и какого размера модель в неё помещается. Скорость генерации оценивается по пропускной способности памяти (на процессоре
генерация упирается в неё): ориентировочно, настоящую скорость агент замеряет сам после запуска и сообщает серверу — по ней сервер и выбирает, кому отдать запрос.

Вердикт у каждой модели: «recommended» (рекомендуем), «ok» (подходит), «slow» (поместится, но будет медленно), «no_fit» (не хватит памяти)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

GB = 1.0
OS_RESERVE_GB = 3.0                # память, которую агент не трогает (Windows и программы владельца)
RUNTIME_OVERHEAD_GB = 0.8          # служебные буферы llama.cpp
MIN_CPU_TPS = 4.0                  # медленнее на процессоре — для живого ответа помощника не годится
MIN_GPU_TPS = 12.0
VRAM_OVERHEAD_GB = 0.7
SERVER_MIN_PARAMS_B = 3.0           # такие и более крупные модели сервер берёт для вопросов людей (настройка сервера «min_params_b»)
GPU_BW = {"use": 300.0, "hybrid": 110.0}       # пропускная способность видеопамяти, ГБ/с (ориентир: GTX 1050 ≈ 112, RTX 3060 ≈ 360)


@dataclass(frozen=True)
class Model:
    id: str
    title: str
    params_b: float
    size_gb: float                 # размер файла GGUF
    layers: int
    kv_kb: float                   # килобайт кэша контекста на один токен (fp16)
    url: str
    filename: str
    note: str = ""
    family: str = ""               # семейство (для списка в окне): Qwen, Gemma, Mistral, Llama, Phi
    experimental: bool = False     # новая модель, на запросах нашего сервера ещё не проверялась


def _hf(repo: str, file: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{file}"


MODELS: list[Model] = [
    Model("qwen25-1.5b", "Qwen2.5 1.5B — быстрая и простая", 1.5, 0.99, 28, 28.7, _hf("bartowski/Qwen2.5-1.5B-Instruct-GGUF", "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"), "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
          "для слабых компьютеров; сервер доверяет ей только фоновые задачи (разбор отчётов)", "Qwen"),
    Model("qwen25-3b", "Qwen2.5 3B — как на сервере", 3.1, 1.93, 36, 36.9, _hf("bartowski/Qwen2.5-3B-Instruct-GGUF", "Qwen2.5-3B-Instruct-Q4_K_M.gguf"), "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
          "та же модель, что на сервере — ответы такие же", "Qwen"),
    Model("phi4-mini", "Phi-4 mini 3.8B", 3.8, 2.49, 32, 131.1, _hf("bartowski/microsoft_Phi-4-mini-instruct-GGUF", "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf"), "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
          "лёгкая от Microsoft; по-русски слабее Qwen и Gemma", "Phi", True),
    Model("qwen3-4b", "Qwen3 4B", 4.0, 2.50, 36, 147.0, _hf("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf"), "Qwen3-4B-Q4_K_M.gguf", "новее и умнее 3B", "Qwen"),
    Model("qwen3-4b-i2507", "Qwen3 4B Instruct (2507)", 4.0, 2.50, 36, 147.5, _hf("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"), "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
          "без «размышлений» — быстро отвечает; хорошо держит русский и JSON", "Qwen", True),
    Model("gemma3-4b", "Gemma 3 4B", 4.3, 2.49, 34, 139.3, _hf("ggml-org/gemma-3-4b-it-GGUF", "gemma-3-4b-it-Q4_K_M.gguf"), "gemma-3-4b-it-Q4_K_M.gguf",
          "от Google; сильна в русском, компактная", "Gemma", True),
    Model("qwen35-4b", "Qwen3.5 4B", 4.0, 2.74, 32, 32.8, _hf("unsloth/Qwen3.5-4B-GGUF", "Qwen3.5-4B-Q4_K_M.gguf"), "Qwen3.5-4B-Q4_K_M.gguf",
          "новое поколение Qwen, малый кэш контекста; не проверена на запросах сервера", "Qwen", True),
    Model("gemma4-e4b", "Gemma 4 E4B", 4.5, 4.60, 42, 60.0, _hf("ggml-org/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_0.gguf"), "gemma-4-E4B-it-Q4_0.gguf",
          "новая от Google (4 млрд «действующих» параметров); не проверена на запросах сервера", "Gemma", True),
    Model("qwen25-7b", "Qwen2.5 7B — умнее", 7.6, 4.68, 28, 57.3, _hf("bartowski/Qwen2.5-7B-Instruct-GGUF", "Qwen2.5-7B-Instruct-Q4_K_M.gguf"), "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
          "заметно умнее; на процессоре медленная", "Qwen"),
    Model("llama31-8b", "Llama 3.1 8B", 8.0, 4.92, 32, 131.1, _hf("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"), "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
          "от Meta; по-русски слабее Qwen, лицензия Llama", "Llama", True),
    Model("ministral3-8b", "Ministral 3 8B", 8.4, 5.20, 34, 139.3, _hf("mistralai/Ministral-3-8B-Instruct-2512-GGUF", "Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"), "Ministral-3-8B-Instruct-2512-Q4_K_M.gguf",
          "от Mistral (Apache 2.0); сильная для своего размера", "Mistral", True),
    Model("qwen3-8b", "Qwen3 8B", 8.2, 5.03, 36, 147.0, _hf("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf"), "Qwen3-8B-Q4_K_M.gguf", "сильная; лучше с видеокартой", "Qwen"),
    Model("qwen25-14b", "Qwen2.5 14B — для мощных", 14.8, 8.99, 48, 196.0, _hf("bartowski/Qwen2.5-14B-Instruct-GGUF", "Qwen2.5-14B-Instruct-Q4_K_M.gguf"), "Qwen2.5-14B-Instruct-Q4_K_M.gguf",
          "для компьютеров с большой памятью и сильной видеокартой", "Qwen"),
    Model("qwen3-14b", "Qwen3 14B", 14.8, 9.00, 40, 164.0, _hf("Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf"), "Qwen3-14B-Q4_K_M.gguf", "для компьютеров с большой памятью и сильной видеокартой", "Qwen"),
]
# Модели, которые администратор кладёт на сервер (папка data/agent_models): средние, 4–8 млрд параметров, разные семейства; компьютеры скачивают их с сервера. Остальные — из интернета.
SERVER_HOSTED = ["qwen3-4b-i2507", "gemma3-4b", "qwen25-7b", "ministral3-8b"]
BY_ID = {m.id: m for m in MODELS}
# порядок предпочтения при автоподборе: от самой сильной к самой простой
# модели, на которых проверялись запросы сервера (Qwen2.5 и Qwen3 8B), — выше менее проверенных (Qwen3 4B): при близкой мощности берём «как на сервере»
# Новые модели (Gemma, Ministral, Llama, Phi, Qwen3 Instruct 2507, Qwen3.5) в автоподбор НЕ входят: на запросах нашего сервера они не проверялись — их выбирают вручную в списке окна.
PREFERENCE = ["qwen25-14b", "qwen3-14b", "qwen3-8b", "qwen25-7b", "qwen25-3b", "qwen3-4b", "qwen25-1.5b"]


def hosted_files() -> dict[str, str]:
    """Файлы моделей, которые должны лежать на сервере: имя файла → id модели (по ним программа находит, что скачать с сервера, а не из интернета)."""
    return {BY_ID[i].filename: i for i in SERVER_HOSTED}


def kv_gb(model: Model, ctx_tokens: int) -> float:
    return model.kv_kb * ctx_tokens / 1024 / 1024


def ram_budget_gb(hw: dict[str, Any], limit_pct: int) -> float:
    """Сколько памяти можно занять: доля от всей памяти минус запас для Windows (но не меньше половины доли — на очень маленьких компьютерах)."""
    total = float(hw.get("ram_gb") or 8.0)
    share = total * limit_pct / 100.0
    return max(share - OS_RESERVE_GB, share * 0.5)


def vram_budget_gb(gpu: Optional[dict[str, Any]], limit_pct: int) -> float:
    if not gpu:
        return 0.0
    return max(0.0, float(gpu["vram_mb"]) / 1024.0 * limit_pct / 100.0 - VRAM_OVERHEAD_GB)


HYBRID_SAFETY = 0.12            # доля видеопамяти про запас: рабочий стол Windows, другие программы; у самого края карта «теряется» (device lost) — замер на GTX 1050 4 ГБ
HYBRID_MAX_SHARE = 0.72         # и при доле 100 % не больше этой части видеопамяти (замер: сбой начинался при ≈80 %, у самого края)
HYBRID_FIXED_GB = 0.4          # буферы вычислений и сам llama-server на карте независимо от числа слоёв (замер: ≈0,35 ГБ)
HYBRID_MIN_LAYERS = 3           # меньше — смысла нет, считаем на процессоре


def hybrid_layers(gpu: dict[str, Any], limit_pct: int, model: Model, ctx_tokens: int) -> int:
    """Сколько слоёв модели отдать видеокарте, когда модель целиком не помещается. Слой на карте — это его веса и его часть кэша контекста
    (кэш считается и для слоёв на карте). Замер на настоящем llama-server (GTX 1050 4 ГБ, Qwen3 4B, контекст 8192): ≈89 МБ на слой + ≈354 МБ постоянно;
    33 и 35 слоёв — карта «теряется», 20–32 и 34 — работают; поэтому берём с запасом и не подходим к краю."""
    total = float(gpu["vram_mb"]) / 1024.0
    avail = total * min(limit_pct / 100.0 - HYBRID_SAFETY, HYBRID_MAX_SHARE) - HYBRID_FIXED_GB
    per_layer = (model.size_gb * 0.9 + kv_gb(model, ctx_tokens)) / max(model.layers, 1)
    return max(0, min(model.layers, int(avail / per_layer)))


def cpu_bandwidth_gbps(hw: dict[str, Any]) -> float:
    """Грубая оценка пропускной способности памяти для генерации на процессоре: растёт с числом ядер до насыщения двух каналов."""
    return 12.0 + 2.5 * min(int(hw["cpu"].get("physical") or 2), 8)


def estimate_tps(model: Model, hw: dict[str, Any], gpu: Optional[dict[str, Any]], limit_pct: int) -> float:
    """Ориентировочная скорость генерации, токенов в секунду."""
    k = (limit_pct / 100.0) ** 0.8
    if gpu and vram_budget_gb(gpu, limit_pct) >= model.size_gb:
        return round(GPU_BW.get(gpu.get("verdict", "hybrid"), 110.0) / max(model.size_gb, 0.5) * 0.55 * k, 1)
    tps = cpu_bandwidth_gbps(hw) * 0.7 / max(model.size_gb, 0.5) * k
    if not hw["cpu"].get("avx2"):
        tps *= 0.35
    if gpu:                                                                       # частичная выгрузка слоёв ускоряет, но не до видеоскорости
        tps *= 1.0 + 1.2 * min(1.0, vram_budget_gb(gpu, limit_pct) / max(model.size_gb, 0.5))
    return round(tps, 1)


def assess(model: Model, hw: dict[str, Any], gpu: Optional[dict[str, Any]], limit_pct: int, ctx_tokens: int) -> dict[str, Any]:
    need_ram = model.size_gb + kv_gb(model, ctx_tokens) + RUNTIME_OVERHEAD_GB
    fits_gpu_fully = bool(gpu) and model.size_gb + kv_gb(model, ctx_tokens) <= vram_budget_gb(gpu, limit_pct)
    # при полной выгрузке в видеопамять оперативная нужна только на буферы; при частичной/на процессоре — вся модель
    fits_ram = fits_gpu_fully or need_ram <= ram_budget_gb(hw, limit_pct)
    tps = estimate_tps(model, hw, gpu, limit_pct)
    floor = MIN_GPU_TPS if fits_gpu_fully else MIN_CPU_TPS
    verdict = "no_fit" if not fits_ram else ("ok" if tps >= floor else "slow")
    return {"model": model, "need_ram_gb": round(need_ram, 1), "fits_gpu": fits_gpu_fully, "fits_ram": fits_ram, "est_tps": tps, "verdict": verdict}


def recommend(hw: dict[str, Any], gpu: Optional[dict[str, Any]], limit_pct: int, ctx_tokens: int = 8192) -> tuple[Model, list[dict[str, Any]]]:
    """Лучшая модель, которая помещается и работает не слишком медленно, и оценка всех моделей для списка в окне агента."""
    rows = {m.id: assess(m, hw, gpu, limit_pct, ctx_tokens) for m in MODELS}
    best: Optional[Model] = None
    # Сервер доверяет вопросы людей моделям от 3 млрд параметров (меньшие — только фоновые дела), поэтому сначала ищем среди таких: самую сильную, которая ещё и отвечает живо
    # (вдвое быстрее минимума), затем — любую подходящую; и только если крупнее 3 млрд ничего не помещается — берём лёгкую.
    for min_params in (SERVER_MIN_PARAMS_B, 0.0):
        for floor_mult in (2.0, 1.0):
            for mid in PREFERENCE:
                r = rows[mid]
                if r["model"].params_b >= min_params and r["verdict"] == "ok" and r["est_tps"] >= (MIN_GPU_TPS if r["fits_gpu"] else MIN_CPU_TPS) * floor_mult:
                    best = BY_ID[mid]
                    break
            if best:
                break
        if best:
            break
    if best is None:                                         # ничего не «подходит» — берём самую лёгкую из поместившихся, иначе самую маленькую вообще
        fitting = [rows[m.id] for m in MODELS if rows[m.id]["fits_ram"]]
        best = fitting[0]["model"] if fitting else MODELS[0]
    out = []
    for m in sorted(MODELS, key=lambda x: x.params_b):
        r = rows[m.id]
        if m.id == best.id and r["verdict"] != "no_fit":
            r = {**r, "verdict": "recommended"}
        out.append(r)
    return best, out


VERDICT_TEXT = {"recommended": "рекомендуем", "ok": "подходит", "slow": "будет медленно", "no_fit": "не хватит памяти"}


@dataclass
class Instance:
    """Один запущенный экземпляр llama-server: на видеокарте целиком, на процессоре или вперемешку (часть слоёв на карте)."""
    kind: str                     # "gpu" | "cpu" | "hybrid"
    n_gpu_layers: int
    threads: int
    batch_threads: int
    slots: int
    ctx: int
    est_tps: float


@dataclass
class Plan:
    model: Model
    gpu: Optional[dict[str, Any]]
    backend: str                  # "cpu" | "vulkan" — какая сборка llama.cpp нужна (vulkan умеет и карту, и процессор)
    instances: list[Instance]
    est_tps: float
    notes: list[str]

    @property
    def slots(self) -> int:
        return sum(i.slots for i in self.instances)

    @property
    def threads(self) -> int:
        return max(i.threads for i in self.instances)




def _gpu_tps(model: Model, gpu: dict[str, Any], pct: int) -> float:
    return round(GPU_BW.get(gpu.get("verdict", "hybrid"), 110.0) * 0.55 / max(model.size_gb, 0.5) * (pct / 100.0) ** 0.5, 1)


def make_plan(hw: dict[str, Any], cfg_limit_pct: int, gpu: Optional[dict[str, Any]], model: Model, ctx_per_slot: int = 8192) -> Plan:
    """Как запускать модель на этом компьютере (потоки — по доле ресурсов; в видеопамять — не больше разрешённой доли):
      • нет подходящей видеокарты — один экземпляр на процессоре;
      • карта есть и модель помещается в видеопамять — ДВА экземпляра, которые работают одновременно: на карте (быстрый) и на процессоре (медленнее, но это ещё одно рабочее место —
        сервер отдаёт им разные запросы), если хватает памяти и ядер для второго;
      • модель в видеопамять не помещается — один смешанный экземпляр: часть слоёв на карте, остальное на процессоре;
    Контекст — 8192 токена на ячейку (как на сервере)."""
    cpu = hw["cpu"]
    pct = cfg_limit_pct / 100.0
    cpu_threads = max(1, round(int(cpu["physical"]) * pct))
    cpu_batch = max(1, round(int(cpu["logical"]) * pct))
    notes: list[str] = []
    instances: list[Instance] = []
    backend = "cpu"
    used_gpu: Optional[dict[str, Any]] = None
    if gpu:
        budget = vram_budget_gb(gpu, cfg_limit_pct)
        need_gpu = model.size_gb * 1.05 + kv_gb(model, ctx_per_slot) + 0.1
        if budget >= need_gpu:
            gpu_threads = 1                                   # карте хватает одного потока процессора на управление
            instances.append(Instance("gpu", 999, gpu_threads, gpu_threads, 1, ctx_per_slot, _gpu_tps(model, gpu, cfg_limit_pct)))
            left_threads = cpu_threads - gpu_threads
            ram_ok = model.size_gb + kv_gb(model, ctx_per_slot) + RUNTIME_OVERHEAD_GB <= ram_budget_gb(hw, cfg_limit_pct)
            cpu_est = estimate_tps(model, hw, None, cfg_limit_pct) * (left_threads / max(cpu_threads, 1)) if left_threads >= 1 else 0.0
            if ram_ok and left_threads >= 2 and cpu_est >= 1.5:
                instances.append(Instance("cpu", 0, left_threads, max(left_threads, cpu_batch - gpu_threads), 1, ctx_per_slot, round(cpu_est, 1)))
                notes.append("модель целиком на видеокарте и ещё один экземпляр на процессоре — два запроса считаются одновременно")
            else:
                notes.append("модель целиком на видеокарте" + ("" if ram_ok else "; для второго экземпляра на процессоре не хватает памяти"))
            backend, used_gpu = "vulkan", gpu
        elif (ngl := hybrid_layers(gpu, cfg_limit_pct, model, ctx_per_slot)) >= HYBRID_MIN_LAYERS:
            frac = min(1.0, ngl / model.layers)
            gbw, cbw = GPU_BW.get(gpu.get("verdict", "hybrid"), 110.0) * 0.55, cpu_bandwidth_gbps(hw) * 0.7
            tps = round(1.0 / (frac * model.size_gb / gbw + (1.0 - frac) * model.size_gb / cbw) * (cfg_limit_pct / 100.0) ** 0.8, 1)
            instances.append(Instance("hybrid", ngl, cpu_threads, cpu_batch, 1, ctx_per_slot, tps))
            notes.append(f"на видеокарту помещается {ngl} из {model.layers} слоёв, остальное считает процессор — работают вместе")
            backend, used_gpu = "vulkan", gpu
        else:
            notes.append("видеопамяти в разрешённой доле не хватает — считаю на процессоре")
    if not instances:
        instances.append(Instance("cpu", 0, cpu_threads, cpu_batch, 1, ctx_per_slot, estimate_tps(model, hw, None, cfg_limit_pct)))
    return Plan(model=model, gpu=used_gpu, backend=backend, instances=instances, est_tps=max(i.est_tps for i in instances), notes=notes)


def llama_args(plan: Plan, inst: Instance, model_path: str, port: int, minimal: bool = False) -> list[str]:
    args = ["-m", model_path, "--host", "127.0.0.1", "--port", str(port), "-c", str(inst.ctx * inst.slots), "-np", str(inst.slots), "-t", str(inst.threads), "-tb", str(inst.batch_threads),
            "-ngl", str(inst.n_gpu_layers)]
    if not minimal:
        args += ["--cache-reuse", "256", "--no-webui"]       # как у сервера: повторное чтение уже знакомой «подсказки» не требуется; без веб-страницы
    return args


def asset_name(tag: str, backend: str) -> str:
    variant = "vulkan" if backend == "vulkan" else "cpu"
    return f"llama-{tag}-bin-win-{variant}-x64.zip"


def asset_url(tag: str, backend: str) -> str:
    return f"https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{asset_name(tag, backend)}"


BUNDLED_LLAMA_TAG = "b11815"            # версия llama.cpp, встроенная в программу (agent/resources/llama): её не нужно скачивать; другая версия (config.llama_tag) — только скачиванием
_TAG_RE = re.compile(r"^b\d{3,6}$")


def valid_tag(tag: str) -> bool:
    return bool(_TAG_RE.match(tag or ""))
