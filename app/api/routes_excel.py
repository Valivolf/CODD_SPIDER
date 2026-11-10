from __future__ import annotations

import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.dates import format_date
from app.core.day_selection import MAX_DAYS, describe_days, parse_days_param
from app.core.warehouse import WAREHOUSE_PARTIAL_LABEL, WAREHOUSE_STATUS
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import (
    crew_repository,
    point_statuses_repository,
    points_repository,
    sim_ip_registry_repository,
    state_repository,
    task_visit_reports_repository,
)
from app.services import excel_service, sim_export, sim_import
from app.services.point_matching import PointIndex

router = APIRouter(prefix="/api/excel", tags=["excel"], dependencies=[Depends(require_password_ok)])

MAX_IMPORT_SIZE = 5 * 1024 * 1024  # 5 MB is generous for a points directory spreadsheet
_READ_CHUNK = 1024 * 1024


async def _read_capped(file: UploadFile, max_size: int) -> bytes:
    """Read the upload in chunks, aborting as soon as it exceeds max_size
    instead of buffering an unbounded body before checking its length."""
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


@router.get("/template")
async def download_template():
    out = excel_service.build_import_template()
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="points-import-template.xlsx"'},
    )


@router.get("/export")
async def export_points(user: UserOut = Depends(require_password_ok)):
    """Same columns as the import template, filled with the current
    directory — an edit-in-place, re-import-friendly export (see
    excel_service.build_points_export's docstring). Login/password columns
    are only filled in for those with the points.secrets permission, same gate as the regular
    /api/points responses (routes_points.py::_strip_credentials)."""
    points = await points_repository.list_points()
    out = excel_service.build_points_export(points, include_credentials=P.secrets_ok(user.permissions))
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="points-export.xlsx"'},
    )


@router.get("/plan/{day}")
async def download_plan(day: str):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    _, data = await state_repository.read_state(include_aux=False)
    crews = await crew_repository.list_for_date(day)
    points = await points_repository.list_points()
    out = excel_service.build_plan_workbook(day, crews, data["tasks"], points)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="plan-{format_date(day)}.xlsx"'},
    )


@router.get("/yandex/{day}")
async def download_yandex_export(day: str):
    try:
        date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата")
    _, data = await state_repository.read_state()
    crews = await crew_repository.list_for_date(day)
    points = await points_repository.list_points()
    out = excel_service.build_yandex_export(day, crews, data["tasks"], points)
    return StreamingResponse(
        out,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="yandex-{format_date(day)}.zip"'},
    )


MAX_PERIOD_DAYS = MAX_DAYS


