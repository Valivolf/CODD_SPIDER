"""Готовые «умения» помощника, которым не нужна планировка запроса моделью: досье комплекса («что с P-2», «почему не работает P-2 и что нужно»).

Данные берутся тем же кодом, что и на вкладке «Проверка» (check_service): статус и с какого времени нет связи, питание, причина и «что нужно» из отчётов, выезды, запланированное, подгруппы.
Права роли учитываются там же (пароли — только админу/диспетчеру, IP — не «просмотру»). Дальше модель отвечает на вопрос человека по этим фактам (narrate.py)."""
from __future__ import annotations

from typing import Any

from app.services import check_service
from app.services.ai import catalog as cat


def _facts_one(item: dict[str, Any]) -> str:
    lines = [f"Комплекс {item['point_id']}" + (f" ({item['category']})" if item.get("category") else "") + (f", адрес: {item['address']}" if item.get("address") else "") + "."]
    status = item.get("status") or "не известен"
    since = f" (нет связи с {item['down_since']}" + (f", {item['down_days']} дн." if item.get("down_days") else "") + ")" if item.get("down_since") and status != "Работает" else ""
    lines.append(f"Статус сейчас: {status}{since}.")
    if item.get("power_source"):
        lines.append(f"Питание: {item['power_source']}.")
    if item.get("fault_reason"):
        lines.append(f"Почему не работает (из отчёта): {item['fault_reason']}.")
    if item.get("fault_need"):
        lines.append(f"Что нужно для восстановления: {item['fault_need']}.")
    if item.get("fault_source"):
        lines.append(f"Источник: {item['fault_source']}.")
    if not item.get("fault_reason") and not item.get("fault_need") and status == "Не работает":
        lines.append("Причина в отчётах не указана.")
    if item.get("visits_count"):
        newest = (item.get("visits") or [{}])[0].get("date_text")
        lines.append(f"Выездов по отчётам: {item['visits_count']}." + (f" Самый последний выезд: {newest}." if newest else "") + " Последние (от новых к старым):")
        for v in item.get("visits", [])[:4]:
            bits = [v["date_text"], v.get("visit_type") or "выезд", f"итог «{v['result']}»" if v.get("result") else "", v.get("executor") or ""]
            tail = "; ".join(x for x in (v.get("comment") and f"сделано: {v['comment']}", v.get("fault") and f"причина: {v['fault']}", v.get("needs") and f"нужно: {v['needs']}") if x)
            lines.append("  - " + ", ".join(b for b in bits if b) + (f" — {tail}" if tail else ""))
    else:
        lines.append("Выездов по отчётам не было.")
    if item.get("planned"):
        lines.append("Запланировано: " + "; ".join(item["planned"][:3]) + ".")
    if item.get("in_plan"):
        lines.append("В плане выездов: " + item["in_plan"]["text"] + ".")
    if item.get("groups"):
        lines.append("Подгруппы: " + "; ".join(f"{g['name']}" + (f" ({g['work_type']})" if g.get("work_type") else "") + (f", нужно: {g['need']}" if g.get("need") else "") for g in item["groups"]) + ".")
    if item.get("description"):
        lines.append(f"Описание в карточке: {item['description']}.")
    if item.get("certificate") and item["certificate"] != "нет данных":
        lines.append(f"Поверка: {item['certificate']}.")
    return "\n".join(lines)


async def dossier(point_ids: list[str], role: str) -> dict[str, Any]:
    """Досье по комплексам: {"items": [...строки проверки...], "facts": текст для модели}."""
    result = await check_service.check(list(point_ids), cat.role_perms(role))
    items = [i for i in result["items"] if i.get("found")]
    facts = "\n\n".join(_facts_one(i) for i in items)
    return {"items": items, "facts": facts[:3600]}
