"""CRUD for operator-uploaded .docx/.xlsx report templates — the file
itself lives on disk (data_dir/report_templates/{id}.{ext}), metadata in
the report_templates table. See app/services/template_engine.py for how
a template is filled in and app/models/report_fields.py for the
available {{variable}} keys.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

from app.config import get_settings
from app.db.database import get_connection
from app.services import template_engine

ALLOWED_TYPES = {"docx", "xlsx"}


def _templates_dir():
    settings = get_settings()
    d = settings.data_dir / "report_templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_path(template_id: int, file_type: str):
    return _templates_dir() / f"{template_id}.{file_type}"


def _row_to_dict(row) -> dict[str, Any]:
    id_, name, file_type, mode, variables, created_at, created_by, visibility, shared_users = row
    return {
        "id": id_,
        "name": name,
        "file_type": file_type,
        "mode": mode,
        "variables": json.loads(variables),
        "created_at": created_at,
        "created_by": created_by,
        "visibility": visibility or "public",
        "shared_users": json.loads(shared_users or "[]"),
    }


_SELECT = "select id, name, file_type, mode, variables, created_at, created_by, visibility, shared_users from report_templates"


async def list_templates() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " order by created_at desc")
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def get_template(template_id: int) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(_SELECT + " where id = ?", (template_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


def read_file(template: dict[str, Any]) -> bytes:
    return _file_path(template["id"], template["file_type"]).read_bytes()


async def create_template(name: str, file_type: str, content: bytes, created_by: Optional[str], visibility: str = "private", shared_users: list[int] | None = None) -> dict[str, Any]:
    if file_type not in ALLOWED_TYPES:
        raise ValueError(f"Неподдерживаемый тип файла: {file_type}")
    variables = template_engine.extract_variables(file_type, content)
    mode = template_engine.classify_mode(variables)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    async with get_connection() as conn:
        cur = await conn.execute(
            "insert into report_templates (name, file_type, mode, variables, created_at, created_by, visibility, shared_users) "
            "values (?, ?, ?, ?, ?, ?, ?, ?)",
            (name.strip(), file_type, mode, json.dumps(variables, ensure_ascii=False), now, created_by, visibility, json.dumps(shared_users or [])),
        )
        template_id = cur.lastrowid
        await conn.commit()

    _file_path(template_id, file_type).write_bytes(content)
    return await get_template(template_id)  # type: ignore[return-value]


async def set_access(template_id: int, visibility: str, shared_users: list[int]) -> Optional[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "update report_templates set visibility = ?, shared_users = ? where id = ?",
            (visibility, json.dumps(shared_users), template_id),
        )
        await conn.commit()
        if not cur.rowcount:
            return None
    return await get_template(template_id)


async def update_template(template_id: int, name: str, content: bytes | None = None) -> Optional[dict[str, Any]]:
    item = await get_template(template_id)
    if not item:
        return None
    variables, mode = item["variables"], item["mode"]
    if content is not None:
        variables = template_engine.extract_variables(item["file_type"], content)
        mode = template_engine.classify_mode(variables)
        _file_path(template_id, item["file_type"]).write_bytes(content)
    async with get_connection() as conn:
        await conn.execute(
            "update report_templates set name = ?, variables = ?, mode = ? where id = ?",
            (name.strip(), json.dumps(variables, ensure_ascii=False), mode, template_id),
        )
        await conn.commit()
    return await get_template(template_id)


async def delete_template(template_id: int) -> None:
    template = await get_template(template_id)
    if not template:
        return
    async with get_connection() as conn:
        await conn.execute("delete from report_templates where id = ?", (template_id,))
        await conn.commit()
    path = _file_path(template_id, template["file_type"])
    if path.exists():
        path.unlink()