@router.get("/period-report")
async def download_period_report(date_from: str = "", date_to: str = "", days: str = "", today: str = ""):
    """«Выезды бригад по дням» за выбранные дни: какие были бригады, их комплексы,
    задание по каждому и итог (см. excel_service.period_outcome) и вся остальная
    информация по заданию и отчёту. Дни — либо `days` (любой набор через запятую,
    например «2026-10-01,2026-10-03»: дни между крайними можно пропускать), либо
    сплошной период `date_from`…`date_to`. `today` — сегодняшняя дата по часам
    браузера (сервер живёт в другом часовом поясе: около полуночи «сегодня» у
    них расходится); без него берётся серверная."""
    try:
        today_iso = date.fromisoformat(today).isoformat() if today else date.today().isoformat()
        if days:
            day_list = parse_days_param(days)
        else:
            d_from, d_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if d_to < d_from:
                raise ValueError("Дата «по» раньше даты «с»")
            if (d_to - d_from).days + 1 > MAX_PERIOD_DAYS:
                raise ValueError(f"Период не больше {MAX_PERIOD_DAYS} дней")
            day_list = [(d_from + timedelta(days=i)).isoformat() for i in range((d_to - d_from).days + 1)]
    except ValueError as exc:
        message = str(exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверная дата" if "isoformat" in message or "Invalid isoformat" in message else message)

    days = day_list
    date_from, date_to = day_list[0], day_list[-1]
    selected = set(day_list)
    _, data = await state_repository.read_state(include_aux=False)
    tasks = [t for t in data["tasks"] if (t.get("date") or "") in selected]
    crews_by_day = {day: await crew_repository.list_for_date(day) for day in days}
    crew_lookup = {c["id"]: c for crews in crews_by_day.values() for c in crews}
    for team in {t.get("team") for t in tasks}:
        if team in crew_lookup:
            continue
        try:
            crew = await crew_repository.get_crew(int(team))
        except (TypeError, ValueError):
            crew = None
        if crew:
            crew_lookup[crew["id"]] = crew
    points = await points_repository.list_points()
    reports = await task_visit_reports_repository.list_for_tasks([t.get("id") for t in tasks])
    out = excel_service.build_period_report(days, crews_by_day, crew_lookup, tasks, points, reports, today_iso)
    name = format_date(date_from) if date_from == date_to else f"{format_date(date_from)}_{format_date(date_to)}"
    if len(day_list) > 1 and (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1 != len(day_list):
        name += f"-{len(day_list)}-days"       # выбраны не все дни между крайними
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="crews-by-days-{name}.xlsx"'},
    )


@router.get("/yandex-points")
async def download_yandex_points(
    search: str = "",
    category_id: list[int] = Query(default=[]),
    uncategorized: bool = False,
    status: list[str] = Query(default=[]),
    sort: str = "",
    certificate: str = "",
):
    """Комплексы для импорта в Конструктор карт Яндекса — те же фильтры, что и
    на «Все АПК» (поиск, категории, статусы, сертификат), но по ВСЕМУ справочнику,
    а не по загруженной странице. Без фильтров — все комплексы."""
    items, _ = await points_repository.search_points(
        search, category_id, None, 0, uncategorized, status, sort, certificate
    )
    colors = {s["name"]: s["color"] for s in await point_statuses_repository.list_statuses()}
    content, exported, skipped = excel_service.build_yandex_points_export(items, colors)
    if not exported:
        raise HTTPException(
            400,
            "Нет комплексов с координатами по текущим фильтрам"
            + (f" (без координат: {skipped})" if skipped else ""),
        )
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="yandex-points-{format_date(date.today().isoformat())}.xlsx"',
            "X-Points-Exported": str(exported),
            "X-Points-Skipped": str(skipped),
        },
    )


@router.get("/sim-ip-export")
async def export_sim_ip(
    scope: str = "points",
    category_id: list[int] = Query(default=[]),
    uncategorized: bool = False,
    status_: list[str] = Query(default=[], alias="status"),
    user: UserOut = Depends(require_password_ok),
):
    """SIM-карты с IP в Excel по фильтрам: scope=points — SIM комплексов (фильтры: категории, «без категории», статусы);
    scope=free — SIM из реестра SIM↔IP, которые нигде не числятся; scope=registry — весь реестр с пометкой, где числится.
    Тем, у кого нет права «Видеть IP-адрес и порт», выгрузка закрыта."""
    if not P.network_ok(user.permissions):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: видеть IP-адрес и порт")
    try:
        headers, rows, conditions = await sim_export.collect(scope, category_id, uncategorized, status_)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    content = sim_export.build_workbook(headers, rows, conditions)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="sim-ip-{scope}-{format_date(date.today().isoformat())}.xlsx"', "X-Rows": str(len(rows))},
    )


@router.get("/sim-template")
async def download_sim_template():
    """Единый шаблон SIM/IP: номер SIM (ICC), IP и необязательный комплекс."""
    return StreamingResponse(
        sim_import.build_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sim-ip-template.xlsx"'},
    )


