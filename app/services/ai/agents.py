"""ИИ-агенты на компьютерах сотрудников: масштабирование языковой модели за пределы сервера.

На Windows-компьютере запускается программа-агент (папка agent/ проекта, exe в папке выдачи): она сама поднимает llama.cpp с выбранной моделью, берёт заданную долю ресурсов
(процессор, память, видеокарта — только если карта достаточно сильная) и сама ПОДКЛЮЧАЕТСЯ к серверу — исходящее соединение, открывать порты на компьютерах не нужно.
Сервер хранит список агентов и при каждом запросе к языковой модели (app/services/ai/client.py::llm_chat) выбирает свободного и самого быстрого агента; нет подходящего —
считает сам, как раньше (локальная модель на сервере), так что система без агентов работает без изменений.

Как устроена связь (агент → сервер, всё по HTTPS через Caddy; адреса /api/agent/*, права по токену агента, а не по сеансу — см. app/api/routes_ai_agents.py):
  • регистрация по ключу подключения, который администратор создаёт в «Настройки → ИИ»: одна строка «lcai://адрес/ключ» (адрес сервера уже внутри), ключ ОДНОРАЗОВЫЙ — после
    подключения компьютера недействителен (хранится только хеш, строка показывается один раз); в ответ агент получает свой токен. Компьютер привязан к ЖЕЛЕЗУ, а не к адресу в сети:
    программа сама считает отпечаток оборудования (hw_id), сервер запоминает его и принимает токен только вместе с тем же отпечатком (заголовок X-Agent-Hw) — скопированный на другой
    компьютер файл настроек не работает; тот же компьютер, подключаемый повторно (переустановка, потерянный токен), узнаётся по железу и остаётся той же записью;
  • heartbeat раз в 10 секунд: состояние (модель, скорость генерации в токенах/с, видеокарта, занят ли, на паузе ли);
  • «длинный опрос» задания: свободный слот агента держит открытым запрос GET /next (до 20 с) — это и есть проверка, что агент сейчас на связи и свободен; задание уходит
    ответом на этот запрос, результат агент присылает отдельным POST. Занятых, отключившихся и не берущих задания агентов сервер не выбирает.
Проверка связи с известными агентами идёт всё время (`loop`): нет сигнала дольше `offline_after` секунд — агент «не отвечает» и в маршрутизации не участвует, но из списка САМ не
убирается, сколько бы ни был недоступен: «дружба» с компьютером заканчивается только вручную (кнопка «Отвязать» у администратора — запись и токен удаляются); вернувшийся компьютер
снова работает с тем же токеном.

Кого выбираем: свободного (есть ожидающий опрос), со включённым режимом, не «в отпуске» после сбоев, а для задач людей (голос монтёра, вопрос помощнику) — ещё и с моделью не меньше
`min_params_b` миллиардов параметров и измеренной скоростью не ниже MIN_TPS: слабые модели берут только фоновые дела (разбор неисправностей по отчётам форм). Среди подходящих — по скорости
генерации (токенов/с, меряет сам агент), с надбавкой агенту, который недавно читал ту же «подсказку» (кэш llama.cpp: первый запрос с новой подсказкой на процессоре читается минуты).
Агент не дал ответ или оборвался — запрос уходит другому агенту, а затем на сервер; ничего не теряется.

ВАЖНО для безопасности: локальная модель на сервере стоит в сети без выхода наружу, а запросы к агентам несут данные (номера комплексов, адреса, описания неисправностей, тексты монтёров)
на компьютеры сотрудников. Поэтому режим выключен по умолчанию, включается администратором осознанно (там же предупреждение)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.db.database import get_connection

log = logging.getLogger(__name__)

CONFIG_KEY = "ai_agents_config"
DEFAULTS: dict[str, Any] = {"enabled": False, "min_params_b": 3.0, "offline_after": 45}
MAX_AGENTS = 100
MAX_KEYS = 50
PICKUP_TIMEOUT = 10.0           # задание, которое агент не забрал за столько секунд, — агент не отвечает
COOLDOWN = 60.0                 # после двух сбоев подряд агента не выбираем столько секунд
WARM_TTL = 1800.0               # столько помним, какой агент читал какую подсказку
MIN_TPS = 2.0                   # медленнее — для задач людей не годится
POLL_MAX = 25.0
MAX_POLLERS = 8                                              # открытых опросов от одного агента: по числу его ячеек, но не больше (зависший или злой клиент не копит соединения)
DB_WRITE_EVERY = 60.0           # как часто состояние агента из heartbeat пишется в базу (в памяти — всегда свежее)
NAME_MAX = 60
HARDWARE_MAX = 8000
KEY_DAYS = 7                    # срок действия нового ключа по умолчанию: неиспользованный ключ не должен лежать месяцами
RETRY_WINDOW = 900              # после подключения тот же компьютер (по железу) может повторить регистрацию тем же ключом ещё столько секунд (сбой связи при ответе)
HW_RE = re.compile(r"^[0-9a-f]{64}$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")

_cfg_cache: dict[str, Any] = {"at": 0.0, "value": None}


class AgentUnavailable(RuntimeError):
    """Агент не взял задание или пропал — задание нужно отдать другому."""


class AgentError(RuntimeError):
    """Агент ответил ошибкой."""


class RegisterRejected(ValueError):
    """Регистрацию не приняли; `counts` — считать ли это попыткой подбора ключа (только неизвестный ключ; использованный, просроченный, выключенный режим — нет)."""

    def __init__(self, message: str, counts: bool = False) -> None:
        super().__init__(message)
        self.counts = counts


# ---------------------------------------------------------------- настройки
async def config(force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    if not force and _cfg_cache["value"] is not None and now - _cfg_cache["at"] < 5:
        return dict(_cfg_cache["value"])
    cfg = dict(DEFAULTS)
    try:
        async with get_connection() as conn:
            cur = await conn.execute("select value from app_flags where key = ?", (CONFIG_KEY,))
            row = await cur.fetchone()
        if row:
            saved = json.loads(row[0])
            cfg = clean_config({**cfg, **saved})
    except Exception:  # noqa: BLE001 — нет таблицы/битое значение: настройки по умолчанию
        cfg = dict(DEFAULTS)
    _cfg_cache.update(at=now, value=cfg)
    return dict(cfg)


def clean_config(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg["enabled"] = bool(raw.get("enabled", cfg["enabled"]))
    for key, lo, hi, cast in (("offline_after", 20, 600, int), ("min_params_b", 0.5, 100.0, float)):
        try:
            cfg[key] = max(lo, min(hi, cast(raw.get(key, cfg[key]))))
        except (TypeError, ValueError):
            pass
    return cfg


async def save_config(changes: dict[str, Any]) -> dict[str, Any]:
    cfg = clean_config({**await config(force=True), **{k: v for k, v in changes.items() if v is not None}})
    async with get_connection() as conn:
        await conn.execute("create table if not exists app_flags (key text primary key, value text not null)")
        await conn.execute("insert into app_flags (key, value) values (?, ?) on conflict(key) do update set value = excluded.value", (CONFIG_KEY, json.dumps(cfg)))
        await conn.commit()
    _cfg_cache.update(at=time.monotonic(), value=cfg)
    return dict(cfg)


# ---------------------------------------------------------------- ключи подключения и регистрация
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connection_string(server_url: str, key: str) -> str:
    """Строка подключения «lcai://адрес[:порт]/ключ»: администратор копирует её целиком, программа-агент разбирает сама (https подразумевается)."""
    m = re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://)?([^/\s?#]+)(?:[/?#].*)?$", (server_url or "").strip())
    host = m.group(1) if m else ""
    if not HOST_RE.match(host):
        raise ValueError("Адрес сервера указан неверно — нужен адрес, по которому компьютеры сотрудников открывают сервер")
    return f"lcai://{host}/{key}"


