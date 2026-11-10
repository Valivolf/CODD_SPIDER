"""Единый импорт SIM/IP из Excel («Документы → SIM-карты → Загрузить таблицу»).

Раньше было два шаблона: «SIM/IP по комплексам» (Комплекс, SIM, IP) и «реестр SIM↔IP» (SIM, IP). Теперь шаблон один, а
столбец «Комплекс» необязателен:
  • комплекса в строке нет — программа сверяет пару «номер SIM (ICC) ↔ IP» с реестром (новая пара добавляется, изменившийся IP
    обновляется, совпавшая остаётся) и, как и раньше, подтягивает изменения комплексам, у которых стоит эта SIM или этот IP;
  • комплекс указан — то же самое, и дополнительно SIM и IP записываются в карточку этого комплекса (привязка).
Номер SIM можно вставлять целиком (ICCID): в учёте хранятся последние 7 цифр. IP можно с нулями в начале октетов.

Столбцы ищутся по заголовку (порядок любой); таблицы старых шаблонов тоже подходят."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from app.repositories import points_repository, sim_ip_registry_repository
from app.services.excel_service import HEADER_FILL, HEADER_FONT, _clean, _last7_digits, _normalize_ip, _zip_bomb_error
from app.services.point_matching import PointIndex

TEMPLATE_HEADERS = ["Номер SIM (ICC)", "IP-адрес", "Комплекс (необязательно)"]
MAX_ROWS = 50_000
_HEADER_SCAN_ROWS = 10


def build_template() -> io.BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "SIM и IP"
    ws.append(TEMPLATE_HEADERS)
    for col in range(1, len(TEMPLATE_HEADERS) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = [28, 20, 28][col - 1]
    ws.freeze_panes = "A2"
    ws.append(["8901234567123459876", "192.168.001.143", "T-100"])
    ws.append(["8901234567123450001", "192.168.001.144", ""])

    guide = wb.create_sheet("Инструкция")
    guide.column_dimensions["A"].width = 110
    lines = [
        "Как заполнять таблицу SIM/IP",
        "",
        "1. Столбцы лучше не переименовывать: программа находит их по названию («Номер SIM»/«ICC», «IP», «Комплекс»).",
        "2. Номер SIM (ICC) — можно вставлять полный номер как есть: в учёте хранятся последние 7 цифр.",
        "3. IP-адрес — можно с нулями в начале октетов (192.168.001.143): они уберутся сами.",
        "4. «Комплекс» заполнять НЕ обязательно.",
        "   • Комплекса нет — программа только сверяет номер SIM (ICC) и IP с реестром: новая пара добавляется, у изменившейся",
        "     обновляется IP, совпавшие остаются как есть. Комплексам, у которых стоит эта SIM (или этот IP), изменения подтянутся сами.",
        "   • Комплекс указан — то же самое, и SIM с IP ещё и привязываются к этому комплексу (записываются в его карточку).",
        "5. Пустые строки и строки-примеры (вторая и третья в шаблоне) при загрузке не мешают: замените их своими данными.",
    ]
    for i, line in enumerate(lines, start=1):
        guide.cell(row=i, column=1, value=line)
    guide.cell(row=1, column=1).font = Font(bold=True, size=13)
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


def _classify(header: str) -> Optional[str]:
    key = header.casefold().replace("ё", "е").strip()       # не fold_name: он превращает латинские буквы в русские («sim» → «siм»)
    if not key:
        return None
    if any(w in key for w in ("комплекс", "апк", "точк", "объект")):
        return "point"
    if "ip" in key or "ай пи" in key or "айпи" in key:
        return "ip"
    if any(w in key for w in ("sim", "сим", "icc", "серийн")):
        return "sim"
    return None


@dataclass
class Row:
    number: int
    sim: Optional[str]          # последние 7 цифр
    icc: str                    # как было в файле
    ip: Optional[str]
    point: str


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse(content: bytes) -> Parsed:
    result = Parsed()
    bomb = _zip_bomb_error(content)
    if bomb:
        result.errors.append(bomb)
        return result
    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Не удалось прочитать файл Excel: {exc}")
        return result
    ws = wb.worksheets[0]
    all_rows = list(ws.iter_rows(values_only=True, max_row=MAX_ROWS + _HEADER_SCAN_ROWS))
    if not all_rows:
        result.errors.append("Файл пуст")
        return result

    cols: dict[str, int] = {}
    start = 0
    for idx, raw in enumerate(all_rows[:_HEADER_SCAN_ROWS]):
        found: dict[str, int] = {}
        for ci, cell in enumerate(raw):
            kind = _classify(_clean(cell))
            if kind and kind not in found:
                found[kind] = ci
        if "sim" in found or ("ip" in found and len(found) >= 2):
            cols, start = found, idx + 1
            break
    if not cols:
        # Заголовков нет: два столбца — «SIM, IP», три — прежний шаблон «Комплекс, SIM, IP».
        width = max((len(r) for r in all_rows[:5]), default=0)
        cols = {"point": 0, "sim": 1, "ip": 2} if width >= 3 else {"sim": 0, "ip": 1}
        start = 0
        result.errors.append("Заголовки столбцов не найдены — взят порядок «SIM, IP» (или «Комплекс, SIM, IP»)")
    if "sim" not in cols and "point" not in cols:
        result.errors.append("Не найден столбец с номером SIM (ICC)")
        return result

    def cell(raw, key):
        i = cols.get(key)
        return raw[i] if i is not None and i < len(raw) else None

    for offset, raw in enumerate(all_rows[start:], start=start + 1):
        if all(c in (None, "") for c in raw):
            continue
        icc = _clean(cell(raw, "sim"))
        point = _clean(cell(raw, "point"))
        ip, ip_error = _normalize_ip(cell(raw, "ip"))
        if ip_error:
            result.errors.append(f"Строка {offset}: {ip_error}")
            continue
        sim = _last7_digits(icc) if icc else None
        if not sim and not ip and not point:
            continue
        if not sim and not point:
            result.errors.append(f"Строка {offset}: нет ни номера SIM, ни комплекса")
            continue
        result.rows.append(Row(number=offset, sim=sim, icc=icc, ip=ip, point=point))
    return result


async def apply(parsed: Parsed) -> dict[str, Any]:
    """Сверка с реестром и привязка к комплексам. Возвращает счётчики и список замечаний по строкам."""
    registry = {r["sim_number"]: r["ip_address"] for r in await sim_ip_registry_repository.list_all()}
    points = await points_repository.list_points()
    index = PointIndex(points)
    by_id = {p["id"]: p for p in points}

    stats = {"registry_new": 0, "registry_changed": 0, "registry_same": 0, "bound": 0, "bound_same": 0,
             "points_synced": 0, "no_point": 0, "skipped": 0}
    notes: list[str] = list(parsed.errors)
    pairs: list[tuple[str, str]] = []
    seen_sims: dict[str, str] = {}

    for row in parsed.rows:
        # --- реестр SIM↔IP
        if row.sim and row.ip:
            if row.sim in seen_sims and seen_sims[row.sim] != row.ip:
                notes.append(f"Строка {row.number}: SIM {row.sim} уже была в файле с другим IP ({seen_sims[row.sim]}) — берётся последний")
            seen_sims[row.sim] = row.ip
            old = registry.get(row.sim)
            if old is None:
                stats["registry_new"] += 1
            elif old != row.ip:
                stats["registry_changed"] += 1
            else:
                stats["registry_same"] += 1
            registry[row.sim] = row.ip
            pairs.append((row.sim, row.ip))
        # --- привязка к комплексу
        if not row.point:
            if row.sim and not row.ip and row.sim not in registry:
                stats["skipped"] += 1
                notes.append(f"Строка {row.number}: SIM {row.sim} без IP и без комплекса — сверять не с чем")
            continue
        point = index.find(row.point)
        if point is None:
            stats["no_point"] += 1
            notes.append(f"Строка {row.number}: комплекс «{row.point}» не найден в справочнике — SIM/IP к нему не привязаны")
            continue
        current = by_id.get(point["id"], point)
        new_sim = row.sim or current.get("sim_number") or ""
        new_ip = row.ip or (registry.get(new_sim) if row.sim else None) or current.get("ip_address") or ""
        if new_sim == (current.get("sim_number") or "") and new_ip == (current.get("ip_address") or ""):
            stats["bound_same"] += 1
            continue
        others = [p["id"] for p in points if row.sim and p.get("sim_number") == row.sim and p["id"] != point["id"]]
        if others:
            notes.append(f"Строка {row.number}: SIM {row.sim} числилась ещё и на комплексе «{', '.join(others[:3])}» — проверьте, не дубль ли это")
        merged = dict(current)
        merged["sim_number"], merged["ip_address"] = new_sim, new_ip
        try:
            await points_repository.update_point(point["id"], merged)
        except Exception as exc:  # noqa: BLE001 — сбой одной строки не должен останавливать остальные
            notes.append(f"Строка {row.number}: комплекс «{point['id']}» не обновлён: {exc}")
            continue
        by_id[point["id"]] = merged
        stats["bound"] += 1

    if pairs:
        await sim_ip_registry_repository.upsert_many(pairs)
        stats["points_synced"] = await points_repository.sync_ip_from_sim_registry(pairs)
    return {"rows": len(parsed.rows), **stats, "notes": notes[:200], "notes_total": len(notes)}