@router.post("/sim-import")
async def import_sim(file: UploadFile):
    """Единый импорт SIM/IP (см. app/services/sim_import.py): без комплекса — сверка номера SIM (ICC) и IP с реестром,
    с комплексом — то же и привязка SIM/IP к его карточке."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, MAX_IMPORT_SIZE)
    parsed = sim_import.parse(content)
    if not parsed.rows:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(parsed.errors[:3]) or "В файле не найдено строк для импорта")
    return await sim_import.apply(parsed)


@router.get("/sim-ip-template")
async def download_sim_ip_template():
    out = excel_service.build_sim_ip_import_template()
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sim-ip-import-template.xlsx"'},
    )


@router.post("/sim-ip-import")
async def import_sim_ip(file: UploadFile):
    """Bulk-fills ip_address (and sim_number) on existing points by
    matching «Комплекс» — see excel_service.parse_sim_ip_import_file for
    the SIM-serial/IP-octet normalization this applies. Unlike the general
    points import, a point that doesn't already exist is reported as an
    error rather than created — this import only ever updates."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, MAX_IMPORT_SIZE)

    parsed = excel_service.parse_sim_ip_import_file(content)
    if not parsed.rows and not parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не найдено строк для импорта")

    existing = await points_repository.list_points()
    points_by_key = {_norm_point_id(p["id"]): p for p in existing if p.get("id")}

    updated = 0
    errors = list(parsed.errors)
    for row in parsed.rows:
        point = points_by_key.get(_norm_point_id(row.point_id))
        if point is None:
            errors.append(f"Строка {row.row_number}: точка «{row.point_id}» не найдена в справочнике")
            continue
        if row.sim_number is None and row.ip_address is None:
            continue
        merged = dict(point)
        if row.sim_number is not None:
            merged["sim_number"] = row.sim_number
        if row.ip_address is not None:
            merged["ip_address"] = row.ip_address
        await points_repository.update_point(point["id"], merged)
        updated += 1

    return {"updated": updated, "row_errors": errors}


@router.get("/sim-ip-registry-template")
async def download_sim_ip_registry_template():
    out = excel_service.build_sim_ip_registry_template()
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="sim-ip-registry-template.xlsx"'},
    )


@router.post("/sim-ip-registry-import")
async def import_sim_ip_registry(file: UploadFile):
    """Standalone SIM↔IP реестр — NOT tied to a «Комплекс» column, unlike
    /sim-ip-import above. Upserts the registry, then reconciles points two
    ways (see points_repository.sync_ip_from_sim_registry): a point keeping
    its SIM gets the SIM's new IP, and — since IP is the stable per-
    connection identity here, not the SIM (a SIM gets replaced when it
    expires, the replacement inherits the same IP) — a point keeping its
    IP gets the IP's new SIM. Re-uploading the same file later with
    refreshed pairs is the normal way to update it."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, MAX_IMPORT_SIZE)

    parsed = excel_service.parse_sim_ip_registry_file(content)
    if not parsed.rows and not parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не найдено строк для импорта")

    pairs = [(row.sim_number, row.ip_address) for row in parsed.rows]
    imported = await sim_ip_registry_repository.upsert_many(pairs)
    points_updated = await points_repository.sync_ip_from_sim_registry(pairs)

    return {"imported": imported, "points_updated": points_updated, "row_errors": list(parsed.errors)}


@router.post("/import")
async def import_points(file: UploadFile):
    """Import / update the repair-points directory from an .xlsx file.

    Task-to-team-and-date assignments are not part of this flow — those
    are still created via the KML "Конструктор карт" import (see routes_kml.py),
    matching the original app's data sources.
    """
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Загрузите файл в формате .xlsx")
    content = await _read_capped(file, MAX_IMPORT_SIZE)

    parsed = excel_service.parse_import_file(content)
    if not parsed.rows and not parsed.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не найдено строк для импорта")

    summary = await _apply_import(parsed.rows)
    summary["row_errors"] = parsed.errors + summary["row_errors"]
    return summary


def _norm_point_id(value: str) -> str:
    """Normalizes a point id for duplicate detection: 'T-100', ' t-100 '
    and 'T-100 ' must all resolve to the same existing point, not spawn
    near-duplicate rows in the directory."""
    return value.strip().casefold()


# «Не известно» в столбце «Статус» — явный сброс статуса; «Частично на складе» —
# не статус, а подпись комплекса «На складе» (см. app/core/warehouse.py).
_CLEAR_STATUS_WORDS = {"не известно", "неизвестно"}


def _resolve_status(raw: str, names: dict[str, str]) -> tuple[str | None, str | None]:
    """(статус для записи, текст ошибки). None вместо статуса — «не менять»."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    key = " ".join(raw.split()).casefold()
    if key in _CLEAR_STATUS_WORDS:
        return "", None
    if key == WAREHOUSE_PARTIAL_LABEL.casefold():
        key = WAREHOUSE_STATUS.casefold()
    if key in names:
        return names[key], None
    return None, f"статус «{raw}» не найден в справочнике статусов — статус не изменён, остальные данные строки загружены"