async def create_key(label: str, days: int, created_by: str, server_url: str = "") -> dict[str, Any]:
    """Новый ОДНОРАЗОВЫЙ ключ подключения: по нему подключается один компьютер, после чего ключ недействителен (срок — days дней, если не использован).
    Ключ и (если дан адрес сервера) готовая строка подключения «lcai://адрес/ключ» возвращаются ОДИН раз."""
    label = " ".join((label or "").split())[:60]
    days = max(1, min(365, int(days or KEY_DAYS)))
    key = "lcai-" + secrets.token_urlsafe(18)
    connection = connection_string(server_url, key) if server_url else ""
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from ai_agent_keys where agent_id = '' and (expires_at = '' or expires_at >= ?)", (_now_iso(),))
        if (await cur.fetchone())[0] >= MAX_KEYS:
            raise ValueError("Слишком много неиспользованных ключей — удалите ненужные")
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(timespec="seconds")
        cur = await conn.execute("insert into ai_agent_keys (key_hash, label, created_at, created_by, expires_at) values (?, ?, ?, ?, ?)", (_hash(key), label, _now_iso(), created_by, expires))
        await conn.commit()
    return {"id": cur.lastrowid, "key": key, "connection": connection, "label": label, "expires_at": expires}


async def list_keys() -> list[dict[str, Any]]:
    """Ключи с состоянием: ждёт подключения / использован (каким компьютером) / истёк."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "select k.id, k.label, k.created_at, k.created_by, k.expires_at, k.used, k.agent_id, k.used_at, a.name from ai_agent_keys k "
            "left join ai_agents a on a.id = k.agent_id order by k.id desc")
        now = _now_iso()
        out = []
        for r in await cur.fetchall():
            used = bool(r[5])
            out.append({"id": r[0], "label": r[1], "created_at": r[2], "created_by": r[3], "expires_at": r[4], "used": r[5], "agent_id": r[6], "used_at": r[7], "agent_name": r[8] or "",
                        "expired": (not used) and bool(r[4] and r[4] < now), "state": "used" if used else ("expired" if r[4] and r[4] < now else "waiting")})
        return out


async def delete_key(key_id: int) -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from ai_agent_keys where id = ?", (key_id,))
        await conn.commit()
        return cur.rowcount > 0


def _clean_name(name: Any) -> str:
    return " ".join(re.sub(r"[\x00-\x1f]", " ", str(name or "")).split())[:NAME_MAX] or "Компьютер"


def _clean_hardware(hw: Any) -> str:
    try:
        text = json.dumps(hw if isinstance(hw, dict) else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        text = "{}"
    return text if len(text) <= HARDWARE_MAX else "{}"


def hw_code(hw_id: str) -> str:
    """Короткий код компьютера для списка (по нему администратор сверяет, тот ли компьютер): первые 8 знаков отпечатка."""
    return (hw_id or "")[:8].upper()


async def register(enroll_key: str, name: str, hardware: Any, version: str, ip: str, hw_id: str = "") -> dict[str, Any]:
    """Регистрация агента по одноразовому ключу: возвращает {agent_id, token, name}. ValueError с понятным текстом — отказ.
    hw_id — отпечаток железа (64 шестнадцатеричных знака): запись привязывается к нему. Тот же компьютер, подключаемый новым ключом (переустановка, потерянный токен),
    остаётся прежней записью с новым токеном; использованный ключ повторно работает только для того же железа и недолго (RETRY_WINDOW) — на случай потерянного ответа."""
    cfg = await config(force=True)
    if not cfg["enabled"]:
        raise RegisterRejected("Подключение компьютеров выключено администратором сервера")
    hw_id = (hw_id or "").strip().lower()
    if not HW_RE.match(hw_id):
        raise RegisterRejected("Эта версия программы-агента не передаёт код компьютера — обновите программу (возьмите свежий файл LanCalendarAIAgent.exe)")
    async with get_connection() as conn:
        cur = await conn.execute("select id, expires_at, used, agent_id, used_at from ai_agent_keys where key_hash = ?", (_hash((enroll_key or "").strip()),))
        row = await cur.fetchone()
        if not row:
            raise RegisterRejected("Ключ подключения не подходит (неверный или отозван)", counts=True)
        key_id, expires_at, used, key_agent, used_at = row
        cur = await conn.execute("select id, name from ai_agents where hw_id = ?", (hw_id,))
        same_hw = await cur.fetchone()
        token = secrets.token_urlsafe(32)
        now = _now_iso()
        if used:
            recent = False
            try:
                recent = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(used_at)).total_seconds() <= RETRY_WINDOW
            except ValueError:
                pass
            if not (same_hw and key_agent == same_hw[0] and recent):
                raise RegisterRejected("Этот ключ уже использован для подключения компьютера и больше не действует. Попросите администратора создать новый ключ (в «Настройки → ИИ»)")
        elif expires_at and expires_at < now:
            raise RegisterRejected("Срок действия ключа подключения истёк — создайте новый")
        if same_hw:                                              # это же железо уже знакомо: та же запись, новый токен
            agent_id, name = same_hw
            await conn.execute("update ai_agents set token_hash = ?, hardware = ?, version = ?, last_seen = ?, last_ip = ? where id = ?",
                               (_hash(token), _clean_hardware(hardware), str(version or "")[:30], now, ip[:60], agent_id))
        else:
            cur = await conn.execute("select count(*) from ai_agents")
            if (await cur.fetchone())[0] >= MAX_AGENTS:
                raise RegisterRejected(f"Слишком много подключённых компьютеров (не больше {MAX_AGENTS}) — отвяжите ненужные")
            name = _clean_name(name)
            cur = await conn.execute("select name from ai_agents")
            taken = {r[0] for r in await cur.fetchall()}
            base, n = name, 2
            while name in taken:
                name, n = f"{base} ({n})", n + 1
            agent_id = "ag_" + secrets.token_hex(6)
            await conn.execute(
                "insert into ai_agents (id, name, token_hash, hardware, version, created_at, last_seen, last_ip, hw_id) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (agent_id, name, _hash(token), _clean_hardware(hardware), str(version or "")[:30], now, now, ip[:60], hw_id))
        if not used:
            cur = await conn.execute("update ai_agent_keys set used = 1, agent_id = ?, used_at = ? where id = ? and used = 0", (agent_id, now, key_id))
            if cur.rowcount == 0:                                # тот же ключ одновременно взял другой компьютер
                await conn.rollback()
                raise RegisterRejected("Этот ключ уже использован для подключения компьютера и больше не действует. Попросите администратора создать новый ключ (в «Настройки → ИИ»)")
        await conn.commit()
    _rows_cache["at"] = 0.0
    log.info("ИИ-агент подключён: %s (%s, код железа %s)", name, agent_id, hw_code(hw_id))
    return {"agent_id": agent_id, "token": token, "name": name}


async def authenticate(token: str, ip: str = "") -> Optional[dict[str, Any]]:
    """Агент по токену (None — токен неизвестен: агент отвязан или токен чужой). В ответе hw_id — привязанное железо (проверяет require_agent)."""
    if not token or len(token) > 200:
        return None
    async with get_connection() as conn:
        cur = await conn.execute("select id, name, enabled, hw_id from ai_agents where token_hash = ?", (_hash(token),))
        row = await cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "enabled": bool(row[2]), "hw_id": row[3] or "", "ip": ip}


async def bind_hardware(agent_id: str, hw_id: str) -> None:
    """Запись, подключённая до привязки к железу (hw_id пуст): привязывается к первому же компьютеру, который пришёл с её токеном."""
    if not HW_RE.match(hw_id or ""):
        return
    async with get_connection() as conn:
        await conn.execute("update ai_agents set hw_id = ? where id = ? and hw_id = ''", (hw_id, agent_id))
        await conn.commit()
    _rows_cache["at"] = 0.0


# ---------------------------------------------------------------- живое состояние (в памяти)
@dataclass
class Job:
    id: str
    agent_id: str
    payload: dict[str, Any]
    system_key: str
    fut: "asyncio.Future[dict[str, Any]]"
    picked: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    done: bool = False


@dataclass
class Live:
    hb: float = 0.0                                         # monotonic последнего сигнала (heartbeat или опрос)
    state: dict[str, Any] = field(default_factory=dict)
    pollers: int = 0                                        # сколько опросов «дай задание» сейчас открыто = свободных мест
    queue: "asyncio.Queue[Job]" = field(default_factory=asyncio.Queue)
    active: int = 0                                         # заданий выполняется
    fails: int = 0
    cooldown_until: float = 0.0
    warm: dict[str, float] = field(default_factory=dict)
    last_db: float = 0.0


_live: dict[str, Live] = {}
_jobs: dict[str, Job] = {}
_rows_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def _l(agent_id: str) -> Live:
    return _live.setdefault(agent_id, Live())


def _alive(live: Live, offline_after: float) -> bool:
    return live.hb > 0 and time.monotonic() - live.hb < offline_after


_ROW_COLS = "id, name, hardware, model, params_b, version, enabled, tps, prompt_tps, slots, gpu_used, created_at, last_seen, last_ip, jobs_done, jobs_failed, note, hw_id"


def _row(r) -> dict[str, Any]:
    try:
        hw = json.loads(r[2] or "{}")
    except ValueError:
        hw = {}
    return {"id": r[0], "name": r[1], "hardware": hw, "model": r[3], "params_b": r[4], "version": r[5], "enabled": bool(r[6]), "tps": r[7], "prompt_tps": r[8], "slots": r[9],
            "gpu_used": bool(r[10]), "created_at": r[11], "last_seen": r[12], "last_ip": r[13], "jobs_done": r[14], "jobs_failed": r[15], "note": r[16], "hw_id": r[17] or ""}


async def rows(max_age: float = 3.0) -> list[dict[str, Any]]:
    if time.monotonic() - _rows_cache["at"] > max_age:
        async with get_connection() as conn:
            cur = await conn.execute(f"select {_ROW_COLS} from ai_agents order by name")
            _rows_cache.update(at=time.monotonic(), rows=[_row(r) for r in await cur.fetchall()])
    return _rows_cache["rows"]


async def heartbeat(agent: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Сигнал агента: обновляет живое состояние (в памяти всегда, в базе — не чаще DB_WRITE_EVERY или при смене модели). Возвращает настройки для агента."""
    live = _l(agent["id"])
    now = time.monotonic()
    if state.get("bye"):                                    # агент закрывается или перезапускает модель и прощается: считать его недоступным сразу, а не через полминуты молчания
        live.hb = 0.0
        live.state = {}
        return {"ok": True, "bye": True}
    live.hb = now
    clean: dict[str, Any] = {}
    for key, cast, lo, hi in (("tps", float, 0, 10000), ("prompt_tps", float, 0, 100000), ("params_b", float, 0, 1000), ("slots", int, 1, 16), ("active", int, 0, 64), ("limit_pct", int, 1, 100)):
        try:
            clean[key] = max(lo, min(hi, cast(state.get(key, 0) or 0)))
        except (TypeError, ValueError):
            clean[key] = 0
    for key in ("ready", "paused", "gpu_used", "busy"):
        clean[key] = bool(state.get(key))
    clean["model"] = str(state.get("model") or "")[:120]
    clean["status"] = str(state.get("status") or "")[:200]
    clean["gpu"] = str(state.get("gpu") or "")[:120]
    prev_model = live.state.get("model")
    live.state = clean
    if now - live.last_db > DB_WRITE_EVERY or prev_model != clean["model"] or "hardware" in state:
        live.last_db = now
        async with get_connection() as conn:
            await conn.execute(
                "update ai_agents set model = ?, params_b = ?, tps = case when ? > 0 then ? else tps end, prompt_tps = case when ? > 0 then ? else prompt_tps end, slots = ?, gpu_used = ?, last_seen = ?, "
                "version = case when ? != '' then ? else version end, hardware = case when ? != '' then ? else hardware end where id = ?",
                (clean["model"], clean["params_b"], clean["tps"], clean["tps"], clean["prompt_tps"], clean["prompt_tps"], clean["slots"], int(clean["gpu_used"]), _now_iso(),
                 str(state.get("version") or "")[:30], str(state.get("version") or "")[:30],
                 _clean_hardware(state["hardware"]) if "hardware" in state else "", _clean_hardware(state["hardware"]) if "hardware" in state else "", agent["id"]))
            await conn.commit()
        _rows_cache["at"] = 0.0
    cfg = await config()
    return {"ok": True, "enabled": cfg["enabled"], "agent_enabled": agent.get("enabled", True), "server_time": _now_iso(), "min_params_b": cfg["min_params_b"]}


