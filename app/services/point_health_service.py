"""Continuous background health check for every point's network gear.

Runs forever from app startup: works through all points in small batches
(a few at a time, concurrently within a batch) with a short pause between
batches so a large directory (hundreds/thousands of points) doesn't fire
off one huge burst of pings/connects — then, once it's cycled through
everyone, pauses briefly and starts the next lap. So every point gets
re-checked roughly every few minutes, continuously, not just once a day.

Before probing, a point's ip_address is reconciled against sim_ip_registry
by its own sim_number (see _sync_ip_from_sim) — the registry is the source
of truth for that SIM's current IP (same rule update_point already applies
when a dispatcher changes a point's SIM number), but the registry itself
can drift on its own between edits, so every check re-syncs it first
instead of quietly pinging a stale address forever.

Pings each point by IP and checks its configured web-interface port (both
run concurrently, see _probe), then sets one of three states so a
dispatcher can see at a glance what's wrong without clicking the manual
Пинг/Веб-интерфейс buttons:
  - port responds                      -> "Работает"
  - no port response, but ping does    -> "Есть только пинг" (device is on
    the network — powered, cabled — but whatever service should be
    listening on that port isn't; worth a technician even though it's
    pingable)
  - neither responds                   -> "Не работает"

Separately, points.port_down_since tracks how long the port specifically
has been unreachable — driven by the port check alone when the point has
one configured, so "Есть только пинг" still counts as down for this
purpose (a device that only answers ping is not actually working); only a
point with no port configured at all falls back to the ping result, since
there's nothing else to measure. This is what "Сначала дольше не
работает" sorts/shows, deliberately independent of the status string above
(which also covers non-network states like "Демонтирован").

A point parked "На складе" isn't deployed anywhere (no network to check)
— a manual state a dispatcher/warehouse set on purpose, so it's skipped
rather than silently overwritten. Which statuses are eligible for
automatic overwrite is data-driven (point_statuses.is_auto — see the
"Настройки → Статусы" manager), not hardcoded, so a dispatcher-defined
custom status is left alone by default too, unless explicitly flagged as
automatic.

Грейс-период. Комплекс, который работал («Работает» / «Есть только пинг») и вдруг не ответил, сразу «неработающим» не
становится: связь могла пропасть на мгновение. Статус остаётся прежним, а точка выпадает из общей очереди и перепроверяется
отдельно (раз в DOWN_GRACE_RETRY_SECONDS) в течение двух минут (POINT_DOWN_GRACE_SECONDS в .env, 0 — выключить). Ответил
хоть раз — всё осталось как было, ничего не мигало. Молчал все две минуты — статус меняется, а «не работает с…» считается
от первой неудачной проверки.

The manual Пинг/Веб-интерфейс buttons (routes_points.py) run this same
classification too, via recheck_and_apply() below — so a dispatcher-
triggered check keeps the status just as current as the background loop
does, without waiting for the next lap.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Optional

from app.config import get_settings
from app.repositories import point_statuses_repository, points_repository, sim_ip_registry_repository
from app.services import network_check

logger = logging.getLogger(__name__)

WORKING = "Работает"
PING_ONLY = "Есть только пинг"
NOT_WORKING = "Не работает"
# Монтёр ставит через бота, когда на комплексе физически нет питания —
# не в auto_names (фоновая тройка её не переставляет как обычную), но и не
# пропускается как «На складе»/«Не подведено питание»: продолжаем пинговать,
# и если ответ появился — значит питание вернулось, уходим в обычный статус.
# Если по-прежнему нет ответа — остаёмся «Нет питания», а не откатываемся
# на общий «Не работает» (иначе статус потерял бы смысл на первом же цикле).
POWER_OFF = "Нет питания"

BATCH_SIZE = 40              # points checked concurrently per batch (пинг и TCP-подключение — копеечная нагрузка)
BATCH_PAUSE_SECONDS = 1      # pause between batches within one lap
LAP_PAUSE_SECONDS = 20       # pause after a full lap over every point, before the next
# Одна точка не должна задерживать круг: проверка (3 попытки пинга и порта) укладывается в ~5 с, остальное — зависание.
POINT_TIMEOUT_SECONDS = 30
DOWN_GRACE_RETRY_SECONDS = 10         # как часто перепроверять точку, пока идёт грейс-период
DOWN_GRACE_SECONDS: Optional[float] = None   # None — брать из настроек (POINT_DOWN_GRACE_SECONDS); число — для тестов
_GRACE_PARALLEL = 40                  # сколько точек одновременно перепроверяется (при массовой потере связи не заваливаем сеть)
# «Хуже» — больше: работает → только пинг → не работает. Грейс-период включается, только если стало хуже, чем было.
_RANK = {WORKING: 0, PING_ONLY: 1, NOT_WORKING: 2}
# Раньше было 15 точек и 5 с паузы: на тысячах точек круг шёл десятки минут, и статус менялся «когда дойдёт очередь»,
# тогда как ручная проверка меняла его сразу.


_sim_ip_map: Optional[dict[str, str]] = None      # реестр SIM→IP на время одного круга проверки (None вне круга)


async def _sync_ip_from_sim(point: dict, fresh: bool = False) -> dict:
    """The SIM↔IP registry is the source of truth for a point's IP (see
    points_repository.update_point's own sync on SIM change) — but that
    only fires when the point's sim_number itself changes. The registry's
    IP for an existing SIM can still change on its own (re-import, carrier
    reassignment) without anyone touching the point, so every check
    re-resolves it first; otherwise a stale IP just pings/connects to
    nothing even though the device is fine at its new address."""
    sim = (point.get("sim_number") or "").strip()
    if not sim:
        return point
    # В фоновом круге реестр SIM→IP прочитан один раз на весь круг (_sim_ip_map): иначе каждая из тысяч проверок открывала своё соединение с базой
    # (по профилю — около пятой части процессорного времени приложения). Ручная проверка и перепроверка в грейс-периоде (fresh=True) спрашивают базу, как раньше.
    if _sim_ip_map is not None and not fresh:
        known_ip = _sim_ip_map.get(sim)
    else:
        known_ip = await sim_ip_registry_repository.get_ip_for_sim(sim)
    if known_ip and known_ip != (point.get("ip_address") or ""):
        # Берём свежую запись: снимок круга мог устареть (статус, пока идёт круг, меняют), а update_point пишет её целиком.
        fresh = await points_repository.get_point(point["id"]) or point
        await points_repository.update_point(point["id"], {**fresh, "ip_address": known_ip})
        point = {**point, "ip_address": known_ip}
    return point


async def _probe(point: dict) -> tuple[str, bool]:
    """Runs ping + port concurrently (independent checks, no reason to
    wait for one before starting the other) and returns (status,
    is_port_down). is_port_down follows the port check alone when a port
    is configured — even a successful ping leaves it down, see module
    docstring — falling back to the ping result only when there's no port
    to check at all."""
    ip = (point.get("ip_address") or "").strip()
    if not ip:
        return NOT_WORKING, True

    port = (point.get("port") or "").strip()
    if port:
        ping_result, port_result = await asyncio.gather(
            network_check.ping_host(ip), network_check.check_tcp_port(ip, port)
        )
    else:
        ping_result, port_result = await network_check.ping_host(ip), None

    if port_result is not None:
        is_down = not port_result.get("ok")
        status = WORKING if port_result.get("ok") else (PING_ONLY if ping_result.get("ok") else NOT_WORKING)
    else:
        is_down = not ping_result.get("ok")
        status = PING_ONLY if ping_result.get("ok") else NOT_WORKING
    return status, is_down


async def _update_port_down_since(point_id: str, is_down: bool) -> None:
    current = await points_repository.get_port_down_since(point_id)
    if is_down and not current:
        await points_repository.set_port_down_since(point_id, dt.datetime.now(dt.timezone.utc).isoformat())
    elif not is_down and current:
        await points_repository.set_port_down_since(point_id, None)


def _eligible(status: str, auto_names: set[str]) -> bool:
    """Можно ли автоматике трогать этот статус (пустой, «автоматический» из справочника или «Нет питания»)."""
    return not status or status in auto_names or status == POWER_OFF


def _grace_seconds() -> float:
    return float(DOWN_GRACE_SECONDS if DOWN_GRACE_SECONDS is not None else get_settings().point_down_grace_seconds)


_grace: dict[str, asyncio.Task] = {}
_grace_sem: Optional[asyncio.Semaphore] = None


def pending_recheck() -> list[str]:
    """Точки, которые прямо сейчас перепроверяются в грейс-периоде (ещё считаются работающими)."""
    return [pid for pid, task in _grace.items() if not task.done()]


def _cancel_grace(point_id: str) -> None:
    task = _grace.pop(point_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _finish(point_id: str, new_status: str, is_down: bool, auto_names: set[str], down_since: Optional[str] = None) -> None:
    """Записать результат проверки по ТЕКУЩЕМУ статусу из базы (его могли сменить, пока шла проверка)."""
    current = await points_repository.get_point(point_id)
    if current is None:
        return
    status = (current.get("status") or "").strip()
    if not _eligible(status, auto_names):
        return  # пока шла проверка, точке выставили «ручной» статус (На складе, Демонтирован…) — не перебиваем
    if down_since and is_down and not await points_repository.get_port_down_since(point_id):
        await points_repository.set_port_down_since(point_id, down_since)       # недоступна с первой неудачной проверки
    else:
        await _update_port_down_since(point_id, is_down)
    await points_repository.mark_health_checked(point_id)
    if status == POWER_OFF and new_status == NOT_WORKING:
        return  # питания всё ещё нет — не откатываем на обычный "Не работает"
    if new_status != status:
        await points_repository.set_status(point_id, new_status, source="auto")


async def _grace_recheck(point_id: str, original: str, first_seen: str) -> None:
    """Перепроверка точки вне общей очереди: ответила — всё как было; молчит весь грейс-период — записываем сбой."""
    global _grace_sem
    if _grace_sem is None:
        _grace_sem = asyncio.Semaphore(_GRACE_PARALLEL)
    deadline = time.monotonic() + _grace_seconds()
    last: Optional[tuple[str, bool]] = None
    try:
        while True:
            await asyncio.sleep(DOWN_GRACE_RETRY_SECONDS)
            point = await points_repository.get_point(point_id)
            if point is None or (point.get("status") or "").strip() != original or not (point.get("ip_address") or "").strip():
                return  # статус уже поменяли (вручную, отчётом монтёра) или адрес убрали — дальше не наше дело
            try:
                async with _grace_sem:
                    point = await _sync_ip_from_sim(point, fresh=True)
                    new_status, is_down = await asyncio.wait_for(_probe(point), timeout=POINT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                new_status, is_down = NOT_WORKING, True
            if _RANK[new_status] <= _RANK[original]:                 # ответ вернулся: связь просто моргнула
                await _finish(point_id, new_status, is_down, await point_statuses_repository.list_auto_names())
                return
            last = (new_status, is_down)
            if time.monotonic() >= deadline:
                break
        await _finish(point_id, last[0], last[1], await point_statuses_repository.list_auto_names(), down_since=first_seen)
        logger.info("Точка %s не отвечала весь грейс-период — статус «%s»", point_id, last[0])
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("Перепроверка точки %s не удалась", point_id)
    finally:
        if _grace.get(point_id) is asyncio.current_task():
            _grace.pop(point_id, None)


def _start_grace(point_id: str, original: str) -> None:
    if point_id in _grace and not _grace[point_id].done():
        return
    first_seen = dt.datetime.now(dt.timezone.utc).isoformat()
    _grace[point_id] = asyncio.create_task(_grace_recheck(point_id, original, first_seen))


async def _check_one(point: dict, auto_names: set[str], use_grace: bool = True) -> None:
    """Проверка одной точки — общий путь и для фонового круга, и для ручных кнопок «Пинг»/«Веб». `point` — снимок
    из начала круга (по нему решаем только «стоит ли проверять»); а что менять — решаем по ТЕКУЩЕМУ статусу из
    базы, прочитанному уже после проверки: пока круг дошёл до точки и проверка шла, статус могли сменить (отчёт
    монтёра, ручная проверка, диспетчер) — сверка со старым снимком раньше пропускала такие точки до следующего круга.
    use_grace=True (фоновый круг): работавшая точка, не ответившая, ещё не считается неработающей — см. грейс-период в
    начале файла. Ручная проверка (use_grace=False) показывает результат сразу, как есть."""
    if not _eligible((point.get("status") or "").strip(), auto_names):
        return
    if not (point.get("ip_address") or "").strip():
        return
    pid = point["id"]
    if use_grace:
        if pid in _grace and not _grace[pid].done():
            return  # точку уже перепроверяют отдельно — второй раз не дёргаем
    else:
        _cancel_grace(pid)  # диспетчер проверил вручную — его результат главнее
    point = await _sync_ip_from_sim(point, fresh=not use_grace)         # ручная проверка (use_grace=False) — по свежему реестру; фоновый круг — по реестру, прочитанному на круг
    new_status, is_down = await _probe(point)
    current = await points_repository.get_point(pid)
    if current is None:
        return
    status = (current.get("status") or "").strip()
    if not _eligible(status, auto_names):
        return  # пока шла проверка, точке выставили «ручной» статус (На складе, Демонтирован…) — не перебиваем
    if (use_grace and _grace_seconds() > 0 and status in (WORKING, PING_ONLY)
            and new_status in _RANK and _RANK[new_status] > _RANK[status]):
        await points_repository.mark_health_checked(pid)
        _start_grace(pid, status)
        return
    await _finish(pid, new_status, is_down, auto_names)


async def _check_one_safe(point: dict, auto_names: set[str]) -> None:
    try:
        await asyncio.wait_for(_check_one(point, auto_names), timeout=POINT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Проверка точки %s не уложилась в %s с — пропускаю до следующего круга", point.get("id"), POINT_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — one bad point shouldn't stop the batch
        logger.exception("Health check failed for point %s", point.get("id"))


async def recheck_and_apply(point_id: str) -> None:
    """Runs the full ping+port classification for one point right now and
    applies it (same eligibility rule as the background loop) — called
    after a manual Пинг/Веб-интерфейс click so the status doesn't wait for
    the next lap. Never raises: a probe failure here must not break the
    manual check's own response."""
    try:
        point = await points_repository.get_point(point_id)
        if point is None:
            return
        await _check_one(point, await point_statuses_repository.list_auto_names(), use_grace=False)
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("Manual recheck failed for point %s", point_id)


