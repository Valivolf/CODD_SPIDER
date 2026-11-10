"""Loose schema for the calendar state payload (tasks).

Tasks carry free-form UI fields that have grown over time, so — as in the
original app — we validate shape (right top-level keys, right container
types) rather than pin every sub-field.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator


class StatePayload(BaseModel):
    rev: int
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def check_shape(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v.get("tasks"), list):
            raise ValueError("Поле 'tasks' должно быть типа list")
        return v
