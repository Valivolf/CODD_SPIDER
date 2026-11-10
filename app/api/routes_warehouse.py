"""Склад — points currently parked «На складе», and the warehouse-only data
attached to them: the point's components (камера, ящик, плата… — any number,
each with an «на складе» flag and a description), "статус паутины", receipt
date and free info.

A point's status follows its components (point_components_repository.
recompute_status): anything on the shelf → «На складе» (shown as «Частично на
складе» while not every component is there — a label, not a status), nothing
on the shelf → the status is reset. Any existing point can be opened here and
have a component put on the shelf — a camera taken off a complex that is
still standing on the road.

Replaces the old serial-number equipment/issue/install/history flow
(equipment/equipment_categories/equipment_models/equipment_history
tables) — that data model stayed in the database untouched but is no
longer exposed here; per-complex warehouse tracking turned out to be
what's actually needed, not per-unit equipment tracking.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.deps import require_warehouse
from app.core.warehouse import SUGGESTED_COMPONENTS, WAREHOUSE_STATUS
from app.repositories import point_components_repository, points_repository, warehouse_items_repository
from app.services import excel_service

router = APIRouter(prefix="/api/warehouse", tags=["warehouse"], dependencies=[Depends(require_warehouse)])

MAX_IMPORT_SIZE = 5 * 1024 * 1024
_READ_CHUNK = 1024 * 1024
_MAX_COMPONENTS = 60
_MAX_COMPONENT_NAME = 60
_MAX_COMPONENT_TEXT = 500


class ComponentIn(BaseModel):
    name: str
    in_stock: bool = False
    description: str = ""


class WarehouseItemIn(BaseModel):
    web_status: str | None = None
    received_at: str | None = None
    info: str | None = None
    # None — состав не трогаем; список — полная замена (карточка всегда шлёт
    # весь список).
    components: list[ComponentIn] | None = None


def _clean_components(items: list[ComponentIn]) -> list[dict]:
    if len(items) > _MAX_COMPONENTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не больше {_MAX_COMPONENTS} компонентов у комплекса")
    cleaned = []
    for it in items:
        name = " ".join(it.name.split())
        if not name:
            continue
        if len(name) > _MAX_COMPONENT_NAME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Название компонента длиннее {_MAX_COMPONENT_NAME} символов")
        cleaned.append({"name": name, "in_stock": it.in_stock, "description": it.description.strip()[:_MAX_COMPONENT_TEXT]})
    return cleaned


@router.get("/points")
async def list_points(
    search: str = "", category_id: list[int] = Query(default=[]), uncategorized: bool = False
):
    return await warehouse_items_repository.list_in_warehouse(search, category_id, uncategorized)


@router.get("/component-names")
async def component_names():
    """Names for the card's autocomplete: the usual ones plus anything already used."""
    used = await point_components_repository.names()
    return list(dict.fromkeys([*SUGGESTED_COMPONENTS, *used]))


@router.get("/points/{point_id}")
async def get_point(point_id: str):
    item = await warehouse_items_repository.get_for_point(point_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    return item


@router.put("/points/{point_id}")
async def update_point(point_id: str, payload: WarehouseItemIn):
    existing = await warehouse_items_repository.get_for_point(point_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Точка не найдена")
    components = _clean_components(payload.components) if payload.components is not None else None
    if components is not None:
        await point_components_repository.replace_for_point(point_id, components)
    new_status = await point_components_repository.recompute_status(point_id)
    if new_status == WAREHOUSE_STATUS:
        # Только присланные поля: карточка шлёт все три, а вызов, который
        # правит один состав, не должен стирать паутину/дату/информацию.
        data = {k: existing.get(k) for k in warehouse_items_repository.FIELDS}
        data.update(payload.model_dump(include=payload.model_fields_set - {"components"}))
        await warehouse_items_repository.upsert(point_id, data)
    else:
        # Ничего не осталось на складе — складские сведения не нужны.
        await warehouse_items_repository.delete_for_point(point_id)
    return await warehouse_items_repository.get_for_point(point_id)


@router.get("/template")
async def download_template():
    out = excel_service.build_warehouse_template()
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="warehouse-import-template.xlsx"'},
    )


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл слишком большой")
        chunks.append(chunk)
    return b"".join(chunks)


def _norm(value: str) -> str:
    return value.strip().casefold()


@router.post("/import")
async def import_warehouse(file: UploadFile):
    """Excel import (one row per component — see excel_service.
    build_warehouse_template). Complexes that are not in the directory yet
    are created; each complex's components are merged by name and its status
    is then derived from them (anything on the shelf → «На складе»)."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, MAX_IMPORT_SIZE)

    parsed = excel_service.parse_warehouse_import_file(content)
    if not parsed.rows and not parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не найдено строк для импорта")

    # Rows of one complex go together, in file order.
    groups: dict[str, list[excel_service.WarehouseImportRow]] = {}
    for row in parsed.rows:
        groups.setdefault(_norm(row.point_id), []).append(row)

    existing = {_norm(p["id"]): p for p in await points_repository.list_points() if p.get("id")}
    errors = list(parsed.errors)
    created = updated = components = 0
    for key, rows in groups.items():
        point = existing.get(key)
        if point is None:
            point = await points_repository.create_point({"id": rows[0].point_id})
            existing[key] = point
            created += 1
        point_id = point["id"]

        items = [
            {"name": r.component, "in_stock": r.in_stock, "description": r.description}
            for r in rows if r.component
        ]
        if items:
            await point_components_repository.merge_for_point(point_id, items)
            components += len(items)
            new_status = await point_components_repository.recompute_status(point_id)
        else:
            # Только сведения о комплексе в целом — он перенесён на склад целиком.
            if point["status"] != WAREHOUSE_STATUS:
                await points_repository.set_status(point_id, WAREHOUSE_STATUS, source="manual")
            new_status = WAREHOUSE_STATUS
        if new_status == WAREHOUSE_STATUS:
            await warehouse_items_repository.bulk_upsert_from_import(
                point_id,
                {
                    "web_status": next((r.web_status for r in rows if r.web_status), None),
                    "received_at": next((r.received_at for r in rows if r.received_at), None),
                    "info": next((r.info for r in rows if r.info), None),
                },
            )
        updated += 1
    return {"updated": updated, "created": created, "components": components, "row_errors": errors}
