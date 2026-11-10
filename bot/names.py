"""Сравнение названий «без разбора» для поиска в боте (то же правило, что app/core/names.py на сервере; бот — отдельный процесс и приложение не импортирует):
регистр, ё/е, пробелы, дефисы и прочие знаки не важны; латинские буквы-двойники русских («TO-5» и «ТО-5») считаются одинаковыми."""
from __future__ import annotations

import re
import unicodedata

_LOOKALIKE = str.maketrans("abcehkmoptxy", "авсенкмортху")
_ALNUM_RE = re.compile(r"[^\W_]+")


def fold_name(text: object) -> str:
    return unicodedata.normalize("NFC", str(text or "")).casefold().replace("ё", "е").translate(_LOOKALIKE)


def compact_name(text: object) -> str:
    return "".join(_ALNUM_RE.findall(fold_name(text)))


def matches_query(query: str, *fields: object) -> bool:
    """Подходит ли запись под запрос монтёра: запрос как есть входит в одно из полей (без регистра), либо ВСЕ слова запроса «без разбора» входят в поля вместе
    («ПУЛЬСАР 1025», «arena0901205», «то 5» найдут «ТО-5» и «ARENA-0901205»). Пустой запрос подходит всему."""
    q = (query or "").strip().lower()
    if not q:
        return True
    texts = [str(f or "") for f in fields]
    if any(q in t.lower() for t in texts):
        return True
    hay = compact_name(" ".join(texts))
    words = [compact_name(w) for w in q.split()]
    words = [w for w in words if w]
    if not words:
        return False
    joined = compact_name(q)
    return (joined in hay) or all(w in hay for w in words)
