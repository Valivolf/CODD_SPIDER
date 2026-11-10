"""Чат монтёра с диспетчером. Монтёр пишет в VK-боте («Написать диспетчеру»), диспетчер читает и отвечает в веб-панели
(окно «Чаты»). История хранится только здесь и показывается только в веб-панели; боту отдаются лишь новые ответы
диспетчера, которые он пересылает монтёру в VK.

direction: 'in' — монтёр → диспетчер (read_at — когда диспетчер открыл переписку), 'out' — диспетчер → монтёр
(claimed_at — бот взял сообщение на отправку, delivered_at — оно ушло в VK). Бот забирает сообщения «с арендой»
(claim_pending): взятое, но не подтверждённое как доставленное за минуту, отдаётся снова — так сообщение не пропадает,
если бот упал на отправке, и не дублируется, если живы два бота.

В сообщении могут быть фото (chat_attachments, до 10 штук; файлы — app/services/chat_files.py). Переписка хранится RETENTION_DAYS (неделю):
purge_old удаляет и старые сообщения, и их файлы."""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Optional

from app.db.database import get_connection
from app.services import chat_files

RETENTION_DAYS = 7
PURGE_EVERY_SECONDS = 3600
MAX_TEXT = 2000
CLAIM_LEASE_SECONDS = 60
MAX_ATTEMPTS = 5   # столько раз бот пробует отправить в VK, потом сообщение помечается «не доставлено»

MAX_BROADCAST_RECIPIENTS = 1000
_MSG_COLS = "id, person_id, direction, text, author, point_id, task_id, created_at, read_at, delivered_at, delivery_error, failed_at, broadcast_id"
_MSG_KEYS = [c.strip() for c in _MSG_COLS.split(",")]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _row(row) -> dict[str, Any]:
    d = dict(zip(_MSG_KEYS, row))
    d["attachments"] = []
    return d


def photo_label(text: str, photos: int) -> str:
    """Текст для списков и уведомлений: у сообщения без слов — «📷 Фото (3)»."""
    if text:
        return text
    return f"📷 Фото ({photos})" if photos > 1 else "📷 Фото" if photos else ""


async def _attach_conn(conn, messages: list[dict[str, Any]]) -> None:
    """Дописывает каждому сообщению список его фото: [{id, ext, name, size}]."""
    by_id = {m["id"]: m for m in messages}
    if not by_id:
        return
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        cur = await conn.execute(
            f"select id, message_id, file_ext, file_name, size, src_id from chat_attachments where message_id in ({', '.join('?' for _ in chunk)}) order by id",
            chunk,
        )
        for att_id, message_id, ext, name, size, src_id in await cur.fetchall():
            by_id[message_id]["attachments"].append({"id": att_id, "file_id": src_id or att_id, "ext": ext, "name": name or "", "size": size})


async def _store_files(conn, message_id: int, files: list[tuple[str, str, bytes]]) -> None:
    """Строки вложений + файлы на диск. Файл не записался — сообщение целиком откатывается (строка без файла не остаётся)."""
    now = _now()
    saved: list[tuple[int, str]] = []
    try:
        for name, ext, content in files:
            cur = await conn.execute(
                "insert into chat_attachments (message_id, file_ext, file_name, size, created_at) values (?, ?, ?, ?, ?)",
                (message_id, ext, name, len(content), now),
            )
            await chat_files.save(cur.lastrowid, ext, content)
            saved.append((cur.lastrowid, ext))
    except BaseException:
        await chat_files.remove(saved)
        await conn.execute("delete from chat_attachments where message_id = ?", (message_id,))
        await conn.execute("delete from chat_messages where id = ?", (message_id,))
        await conn.commit()
        raise


async def add_incoming(
    person_id: int, text: str, point_id: Optional[str] = None, task_id: Optional[str] = None,
    files: Optional[list[tuple[str, str, bytes]]] = None,
) -> dict[str, Any]:
    """Сообщение монтёра. `files` — уже проверенные chat_files.check(): [(имя, расширение, содержимое)]."""
    text = (text or "").strip()[:MAX_TEXT]
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into chat_messages (person_id, direction, text, point_id, task_id, created_at) values (?, 'in', ?, ?, ?, ?)",
            (person_id, text, point_id or None, task_id or None, _now()),
        )
        await conn.commit()
        message_id = cur.lastrowid
        if files:
            await _store_files(conn, message_id, files)
            await conn.commit()
        cur = await conn.execute(f"select {_MSG_COLS} from chat_messages where id = ?", (message_id,))
        msg = _row(await cur.fetchone())
        await _attach_conn(conn, [msg])
        return msg


