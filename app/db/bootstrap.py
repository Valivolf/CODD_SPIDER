"""First-run setup: create the default administrator if no users exist."""
from __future__ import annotations

import logging

from app.config import get_settings
from app.core.security import hash_password
from app.repositories import user_repository

logger = logging.getLogger(__name__)


async def bootstrap_admin() -> None:
    if await user_repository.count_users() > 0:
        return
    settings = get_settings()
    await user_repository.create_user(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        role="admin",
        must_change_password=True,
    )
    logger.warning(
        "Создан администратор по умолчанию (логин: %s). "
        "Смените пароль при первом входе или задайте ADMIN_USERNAME/ADMIN_PASSWORD в .env.",
        settings.admin_username,
    )
