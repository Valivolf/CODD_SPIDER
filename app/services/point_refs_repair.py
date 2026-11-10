"""Исправление «чужих» ссылок на комплексы в заданиях, фото, отчётах, сессиях и т. п. (см. app/core/point_ref.py): запись ссылается на комплекс текстом, набранным иначе, чем его номер
(другой регистр, лишний пробел, латинская буква вместо русской), — и задание «не видит» своего комплекса.

Запускается при каждом старте (быстро: ищутся только ссылки, которых нет среди номеров комплексов). Переписывает ссылку, только когда комплекс определяется однозначно; номер, который
есть среди комплексов, не трогается. Файлы фото остаются там, где лежат: в записи сначала закрепляется путь файла, потом меняется номер комплекса (как при объединении дублей).
Вспомогательные точки в заданиях не затрагиваются — у них в этом поле собственная подпись, а не номер комплекса."""
from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from app.core.point_ref import load_refs
from app.core.work_types import AUX_TYPE_NAME

log = logging.getLogger(__name__)


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cur = await conn.execute("select 1 from sqlite_master where type = 'table' and name = ?", (table,))
    return await cur.fetchone() is not None


async def repair(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Возвращает {"fixed": {прежний текст: номер комплекса}, "rows": сколько записей изменено}."""
    from app.db.database import _POINT_ID_REFERENCES  # noqa: PLC0415 — список мест, где хранится номер комплекса
    from app.services import point_rename  # noqa: PLC0415

    refs = await load_refs(conn)
    out: dict[str, Any] = {"fixed": {}, "rows": 0}
    if not refs.ids:
        return out
    candidates: set[str] = set()
    for table, col in _POINT_ID_REFERENCES:
        if not await _table_exists(conn, table):
            continue
        extra = f" and type != '{AUX_TYPE_NAME}'" if table == "tasks" else ""
        try:
            cur = await conn.execute(f"select distinct {col} from {table} where {col} is not null and {col} != '' and {col} not in (select id from points){extra}")
        except aiosqlite.Error:
            continue
        candidates |= {r[0] for r in await cur.fetchall()}
    fixed = {}
    for text in candidates:
        canon = refs.resolve(text)
        if canon and canon != text:
            fixed[text] = canon
    if not fixed:
        return out

    for old, new in fixed.items():
        try:        # те же шаги, что при переименовании комплекса: путь файла фото закрепляется, потом ссылки переезжают на настоящий номер
            moved = await point_rename.rename_references_conn(conn, old, new)
        except aiosqlite.Error:
            log.exception("Не удалось исправить ссылку на комплекс %r", old)
            continue
        out["rows"] += sum(moved.values())
    await conn.commit()
    out["fixed"] = fixed
    log.warning("Ссылки на комплексы: исправлено названий — %d, записей — %d (например: %s)", len(fixed), out["rows"],
                "; ".join(f"{o!r} → {n!r}" for o, n in list(fixed.items())[:8]))
    return out
