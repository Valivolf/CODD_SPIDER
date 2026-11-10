"""Visit reports — "что было сделано" per задание, see
task_visit_reports_repository.py. The point a report applies to is looked
up server-side from the task row (not trusted from the client), since a
report should always describe the task's actual point."""
from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.dates import format_date
from app.core.deps import require_password_ok
from app.core.work_types import combined_visit_type, is_aux_type, is_repair_type, work_type_names
from app.db.database import get_connection
from app.models.task_visit_reports import TaskVisitReportIn, TaskVisitReportOut
from app.models.user import UserOut
from app.core.power_source import normalize_power_source
from app.repositories import (
    crew_repository, people_repository, points_repository, task_visit_reports_repository, work_types_repository,
)
from app.services import equipment_service, excel_service

router = APIRouter(
    prefix="/api/task-visit-reports", tags=["task-visit-reports"], dependencies=[Depends(require_password_ok)]
)


def _resolve_visit_type(base: str, with_repair: bool) -> str:
    """Тип из справочника «Типы выездов» (+ «+ Ремонт» для не связанных с ремонтом); вспомогательная точка — не работа."""
    base = (base or "").strip()
    if not base or is_aux_type(base) or base not in work_type_names():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Такого типа выезда нет в справочнике")
    return combined_visit_type(base) if with_repair else base


