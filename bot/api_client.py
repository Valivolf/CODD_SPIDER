"""Тонкий HTTP-клиент к API lan_calendar (/api/bot/*) — единственный
источник данных для этого бота. У бота больше нет собственной базы:
задания, точки, отчёты и фото — всё через эти вызовы.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)


def _q(text: object) -> str:
    """Номер комплекса в адресе запроса: «/», «?», «#», «%» и пробелы в названии не должны ломать адрес."""
    return quote(str(text), safe="")


class ApiError(Exception):
    """Обёртка над HTTP-ошибкой lan_calendar — `detail` содержит
    русскоязычное сообщение, которое можно показать монтёру как есть."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{status_code}: {detail}")


class NotFoundError(ApiError):
    pass


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.lan_calendar_api_url,
        headers={"X-Bot-Key": settings.bot_api_key},
        timeout=15.0,
    )


async def _request(method: str, path: str, **kwargs) -> Any:
    async with _client() as client:
        resp = await client.request(method, path, **kwargs)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001 — best-effort error message
            detail = resp.text
        cls = NotFoundError if resp.status_code == 404 else ApiError
        raise cls(resp.status_code, str(detail))
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def get_bot_enabled() -> bool:
    """Checked by AuthMiddleware on every event — lets the web panel
    silence this process if another bot instance should be the live one."""
    result = await _request("GET", "/api/bot/enabled")
    return bool(result.get("enabled", True))


async def get_me(vk_id: int) -> Optional[dict]:
    try:
        return await _request("GET", "/api/bot/me", params={"vk_id": vk_id})
    except NotFoundError:
        return None


async def list_tasks(vk_id: int, day: str) -> list[dict]:
    return await _request("GET", "/api/bot/tasks", params={"vk_id": vk_id, "day": day})


async def accept_aux_task(task_id: str, vk_id: int) -> dict:
    return await _request("POST", "/api/bot/aux-accept", json={"task_id": task_id, "vk_id": vk_id})


async def start_work_session(task_id: str, vk_id: int) -> dict:
    return await _request("POST", "/api/bot/work-sessions", json={"task_id": task_id, "vk_id": vk_id})


async def end_work_session(session_id: int) -> dict:
    return await _request("PUT", f"/api/bot/work-sessions/{session_id}/end")


async def get_point(point_id: str) -> dict:
    return await _request("GET", f"/api/bot/points/{_q(point_id)}")


async def set_point_sim(point_id: str, sim_number: str) -> dict:
    """Saves the point's SIM; the answer says what IP the point ended up with
    ({"sim_number", "ip_address", "ip_known"} — ip_known=False when the new
    SIM isn't in the SIM↔IP registry yet)."""
    return await _request("POST", f"/api/bot/points/{_q(point_id)}/sim", json={"sim_number": sim_number})


async def set_point_power_source(point_id: str, power_source: str) -> dict:
    """«СЭС» / «Электросеть» — ответ монтёра на вопрос при взятии комплекса в работу."""
    return await _request("POST", f"/api/bot/points/{_q(point_id)}/power-source", json={"power_source": power_source})


async def recheck_point(point_id: str) -> dict:
    return await _request("POST", f"/api/bot/points/{_q(point_id)}/recheck")


async def submit_visit_report(
    task_id: str,
    vk_id: int,
    result_status: str,
    comment: str = "",
    equipment_removed: Optional[str] = None,
    equipment_installed: Optional[str] = None,
    repair_mode: bool = False,
    fault: Optional[str] = None,
    needs: Optional[str] = None,
    outcome: Optional[str] = None,
    sim_removed: Optional[str] = None,
    sim_installed: Optional[str] = None,
    sim_removed_state: Optional[str] = None,
) -> dict:
    return await _request(
        "POST",
        "/api/bot/visit-reports",
        json={
            "task_id": task_id,
            "vk_id": vk_id,
            "result_status": result_status,
            "comment": comment,
            "equipment_removed": equipment_removed,
            "equipment_installed": equipment_installed,
            "repair_mode": repair_mode,
            "fault": fault,
            "needs": needs,
            "outcome": outcome,
            "sim_removed": sim_removed,
            "sim_installed": sim_installed,
            "sim_removed_state": sim_removed_state,
        },
    )


UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=15.0)       # сервер пишет фото на сетевой диск — это бывает медленно; 15 секунд на всё не хватало, и фото терялось


