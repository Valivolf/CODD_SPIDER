"""Удаление сотрудника с очисткой и обезличиванием всего, что с ним связано (после подтверждения в окне «Персонал»).

Что происходит:
* УДАЛЯЕТСЯ — то, что относится к самому человеку и не нужно для истории работ: запись в персонале (ФИО, телефон, VK ID, заметки), переписка с ним в чате вместе с фото, его заявки на вступление
  в бота, отсутствия, участие в составах бригад (всех дней), открытые рабочие сессии в боте;
* ОБЕЗЛИЧИВАЕТСЯ — то, что нужно для истории и статистики выездов: отчёты о выездах, фото, движение оборудования, отметки «принято / выполнено / неисправность» остаются, но вместо ФИО
  в них — «Удалённый сотрудник», а ссылки на человека обнуляются. Отчёт не пропадает и не теряет содержания.
* Нельзя удалить того, на ком числится оборудование (сначала вернуть его на склад).
* Записи, где ФИО лежит текстом, находятся по точному совпадению ФИО. Если в персонале есть тёзка (то же ФИО), такие записи не трогаются — иначе чужие были бы обезличены вместе с этими;
  ссылки по номеру обнуляются в любом случае.
Свободный текст в отчётах (комментарии, «что сделано») не разбирается: если в нём кто-то назвал человека по имени, оно останется.
Всё выполняется одной транзакцией; файлы переписки удаляются после неё."""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.db.database import get_connection
from app.repositories import equipment_repository, people_repository
from app.services import chat_files

log = logging.getLogger(__name__)

ANON_NAME = "Удалённый сотрудник"

# Где ФИО хранится текстом: (таблица, столбец, человекочитаемое название группы). У отчётов, которые пишет бот, значение бывает вида «vk:ФИО».
NAME_COLUMNS: list[tuple[str, str, str]] = [
    ("task_visit_reports", "created_by", "отчёты о выездах"),
    ("task_visit_reports", "updated_by", "отчёты о выездах"),
    ("visit_photos", "uploaded_by", "фото выездов"),
    ("point_files", "uploaded_by", "файлы в карточках комплексов"),
    ("equipment_history", "performed_by", "движение оборудования"),
    ("equipment_history", "person_name", "движение оборудования"),
    ("equipment", "installed_by", "движение оборудования"),
    ("equipment", "removed_by", "движение оборудования"),
    ("equipment", "accepted_by", "движение оборудования"),
    ("aux_task_done", "done_by", "прочие отметки"),
    ("bot_task_progress", "updated_by", "прочие отметки"),
    ("demontage_requests", "initiated_by", "прочие отметки"),
    ("points", "fault_by", "прочие отметки"),
]
# Ссылки на человека по номеру, которые обнуляются: (таблица, столбец, группа).
ID_COLUMNS: list[tuple[str, str, str]] = [
    ("task_visit_reports", "author_person_id", "отчёты о выездах"),
    ("equipment_history", "person_id", "движение оборудования"),
]


class ErasureError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _has_column(conn, table: str, column: str) -> bool:
    cur = await conn.execute(f'pragma table_info("{table}")')
    return any(r[1] == column for r in await cur.fetchall())


async def _count(conn, sql: str, args: tuple = ()) -> int:
    cur = await conn.execute(sql, args)
    return int((await cur.fetchone())[0])


async def _namesakes(conn, person_id: int, name: str) -> int:
    cur = await conn.execute("select count(*) from people where id != ? and full_name = ?", (person_id, name))
    return int((await cur.fetchone())[0])


async def _held(person_id: int) -> list[Any]:
    return await equipment_repository.person_items(person_id)


async def preview(person_id: int) -> dict[str, Any]:
    """Что будет удалено и обезличено — для окна подтверждения. Ничего не меняет."""
    person = await people_repository.get_person(person_id)
    if not person:
        raise ErasureError("Не найдено", 404)
    name = person["full_name"] or ""
    vk = person.get("vk_id")
    held = await _held(person_id)
    async with get_connection() as conn:
        namesakes = await _namesakes(conn, person_id, name)
        delete = {
            "chat_messages": await _count(conn, "select count(*) from chat_messages where person_id = ?", (person_id,)),
            "chat_files": await _count(conn, "select count(*) from chat_attachments a join chat_messages m on m.id = a.message_id where m.person_id = ?", (person_id,)),
            "join_requests": await _count(conn, "select count(*) from bot_join_requests where vk_id = ?", (vk,)) if vk else 0,
            "absences": await _count(conn, "select count(*) from people_absences where person_id = ?", (person_id,)),
            "crew_days": await _count(conn, "select count(*) from crew_members where person_id = ?", (person_id,)),
            "sessions": await _count(conn, "select count(*) from bot_work_sessions where person_id = ?", (person_id,)),
        }
        anonymize: dict[str, int] = {}
        for table, column, group in ID_COLUMNS:
            if await _has_column(conn, table, column):
                anonymize[group] = anonymize.get(group, 0) + await _count(conn, f'select count(*) from "{table}" where "{column}" = ?', (person_id,))
        if name and not namesakes:
            for table, column, group in NAME_COLUMNS:
                if await _has_column(conn, table, column):
                    anonymize[group] = anonymize.get(group, 0) + await _count(conn, f'select count(*) from "{table}" where "{column}" in (?, ?)', (name, "vk:" + name))
    return {
        "person": {"id": person["id"], "full_name": name, "has_vk": bool(vk), "has_phone": bool(person.get("phone")), "has_notes": bool((person.get("notes") or "").strip())},
        "blocked": (f"На «{name}» числится оборудование ({len(held)} ед.) — сначала верните или примите его на склад (Склад → Выдача и приём)." if held else None),
        "delete": delete, "anonymize": anonymize, "namesakes": namesakes, "anon_name": ANON_NAME,
    }


