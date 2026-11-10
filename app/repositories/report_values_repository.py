"""Manually-entered values for report templates (see
app/models/report_fields.py for what a "report_type" is and which fields
it has) — the operator-filled half of a report whose other half is
computed fresh from app data on every request.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from app.db.database import get_connection

# period='' is the sentinel for a global, non-dated template (план-наряд).
GLOBAL_PERIOD = ""


async def get_values(report_type: str, period: str) -> dict[str, str]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select field_key, value from report_field_values where report_type = ? and period = ?",
            (report_type, period),
        )
        return {k: v for k, v in await cur.fetchall()}


async def get_values_with_carry_forward(report_type: str, period: str, field_keys: list[str]) -> dict[str, str]:
    """Per-day reports (period = a date) start pre-filled with the most
    recently saved value for each field, so an operator isn't retyping a
    slowly-changing number (staff headcount, vehicle count) every single
    day — they only need to touch what actually changed before saving,
    which always writes under the *current* period, leaving history
    intact."""
    values = await get_values(report_type, period)
    missing = [k for k in field_keys if k not in values]
    if not missing:
        return values
    async with get_connection() as conn:
        for key in missing:
            cur = await conn.execute(
                "select value from report_field_values "
                "where report_type = ? and field_key = ? and period != ? and period <= ? "
                "order by period desc limit 1",
                (report_type, key, GLOBAL_PERIOD, period),
            )
            row = await cur.fetchone()
            if row:
                values[key] = row[0]
    return values


async def save_values(report_type: str, period: str, values: dict[str, str], updated_by: Optional[str]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with get_connection() as conn:
        for key, value in values.items():
            await conn.execute(
                "insert into report_field_values (report_type, period, field_key, value, updated_at, updated_by) "
                "values (?, ?, ?, ?, ?, ?) "
                "on conflict(report_type, period, field_key) do update set "
                "value = excluded.value, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (report_type, period, key, value, now, updated_by),
            )
        await conn.commit()
