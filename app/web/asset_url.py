"""Cache-busting static-asset URLs, shared by every page's Jinja environment."""
from __future__ import annotations

from app.config import ROOT_DIR


def asset_url(path: str) -> str:
    """Appends a cache-busting ?v=<mtime> so editing a CSS/JS file takes
    effect immediately instead of waiting out the browser's HTTP cache."""
    full = ROOT_DIR / path.lstrip("/")
    try:
        version = int(full.stat().st_mtime)
    except OSError:
        version = 0
    return f"/{path.lstrip('/')}?v={version}"
