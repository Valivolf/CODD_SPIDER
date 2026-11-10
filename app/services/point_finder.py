"""Поиск комплекса по тому, как его записали в чужом файле: по НОМЕРУ (название отбрасывается — сравниваются цифры), а если номера
нет или он не нашёлся — по коду ICC (последние 7 цифр SIM), IP-адресу, инвентарному номеру и адресу.

Используется загрузкой отчётов из Яндекс Форм (forms_import.py) и «Журнала выездов» (visit_history_import.py). Порядок строгий, чтобы
не склеить разные комплексы: совпал номер целиком → совпали цифры (если у нескольких комплексов цифры одинаковы, помогают буквы) →
ICC → IP → инвентарный номер → адрес. Неоднозначность (под запись подходят два разных комплекса) — не совпадение."""
from __future__ import annotations

import re
from typing import Any, Optional

from app.core.equipment import sim_key
from app.core.names import fold_name
from app.services.point_matching import PointIndex

_DIGITS_RE = re.compile(r"\d+")
_SIM_WORD_RE = re.compile(r"(?<![^\W\d_])(?:сим|sim|симка|сим-карта)(?![^\W\d_])", re.IGNORECASE)


def digits_key(text: Any) -> str:
    """Номер комплекса без названия: все цифры подряд («POST-1019771» → «1019771», «Пульсар-1025-26717» → «102526717»)."""
    return "".join(_DIGITS_RE.findall(str(text or "")))


def letters_key(text: Any) -> str:
    """Буквенная часть названия без слова «Сим» («Сим Пульсар-1025» → «пульсар») — для различения комплексов с одинаковыми цифрами."""
    cleaned = _SIM_WORD_RE.sub(" ", str(text or ""))
    return re.sub(r"[\W\d_]+", "", fold_name(cleaned))


# Несколько комплексов в одной ячейке («Пульсар-1025-26717, POST-1019771 и ARENA-1111040»): разделители — перенос строки, «;», «,», «/», «|», «и», «&», « + » (с пробелами:
# «AR-1 +1» — это один комплекс с меткой дубля); внутри куска названия, идущие подряд без разделителя («26717 26718», «Пульсар-1025-26717 Пульсар-1026-26718»), делятся по
# самим названиям: необязательные слова, затем цифры через дефис и необязательная метка «+N».
_HARD_SEP = re.compile(r"[\r\n;,/|]+|\s+и\s+|\s*&\s*|\s+\+\s+", re.IGNORECASE)
_DESIGNATION = re.compile(r"(?:[^\W\d_]+[\s\-]*)*\d+(?:-\d+)*(?:\s*\+\d+)?")


def split_designations(raw: Any) -> list[str]:
    """Названия комплексов, перечисленные в одной ячейке (одно название — список из одного элемента); порядок сохраняется, повторы убираются."""
    text = " ".join(str(raw or "").split())
    out: list[str] = []
    for piece in _HARD_SEP.split(text):
        piece = piece.strip(" -")
        if not piece:
            continue
        found = [m.group(0).strip(" -") for m in _DESIGNATION.finditer(piece)]
        out.extend(found if len(found) >= 2 else [piece])
    seen: set[str] = set()
    return [p for p in out if not (p.casefold() in seen or seen.add(p.casefold()))]


def norm_address(text: Any) -> str:
    """Адрес для сравнения: без регистра, знаков и пробелов («ул. Северная, д.461» = «ул Северная д 461»)."""
    return re.sub(r"[^0-9a-zа-я]+", "", str(text or "").casefold().replace("ё", "е"))


def _loose_id(text: Any) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").casefold().replace("ё", "е"))


