"""Shared helpers for the "командировка" (multi-day crew trip) concept.

A trip is a shared crews.trip_id across N per-day crew rows — one clone
per date in range, same composition — created by
crew_repository.start_trip. It is NOT a property of a task: a task keeps
its own real date/team, whichever day it was actually created under.
Trip-awareness only changes which tasks count as "this crew's" for a
given day's crew card / bot task list / план-наряд print — see
trip_visible_crew_ids and is_trip_first_day below, used together with
tasks already filtered to `not yet reported` / a date range as needed by
each caller.

(tasks.trip_id/date_to are a retired earlier scheme — one task row per
trip day, all sharing a trip_id — kept only so historical rows created
under it still round-trip through state_repository/app.js unchanged.)
"""
from __future__ import annotations


def trip_visible_crew_ids(crew: dict) -> set:
    """Crew ids whose tasks should count as "this crew's" for `crew`'s own
    day — just itself if it isn't on a trip, every sibling day's crew id
    (across the whole trip) if it is."""
    ids = crew.get("trip_crew_ids")
    return set(ids) if ids else {crew["id"]}


def is_trip_first_day(crew: dict) -> bool:
    """Whether `crew`'s day is its trip's first — the only day a план-
    наряд/export gets generated for it, aggregating every trip task."""
    trip_from = crew.get("trip_from")
    return not trip_from or crew["date"] == trip_from