async def next_job(agent: dict[str, Any], wait: float) -> Optional[dict[str, Any]]:
    """Длинный опрос агента: держит соединение до `wait` секунд и отдаёт задание, если оно появилось. Пока опрос открыт, агент считается свободным и доступным."""
    live = _l(agent["id"])
    live.hb = time.monotonic()
    wait = max(1.0, min(POLL_MAX, wait))
    if live.pollers >= MAX_POLLERS:
        await asyncio.sleep(min(wait, 2.0))                 # не «горячий» цикл у клиента, который шлёт опросы без остановки
        return None
    live.pollers += 1
    try:
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                job = await asyncio.wait_for(live.queue.get(), timeout=left)
            except asyncio.TimeoutError:
                return None
            if job.cancelled or job.done:
                continue                                    # хозяин задания уже отказался от него
            job.picked.set()
            live.active += 1
            live.hb = time.monotonic()
            return {"job_id": job.id, "payload": job.payload}
    finally:
        live.pollers = max(0, live.pollers - 1)
        live.hb = time.monotonic()


async def submit_result(agent: dict[str, Any], job_id: str, result: dict[str, Any]) -> bool:
    job = _jobs.get(job_id)
    if not job or job.agent_id != agent["id"] or job.done:
        return False
    job.done = True
    live = _l(agent["id"])
    live.active = max(0, live.active - 1)
    if not job.fut.done():
        job.fut.set_result(result)
    return True


