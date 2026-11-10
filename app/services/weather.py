"""Погода для анимированного фона (static/js/fx.js).

Источник и периодичность запросов настраиваются («Настройки → Погода и фон…», только администратор; хранятся в app_flags).
Доступные источники:
  • «Проект Эол» (weatherapi.projecteol.ru, российский сервис) — открытый, без регистрации и ключа, до 60 запросов в минуту; отдаёт
    почасовой прогноз модели NOAA GFS, из него здесь выводится «ясно / облачно / дождь / снег / гроза / туман»;
  • Яндекс.Погода (api.weather.yandex.ru) — нужен ключ (в настройках или YANDEX_WEATHER_API_KEY в .env; тариф «Тестовый» —
    50 запросов в сутки);
  • Open-Meteo (api.open-meteo.com) — без ключа, международный; единственный, который может спросить и сам браузер (CORS);
  • «Только время суток» — ничего не запрашивается: небо по времени суток (для серверов без интернета);
  • «Авто» — по очереди: Яндекс (если есть ключ) → Эол → Open-Meteo.
Кроме вида погоды отдаются числа, по которым фон подбирает силу эффектов: cloud — облачность 0…1, precip — осадки мм/ч, wind — ветер м/с
(если источник их не даёт — берутся типичные для вида погоды, см. LEVEL_DEFAULTS): чем больше облачность — тем больше и плотнее облака, до полного закрытия Солнца;
чем сильнее осадки — тем гуще дождь и снег.
Данные кэшируются на период запроса (по умолчанию час), поэтому лишних запросов нет. Сервер без интернета ответит {"ok": false} — тогда
в режимах «Авто» и «Open-Meteo» браузер (у операторов интернет есть, раз работают Яндекс.Карты) спросит Open-Meteo сам, см. fx.js.
Фон — украшение: любой сбой не должен ничего ломать, поэтому ошибки превращаются в {"ok": false}."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import os
import time
import urllib.request
from typing import Any, Optional

from app.db.database import get_connection

log = logging.getLogger(__name__)

CITY = {"name": "Краснодар", "lat": 45.0355, "lon": 38.9753}      # по умолчанию
CONFIG_KEY = "weather_config"
MIN_REFRESH, MAX_REFRESH, DEFAULT_REFRESH = 10, 720, 60              # минуты
_FAIL_TTL = 3 * 60.0        # после неудачи не спрашиваем чаще, чем раз в 3 минуты
_TIMEOUT = 8.0

SOURCES = [
    {"id": "auto", "label": "Авто — по очереди", "hint": "Яндекс.Погода (если введён ключ) → «Проект Эол» (projecteol.ru) → Open-Meteo: берётся первый, кто ответил."},
    {"id": "eol", "label": "Проект Эол (projecteol.ru)", "hint": "Российский сервис, без регистрации и ключа; почасовой прогноз модели NOAA GFS. Серверу нужен доступ к weatherapi.projecteol.ru:443."},
    {"id": "yandex", "label": "Яндекс.Погода (yandex.ru)", "hint": "Нужен ключ (developer.tech.yandex.ru → «Погода»). Тариф «Тестовый» — 50 запросов в сутки: при периоде короче 30 минут лимит будет превышен."},
    {"id": "open_meteo", "label": "Open-Meteo (open-meteo.com)", "hint": "Международный, без ключа. Единственный, который может запросить и браузер сотрудника, если у сервера нет интернета."},
    {"id": "sky", "label": "Без погоды — только время суток", "hint": "Ничего не запрашивается: небо по времени суток (день — солнце, вечер — закат, ночь — звёзды и луна). Для серверов без интернета."},
]
SOURCE_IDS = [s["id"] for s in SOURCES]
DEFAULTS: dict[str, Any] = {"source": "auto", "refresh_minutes": DEFAULT_REFRESH, "yandex_key": "", "city": CITY["name"], "lat": CITY["lat"], "lon": CITY["lon"]}

# Типичные облачность (0…1), осадки (мм/ч) и ветер (м/с) для вида погоды — когда источник чисел не дал (и когда погода выбрана вручную).
LEVEL_DEFAULTS = {
    "clear": (0.05, 0.0, 2.0), "partly": (0.4, 0.0, 3.0), "clouds": (0.92, 0.0, 4.0), "rain": (0.92, 2.5, 5.0),
    "snow": (0.9, 1.0, 3.0), "storm": (0.97, 9.0, 11.0), "fog": (0.6, 0.0, 1.0),
}

DESCRIPTIONS = {"clear": "ясно", "partly": "малооблачно", "clouds": "облачно", "rain": "дождь", "snow": "снег", "storm": "гроза", "fog": "туман"}
_YANDEX = {
    "clear": "clear", "partly-cloudy": "partly", "cloudy": "clouds", "overcast": "clouds",
    "drizzle": "rain", "light-rain": "rain", "rain": "rain", "moderate-rain": "rain", "heavy-rain": "rain", "continuous-heavy-rain": "rain",
    "showers": "rain", "hail": "rain", "wet-snow": "snow", "light-snow": "snow", "snow": "snow", "snow-showers": "snow",
    "thunderstorm": "storm", "thunderstorm-with-rain": "storm", "thunderstorm-with-hail": "storm",
}

_cache: dict[str, Any] = {"at": 0.0, "data": None, "failed_at": 0.0, "sig": ""}
_cfg_cache: Optional[dict[str, Any]] = None
_lock: Optional[asyncio.Lock] = None


# ---------- настройки ----------
def clean_config(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if raw.get("source") in SOURCE_IDS:
        cfg["source"] = raw["source"]
    try:
        cfg["refresh_minutes"] = max(MIN_REFRESH, min(MAX_REFRESH, int(raw.get("refresh_minutes", DEFAULT_REFRESH))))
    except (TypeError, ValueError):
        pass
    cfg["yandex_key"] = str(raw.get("yandex_key") or "").strip()[:200]
    cfg["city"] = str(raw.get("city") or "").strip()[:60] or DEFAULTS["city"]
    for k, lo, hi in (("lat", -90.0, 90.0), ("lon", -180.0, 180.0)):
        try:
            v = float(str(raw.get(k, DEFAULTS[k])).replace(",", "."))
            if lo <= v <= hi:
                cfg[k] = round(v, 4)
        except (TypeError, ValueError):
            pass
    return cfg


async def load_config() -> dict[str, Any]:
    global _cfg_cache
    if _cfg_cache is not None:
        return _cfg_cache
    raw: dict[str, Any] = {}
    try:
        async with get_connection() as conn:
            cur = await conn.execute("select value from app_flags where key = ?", (CONFIG_KEY,))
            row = await cur.fetchone()
        if row:
            raw = json.loads(row[0])
    except Exception:  # noqa: BLE001 — нет таблицы/битое значение: работаем с настройками по умолчанию
        raw = {}
    _cfg_cache = clean_config(raw if isinstance(raw, dict) else {})
    return _cfg_cache


async def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Сохраняет настройки. Ключ Яндекса: None — оставить прежний, пустая строка — удалить."""
    global _cfg_cache
    cur_cfg = await load_config()
    merged = {**cur_cfg, **{k: v for k, v in patch.items() if k != "yandex_key"}}
    if patch.get("yandex_key") is not None:
        merged["yandex_key"] = patch["yandex_key"]
    cfg = clean_config(merged)
    async with get_connection() as conn:
        await conn.execute("create table if not exists app_flags (key text primary key, value text not null)")
        await conn.execute(
            "insert into app_flags (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
            (CONFIG_KEY, json.dumps(cfg, ensure_ascii=False)),
        )
        await conn.commit()
    _cfg_cache = cfg
    _cache.update(at=0.0, data=None, failed_at=0.0, sig="")        # новые настройки — новый запрос
    return cfg