LAST_ROUND: dict = {}          # когда закончился последний круг проверки и сколько комплексов обошли (для «Состояния системы»)


async def check_all_points() -> None:
    """One full lap over every point, in small concurrent batches with a
    pause between them. Used directly by tests/manual runs; the running
    app instead uses `point_health_loop` below, which calls this
    repeatedly forever."""
    global _sim_ip_map
    started = time.monotonic()
    points = await points_repository.list_points()
    auto_names = await point_statuses_repository.list_auto_names()
    _sim_ip_map = await sim_ip_registry_repository.get_all_map()
    try:
        for i in range(0, len(points), BATCH_SIZE):
            batch = points[i : i + BATCH_SIZE]
            await asyncio.gather(*(_check_one_safe(p, auto_names) for p in batch))
            if i + BATCH_SIZE < len(points):
                await asyncio.sleep(BATCH_PAUSE_SECONDS)
    finally:
        _sim_ip_map = None
    LAST_ROUND.update(at=time.time(), points=len(points), seconds=round(time.monotonic() - started))
    logger.info("Проверка доступности: круг по %d точкам занял %.0f с", len(points), time.monotonic() - started)


async def point_health_loop() -> None:
    while True:
        try:
            await check_all_points()
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("Плановая проверка точек завершилась ошибкой")
        await asyncio.sleep(LAP_PAUSE_SECONDS)