class PointLookup:
    def __init__(self, points: list[dict[str, Any]]):
        self.points = {p["id"]: p for p in points}
        self.index = PointIndex(points)
        self.by_loose: dict[str, list[str]] = {}          # название как есть (без регистра и знаков) → комплексы
        for p in points:
            key = _loose_id(p["id"])
            if key:
                self.by_loose.setdefault(key, []).append(p["id"])
        self.by_digits: dict[str, list[str]] = {}
        self.by_digits0: dict[str, list[str]] = {}
        self.by_group: dict[str, list[str]] = {}          # группа цифр в названии («Пульсар-1025-26717» → «1025», «26717») → комплексы: так находится комплекс по части названия — номеру
        self.by_group0: dict[str, list[str]] = {}
        for p in points:
            d = digits_key(p["id"])
            if d:
                self.by_digits.setdefault(d, []).append(p["id"])
                self.by_digits0.setdefault(d.lstrip("0") or "0", []).append(p["id"])
            for g in _DIGITS_RE.findall(str(p["id"])):
                if len(g) >= 3:                           # группы из одной-двух цифр («-1», «+2») — шум, по ним не ищем
                    self.by_group.setdefault(g, []).append(p["id"])
                    self.by_group0.setdefault(g.lstrip("0") or "0", []).append(p["id"])

    def match(self, raw: str, inventory: str = "") -> tuple[Optional[str], str, list[str]]:
        """(id комплекса, 'ok' | 'none' | 'ambiguous', кандидаты)."""
        # Название записано в точности как у комплекса (в том числе «AR-1 +1», если такой комплекс заведён отдельно) — он и есть
        exact = self.by_loose.get(_loose_id(raw), [])
        if len(exact) == 1:
            return exact[0], "ok", []
        cleaned = " ".join(_SIM_WORD_RE.sub(" ", raw).split())
        for cand in (raw, cleaned):
            hit = self.index.find(cand)
            if hit:
                return hit["id"], "ok", []
        d = digits_key(raw)
        if d:
            for table, key in ((self.by_digits, d), (self.by_digits0, d.lstrip("0") or "0")):
                cands = list(dict.fromkeys(table.get(key, [])))
                if not cands:
                    continue
                if len(cands) == 1:
                    return cands[0], "ok", []
                letters = letters_key(raw)
                same = [c for c in cands if letters and letters_key(c) == letters]
                if len(same) == 1:
                    return same[0], "ok", []
                return None, "ambiguous", cands[:6]
        if inventory:
            hit = self.index.find(inventory)
            if hit:
                return hit["id"], "ok", []
        return None, "none", []


class PointFinder(PointLookup):
    """PointLookup + запасные признаки (ICC, IP, инвентарный номер, адрес) — для строк, где номера нет или он не найден."""

    def __init__(self, points: list[dict[str, Any]]):
        super().__init__(points)
        self.by_sim: dict[str, list[str]] = {}
        self.by_ip: dict[str, list[str]] = {}
        self.by_addr: dict[str, list[str]] = {}
        for p in points:
            sim = sim_key(p.get("sim_number"))
            if sim and sim.isdigit() and len(sim) >= 5:
                self.by_sim.setdefault(sim, []).append(p["id"])
            ip = str(p.get("ip_address") or "").strip()
            if ip:
                self.by_ip.setdefault(ip, []).append(p["id"])
            addr = norm_address(p.get("address"))
            if len(addr) >= 12:
                self.by_addr.setdefault(addr, []).append(p["id"])

    @staticmethod
    def _one(table: dict[str, list[str]], key: str) -> Optional[str]:
        hit = list(dict.fromkeys(table.get(key, [])))
        return hit[0] if len(hit) == 1 else None

    def find(self, number: str = "", *, icc: str = "", ip: str = "", inventory: str = "", address: str = "") -> tuple[Optional[str], str, list[str]]:
        """(id, как найден: 'number' | 'sim' | 'ip' | 'inventory' | 'address' | 'ambiguous' | 'none', кандидаты)."""
        cands: list[str] = []
        if number:
            pid, how, cands = self.match(number, "")
            if pid:
                return pid, "number", []
            if how == "ambiguous":
                return None, "ambiguous", cands
        icc_key = sim_key(icc) if icc else ""
        if icc_key and icc_key.isdigit() and len(icc_key) >= 5:
            pid = self._one(self.by_sim, icc_key)
            if pid:
                return pid, "sim", []
        if ip:
            pid = self._one(self.by_ip, ip.strip())
            if pid:
                return pid, "ip", []
        if inventory:
            hit = self.index.find(inventory)
            if hit:
                return hit["id"], "inventory", []
        addr = norm_address(address)
        if len(addr) >= 12:
            pid = self._one(self.by_addr, addr)
            if pid:
                return pid, "address", []
        return None, "none", []