# ---------------------------------------------------------------- маршрутизация
def _eligible(row: dict[str, Any], live: Optional[Live], cfg: dict[str, Any], background: bool) -> bool:
    if not live or not row["enabled"] or not _alive(live, cfg["offline_after"]):
        return False
    if live.state.get("paused") or not live.state.get("ready"):
        return False
    if live.pollers - live.queue.qsize() <= 0:              # нет свободного места (не ждёт задание)
        return False
    if time.monotonic() < live.cooldown_until:
        return False
    if background:
        return live.state.get("params_b", 0) >= 1.0 or row["params_b"] >= 1.0
    tps = live.state.get("tps") or row["tps"]
    return (live.state.get("params_b") or row["params_b"]) >= cfg["min_params_b"] and tps >= MIN_TPS


def _score(row: dict[str, Any], live: Live, system_key: str) -> float:
    tps = live.state.get("tps") or row["tps"] or 1.0
    warm = live.warm.get(system_key)
    bonus = 1.5 if warm and time.monotonic() - warm < WARM_TTL else 1.0
    return tps * bonus


async def pick(background: bool, system_key: str, exclude: Optional[set[str]] = None) -> Optional[dict[str, Any]]:
    cfg = await config()
    if not cfg["enabled"]:
        return None
    best, best_score = None, 0.0
    for row in await rows():
        if exclude and row["id"] in exclude:
            continue
        live = _live.get(row["id"])
        if not _eligible(row, live, cfg, background):
            continue
        score = _score(row, live, system_key)  # type: ignore[arg-type]
        if best is None or score > best_score:
            best, best_score = row, score
    return best


