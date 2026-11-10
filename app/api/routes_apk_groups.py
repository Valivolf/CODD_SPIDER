"""Подгруппы АПК («Все АПК» → подгруппы): состав, описание и «что нужно на выезде» по каждому комплексу подгруппы, добавление вручную и из файла Яндекс.Карт.
Список комплексов подгруппы (с картой, фильтрами, пингом, журналом и фото) отдаёт обычный GET /api/points?group_id=…, здесь — управление подгруппами.

Читать могут администратор, диспетчер и «только просмотр»; менять — администратор и диспетчер."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.api.assign_common import check_work_type
from app.core.deps import require_password_ok
from app.models.user import UserOut
from app.core import permissions as P
from app.repositories import apk_groups_repository as groups, points_repository
from app.services import apk_group_auto, apk_group_import as importer
from app.db.database import get_connection

router = APIRouter(prefix="/api/apk-groups", tags=["apk-groups"], dependencies=[Depends(require_password_ok)])

_MAX_FILE = 8 * 1024 * 1024


def _reader(user: UserOut) -> None:
    if not P.has(user.permissions, "points.view"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: видеть раздел «Все АПК»")


def _writer(user: UserOut) -> None:
    if not P.has(user.permissions, "points.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нет права: изменять комплексы и подгруппы")


async def _group_or_404(group_id: int) -> dict:
    group = await groups.get(group_id)
    if not group:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Подгруппа не найдена")
    return group


async def _editable_group(group_id: int) -> dict:
    """Подгруппа, которую можно менять вручную. «Требуют повторного выезда» ведёт сама программа (app/services/apk_group_auto.py) — ни состав, ни название, ни тексты в ней не правятся."""
    group = await _group_or_404(group_id)
    if group.get("kind"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Подгруппа «{group['name']}» ведётся автоматически: комплексы попадают в неё и уходят сами. "
                                                         "Чтобы изменить запись, исправьте сам комплекс — статус, отчёт о выезде или поля «Неисправность» в карточке.")
    return group


def _work_type(value: str | None) -> str | None:
    """None — не менять; '' — «тип не выбран»; иначе должен быть известный тип работы."""
    if value is None:
        return None
    value = value.strip()
    if value:
        check_work_type(value)
    return value


async def _existing(ids: list[str]) -> dict[str, str]:
    """Номера из списка, которые есть в справочнике: без учёта регистра и пробелов по краям → как записано в справочнике."""
    out: dict[str, str] = {}
    wanted = [i.strip() for i in ids if i and i.strip()]
    async with get_connection() as conn:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            cur = await conn.execute(f"select id from points where pylower(trim(id)) in ({','.join('pylower(?)' for _ in chunk)})", chunk)
            for (pid,) in await cur.fetchall():
                out[pid.strip().casefold()] = pid
    return out


class GroupIn(BaseModel):
    name: str
    description: str = ""
    auto_remove: bool = False          # тумблер «убирать комплексы после выезда»


class GroupPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    auto_remove: bool | None = None


@router.get("")
async def list_groups(user: UserOut = Depends(require_password_ok)):
    _reader(user)
    await apk_group_auto.sync_if_stale()          # «Требуют повторного выезда»: состав свежий, когда открывают «Все АПК»
    return await groups.list_groups()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(payload: GroupIn, user: UserOut = Depends(require_password_ok)):
    _writer(user)
    name = groups.clean_name(payload.name)
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Впишите название подгруппы")
    if await groups.find_by_name(name):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Подгруппа «{name}» уже есть")
    return await groups.create(name, payload.description, user.username, payload.auto_remove)


@router.put("/{group_id}")
async def update_group(group_id: int, payload: GroupPatch, user: UserOut = Depends(require_password_ok)):
    _writer(user)
    await _editable_group(group_id)
    if payload.name is not None:
        name = groups.clean_name(payload.name)
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Впишите название подгруппы")
        other = await groups.find_by_name(name)
        if other and other["id"] != group_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Подгруппа «{name}» уже есть")
    await groups.update(group_id, name=payload.name, description=payload.description, auto_remove=payload.auto_remove)
    result = await groups.get(group_id)
    if payload.auto_remove:           # тумблер включили — комплексы, по которым выезд уже был после попадания в подгруппу, уходят сразу
        result["removed_visited"] = await apk_group_auto.prune_visited()
    return result


@router.delete("/{group_id}")
async def delete_group(group_id: int, user: UserOut = Depends(require_password_ok)):
    _writer(user)
    await _editable_group(group_id)
    await groups.delete(group_id)
    return {"ok": True}


class AddPointsIn(BaseModel):
    point_ids: list[str] = Field(default_factory=list)
    work_type: str | None = None
    note: str | None = None
    need: str | None = None


@router.post("/{group_id}/points")
async def add_points(group_id: int, payload: AddPointsIn, user: UserOut = Depends(require_password_ok)):
    """Добавить комплексы (точные номера из справочника — например, отмеченные в списке или на карте) в подгруппу; сразу можно задать тип работы,
    описание и «что нужно на выезде» (общие для добавляемых). Уже стоящие в подгруппе не дублируются (поля, если указаны, обновляются)."""
    _writer(user)
    await _editable_group(group_id)
    if not payload.point_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не выбрано ни одного комплекса")
    work_type = _work_type(payload.work_type)
    found = await _existing(payload.point_ids)
    ids, unknown = [], []
    for raw in payload.point_ids:
        pid = found.get(raw.strip().casefold())
        (ids if pid else unknown).append(pid or raw.strip())
    added, already = await groups.add_points(group_id, ids, work_type=work_type, note=payload.note, need=payload.need)
    return {"added": added, "already": already, "unknown": unknown, "total": (await groups.get(group_id))["count"]}


class PasteIn(BaseModel):
    text: str
    work_type: str | None = None
    note: str | None = None
    need: str | None = None


@router.post("/{group_id}/paste")
async def add_pasted(group_id: int, payload: PasteIn, user: UserOut = Depends(require_password_ok)):
    """Вручную списком: номера комплексов через пробел, запятую или с новой строки. Номера сопоставляются с справочником так же, как при импорте файла."""
    _writer(user)
    await _editable_group(group_id)
    labels = importer.split_tokens(payload.text)
    if not labels:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Впишите номера комплексов")
    return await _add_labels(group_id, [importer.Mark(t) for t in labels], _work_type(payload.work_type), payload.note, payload.need, create_missing=False, use_description=False)


async def _add_labels(group_id: int, marks: list[importer.Mark], work_type: str | None, note: str | None, need: str | None, *, create_missing: bool,
                      use_description: bool) -> dict:
    points = await points_repository.list_points()
    matcher = importer.Matcher(points)
    matched: dict[str, importer.Mark] = {}
    unknown: list[str] = []
    ambiguous: list[dict] = []
    created: list[str] = []
    new_by_key: dict[str, str] = {}
    for mark in marks:
        pid, cands = matcher.match(mark.label)
        if pid is None and cands:
            ambiguous.append({"label": mark.label, "candidates": cands})
            continue
        if pid is None and create_missing and mark.has_coords():
            key = importer.new_key(mark.label)
            pid = new_by_key.get(key)
            if pid is None:
                name = matcher.new_name(mark.label)
                try:
                    point = await points_repository.create_point({"id": name, "description": mark.description, "lat": mark.lat, "lon": mark.lon})
                except Exception:  # noqa: BLE001 — номер занят/не подошёл: метку просто не добавляем
                    unknown.append(mark.label)
                    continue
                matcher.register(point)
                pid = point["id"]
                new_by_key[key] = pid
                created.append(pid)
        if pid is None:
            unknown.append(mark.label)
            continue
        matched.setdefault(pid, mark)
    added = already = 0
    # у каждой метки своё описание из файла (если попросили взять его в «описание в подгруппе»), поэтому по одному
    for pid, mark in matched.items():
        row_note = note if (note or not use_description) else (mark.description or None)
        a, b = await groups.add_points(group_id, [pid], work_type=work_type, note=row_note, need=need)
        added += a
        already += b
    return {"added": added, "already": already, "unknown": unknown, "ambiguous": ambiguous, "created": created, "marks": len(marks),
            "total": (await groups.get(group_id))["count"]}


@router.post("/{group_id}/import")
async def import_file(
    group_id: int, file: UploadFile = File(...), work_type: str = Form(""), note: str = Form(""), need: str = Form(""),
    create_missing: bool = Form(True), use_description: bool = Form(False), user: UserOut = Depends(require_password_ok),
):
    """Добавить в подгруппу комплексы из файла Яндекс.Карт (KML/KMZ/GeoJSON/GPX/Excel/CSV/текст). Тип работы, описание и «что нужно на выезде» можно задать
    сразу для всех добавляемых. Комплексов, которых нет в справочнике, но у метки есть координаты, — заводит (как импорт KML в бригаду)."""
    _writer(user)
    await _editable_group(group_id)
    data = await file.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Файл слишком большой (максимум 8 МБ)")
    try:
        marks = importer.parse_file(file.filename or "", data)
    except importer.ImportError_ as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    if not marks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "В файле не нашлось ни одной подписанной метки")
    return await _add_labels(group_id, marks, _work_type(work_type or None), note or None, need or None, create_missing=create_missing, use_description=use_description)


class MembersPatch(BaseModel):
    point_ids: list[str] = Field(default_factory=list)
    work_type: str | None = None
    note: str | None = None
    need: str | None = None


@router.post("/{group_id}/points/update")
async def update_members(group_id: int, payload: MembersPatch, user: UserOut = Depends(require_password_ok)):
    """Тип работы / описание / «что нужно» сразу у нескольких комплексов подгруппы (меняются только присланные поля)."""
    _writer(user)
    await _editable_group(group_id)
    changed = await groups.update_members(group_id, payload.point_ids, work_type=_work_type(payload.work_type), note=payload.note, need=payload.need)
    return {"changed": changed}


class MemberPatch(BaseModel):
    work_type: str | None = None
    note: str | None = None
    need: str | None = None


@router.put("/{group_id}/points/{point_id:path}")
async def update_member(group_id: int, point_id: str, payload: MemberPatch, user: UserOut = Depends(require_password_ok)):
    _writer(user)
    await _editable_group(group_id)
    changed = await groups.update_members(group_id, [point_id], work_type=_work_type(payload.work_type), note=payload.note, need=payload.need)
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Комплекса нет в этой подгруппе")
    return {"ok": True}


class RemoveIn(BaseModel):
    point_ids: list[str] = Field(default_factory=list)


@router.post("/{group_id}/points/remove")
async def remove_points(group_id: int, payload: RemoveIn, user: UserOut = Depends(require_password_ok)):
    _writer(user)
    await _editable_group(group_id)
    return {"removed": await groups.remove_points(group_id, payload.point_ids)}