async def erase(person_id: int) -> dict[str, Any]:
    """Удаляет сотрудника и обезличивает связанные записи (см. описание модуля). Возвращает, сколько записей затронуто."""
    person = await people_repository.get_person(person_id)
    if not person:
        raise ErasureError("Не найдено", 404)
    name = person["full_name"] or ""
    vk = person.get("vk_id")
    held = await _held(person_id)
    if held:
        raise ErasureError(f"На «{name}» числится оборудование ({len(held)} ед.) — сначала верните или примите его на склад (Склад → Выдача и приём).", 409)
    files: list[tuple[int, str]] = []
    result: dict[str, Any] = {"deleted": {}, "anonymized": {}, "namesakes": 0}
    async with get_connection() as conn:
        try:
            result["namesakes"] = namesakes = await _namesakes(conn, person_id, name)
            cur = await conn.execute(
                "select a.id, a.file_ext, a.src_id from chat_attachments a join chat_messages m on m.id = a.message_id where m.person_id = ?", (person_id,))
            mine = await cur.fetchall()
            await conn.execute("delete from chat_attachments where message_id in (select id from chat_messages where person_id = ?)", (person_id,))
            result["deleted"]["chat_messages"] = (await conn.execute("delete from chat_messages where person_id = ?", (person_id,))).rowcount
            result["deleted"]["chat_files"] = len(mine)
            # фото массовой рассылки — один файл на всех получателей: удаляется, только когда на него не осталось вложений у других людей
            shared: dict[int, str] = {}
            for att_id, ext, src_id in mine:
                file_id = src_id or att_id
                cur = await conn.execute("select 1 from chat_attachments where id = ? or src_id = ? limit 1", (file_id, file_id))
                if not await cur.fetchone():
                    shared[file_id] = ext
            files = list(shared.items())
            result["deleted"]["join_requests"] = (await conn.execute("delete from bot_join_requests where vk_id = ?", (vk,))).rowcount if vk else 0
            result["deleted"]["sessions"] = (await conn.execute("delete from bot_work_sessions where person_id = ?", (person_id,))).rowcount
            result["deleted"]["absences"] = (await conn.execute("delete from people_absences where person_id = ?", (person_id,))).rowcount
            result["deleted"]["crew_days"] = (await conn.execute("delete from crew_members where person_id = ?", (person_id,))).rowcount
            for table, column, group in ID_COLUMNS:
                if await _has_column(conn, table, column):
                    n = (await conn.execute(f'update "{table}" set "{column}" = null where "{column}" = ?', (person_id,))).rowcount
                    result["anonymized"][group] = result["anonymized"].get(group, 0) + n
            if name and not namesakes:
                for table, column, group in NAME_COLUMNS:
                    if not await _has_column(conn, table, column):
                        continue
                    # «vk:ФИО» (так бот подписывает отчёты) → «vk:Удалённый сотрудник»; остальное → «Удалённый сотрудник»
                    n = (await conn.execute(f'update "{table}" set "{column}" = ? where "{column}" = ?', ("vk:" + ANON_NAME, "vk:" + name))).rowcount
                    n += (await conn.execute(f'update "{table}" set "{column}" = ? where "{column}" = ?', (ANON_NAME, name))).rowcount
                    result["anonymized"][group] = result["anonymized"].get(group, 0) + n
            await conn.execute("delete from people where id = ?", (person_id,))
            cur = await conn.execute("select rev from state_rev where id = 1")
            row = await cur.fetchone()
            if row:
                await conn.execute("update state_rev set rev = ? where id = 1", (row[0] + 1,))        # открытые календари обновятся
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    if files:
        try:
            await chat_files.remove(files)
        except Exception:  # noqa: BLE001 — файлы переписки удаляются повторно при очистке сирот (chat_repository.purge_old)
            log.exception("Не удалось удалить файлы переписки удалённого сотрудника")
    return result