def _fail(agent_id: str, reason: str) -> None:
    live = _l(agent_id)
    live.fails += 1
    if live.fails >= 2:
        live.cooldown_until = time.monotonic() + COOLDOWN
        log.warning("ИИ-агент %s: два сбоя подряд (%s) — на минуту исключён из выбора", agent_id, reason)


async def _bump(agent_id: str, ok: bool, tps: float = 0.0, prompt_tps: float = 0.0) -> None:
    live = _l(agent_id)
    if ok:
        live.fails = 0
    try:
        async with get_connection() as conn:
            if ok:
                await conn.execute("update ai_agents set jobs_done = jobs_done + 1, tps = case when ? > 0 then (case when tps > 0 then tps * 0.7 + ? * 0.3 else ? end) else tps end, "
                                   "prompt_tps = case when ? > 0 then (case when prompt_tps > 0 then prompt_tps * 0.7 + ? * 0.3 else ? end) else prompt_tps end where id = ?",
                                   (tps, tps, tps, prompt_tps, prompt_tps, prompt_tps, agent_id))
            else:
                await conn.execute("update ai_agents set jobs_failed = jobs_failed + 1 where id = ?", (agent_id,))
            await conn.commit()
        _rows_cache["at"] = 0.0
    except Exception:  # noqa: BLE001 — статистика не должна мешать ответу
        log.debug("Не удалось записать статистику агента", exc_info=True)