def _utc_iso(value: str) -> str:
    """Время отчёта из формы диспетчера → UTC ISO. Без часового пояса считаем, что это уже UTC (клиент присылает toISOString)."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверное время отчёта")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


async def _get_task_point_and_type(task_id: str) -> tuple[str, str] | None:
    async with get_connection() as conn:
        cur = await conn.execute("select point, type from tasks where id = ?", (task_id,))
        row = await cur.fetchone()
        return (row[0], row[1]) if row else None


@router.get("/export")
async def export_visit_reports(date_from: str, date_to: str, fields: list[str] = Query(default=[])):
    """История ремонта/выездов as .xlsx — registered before /{task_id}
    below, otherwise FastAPI would match "export" as a task_id (same
    single-segment path shape, and routes match in registration order)."""
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный формат даты")
    known_keys = {k for k, _ in task_visit_reports_repository.EXPORT_FIELDS}
    selected = set(fields) & known_keys
    chosen_fields = [(k, label) for k, label in task_visit_reports_repository.EXPORT_FIELDS if not fields or k in selected]
    if not chosen_fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите хотя бы одно поле")

    rows = await task_visit_reports_repository.list_for_export(date_from, date_to)
    out = excel_service.build_visit_reports_export(rows, chosen_fields)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="visit-reports-{format_date(date_from)}_{format_date(date_to)}.xlsx"'},
    )


@router.get("/archive")
async def visit_reports_archive(
    q: str = "", date_from: str = "", date_to: str = "", source: str = "", status_filter: str = Query(default="", alias="status"),
    visit_type: str = "", limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0),
):
    """Поиск по всей истории выездов, в том числе старым импортированным отчётам."""
    for value in (date_from, date_to):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неверный формат даты")
    if source not in ("", "imported", "forms", "bot", "system"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный источник отчёта")
    return await task_visit_reports_repository.search_archive(
        q=q, date_from=date_from, date_to=date_to, source=source, status=status_filter,
        visit_type=visit_type, limit=limit, offset=offset,
    )


@router.get("/point-equipment/{point_id}")
async def point_equipment(point_id: str):
    """Что числится установленным на комплексе (для выбора «весь комплекс / отдельные элементы» при демонтаже в форме отчёта)."""
    items = await equipment_service.point_equipment_list(point_id)
    return [
        {"id": i["id"], "serial_number": i["serial_number"], "category_name": i["category_name"], "model_name": i["model_name"],
         "category_kind": i.get("category_kind") or "", "label": equipment_service.item_label(i)}
        for i in items
    ]


@router.get("/{task_id}/context")
async def report_context(task_id: str):
    """Всё, что нужно форме «Отчёт о выезде», когда она открыта не из календаря (например, из «Мониторинга»): само задание,
    комплекс (адрес, питание), бригада и её состав, активные сотрудники и готовый отчёт, если он уже есть."""
    async with get_connection() as conn:
        cur = await conn.execute("select id, date, team, point, work, address, type from tasks where id = ?", (task_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено")
    task = dict(zip(["id", "date", "team", "point", "work", "address", "type"], row))
    point = await points_repository.get_point(task["point"]) if task["point"] else None
    crew = await crew_repository.get_crew(task["team"]) if task["team"] else None
    people = await people_repository.list_people(active_only=True)
    return {
        "task": {**task, "is_aux": is_aux_type(task["type"]), "crew_name": (crew or {}).get("name") or ""},
        "point": {
            "id": (point or {}).get("id") or task["point"], "address": (point or {}).get("address") or task["address"] or "",
            "power_source": (point or {}).get("power_source") or "", "status": (point or {}).get("status") or "",
            "sim_number": (point or {}).get("sim_number") or "",
        },
        "crew_member_ids": [m["id"] for m in ((crew or {}).get("members") or [])],
        "people": [{"id": p["id"], "full_name": p["full_name"], "role_type": p.get("role_type")} for p in people],
        "report": await task_visit_reports_repository.get_for_task(task_id),
    }


@router.get("/{task_id}", response_model=TaskVisitReportOut | None)
async def get_report(task_id: str):
    return await task_visit_reports_repository.get_for_task(task_id)


@router.put("/{task_id}", response_model=TaskVisitReportOut)
async def put_report(task_id: str, payload: TaskVisitReportIn, user: UserOut = Depends(require_password_ok)):
    found = await _get_task_point_and_type(task_id)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задание не найдено")
    point_id, visit_type = found
    if is_aux_type(visit_type):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Вспомогательная точка — метка на карте, отчёт по ней не нужен")
    if not point_id.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указана точка")
    if not visit_type.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У задания не указан тип работы")
    # Отчёт, который монтёр в боте перевёл в режим ремонта («Инвентаризация +
    # Ремонт»), при правке из календаря не должен терять это сочетание.
    existing = await task_visit_reports_repository.get_for_task(task_id)
    task_type = visit_type
    if payload.visit_type is not None and payload.visit_type.strip():
        # Итоговый тип выезда выбран явно (форма отчёта): записываем именно его, а статусы проверяем по нему же.
        task_type = payload.visit_type.strip()
        visit_type = _resolve_visit_type(task_type, bool(payload.with_repair))
    elif existing and existing["visit_type"] == combined_visit_type(visit_type):
        visit_type = existing["visit_type"]

    data = payload.model_dump()
    outcome = (payload.outcome or "").strip()
    if outcome:
        # «Статус выезда» бывает только у не связанного с ремонтом типа и только
        # из списка, настроенного для него («Настройки → Типы выездов»); состояние
        # комплекса он не меняет, поэтому result_status у такого отчёта пуст.
        if is_repair_type(visit_type):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "У типов, связанных с ремонтом, статус выбирается из стандартных")
        # Статус, который уже записан в отчёте (например, при загрузке из таблицы Яндекс Форм), остаётся допустимым при правке.
        if outcome not in await work_types_repository.outcomes_for(task_type) and not (existing and existing.get("outcome") == outcome):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Такого статуса выезда нет у этого типа работ")
        data["result_status"] = ""
    elif not payload.result_status:
        # Отчёт, загруженный из таблицы Яндекс Форм, может быть без статуса — его правку (текст, SIM, оборудование)
        # не блокируем; новый отчёт без статуса по-прежнему нельзя.
        if not (existing and not existing.get("result_status") and not existing.get("outcome")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите статус")
    # Выбран обычный статус — прежний «статус выезда» (если был) сбрасывается.
    data["outcome"] = outcome

    # Автор отчёта — человек из «Персонала», проводивший работы. Диспетчер, пишущий отчёт с нуля, обязан его выбрать;
    # при правке готового отчёта автор не меняется (если только диспетчер явно не выбрал другого).
    author = None
    if payload.author_person_id:
        author = await people_repository.get_person(payload.author_person_id)
        if not author:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Выбранный исполнитель не найден")
    if not existing and not author:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите, кто проводил работы на комплексе")

    # Демонтаж: весь комплекс или отдельные элементы. Оборудование снимается на исполнителя (числится на нём как снятое,
    # ждёт приёмки на склад); что снято — попадает в текст отчёта.
    dem = payload.demontage
    if dem is not None and existing and any(
        mark in (existing.get("comment") or "") for mark in ("Демонтирован весь комплекс", "Демонтированы отдельные элементы")
    ):
        dem = None      # по этому отчёту демонтаж уже оформлен — оборудование повторно не списываем и перечень не дублируем
    if dem is not None:
        holder_id = (author or {}).get("id") or (existing or {}).get("author_person_id")
        if not holder_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите, кто проводил работы — снятое оборудование запишется на него")
        mode = "parts" if dem.mode == "parts" else "whole"
        items = await equipment_service.point_equipment_list(point_id)
        wanted = set(dem.ids)
        taken = items if mode == "whole" else [i for i in items if i["id"] in wanted]
        left = [] if mode == "whole" else [i for i in items if i["id"] not in wanted]
        labels, summary = equipment_service.describe_demontage(mode, taken, left, dem.other)
        try:
            await equipment_service.demontage_remove(
                point_id, equipment_ids=None if mode == "whole" else sorted(wanted), person_id=holder_id,
                performed_by=user.username, reason="Демонтаж (отчёт внесён диспетчером)",
            )
        except equipment_service.EquipmentError as exc:
            raise HTTPException(exc.status_code, exc.detail)
        comment = (data.get("comment") or "").strip()
        if summary not in comment:
            data["comment"] = (comment + "\n" if comment else "") + summary
        removed_text = "; ".join(p for p in (labels, (dem.other or "").strip()) if p)
        if removed_text and not data.get("equipment_removed"):
            data["equipment_removed"] = removed_text

    if payload.power_source is not None:
        power = normalize_power_source(payload.power_source)
        if power is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Питание комплекса: «СЭС» или «Электросеть»")
        await points_repository.set_power_source(point_id, power)
    report_time = _utc_iso(payload.created_at) if payload.created_at else None
    return await task_visit_reports_repository.upsert(
        task_id, point_id, visit_type, data, author["full_name"] if author else user.username,
        author_person_id=author["id"] if author else None, edited_by=user.username, created_at=report_time,
    )


class VisitTypeIn(BaseModel):
    visit_type: str
    # «<тип> + Ремонт»: визит начался как другой тип, но на комплексе ещё и чинили.
    with_repair: bool = False


@router.put("/{task_id}/visit-type", response_model=TaskVisitReportOut)
async def put_visit_type(task_id: str, payload: VisitTypeIn):
    """Поменять только итоговый тип выезда в готовом отчёте (случайно включённый монтёром режим ремонта и т. п.).
    Работает и для записей, загруженных из Excel; статус, комментарий и состояние комплекса не меняются."""
    new_type = _resolve_visit_type(payload.visit_type, payload.with_repair)
    report = await task_visit_reports_repository.set_visit_type(task_id, new_type)
    if report is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Отчёта по этому заданию нет")
    return report


@router.get("/point-state/{point_id}")
async def point_state(point_id: str):
    """Для верха журнала комплекса: сегодняшний статус и, если комплекс не работает, последняя известная причина и «что нужно
    для восстановления» — из самого свежего отчёта, где они записаны (если после него не было отчёта «Работает»)."""
    point = await points_repository.get_point(point_id)
    last = None
    for r in await task_visit_reports_repository.list_for_point(point_id, 300):      # новые сверху
        if r.get("result_status") == "Работает":
            break                      # после этого комплекс уже починили (причина в таком отчёте — о том, что чинили) — прежняя причина устарела
        if (r.get("fault") or "").strip() or (r.get("needs") or "").strip():
            last = {"fault": r.get("fault") or "", "needs": r.get("needs") or "", "created_at": r["created_at"],
                    "created_by": r.get("created_by") or "", "visit_type": r["visit_type"], "imported": bool(r.get("imported"))}
            break
    return {"status": (point or {}).get("status") or "", "down_since": await points_repository.get_port_down_since(point_id) if point else None,
            "last_fault": last}


@router.get("/by-point/{point_id}", response_model=list[TaskVisitReportOut])
async def list_reports_for_point(point_id: str, limit: int = 50):
    return await task_visit_reports_repository.list_for_point(point_id, max(1, min(limit, 1000)))
