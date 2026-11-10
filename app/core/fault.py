"""Неисправность комплекса по отчётам выездов: «почему не работает» и «что нужно для восстановления».

В карточке комплекса, в разделе «Неисправность / описание», эти два поля заполняются из отчётов сами: свежий отчёт «Не работает» с причиной/нужным
записывает их, отчёт «Работает» (а также «Демонтирован», «Установлен») их очищает — комплекс исправен или снят, причина больше не актуальна. Более
старый отчёт (его время раньше уже записанного) ничего не меняет: поздно загруженный архивный отчёт не перебьёт свежую информацию. Вручную поля можно
править в карточке; следующий более свежий отчёт заменит их новыми данными. Модуль без обращений к базе — чистая функция (её же использует разовая заливка из
уже сданных отчётов при старте, database._backfill_fault_fields)."""
from __future__ import annotations

from typing import Any, Optional

_FIXED = {"Работает", "Демонтирован", "Установлен"}


def next_state(current: dict[str, str], report: dict[str, Any]) -> Optional[dict[str, str]]:
    """current — {reason, need, at, by}; report — {result_status, fault, needs, created_at, by}. Новое состояние или None (ничего не меняется)."""
    at = str(report.get("created_at") or "")
    if current.get("at") and at and at < current["at"]:
        return None
    status = str(report.get("result_status") or "")
    if status == "Не работает":
        reason = str(report.get("fault") or "").strip()
        need = str(report.get("needs") or "").strip()
        if not reason and not need:
            return None             # отчёт без подробностей: ничего не стираем и ничего не придумываем
        return {"reason": reason, "need": need, "at": at, "by": str(report.get("by") or "")}
    if status in _FIXED and (current.get("reason") or current.get("need")):
        return {"reason": "", "need": "", "at": at, "by": str(report.get("by") or "")}
    return None