async def _run(row: dict[str, Any], payload: dict[str, Any], system_key: str, timeout: float) -> dict[str, Any]:
    """Отдать задание агенту и дождаться ответа. AgentUnavailable — агент не взял задание или пропал; AgentError — ответил ошибкой."""
    live = _l(row["id"])
    job = Job(id="j_" + secrets.token_hex(8), agent_id=row["id"], payload=payload, system_key=system_key, fut=asyncio.get_running_loop().create_future())
    _jobs[job.id] = job
    live.queue.put_nowait(job)
    started = time.monotonic()
    try:
        try:
            await asyncio.wait_for(job.picked.wait(), PICKUP_TIMEOUT)
        except asyncio.TimeoutError:
            job.cancelled = True
            _fail(row["id"], "не взял задание")
            await _bump(row["id"], False)
            raise AgentUnavailable("агент не взял задание") from None
        cfg = await config()
        deadline = started + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                job.cancelled = True
                _fail(row["id"], "не ответил вовремя")
                await _bump(row["id"], False)
                raise AgentUnavailable("агент не ответил вовремя")
            try:
                result = await asyncio.wait_for(asyncio.shield(job.fut), timeout=min(5.0, left))
                break
            except asyncio.TimeoutError:
                if not _alive(live, cfg["offline_after"]):
                    job.cancelled = True
                    _fail(row["id"], "пропала связь")
                    await _bump(row["id"], False)
                    raise AgentUnavailable("пропала связь с агентом") from None
        seconds = round(time.monotonic() - started, 2)
        if result.get("error") or not isinstance(result.get("status"), int):
            _fail(row["id"], str(result.get("error") or "нет ответа"))
            await _bump(row["id"], False)
            raise AgentError(str(result.get("error") or "агент не вернул ответ")[:200])
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        await _bump(row["id"], result["status"] < 500, float(timings.get("predicted_per_second") or 0), float(timings.get("prompt_per_second") or 0))
        live.warm[system_key] = time.monotonic()
        return {"status": result["status"], "body": result.get("body"), "text": str(result.get("text") or ""), "agent": row["name"], "agent_id": row["id"], "seconds": seconds}
    finally:
        job.done = True
        if job.picked.is_set() and not job.fut.done():
            live.active = max(0, live.active - 1)
        _jobs.pop(job.id, None)