def reset_cache() -> None:
    """Сброс после подмены базы (восстановление из копии): настройки читаются заново."""
    global _cfg_cache
    _cfg_cache = None
    _cache.update(at=0.0, data=None, failed_at=0.0, sig="")


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Настройки для окна администратора: сам ключ наружу не отдаётся — только факт и последние 4 символа."""
    key = cfg.get("yandex_key") or ""
    return {
        "source": cfg["source"], "refresh_minutes": cfg["refresh_minutes"], "city": cfg["city"], "lat": cfg["lat"], "lon": cfg["lon"],
        "yandex_key_set": bool(key), "yandex_key_hint": ("…" + key[-4:]) if len(key) >= 8 else ("задан" if key else ""),
        "env_key_set": bool(os.environ.get("YANDEX_WEATHER_API_KEY", "").strip()),
        "sources": SOURCES, "min_refresh": MIN_REFRESH, "max_refresh": MAX_REFRESH,
    }


# ---------- преобразования ----------
def wmo_to_code(code: int) -> str:
    """Код погоды WMO (Open-Meteo) → наш вид фона."""
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "clouds"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "clouds"


def sun_elevation(lat: float, lon: float, when: dt.datetime) -> float:
    """Высота Солнца над горизонтом, градусы (упрощённая формула NOAA — для «день/вечер/ночь» хватает с запасом)."""
    when = when.astimezone(dt.timezone.utc)
    day = when.timetuple().tm_yday
    frac = (2 * math.pi / 365) * (day - 1 + (when.hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(frac) - 0.032077 * math.sin(frac) - 0.014615 * math.cos(2 * frac) - 0.040849 * math.sin(2 * frac))
    decl = (0.006918 - 0.399912 * math.cos(frac) + 0.070257 * math.sin(frac) - 0.006758 * math.cos(2 * frac) + 0.000907 * math.sin(2 * frac)
            - 0.002697 * math.cos(3 * frac) + 0.00148 * math.sin(3 * frac))
    minutes = when.hour * 60 + when.minute + when.second / 60
    angle = math.radians((minutes + eqtime + 4 * lon - 720) / 4)          # часовой угол Солнца
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(angle)))


def eol_to_code(temp_c: float, precip_mm_h: float, cloud: float, gust: float, visibility: float) -> str:
    """Прогноз модели (температура, осадки мм/ч, облачность 0…1, порывы м/с, видимость м) → наш вид фона."""
    if precip_mm_h >= 0.15:
        if temp_c <= 0.5:
            return "snow"
        if temp_c >= 8 and (precip_mm_h >= 8 or (precip_mm_h >= 4 and gust >= 12)):
            return "storm"                   # в данных нет молний — гроза определяется по сильному ливню с порывистым ветром
        return "rain"
    if visibility < 1000:
        return "fog"
    if cloud < 0.25:
        return "clear"
    return "partly" if cloud < 0.65 else "clouds"


# ---------- источники ----------
def _get_json(url: str, headers: Optional[dict[str, str]] = None) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "lan-calendar/weather", **(headers or {})})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:     # noqa: S310 — адреса заданы в этом файле
        return json.loads(resp.read().decode("utf-8"))


def _from_yandex(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    d = _get_json(f"https://api.weather.yandex.ru/v2/forecast?lat={cfg['lat']}&lon={cfg['lon']}&lang=ru_RU&limit=1&hours=false&extra=false",
                  {"X-Yandex-Weather-Key": key})
    fact = d["fact"]
    code = "storm" if fact.get("is_thunder") else _YANDEX.get(str(fact.get("condition")), "clouds")
    strength = _num(fact.get("prec_strength"))                      # 0, 0.25, 0.5, 0.75, 1 → примерно мм/ч
    precip = None if strength is None else {0.0: 0.0, 0.25: 0.4, 0.5: 1.5, 0.75: 4.0, 1.0: 9.0}.get(strength, strength * 9.0)
    return {"code": code, "temp": fact.get("temp"), "source": "Яндекс.Погода", "cloud": _num(fact.get("cloudness")), "precip": precip, "wind": _num(fact.get("wind_speed"))}


_EOL_URL = "https://weatherapi.projecteol.ru/mcp/"
_EOL_PARAMS = ["air_temperature_2m", "precipitation_flux", "cloud_area_fraction", "wind_speed_of_gust", "visibility_in_air"]


def _from_eol(cfg: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather_forecast", "arguments": {
        "latitude": cfg["lat"], "longitude": cfg["lon"], "start": hour.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": 2,
        "interpolation": "linear", "parameters": _EOL_PARAMS}}}).encode("utf-8")
    req = urllib.request.Request(_EOL_URL, data=body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                                               "User-Agent": "lan-calendar/weather"})
    d = None
    for attempt in (1, 2):               # у сервиса бывают разовые обрывы соединения — одна повторная попытка
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:       # noqa: S310 — адрес задан в этом файле
                d = json.loads(resp.read().decode("utf-8"))
            break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(1.0)
    res = d["result"]
    if res.get("isError"):
        raise RuntimeError(str(res.get("content"))[:200])
    rows = (res.get("structuredContent") or json.loads(res["content"][0]["text"]))["forecast"]
    w = min(max((now - hour).total_seconds() / 3600.0, 0.0), 1.0) if len(rows) > 1 else 0.0       # между двумя часами прогноза

    def val(code: str) -> float:
        first = rows[0]["values"][code]["value"]
        return first + ((rows[1]["values"][code]["value"] - first) * w if len(rows) > 1 else 0.0)

    temp = val("air_temperature_2m") - 273.15
    rate = max(val("precipitation_flux"), 0.0) * 3600.0              # кг/м²/с → мм/ч
    code = eol_to_code(temp, rate, val("cloud_area_fraction"), val("wind_speed_of_gust"), val("visibility_in_air"))
    return {"code": code, "temp": round(temp, 1), "source": "Проект Эол (projecteol.ru)", "cloud": val("cloud_area_fraction"), "precip": rate,
            "wind": max(val("wind_speed_of_gust"), 0.0) * 0.6}          # порывы → средний ветер: порыв примерно в 1,6 раза сильнее


def _from_open_meteo(cfg: dict[str, Any]) -> dict[str, Any]:
    d = _get_json(f"https://api.open-meteo.com/v1/forecast?latitude={cfg['lat']}&longitude={cfg['lon']}"
                  "&current=temperature_2m,weather_code,is_day,cloud_cover,precipitation,wind_speed_10m&timezone=auto")
    cur = d["current"]
    cloud, wind = _num(cur.get("cloud_cover")), _num(cur.get("wind_speed_10m"))
    return {"code": wmo_to_code(int(cur["weather_code"])), "temp": cur.get("temperature_2m"), "source": "Open-Meteo",
            "cloud": None if cloud is None else cloud / 100.0, "precip": _num(cur.get("precipitation")), "wind": None if wind is None else wind / 3.6}      # % → доля, км/ч → м/с


def _num(value: Any) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def with_levels(data: dict[str, Any]) -> dict[str, Any]:
    """Дополняет ответ источника числами силы погоды: облачность 0…1, осадки мм/ч, ветер м/с — недостающее и неправдоподобное берётся по виду погоды.
    Числа согласуются с видом: дождь/снег/гроза без осадков получают небольшие, «облачно» не бывает с облачностью ниже 0,6, «ясно» — выше 0,3."""
    code = data.get("code", "clouds")
    d_cloud, d_precip, d_wind = LEVEL_DEFAULTS.get(code, LEVEL_DEFAULTS["clouds"])
    cloud, precip, wind = _num(data.get("cloud")), _num(data.get("precip")), _num(data.get("wind"))
    cloud = d_cloud if cloud is None else min(max(cloud, 0.0), 1.0)
    precip = d_precip if precip is None else min(max(precip, 0.0), 60.0)
    wind = d_wind if wind is None else min(max(wind, 0.0), 40.0)
    if code in ("rain", "snow", "storm"):
        precip = max(precip, 0.3 if code != "storm" else 4.0)
        cloud = max(cloud, 0.8)
    elif code == "clouds":
        cloud = max(cloud, 0.6)
    elif code == "clear":
        cloud = min(cloud, 0.3)
        precip = 0.0
    elif code == "partly":
        cloud = min(max(cloud, 0.15), 0.8)
        precip = 0.0
    data["cloud"], data["precip"], data["wind"] = round(cloud, 2), round(precip, 2), round(wind, 1)
    return data


def _yandex_key(cfg: dict[str, Any]) -> str:
    return (cfg.get("yandex_key") or os.environ.get("YANDEX_WEATHER_API_KEY", "")).strip()


def _chain(cfg: dict[str, Any], source: Optional[str] = None) -> list[tuple[str, Any]]:
    """Список (название, функция) в порядке опроса для выбранного источника."""
    source = source or cfg["source"]
    key = _yandex_key(cfg)

    def yandex() -> dict[str, Any]:
        if not key:
            raise RuntimeError("не задан ключ Яндекс.Погоды")
        return _from_yandex(cfg, key)

    yandex_step = ("Яндекс.Погода", yandex)
    eol_step = ("Проект Эол", lambda: _from_eol(cfg))
    om_step = ("Open-Meteo", lambda: _from_open_meteo(cfg))
    if source == "eol":
        return [eol_step]
    if source == "yandex":
        return [yandex_step]
    if source == "open_meteo":
        return [om_step]
    if source == "sky":
        return []
    return ([yandex_step] if key else []) + [eol_step, om_step]


def _finish(cfg: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    data["ok"] = True
    with_levels(data)
    data["city"] = cfg["city"]
    data["lat"], data["lon"] = cfg["lat"], cfg["lon"]
    data["refresh_minutes"] = cfg["refresh_minutes"]
    data["is_day"] = sun_elevation(cfg["lat"], cfg["lon"], now) > -0.8
    data["desc"] = DESCRIPTIONS.get(data["code"], "")
    data["updated_at"] = now.isoformat(timespec="seconds")
    return data


def _fetch_sync(cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    if cfg["source"] == "sky":
        return _finish(cfg, {"code": "clear", "temp": None, "source": "время суток"})
    for name, fn in _chain(cfg):
        try:
            return _finish(cfg, fn())
        except Exception as exc:  # noqa: BLE001 — нет интернета, ключ не принят, формат изменился…
            log.info("Погода: %s недоступна (%s)", name, exc)
    return None


def test_sync(cfg: dict[str, Any], source: Optional[str] = None) -> list[dict[str, Any]]:
    """Проверка источников из окна настроек: по каждому — получилось ли и что вернул (или почему не вышло)."""
    source = source or cfg["source"]
    if source == "sky":
        return [{"source": "время суток", "ok": True, "code": "clear", "desc": "ясно", "temp": None, "error": "", "cloud": 0.05, "precip": 0.0, "wind": 2.0, "ms": 0}]
    out: list[dict[str, Any]] = []
    if source == "auto" and not _yandex_key(cfg):
        out.append({"source": "Яндекс.Погода", "ok": False, "code": "", "desc": "", "temp": None, "error": "пропущена: ключ не задан", "ms": 0})
    for name, fn in _chain(cfg, source):
        t0 = time.monotonic()
        try:
            r = fn()
            r = with_levels(dict(r))
            out.append({"source": name, "ok": True, "code": r["code"], "desc": DESCRIPTIONS.get(r["code"], ""), "temp": r.get("temp"), "error": "",
                        "cloud": r["cloud"], "precip": r["precip"], "wind": r["wind"], "ms": int((time.monotonic() - t0) * 1000)})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or type(exc).__name__
            if "403" in msg or "401" in msg:
                msg += " — ключ не принят"
            out.append({"source": name, "ok": False, "code": "", "desc": "", "temp": None, "error": msg[:200], "ms": int((time.monotonic() - t0) * 1000)})
    return out


def _failed(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "browser_fallback": cfg["source"] in ("auto", "open_meteo"), "city": cfg["city"], "lat": cfg["lat"], "lon": cfg["lon"],
            "refresh_minutes": cfg["refresh_minutes"]}


async def current() -> dict[str, Any]:
    """Погода сейчас. Свежее из кэша мгновенно; устаревшее — после запроса (не чаще раза в 3 минуты при неудачах)."""
    global _lock
    cfg = await load_config()
    ttl = cfg["refresh_minutes"] * 60.0
    sig = f"{cfg['source']}|{cfg['lat']}|{cfg['lon']}|{_yandex_key(cfg)[-6:]}"
    if _cache["sig"] != sig:
        _cache.update(at=0.0, data=None, failed_at=0.0, sig=sig)
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < ttl:
        return _cache["data"]
    if _cache["failed_at"] and now - _cache["failed_at"] < _FAIL_TTL:
        return _cache["data"] or _failed(cfg)
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache["data"] is not None and time.monotonic() - _cache["at"] < ttl:
            return _cache["data"]
        data = await asyncio.to_thread(_fetch_sync, cfg)
        if data:
            _cache.update(at=time.monotonic(), data=data, failed_at=0.0)
            return data
        _cache["failed_at"] = time.monotonic()
        return _cache["data"] or _failed(cfg)
