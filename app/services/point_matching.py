"""Поиск уже заведённого комплекса по названию из чужого файла, Excel или метки на карте.

Порядок такой, чтобы не плодить дубли и не склеить разные комплексы:
  1. номер совпал целиком (без пробелов по краям и регистра);
  2. совпал инвентарный номер (в Конструктор карт уходит именно он — метка может прийти обратно инвентарным номером);
  3. совпало «по сути»: без регистра, ё/е, пробелов и знаков, русские и латинские буквы-двойники одинаковы
     («ТО-5» = «TO 5» = «то5») — только если такой комплекс один;
  4. у названия хвост « +1», « +2», « (2)» (так помечают повтор комплекса на карте) — то же самое для названия без хвоста.
Неоднозначность (под название подходят два разных комплекса) — не совпадение: решает человек, а не программа."""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.core.names import compact_name, strip_dup_suffix


class PointIndex:
    def __init__(self, points: Iterable[dict[str, Any]]):
        self.points: dict[str, dict[str, Any]] = {}
        self._exact: dict[str, str] = {}
        self._inv_exact: dict[str, list[str]] = {}
        self._compact: dict[str, list[str]] = {}
        self._inv_compact: dict[str, list[str]] = {}
        for p in points:
            self.add(p)

    def add(self, p: dict[str, Any]) -> None:
        pid = str(p.get("id") or "")
        if not pid:
            return
        self.points[pid] = p
        self._exact.setdefault(pid.strip().casefold(), pid)
        key = compact_name(pid)
        if key:
            self._compact.setdefault(key, []).append(pid)
        inv = str(p.get("inventory_number") or "").strip()
        if inv:
            self._inv_exact.setdefault(inv.casefold(), []).append(pid)
            ikey = compact_name(inv)
            if ikey:
                self._inv_compact.setdefault(ikey, []).append(pid)

    def _one(self, text: str) -> Optional[str]:
        """Совпадение по шагам 1–3 для готового названия (без разбора хвоста)."""
        text = (text or "").strip()
        if not text:
            return None
        pid = self._exact.get(text.casefold())
        if pid:
            return pid
        hit = self._inv_exact.get(text.casefold())
        if hit and len(set(hit)) == 1:
            return hit[0]
        key = compact_name(text)
        if not key:
            return None
        hit = self._compact.get(key)
        if hit and len(set(hit)) == 1:
            return hit[0]
        hit = self._inv_compact.get(key)
        if hit and len(set(hit)) == 1:
            return hit[0]
        return None

    def find_id(self, label: object) -> Optional[dict[str, Any]]:
        """Как find, но только по номерам комплексов (без инвентарных): для файлов, где первый столбец — номер, а не метка карты."""
        text = str(label or "").strip()
        base, n = strip_dup_suffix(text)
        for cand in ([base] if n is not None else []) + [text]:
            pid = self._exact.get(cand.strip().casefold())
            if pid is None:
                hit = self._compact.get(compact_name(cand)) if compact_name(cand) else None
                pid = hit[0] if hit and len(set(hit)) == 1 else None
            if pid:
                return self.points[pid]
        return None

    def find(self, label: object) -> Optional[dict[str, Any]]:
        """Комплекс из справочника для названия из файла/карты или None."""
        text = str(label or "").strip()
        base, n = strip_dup_suffix(text)
        if n is not None:
            # «ARENA-1 +1»: если есть «ARENA-1» — это он; точно такое же название с хвостом заведено ошибочно прошлым импортом
            pid = self._one(base)
            if pid:
                return self.points[pid]
        pid = self._one(text)
        return self.points[pid] if pid else None

    def base_name(self, label: object) -> str:
        """Название для НОВОГО комплекса: без хвоста « +N» (повтор — это тот же комплекс, а не новый)."""
        base, n = strip_dup_suffix(label)
        return base if n is not None else str(label or "").strip()