async def upload_photo(vk_id: int, task_id: str, filename: str, content: bytes) -> dict:
    async with httpx.AsyncClient(base_url=settings.lan_calendar_api_url, headers={"X-Bot-Key": settings.bot_api_key}, timeout=UPLOAD_TIMEOUT) as client:
        resp = await client.post(
            "/api/bot/photos",
            data={"vk_id": str(vk_id), "task_id": task_id},
            files={"file": (filename, content)},
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ApiError(resp.status_code, str(detail))
    return resp.json()


async def list_my_photos(vk_id: int, task_id: str) -> list[dict]:
    """Фото этого монтёра по заданию: [{"id", "uploaded_at"}], старые первыми."""
    return await _request("GET", "/api/bot/photos", params={"vk_id": vk_id, "task_id": task_id})


async def submit_join_request(vk_id: int, vk_name: Optional[str]) -> dict:
    """Returns {"status": "pending"|"blocked", "cooldown_active": bool} —
    always a normal 200, never raises for a routine outcome."""
    return await _request("POST", "/api/bot/join-requests", json={"vk_id": vk_id, "vk_name": vk_name})


async def get_dispatcher_phones() -> list[dict]:
    return await _request("GET", "/api/bot/dispatcher-phones")


async def create_access_issue_request(task_id: str) -> dict:
    return await _request("POST", "/api/bot/access-issues", json={"task_id": task_id})


async def get_access_issue_request(request_id: int) -> dict:
    return await _request("GET", f"/api/bot/access-issues/{request_id}")


async def set_point_power_status(point_id: str, status: str) -> None:
    await _request("POST", f"/api/bot/points/{_q(point_id)}/power-status", json={"status": status})


async def create_demontage_request(task_id: str, reason: str, kind: str = "demontage") -> dict:
    return await _request(
        "POST", "/api/bot/demontage-requests", json={"task_id": task_id, "reason": reason, "kind": kind}
    )


async def get_demontage_request(request_id: int) -> dict:
    return await _request("GET", f"/api/bot/demontage-requests/{request_id}")


async def watch_task(task_id: str, work_session_id: int, vk_id: Optional[int] = None) -> dict:
    """Следит за заданием: закрыто ли диспетчером, одобрен ли демонтаж, сдал ли отчёт второй монтёр бригады
    (`finished`) и общий прогресс комплекса (`progress`, если передан vk_id)."""
    params: dict = {"work_session_id": work_session_id}
    if vk_id:
        params["vk_id"] = vk_id
    return await _request("GET", f"/api/bot/tasks/{task_id}/watch", params=params)


async def get_task_progress(task_id: str, vk_id: int) -> dict:
    """{"state": {...}, "photo_count", "has_report", "workers": [{name, me}], "finished", "updated_by"} —
    что уже сделано по комплексу (общее для бригады, хранится на сервере)."""
    return await _request("GET", f"/api/bot/tasks/{task_id}/progress", params={"vk_id": vk_id})


async def patch_task_progress(task_id: str, vk_id: int, patch: dict) -> dict:
    """Записывает изменённые поля прогресса (остальные на сервере не трогаются); возвращает общий вид прогресса."""
    return await _request("PUT", f"/api/bot/tasks/{task_id}/progress", json={"vk_id": vk_id, "patch": patch})


async def get_active_session(vk_id: int) -> dict:
    """{"session": {...} | None} — открытая рабочая сессия монтёра: по ней бот после перезапуска возвращает его на комплекс."""
    return await _request("GET", "/api/bot/active-session", params={"vk_id": vk_id})


async def delete_photo(photo_id: int, vk_id: int, task_id: str) -> None:
    await _request("DELETE", f"/api/bot/photos/{photo_id}", params={"vk_id": vk_id, "task_id": task_id})


# ------------------------------------------------------------------ оборудование (монтёр)

async def get_point_equipment(point_id: str) -> dict:
    """{"groups": [{category_id, category_name, items: [...]}], "categories": [{id, name}]}"""
    return await _request("GET", f"/api/bot/equipment/point/{_q(point_id)}")


async def get_held_equipment(vk_id: int, category_id: Optional[int] = None) -> list[dict]:
    """Что числится на монтёре: выданное (held_reason='issued') и снятое (held_reason='removed')."""
    params: dict = {"vk_id": vk_id}
    if category_id:
        params["category_id"] = category_id
    return await _request("GET", "/api/bot/equipment/held", params=params)


async def find_equipment(vk_id: int, q: str) -> list[dict]:
    """Поиск по номеру среди всего оборудования (для «стоит другой номер»)."""
    return await _request("GET", "/api/bot/equipment/find", params={"vk_id": vk_id, "q": q})


async def replace_equipment(
    vk_id: int, point_id: str, old_id: int, new_id: int, actual: Optional[dict] = None, reason: Optional[str] = None,
) -> dict:
    """`actual` — «стояло другое»: {"actual_equipment_id": id} либо {"actual_serial": "...", "create_missing": True};
    `reason` — почему снимается старое оборудование (какая проблема)."""
    return await _request(
        "POST", "/api/bot/equipment/replace",
        json={
            "vk_id": vk_id, "point_id": point_id, "old_equipment_id": old_id, "new_equipment_id": new_id,
            "reason": reason, **(actual or {}),
        },
    )


async def find_equipment_by_suffix(vk_id: int, suffix: str, category_id: Optional[int] = None) -> list[dict]:
    """Поиск по последним символам серийного номера (6 и больше) по всему учёту: у каждой единицы usable (можно ли ставить) и why (где она сейчас)."""
    params: dict = {"vk_id": vk_id, "suffix": suffix}
    if category_id:
        params["category_id"] = category_id
    return await _request("GET", "/api/bot/equipment/by-suffix", params=params)


async def install_equipment_by_suffix(
    vk_id: int, point_id: str, suffix: str, *, old_id: Optional[int] = None, new_id: Optional[int] = None,
    category_id: Optional[int] = None, reason: Optional[str] = None,
) -> dict:
    """Установка по последним символам номера (взамен old_id, если он снимается). Ответ: mode='linked' (поставлена найденная единица) или 'pending' (в учёте номера нет — привяжется само)."""
    return await _request(
        "POST", "/api/bot/equipment/install-by-suffix",
        json={"vk_id": vk_id, "point_id": point_id, "suffix": suffix, "old_equipment_id": old_id, "new_equipment_id": new_id,
              "category_id": category_id, "reason": reason},
    )


async def install_equipment(vk_id: int, point_id: str, equipment_id: int) -> dict:
    return await _request("POST", "/api/bot/equipment/install", json={"vk_id": vk_id, "point_id": point_id, "equipment_id": equipment_id})


async def remove_equipment(
    vk_id: int, point_id: str, equipment_id: int, actual: Optional[dict] = None, reason: Optional[str] = None,
) -> dict:
    return await _request(
        "POST", "/api/bot/equipment/remove",
        json={"vk_id": vk_id, "point_id": point_id, "equipment_id": equipment_id, "reason": reason, **(actual or {})},
    )


async def sim_replace(
    vk_id: int, point_id: str, serial_number: str, reason: Optional[str] = None, removed_working: Optional[bool] = None,
) -> dict:
    """Заменили SIM-карту комплекса: {"installed", "removed": [...], "created", "ip_known", "ip_address"}.
    `removed_working` — снятая SIM рабочая (True) или нет (False)."""
    return await _request(
        "POST", "/api/bot/equipment/sim/replace",
        json={"vk_id": vk_id, "point_id": point_id, "serial_number": serial_number, "reason": reason, "removed_working": removed_working},
    )


async def sim_fix(vk_id: int, point_id: str, serial_number: str) -> dict:
    """Исправили номер SIM комплекса (был указан неверно): {"old", "installed", "ip_known", "ip_address"}."""
    return await _request(
        "POST", "/api/bot/equipment/sim/fix", json={"vk_id": vk_id, "point_id": point_id, "serial_number": serial_number},
    )


async def demontage_equipment(
    vk_id: int, point_id: str, whole: bool, equipment_ids: Optional[list[int]] = None, reason: Optional[str] = None,
) -> dict:
    """Демонтаж: весь комплекс (`whole`) или выбранные элементы снимаются на монтёра."""
    return await _request(
        "POST", "/api/bot/equipment/demontage",
        json={"vk_id": vk_id, "point_id": point_id, "whole": whole, "equipment_ids": equipment_ids or [], "reason": reason},
    )


# ------------------------------------------------------------------ склад (роль «склад» в персонале)

async def wh_summary(vk_id: int) -> dict:
    return await _request("GET", "/api/bot/warehouse/summary", params={"vk_id": vk_id})


async def wh_awaiting(vk_id: int) -> list[dict]:
    return await _request("GET", "/api/bot/warehouse/awaiting", params={"vk_id": vk_id})


async def wh_person_items(vk_id: int, person_id: int) -> list[dict]:
    return await _request("GET", f"/api/bot/warehouse/person/{person_id}/items", params={"vk_id": vk_id})


async def wh_accept(vk_id: int, equipment_ids: list[int], person_id: Optional[int] = None) -> dict:
    return await _request("POST", "/api/bot/warehouse/accept", json={"vk_id": vk_id, "equipment_ids": equipment_ids, "person_id": person_id})


async def wh_accept_substitute(vk_id: int, expected_id: int, person_id: Optional[int], actual: dict) -> dict:
    """Монтёр сдал другой номер: `actual` — {"actual_equipment_id": id} либо {"actual_serial": "...", "create_missing": True}."""
    return await _request(
        "POST", "/api/bot/warehouse/accept-substitute",
        json={"vk_id": vk_id, "expected_id": expected_id, "person_id": person_id, **actual},
    )


async def wh_found(vk_id: int, equipment_id: int) -> dict:
    return await _request("POST", "/api/bot/warehouse/found", json={"vk_id": vk_id, "equipment_id": equipment_id})


async def wh_fitters(vk_id: int, q: str = "") -> list[dict]:
    return await _request("GET", "/api/bot/warehouse/fitters", params={"vk_id": vk_id, "q": q})


async def wh_categories(vk_id: int) -> list[dict]:
    return await _request("GET", "/api/bot/warehouse/categories", params={"vk_id": vk_id})


async def wh_stock(vk_id: int, category_id: Optional[int] = None, q: str = "", limit: int = 100) -> dict:
    params: dict = {"vk_id": vk_id, "q": q, "limit": limit}
    if category_id:
        params["category_id"] = category_id
    return await _request("GET", "/api/bot/warehouse/stock", params=params)


async def wh_issue(vk_id: int, person_id: int, equipment_ids: list[int], serials: list[str]) -> dict:
    return await _request(
        "POST", "/api/bot/warehouse/issue",
        json={"vk_id": vk_id, "person_id": person_id, "equipment_ids": equipment_ids, "serials": serials},
    )


async def wh_find(vk_id: int, q: str) -> list[dict]:
    return await _request("GET", "/api/bot/warehouse/find", params={"vk_id": vk_id, "q": q})


async def wh_statuses(vk_id: int) -> list[dict]:
    return await _request("GET", "/api/bot/warehouse/statuses", params={"vk_id": vk_id})


async def wh_set_status(vk_id: int, equipment_id: int, status_id: int, comment: Optional[str] = None) -> dict:
    return await _request(
        "POST", "/api/bot/warehouse/set-status",
        json={"vk_id": vk_id, "equipment_id": equipment_id, "status_id": status_id, "comment": comment},
    )


# ------------------------------------------------------------------ чат с диспетчером (история — только в веб-панели)

async def chat_send(vk_id: int, text: str, task_id: Optional[str] = None, point_id: Optional[str] = None) -> dict:
    return await _request("POST", "/api/bot/chat/send", json={"vk_id": vk_id, "text": text, "task_id": task_id, "point_id": point_id})


async def ai_config() -> dict:
    """Включён ли ИИ в боте и отвечают ли сервисы: {"bot_voice", "asr_ready", "llm_ready"}."""
    return await _request("GET", "/api/bot/ai/config")


async def ai_voice(vk_id: int, task_id: str, point_id: str, visit_type: str, filename: str, content: bytes) -> dict:
    """Голосовой итог по комплексу → задача расшифровки и разбора; возвращает {"job_id"}."""
    async with httpx.AsyncClient(base_url=settings.lan_calendar_api_url, headers={"X-Bot-Key": settings.bot_api_key}, timeout=60.0) as client:
        resp = await client.post(
            "/api/bot/ai/voice", data={"vk_id": str(vk_id), "task_id": task_id or "", "point_id": point_id or "", "visit_type": visit_type or ""},
            files={"file": (filename, content)},
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ApiError(resp.status_code, str(detail))
    return resp.json()


async def ai_voice_job(job_id: str, vk_id: int) -> dict:
    return await _request("GET", f"/api/bot/ai/jobs/{job_id}", params={"vk_id": vk_id})


async def chat_send_photos(
    vk_id: int, text: str, files: list[tuple[str, bytes]], task_id: Optional[str] = None, point_id: Optional[str] = None,
) -> dict:
    """Сообщение монтёра с фото (до 10) и подписью — одним сообщением в чате диспетчера."""
    data = {"vk_id": str(vk_id), "text": text}
    if task_id:
        data["task_id"] = task_id
    if point_id:
        data["point_id"] = point_id
    async with httpx.AsyncClient(base_url=settings.lan_calendar_api_url, headers={"X-Bot-Key": settings.bot_api_key}, timeout=60.0) as client:
        resp = await client.post("/api/bot/chat/send-photos", data=data, files=[("files", (name, content)) for name, content in files])
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ApiError(resp.status_code, str(detail))
    return resp.json()


async def chat_file(att_id: int) -> bytes:
    """Фото из ответа диспетчера (байты) — бот загрузит его в VK и пришлёт монтёру."""
    async with httpx.AsyncClient(base_url=settings.lan_calendar_api_url, headers={"X-Bot-Key": settings.bot_api_key}, timeout=60.0) as client:
        resp = await client.get(f"/api/bot/chat/files/{att_id}")
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, "фото недоступно")
    return resp.content


async def chat_claim() -> list[dict]:
    """Новые ответы диспетчера для пересылки монтёрам: [{id, vk_id, text, author, …}]."""
    return await _request("POST", "/api/bot/chat/claim")


async def chat_delivered(ids: list[int]) -> None:
    if ids:
        await _request("POST", "/api/bot/chat/delivered", json={"ids": ids})


async def chat_failed(message_id: int, error: str) -> None:
    await _request("POST", "/api/bot/chat/failed", json={"id": message_id, "error": error})


async def create_verification_request(task_id: str) -> dict:
    return await _request("POST", "/api/bot/verification-requests", json={"task_id": task_id})


async def get_verification_request(request_id: int) -> dict:
    return await _request("GET", f"/api/bot/verification-requests/{request_id}")
