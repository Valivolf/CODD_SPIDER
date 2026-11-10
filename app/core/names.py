"""Сравнение названий «без разбора»: регистр, ё/е, пробелы и знаки, русские и латинские буквы-двойники.

Нужно везде, где название приходит от человека или из чужой программы и должно совпасть с уже заведённым: номера комплексов
при импорте из Excel и из Конструктора карт, имена общих папок на сетевом диске, папки комплексов с фото."""
from __future__ import annotations

import re
import unicodedata

# Латинские буквы, неотличимые от русских на глаз: «TO-5» и «ТО-5» — один и тот же комплекс.
_LOOKALIKE = str.maketrans("abcehkmoptxy", "авсенкмортху")
_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+")
_ALNUM_RE = re.compile(r"[^\W_]+")

# Хвост вида «+1», «+ 2», «(2)» — так помечают второй, третий… комплекс с тем же названием (дубль на карте или в файле).
_DUP_SUFFIX_RE = re.compile(r"^(?P<base>.*?\S)\s*(?:\+\s*(?P<n1>\d{1,2})|\(\s*(?P<n2>\d{1,2})\s*\))\s*$")


# «АРЕНА-СМ-1111091», «ARENA-SM-1111091» и «ARENA-1111091» — один и тот же комплекс: у «Арена-СМ» (новое поколение) номер тот же, что у «Арена», добавлено только «СМ».
# После перевода двойников: латинское «arena» стало «аrеnа» (r и n двойников не имеют), «cm» → «см», «sm» → «sм».
_ARENA_LATIN = "аrеnа"
_ARENA_SM_RE = re.compile(r"(арена)[\W_]*[сs]м(?=[\W_\d]|$)")


def fold_name(text: object) -> str:
    """Без регистра, ё = е, латинские двойники русских букв приведены к русским; «ARENA» = «АРЕНА», а «-СМ» у «Арена-СМ» не считается."""
    s = unicodedata.normalize("NFC", str(text or "")).casefold().replace("ё", "е").translate(_LOOKALIKE).replace(_ARENA_LATIN, "арена")
    return _ARENA_SM_RE.sub(r"\1", s)


def compact_name(text: object) -> str:
    """То же, но без пробелов, дефисов, подчёркиваний и прочих знаков: «Отдел_ТО» = «отдел то» = «OTDEL-TO»."""
    return "".join(_ALNUM_RE.findall(fold_name(text)))


def tokens(text: object) -> list[str]:
    """Слова и числа в названии (буквы и цифры раздельно): «АПК-123 (ул. Ленина)» → апк, 123, ул, ленина."""
    return _TOKEN_RE.findall(fold_name(text))


def strip_dup_suffix(text: object) -> tuple[str, int | None]:
    """«ARENA-1 +2» → («ARENA-1», 2); «ARENA-1 (3)» → («ARENA-1», 3); без такого хвоста — (название, None)."""
    s = str(text or "").strip()
    m = _DUP_SUFFIX_RE.match(s)
    if not m:
        return s, None
    return m.group("base").strip(), int(m.group("n1") or m.group("n2"))