async def dispatch(payload: dict[str, Any], *, background: bool, system_key: str, timeout: float) -> Optional[dict[str, Any]]:
    """Решить, кому считать запрос к модели. Возвращает {status, body, text, agent, seconds} или None — «считай на сервере» (режим выключен, нет подходящего свободного агента,
    агенты не справились). Два агента подряд не справились — дальше сервер сам, чтобы человек не ждал."""
    cfg = await config()
    if not cfg["enabled"]:
        return None
    tried: set[str] = set()
    for _ in range(2):
        row = await pick(background, system_key, tried)
        if row is None:
            return None
        tried.add(row["id"])
        try:
            return await _run(row, payload, system_key, timeout)
        except (AgentUnavailable, AgentError) as exc:
            log.info("ИИ-агент «%s» не справился (%s) — пробую другого или считаю на сервере", row["name"], exc)
    return None


async def test_agent(agent_id: str) -> dict[str, Any]:
    """Проверка конкретного агента из окна администратора: короткий вопрос и замер времени."""
    for row in await rows(0):
        if row["id"] != agent_id:
            continue
        cfg = await config()
        live = _live.get(agent_id)
        if not live or not _alive(live, cfg["offline_after"]):
            raise AgentUnavailable("агент сейчас не на связи")
        if live.pollers - live.queue.qsize() <= 0:
            raise AgentUnavailable("агент занят или не берёт задания (на паузе, грузит модель)")
        payload = {"messages": [{"role": "user", "content": "Ответь одним словом: готов"}], "temperature": 0.0, "max_tokens": 12, "stream": False}
        res = await _run(row, payload, "selftest", 120.0)
        text = ""
        try:
            text = (res["body"]["choices"][0]["message"]["content"] or "").strip()
        except (TypeError, KeyError, IndexError):
            pass
        return {"seconds": res["seconds"], "text": text[:200], "status": res["status"]}
    raise KeyError(agent_id)


# ---------------------------------------------------------------- состояние для окна администратора и для «ИИ работает?»
def _gpu_text(hw: dict[str, Any]) -> str:
    gpus = hw.get("gpus") or []
    if not gpus:
        return "нет"
    return "; ".join(f"{g.get('name', '?')} {round((g.get('vram_mb') or 0) / 1024, 1)} ГБ — {g.get('verdict_text') or g.get('verdict') or ''}" for g in gpus[:3])


def status_of(row: dict[str, Any], live: Optional[Live], cfg: dict[str, Any]) -> str:
    if not live or not _alive(live, cfg["offline_after"]):
        return "offline"
    if not row["enabled"]:
        return "disabled"
    if live.state.get("paused"):
        return "paused"
    if live.active > 0:
        return "busy"
    if not live.state.get("ready"):
        return "loading"
    if live.pollers <= 0:
        return "no_poll"
    return "idle"


