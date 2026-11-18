"""Shared accounting policy. The role keeps its independent warehouse permissions."""
from app.core import permissions as P


def is_accountant(user) -> bool:
    return user.role == "accounting"


def can_edit(user) -> bool:
    return (user.role == "admin" or is_accountant(user)) and P.has(user.permissions, "accounting.edit")


def apk_request_allowed(method: str, path: str) -> bool:
    """Additional boundary for accounting users, without revoking other sections."""
    if path == "/api/points" or path.startswith("/api/points/"):
        # Read-only, minimal projections are returned by routes_points.
        return method in ("GET", "HEAD") and not any(
            path.endswith(suffix) for suffix in ("/sim-links", "/ping", "/check-port", "/duplicates")
        )
    if path.startswith(("/api/point-plans", "/api/apk-groups", "/api/check", "/api/visit-history", "/api/task-visit-reports")):
        return False
    if path.startswith("/api/point-categories") or path.startswith("/api/point-statuses"):
        return method in ("GET", "HEAD")
    return True


async def require_point_access(user, point_id: str, *, write: bool = False) -> None:
    if not is_accountant(user):
        return
    from fastapi import HTTPException
    from app.repositories.point_category_fields_repository import point_exists

    if not (can_edit(user) if write else P.has(user.permissions, "accounting.view")):
        raise HTTPException(403, "Нет права на бухгалтерские сведения")
    if not await point_exists(point_id):
        raise HTTPException(404, "Комплекс не найден")