async def add_outgoing(person_id: int, text: str, author: str, files: Optional[list[tuple[str, str, bytes]]] = None) -> dict[str, Any]:
    text = (text or "").strip()[:MAX_TEXT]
    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into chat_messages (person_id, direction, text, author, created_at) values (?, 'out', ?, ?, ?)",
            (person_id, text, author, _now()),
        )
        await conn.commit()
        message_id = cur.lastrowid
        if files:
            await _store_files(conn, message_id, files)
            await conn.commit()
        cur = await conn.execute(f"select {_MSG_COLS} from chat_messages where id = ?", (message_id,))
        msg = _row(await cur.fetchone())
        await _attach_conn(conn, [msg])
        return msg


async def get_attachment(att_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select a.id, a.message_id, a.file_ext, a.file_name, a.size, m.person_id, a.src_id from chat_attachments a "
            "join chat_messages m on m.id = a.message_id where a.id = ?", (att_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    # file_id — вложение, под номером которого лежит файл на диске: у рассылки одно фото на всех получателей (src_id)
    return {"id": row[0], "message_id": row[1], "ext": row[2], "name": row[3] or "", "size": row[4], "person_id": row[5], "file_id": row[6] or row[0]}


async def purge_old(days: int = RETENTION_DAYS) -> dict[str, int]:
    """Переписка хранится неделю: сообщения старше и их фото (строки и файлы) удаляются — прочитанные и нет."""
    edge = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            "select a.id, a.file_ext, a.src_id from chat_attachments a join chat_messages m on m.id = a.message_id where m.created_at < ?", (edge,)
        )
        expiring = await cur.fetchall()
        await conn.execute("delete from chat_attachments where message_id in (select id from chat_messages where created_at < ?)", (edge,))
        cur = await conn.execute("delete from chat_messages where created_at < ?", (edge,))
        deleted = cur.rowcount
        await conn.execute(
            "delete from chat_broadcasts where created_at < ? and id not in (select broadcast_id from chat_messages where broadcast_id is not null)", (edge,)
        )
        # Файл рассылки общий для всех её получателей: удаляем его, только когда на него не осталось ни одного вложения.
        files: dict[int, str] = {}
        for att_id, ext, src_id in expiring:
            file_id = src_id or att_id
            cur = await conn.execute("select 1 from chat_attachments where id = ? or src_id = ? limit 1", (file_id, file_id))
            if not await cur.fetchone():
                files[file_id] = ext
        await conn.commit()
    removed = await chat_files.remove(list(files.items())) if files else 0
    async with get_connection() as conn:
        cur = await conn.execute("select id from chat_attachments union select src_id from chat_attachments where src_id is not null")
        known = {r[0] for r in await cur.fetchall()}
    removed += await chat_files.remove_orphans(known)
    return {"messages": deleted, "files": removed}


async def retention_loop() -> None:
    """Раз в час убирает переписку старше RETENTION_DAYS (запускается вместе с сервером, первый раз — сразу)."""
    import asyncio
    import logging

    while True:
        try:
            result = await purge_old()
            if result["messages"]:
                logging.getLogger(__name__).info("Чат: удалено сообщений старше %s дн.: %s (файлов: %s)", RETENTION_DAYS, result["messages"], result["files"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сбой одной очистки не должен остановить цикл
            logging.getLogger(__name__).exception("Чат: не удалось удалить старую переписку")
        await asyncio.sleep(PURGE_EVERY_SECONDS)


async def conversations(query: str = "", include_all: bool = False) -> list[dict[str, Any]]:
    """Список чатов: сначала с непрочитанными, потом по времени последнего сообщения. `include_all` — добавить и тех,
    с кем переписки ещё нет (чтобы диспетчер мог написать первым). Сообщения массовой рассылки список не меняют: иначе
    после каждой рассылки в нём оказались бы все монтёры (рассылка видна в переписке самого человека и в окне «Рассылки»)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            select p.id, p.full_name, p.vk_id, p.role_type, p.phone, p.active,
                   m.id, m.direction, m.text, m.created_at, m.point_id, m.delivered_at, m.failed_at,
                   (select count(*) from chat_messages u where u.person_id = p.id and u.direction = 'in' and u.read_at is null),
                   (select count(*) from chat_attachments a where a.message_id = m.id)
            from people p
            left join chat_messages m on m.id = (select max(id) from chat_messages where person_id = p.id and broadcast_id is null)
            where p.vk_id is not null and (m.id is not null or ?)
            """,
            (1 if include_all else 0,),
        )
        rows = await cur.fetchall()
    items = []
    key = (query or "").strip().casefold()
    for (pid, name, vk_id, role, phone, active, mid, direction, text, ts, point, delivered, failed, unread, photos) in rows:
        if key and key not in (name or "").casefold():
            continue
        items.append({
            "person_id": pid, "full_name": name, "vk_id": vk_id, "role_type": role, "phone": phone or "", "active": bool(active),
            "last_id": mid, "last_direction": direction, "last_text": photo_label(text or "", photos or 0), "last_at": ts, "last_point_id": point or "",
            "last_delivered": bool(delivered) if direction == "out" else None, "last_failed": bool(failed), "unread": unread or 0,
        })
    items.sort(key=lambda i: (0 if i["unread"] else 1, -(i["last_id"] or 0)))
    return items


async def messages(person_id: int, after_id: int = 0, limit: int = 300) -> list[dict[str, Any]]:
    async with get_connection() as conn:
        if after_id:
            cur = await conn.execute(
                f"select {_MSG_COLS} from chat_messages where person_id = ? and id > ? order by id limit ?", (person_id, after_id, limit)
            )
            rows = await cur.fetchall()
        else:
            # Без after_id — последние `limit` сообщений по порядку.
            cur = await conn.execute(
                f"select {_MSG_COLS} from (select {_MSG_COLS} from chat_messages where person_id = ? order by id desc limit ?) order by id",
                (person_id, limit),
            )
            rows = await cur.fetchall()
        result = [_row(r) for r in rows]
        await _attach_conn(conn, result)
    return result


async def mark_read(person_id: int) -> int:
    async with get_connection() as conn:
        cur = await conn.execute(
            "update chat_messages set read_at = ? where person_id = ? and direction = 'in' and read_at is null", (_now(), person_id)
        )
        await conn.commit()
        return cur.rowcount


async def unread_summary() -> dict[str, Any]:
    """Для значка на кнопке чатов и сигнала о новом сообщении: сколько непрочитанных, откуда и последнее входящее."""
    async with get_connection() as conn:
        cur = await conn.execute("select count(*) from chat_messages where direction = 'in' and read_at is null")
        (total,) = await cur.fetchone()
        cur = await conn.execute(
            "select m.id, m.person_id, p.full_name, m.text, m.created_at, (select count(*) from chat_attachments a where a.message_id = m.id) "
            "from chat_messages m join people p on p.id = m.person_id "
            "where m.direction = 'in' and m.read_at is null order by m.id desc limit 5"
        )
        recent = [{"id": r[0], "person_id": r[1], "full_name": r[2], "text": photo_label(r[3] or "", r[5] or 0), "created_at": r[4]} for r in await cur.fetchall()]
        cur = await conn.execute("select count(distinct person_id) from chat_messages where direction = 'in' and read_at is null")
        (people,) = await cur.fetchone()
    return {"total": total, "people": people, "recent": recent, "last_id": recent[0]["id"] if recent else 0}


# ---------------------------------------------------------------- сторона бота

async def claim_pending(limit: int = 20) -> list[dict[str, Any]]:
    """Новые ответы диспетчера для пересылки монтёрам (с vk_id получателя). Взятые помечаются на `CLAIM_LEASE_SECONDS`."""
    now = dt.datetime.now(dt.timezone.utc)
    lease_edge = (now - dt.timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat()
    async with get_connection() as conn:
        await conn.execute("begin immediate")
        cur = await conn.execute(
            "select m.id, m.person_id, p.vk_id, p.full_name, m.text, m.author, m.created_at, m.broadcast_id from chat_messages m "
            "join people p on p.id = m.person_id where m.direction = 'out' and m.delivered_at is null and m.failed_at is null "
            "and p.vk_id is not null and (m.claimed_at is null or m.claimed_at < ?) "
            "order by (m.broadcast_id is not null), m.id limit ?",   # обычные ответы диспетчера — вперёд рассылки, не стоят за сотней сообщений
            (lease_edge, limit),
        )
        rows = await cur.fetchall()
        for r in rows:
            await conn.execute("update chat_messages set claimed_at = ? where id = ?", (now.isoformat(), r[0]))
        await conn.commit()
        out = [
            {"id": r[0], "person_id": r[1], "vk_id": r[2], "full_name": r[3], "text": r[4], "author": r[5] or "", "created_at": r[6],
             "broadcast": r[7] is not None, "attachments": []}
            for r in rows
        ]
        await _attach_conn(conn, out)
    return out


async def mark_delivered(ids: list[int]) -> None:
    if not ids:
        return
    async with get_connection() as conn:
        await conn.execute(
            f"update chat_messages set delivered_at = ? where direction = 'out' and id in ({', '.join('?' for _ in ids)})", (_now(), *ids)
        )
        await conn.commit()


async def report_failure(message_id: int, error: str) -> None:
    """VK не принял сообщение: запоминаем причину, повторим через минуту; после MAX_ATTEMPTS попыток — «не доставлено»."""
    now = _now()
    async with get_connection() as conn:
        await conn.execute(
            "update chat_messages set attempts = attempts + 1, delivery_error = ?, claimed_at = ?, "
            "failed_at = case when attempts + 1 >= ? then ? else null end where id = ? and direction = 'out' and delivered_at is null",
            ((error or "")[:200], now, MAX_ATTEMPTS, now, message_id),
        )
        await conn.commit()


async def release(ids: list[int]) -> None:
    """Не удалось отправить в VK — вернуть в очередь (повторит следующий проход бота)."""
    if not ids:
        return
    async with get_connection() as conn:
        await conn.execute(
            f"update chat_messages set claimed_at = null where direction = 'out' and delivered_at is null and id in ({', '.join('?' for _ in ids)})", ids
        )
        await conn.commit()


# ---------------------------------------------------------------- массовая рассылка

def _in(n: int) -> str:
    return ", ".join("?" for _ in range(n))


async def reachable_ids(person_ids: list[int]) -> list[int]:
    """Из списка — те, кому можно написать в бота: человек есть, активен и привязан к VK. Порядок и уникальность сохраняются."""
    wanted = list(dict.fromkeys(int(i) for i in person_ids))
    ok: set[int] = set()
    async with get_connection() as conn:
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            cur = await conn.execute(f"select id from people where id in ({_in(len(chunk))}) and active = 1 and vk_id is not null", chunk)
            ok.update(r[0] for r in await cur.fetchall())
    return [i for i in wanted if i in ok]


async def add_broadcast(
    person_ids: list[int], text: str, author: str, files: Optional[list[tuple[str, str, bytes]]] = None, *,
    mode: str = "pick", filter_desc: str = "", token: Optional[str] = None,
) -> dict[str, Any]:
    """Массовая рассылка: заголовок в chat_broadcasts и по исходящему сообщению чата на каждого получателя — их забирает бот
    тем же путём, что ответы диспетчера (повторы, «доставлено / не доставлено»). Фото сохраняется на диск один раз.
    `token` — одноразовый номер от окна рассылки: повторная отправка с тем же номером (двойной щелчок, повтор запроса)
    ничего не создаёт, а возвращает уже созданную рассылку. Получатели без VK и деактивированные пропускаются (`skipped`)."""
    text = (text or "").strip()[:MAX_TEXT]
    ids = await reachable_ids(person_ids)
    skipped = len(set(int(i) for i in person_ids)) - len(ids)
    now = _now()
    async with get_connection() as conn:
        if token:
            cur = await conn.execute("select id from chat_broadcasts where token = ?", (token,))
            row = await cur.fetchone()
            if row:
                summary = await _broadcast_summary(conn, row[0])
                summary["duplicate"] = True
                return summary
        try:
            cur = await conn.execute(
                "insert into chat_broadcasts (created_at, author, text, mode, filter_desc, total, token) values (?, ?, ?, ?, ?, ?, ?)",
                (now, author or "", text, mode, filter_desc[:500], len(ids), token or None),
            )
        except sqlite3.IntegrityError:                    # тот же номер пришёл одновременно из двух запросов: второй ждал первый
            await conn.rollback()
            cur = await conn.execute("select id from chat_broadcasts where token = ?", (token,))
            row = await cur.fetchone()
            if not row:
                raise
            summary = await _broadcast_summary(conn, row[0])
            summary["duplicate"] = True
            return summary
        broadcast_id = cur.lastrowid
        owners: list[tuple[int, str, str, int]] = []      # (номер вложения-владельца файла, расширение, имя, размер)
        saved: list[tuple[int, str]] = []
        try:
            for index, person_id in enumerate(ids):
                cur = await conn.execute(
                    "insert into chat_messages (person_id, direction, text, author, created_at, broadcast_id) values (?, 'out', ?, ?, ?, ?)",
                    (person_id, text, author or "", now, broadcast_id),
                )
                message_id = cur.lastrowid
                for k, (name, ext, content) in enumerate(files or []):
                    if index == 0:
                        cur = await conn.execute(
                            "insert into chat_attachments (message_id, file_ext, file_name, size, created_at) values (?, ?, ?, ?, ?)",
                            (message_id, ext, name, len(content), now),
                        )
                        await chat_files.save(cur.lastrowid, ext, content)
                        saved.append((cur.lastrowid, ext))
                        owners.append((cur.lastrowid, ext, name, len(content)))
                    else:
                        owner_id, o_ext, o_name, o_size = owners[k]
                        await conn.execute(
                            "insert into chat_attachments (message_id, file_ext, file_name, size, created_at, src_id) values (?, ?, ?, ?, ?, ?)",
                            (message_id, o_ext, o_name, o_size, now, owner_id),
                        )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            await chat_files.remove(saved)
            raise
        summary = await _broadcast_summary(conn, broadcast_id)
    summary["skipped"] = skipped
    return summary


_BROADCAST_STATS = (
    "select b.id, b.created_at, b.author, b.text, b.mode, b.filter_desc, b.total, "
    "(select count(*) from chat_messages m where m.broadcast_id = b.id), "
    "(select count(*) from chat_messages m where m.broadcast_id = b.id and m.delivered_at is not null), "
    "(select count(*) from chat_messages m where m.broadcast_id = b.id and m.delivered_at is null and m.failed_at is not null), "
    "(select count(distinct coalesce(a.src_id, a.id)) from chat_attachments a join chat_messages m on m.id = a.message_id where m.broadcast_id = b.id) "
    "from chat_broadcasts b"
)


def _broadcast_row(r) -> dict[str, Any]:
    present, delivered, failed = r[7], r[8], r[9]
    return {
        "id": r[0], "created_at": r[1], "author": r[2], "text": r[3], "mode": r[4], "filter_desc": r[5], "total": r[6],
        "present": present, "delivered": delivered, "failed": failed, "pending": max(0, present - delivered - failed), "photos": r[10],
    }


async def _broadcast_summary(conn, broadcast_id: int) -> dict[str, Any]:
    cur = await conn.execute(f"{_BROADCAST_STATS} where b.id = ?", (broadcast_id,))
    return _broadcast_row(await cur.fetchone())


async def broadcasts(limit: int = 30) -> list[dict[str, Any]]:
    """Последние рассылки с итогами: сколько доставлено, сколько не удалось, сколько ещё ждёт (переписка хранится неделю)."""
    async with get_connection() as conn:
        cur = await conn.execute(f"{_BROADCAST_STATS} order by b.id desc limit ?", (limit,))
        return [_broadcast_row(r) for r in await cur.fetchall()]


async def broadcast_detail(broadcast_id: int) -> Optional[dict[str, Any]]:
    """Рассылка и по каждому получателю: доставлено / не доставлено (с причиной) / ждёт."""
    async with get_connection() as conn:
        cur = await conn.execute(f"{_BROADCAST_STATS} where b.id = ?", (broadcast_id,))
        row = await cur.fetchone()
        if not row:
            return None
        out = _broadcast_row(row)
        cur = await conn.execute(
            "select p.id, p.full_name, p.role_type, m.delivered_at, m.failed_at, m.delivery_error, m.attempts from chat_messages m "
            "join people p on p.id = m.person_id where m.broadcast_id = ? order by p.full_name", (broadcast_id,),
        )
        out["recipients"] = [
            {"person_id": r[0], "full_name": r[1], "role_type": r[2], "status": "delivered" if r[3] else ("failed" if r[4] else "pending"),
             "error": r[5] or "", "delivered_at": r[3], "attempts": r[6]}
            for r in await cur.fetchall()
        ]
    return out


async def retry_broadcast(broadcast_id: int) -> int:
    """«Повторить неудачным»: сообщения со статусом «не доставлено» снова встают в очередь бота. Возвращает, сколько."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "update chat_messages set failed_at = null, attempts = 0, claimed_at = null, delivery_error = null "
            "where broadcast_id = ? and direction = 'out' and delivered_at is null and failed_at is not null", (broadcast_id,),
        )
        await conn.commit()
        return cur.rowcount