async def snapshot() -> dict[str, Any]:
    cfg = await config(force=True)
    out = []
    now_wall = dt.datetime.now(dt.timezone.utc)
    for row in await rows(0):
        live = _live.get(row["id"])
        st = status_of(row, live, cfg)
        seen = row["last_seen"]
        if live and live.hb:
            seen = (now_wall - dt.timedelta(seconds=time.monotonic() - live.hb)).isoformat(timespec="seconds")
        hw = row["hardware"] or {}
        state = live.state if live else {}
        out.append({
            "id": row["id"], "name": row["name"], "status": st, "enabled": row["enabled"], "last_seen": seen, "ip": row["last_ip"], "version": row["version"],
            "model": state.get("model") or row["model"], "params_b": state.get("params_b") or row["params_b"], "tps": round(state.get("tps") or row["tps"] or 0, 1),
            "prompt_tps": round(state.get("prompt_tps") or row["prompt_tps"] or 0), "slots": state.get("slots") or row["slots"], "gpu_used": bool(state.get("gpu_used", row["gpu_used"])),
            "gpu": _gpu_text(hw), "cpu": (hw.get("cpu") or {}).get("name", ""), "cores": (hw.get("cpu") or {}).get("logical", 0), "ram_gb": hw.get("ram_gb", 0), "limit_pct": state.get("limit_pct", 0),
            "message": state.get("status", ""), "jobs_done": row["jobs_done"], "jobs_failed": row["jobs_failed"], "note": row["note"], "created_at": row["created_at"], "hw_code": hw_code(row["hw_id"]),
            "cooldown": bool(live and time.monotonic() < live.cooldown_until),
        })
    online = [a for a in out if a["status"] in ("idle", "busy", "loading", "no_poll", "paused")]
    return {"config": cfg, "agents": out, "summary": {"total": len(out), "online": len(online), "free": sum(1 for a in out if a["status"] == "idle"), "busy": sum(1 for a in out if a["status"] == "busy")}}


async def availability() -> dict[str, Any]:
    """Коротко для client.status(): сколько агентов сейчас может взять работу (режим выключен — нисколько)."""
    cfg = await config()
    if not cfg["enabled"]:
        return {"enabled": False, "free": 0, "online": 0}
    free = online = 0
    for row in await rows():
        live = _live.get(row["id"])
        if live and row["enabled"] and _alive(live, cfg["offline_after"]):
            online += 1
            if _eligible(row, live, cfg, True):
                free += 1
    return {"enabled": True, "free": free, "online": online}


async def update_agent(agent_id: str, *, enabled: Optional[bool] = None, name: Optional[str] = None, note: Optional[str] = None) -> bool:
    sets, args = [], []
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(int(enabled))
    if name is not None:
        sets.append("name = ?")
        args.append(_clean_name(name))
    if note is not None:
        sets.append("note = ?")
        args.append(str(note)[:200])
    if not sets:
        return False
    async with get_connection() as conn:
        cur = await conn.execute(f"update ai_agents set {', '.join(sets)} where id = ?", (*args, agent_id))
        await conn.commit()
    _rows_cache["at"] = 0.0
    return cur.rowcount > 0


async def remove_agent(agent_id: str, reason: str = "") -> bool:
    async with get_connection() as conn:
        cur = await conn.execute("delete from ai_agents where id = ?", (agent_id,))
        await conn.commit()
    _rows_cache["at"] = 0.0
    live = _live.pop(agent_id, None)
    if live:
        while not live.queue.empty():
            try:
                live.queue.get_nowait().cancelled = True
            except asyncio.QueueEmpty:
                break
    if cur.rowcount:
        log.info("ИИ-агент %s убран из списка%s", agent_id, f" ({reason})" if reason else "")
    return cur.rowcount > 0


# ---------------------------------------------------------------- постоянная проверка связи
async def check_once() -> dict[str, int]:
    """Один обход известных агентов: считает, кто сейчас не отвечает. Из списка никого не убираем и токены не отзываем — недоступный компьютер остаётся «не отвечает»,
    пока администратор сам не отвяжет его; вернувшийся продолжает с прежним токеном."""
    cfg = await config(force=True)
    offline = 0
    for row in await rows(0):
        live = _live.get(row["id"])
        if not (live and _alive(live, cfg["offline_after"])):
            offline += 1
    # завершённые и брошенные задания не копим
    for jid in [j for j, job in _jobs.items() if job.done and job.fut.done()]:
        _jobs.pop(jid, None)
    return {"offline": offline}


async def loop() -> None:
    """Фон: раз в 15 секунд проверяет связь со всеми известными агентами (кто не отвечает — видно в списке; сами агенты не удаляются)."""
    await asyncio.sleep(20)
    while True:
        try:
            await check_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Проверка связи с ИИ-агентами не удалась")
        await asyncio.sleep(15)
