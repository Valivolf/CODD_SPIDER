"""KML export for map points / daily task plans."""
from __future__ import annotations

import io
from typing import Any, Optional
from xml.sax.saxutils import escape

from app.core.text import decode_entities


def _coord(v: Any) -> Optional[str]:
    try:
        return str(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def build_kml(items: list[dict[str, Any]], title: str) -> Optional[io.BytesIO]:
    marks = []
    for x in items:
        lon, lat = _coord(x.get("lon")), _coord(x.get("lat"))
        if not lon or not lat:
            continue
        marks.append(
            "<Placemark><name>"
            + escape(decode_entities(x.get("name", "")))
            + "</name><description>"
            + escape(decode_entities(x.get("description", "")))
            + "</description><Point><coordinates>"
            + lon + "," + lat + ",0</coordinates></Point></Placemark>"
        )
    if not marks:
        return None
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>'
        + escape(title) + "</name>" + "".join(marks) + "</Document></kml>"
    )
    return io.BytesIO(xml.encode("utf-8"))