async def _apply_import(rows: list[excel_service.ImportRow]) -> dict:
    """Points are a per-row resource now (points_repository), so this no
    longer needs the whole-document CAS retry loop the calendar's tasks
    still use — each row is its own create/update."""
    existing = await points_repository.list_points()
    points_by_key = {_norm_point_id(p["id"]): p for p in existing if p.get("id")}
    # Номер из файла узнаётся и без учёта регистра, пробелов и знаков, и с хвостом «+1»/«+2» — чтобы не плодить дубли комплексов.
    index = PointIndex(existing)
    status_names = {s["name"].casefold(): s["name"] for s in await point_statuses_repository.list_statuses()}

    added = updated = 0
    errors: list[str] = []
    for row in rows:
        key = _norm_point_id(row.id)
        point = points_by_key.get(key)
        if point is None:
            point = index.find_id(row.id)
            if point is not None:
                key = _norm_point_id(point["id"])
        new_status, status_error = _resolve_status(row.status, status_names)
        if status_error:
            errors.append(f"Строка {row.row_number}: {status_error}")
        if new_status == "Работает":
            # «Работает» из таблицы без SIM или IP ничем не подтверждено — статус «Не известно» (пустой); SIM и IP — из строки, а для имеющегося комплекса — прежние, если в строке пусто.
            sim_now = (row.sim_number or (point or {}).get("sim_number") or "").strip()
            ip_now = (row.ip_address or (point or {}).get("ip_address") or "").strip()
            if not (sim_now and ip_now):
                new_status = ""
        if point is None:
            created = await points_repository.create_point(
                {
                    "id": row.id, "address": row.address, "description": row.description,
                    "lat": row.lat, "lon": row.lon,
                    "ip_address": row.ip_address, "port": row.port, "sim_number": row.sim_number,
                    "inventory_number": row.inventory_number,
                    "category_name": row.category,
                    "verification_valid_until": row.verification_valid_until,
                    "login": row.login,
                    "password": row.password,
                    "power_source": row.power_source,
                }
            )
            if new_status:
                # Через set_status, а не полем при создании: тот же след в
                # истории статусов и те же складские побочные эффекты, что у
                # любой другой смены статуса.
                await points_repository.set_status(created["id"], new_status, source="manual")
                created = {**created, "status": new_status}
            points_by_key[key] = created
            index.add(created)
            added += 1
        else:
            merged = dict(point)
            if row.address:
                merged["address"] = row.address
            if row.description:
                merged["description"] = row.description
            if row.lat:
                merged["lat"] = row.lat
            if row.lon:
                merged["lon"] = row.lon
            if row.ip_address:
                merged["ip_address"] = row.ip_address
            if row.port:
                merged["port"] = row.port
            if row.sim_number:
                merged["sim_number"] = row.sim_number
            if row.inventory_number:
                merged["inventory_number"] = row.inventory_number
            if row.category:
                # A category name from the import isn't necessarily new — but
                # if it is, it must get a fresh default colour rather than
                # inheriting whatever colour this point's OLD category had.
                merged["category_name"] = row.category
                merged["category_color"] = ""
            if row.verification_valid_until:
                merged["verification_valid_until"] = row.verification_valid_until
            if row.login:
                merged["login"] = row.login
            if row.password:
                merged["password"] = row.password
            if row.power_source:
                merged["power_source"] = row.power_source
            if new_status is not None:
                merged["status"] = new_status
            await points_repository.update_point(point["id"], merged)
            points_by_key[key] = merged
            updated += 1
    return {"added": added, "updated": updated, "row_errors": errors}
