"""Ссылка на комплекс в заданиях, фото, отчётах и сессиях — это ТЕКСТ, а не внешний ключ. Его мог набрать человек («метеор-00048» вместо «Метеор-00048», лишний пробел, латинская «A»
вместо русской «А», «ТО 5» вместо «ТО-5»), и тогда задание перестаёт «видеть» свой комплекс: в боте нет адреса и SIM, фото ложатся под несуществующий номер и не видны
в карточке комплекса, поиск не находит. Здесь — единое правило «чей это комплекс»:

  1. точное совпадение с номером комплекса — ничего не меняем;
  2. совпало «без разбора»: без регистра, ё/е, лишних пробелов, латинские буквы-двойники русских одинаковы (знаки остаются) — если такой комплекс один;
  3. совпало ещё свободнее (без пробелов, дефисов и знаков), но цифры в номере ровно те же — если такой комплекс один.
Неоднозначность (подходят два комплекса) — не совпадение: решает человек. Точный номер другого комплекса никогда не подменяется."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from app.core.names import compact_name, fold_name

_SPACES = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


def strict_key(text: object) -> str:
    """Название без регистра, ё/е и двойников, пробелы схлопнуты, знаки остаются."""
    return _SPACES.sub(" ", fold_name(text).strip())


def digits_of(text: object) -> str:
    return "".join(_DIGITS.findall(str(text or "")))


class PointRefs:
    """Индекс номеров комплексов для разбора ссылок: PointRefs(ids).resolve(текст) → номер комплекса или None."""

    def __init__(self, ids: Iterable[str]):
        self.ids = {str(i) for i in ids if i}
        self._strict: dict[str, list[str]] = {}
        self._compact: dict[str, list[str]] = {}
        for pid in self.ids:
            self._strict.setdefault(strict_key(pid), []).append(pid)
            key = compact_name(pid)
            if key:
                self._compact.setdefault(key, []).append(pid)

    def resolve(self, text: object) -> Optional[str]:
        raw = str(text or "")
        if not raw.strip():
            return None
        if raw in self.ids:
            return raw
        hit = self._strict.get(strict_key(raw))
        if hit:
            return hit[0] if len(hit) == 1 else None
        key = compact_name(raw)
        hit = self._compact.get(key) if key else None
        if hit and len(hit) == 1 and digits_of(hit[0]) == digits_of(raw):
            return hit[0]
        return None


    def suggest(self, text: object) -> Optional[str]:
        """Подсказка человеку (не для автоматической правки): комплекс, в номере которого есть та же длинная цифровая часть («ARENA-1111091» → «АРЕНА-СМ-1111091»), если он один.
        Нужна, когда комплекс переименовали и на старый номер остались ссылки: resolve такой номер не узнаёт."""
        runs = sorted({r for r in _DIGITS.findall(str(text or "")) if len(r) >= 5}, key=len, reverse=True)
        for run in runs:
            rx = re.compile(rf"(?<!\d){run}(?!\d)")
            hits = [pid for pid in self.ids if rx.search(pid)]
            if len(hits) == 1:
                return hits[0]
        return None


async def load_refs(conn) -> PointRefs:
    cur = await conn.execute("select id from points")
    return PointRefs(r[0] for r in await cur.fetchall())
