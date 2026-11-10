"""Игры для перерывов (страница /games): каталог, правила подсчёта рекордов, доступ и таблица рекордов.

Игры написаны прямо в приложении (static/js/games_impl.js) и работают без интернета. Сервер только решает, КОМУ они включены (администратор — для каждого пользователя отдельно,
«Администрирование → Игры») и хранит лучшие результаты. Результат присылает браузер, поэтому защиты от подделки нет и быть не может — для таблицы рекордов «для души»
сервер лишь отбрасывает невозможные значения (больше потолка игры) и ограничивает частоту отправки."""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any, Optional

from app.db.database import get_connection

# key — внутреннее имя (совпадает с ключом в games_impl.js); better: high — чем больше, тем лучше, low — чем меньше (время, число ходов);
# max/min — правдоподобные границы результата (за ними — подделка или ошибка); unit — как подписывать результат.
CATEGORIES = [("puzzle", "Головоломки"), ("sort", "Сортировка"), ("logic", "Логика"), ("memory", "Память"), ("arcade", "Аркады"), ("speed", "Скорость")]


def _g(key, title, icon, cat, hue, hint, better, unit, lo, hi):
    return {"key": key, "title": title, "icon": icon, "cat": cat, "hue": hue, "hint": hint, "better": better, "unit": unit, "min": lo, "max": hi}


# cat — раздел на странице; hue — оттенок рамки и карточки (0–360).
CATALOG: list[dict[str, Any]] = [
    _g("bubble", "Пузырьковый стрелок", "🫧", "arcade", 195, "Стреляйте пузырями, собирайте группы одного цвета и набирайте очки. Результат попадёт в общую таблицу рекордов.", "high", "очков", 0, 2_000_000),
    {**_g("arrows", "Стрелки: путь наружу", "↗️", "puzzle", 190, "Распутайте поле: выпускайте стрелки по одной, не сталкиваясь с соседями. Сотни уровней, подсказки и сохранение прогресса.", "high", "уровень", 0, 9999), "difficulty": False},
    _g("workshop", "Мастерская", "🛠️", "arcade", 28, "Выполняйте заявки, получайте жетоны, улучшайте инструменты и открывайте новые уровни. Прогресс сохраняется на этом устройстве.", "high", "уровней", 0, 99),
    _g("water", "Переливашки", "🧪", "sort", 190, "Переливайте цвета, пока в каждой пробирке не останется один. Уровни всё сложнее.", "high", "уровней", 0, 500),
    _g("order", "Расставь по порядку", "🔢", "sort", 215, "Меняйте плитки местами и расставьте числа по возрастанию. 5 раундов на время.", "low", "с", 3, 3600),
    _g("sorter", "Сортировщик", "📦", "sort", 30, "Шарики падают — отправляйте каждый в корзину своего цвета. Три ошибки — конец.", "high", "шт", 0, 2000),
    _g("g2048", "2048", "🧮", "puzzle", 45, "Сдвигайте плитки и собирайте 2048. Стрелки или свайп.", "high", "очков", 0, 2_000_000),
    _g("match3", "Три в ряд", "💎", "puzzle", 300, "Меняйте соседние камни и собирайте ряды из трёх. Минута на всё.", "high", "очков", 0, 200_000),
    _g("flood", "Заливка", "🎨", "puzzle", 160, "Закрасьте всё поле одним цветом не больше чем за 25 ходов.", "low", "ходов", 5, 25),
    _g("slide15", "Пятнашки", "🧩", "puzzle", 20, "Сдвигайте плитки и расставьте числа по порядку. Меньше ходов — лучше.", "low", "ходов", 5, 5000),
    _g("mines", "Сапёр", "💣", "logic", 0, "Поле 9×9, 10 мин. Правая кнопка (или долгое нажатие) — флажок. Важно время.", "low", "с", 4, 3600),
    _g("tictactoe", "Крестики-нолики", "❌", "logic", 340, "Против компьютера. Победа — 3 очка, ничья — 1, поражение завершает серию.", "high", "очков", 0, 3000),
    _g("lights", "Лампочки", "💡", "logic", 55, "Нажатие переключает лампочку и четырёх соседей. Погасите все.", "low", "ходов", 1, 1000),
    _g("hanoi", "Ханойская башня", "🗼", "logic", 260, "Перенесите 5 дисков на другой стержень: больший на меньший класть нельзя.", "low", "ходов", 31, 2000),
    _g("rps", "Камень-ножницы-бумага", "✂️", "logic", 120, "Серия побед подряд против компьютера. Проигрыш завершает партию.", "high", "побед", 0, 500),
    _g("hangman", "Виселица", "🔤", "logic", 280, "Угадывайте слова по буквам. Шесть жизней на слово, слов — сколько сможете.", "high", "слов", 0, 500),
    _g("guess", "Угадай число", "🎯", "logic", 10, "Я загадал число от 1 до 100. Подсказки «больше» и «меньше». Меньше попыток — лучше.", "low", "попыток", 1, 100),
    _g("memory", "Память", "🃏", "memory", 330, "Найдите все пары за минимум ходов.", "low", "ходов", 8, 500),
    _g("simon", "Повтори за мной", "🎹", "memory", 175, "Запомните цепочку огней и повторите её. С каждым разом длиннее.", "high", "раундов", 0, 200),
    _g("snake", "Змейка", "🐍", "arcade", 140, "Ешьте яблоки и не врезайтесь. Стрелки или свайп.", "high", "яблок", 0, 3000),
    _g("tetris", "Тетрис", "🧱", "arcade", 25, "Стройте линии. ← → — сдвиг, ↑ — поворот, ↓ — ускорить, пробел — сбросить.", "high", "очков", 0, 2_000_000),
    _g("breakout", "Арканоид", "🏓", "arcade", 200, "Отбивайте мяч и ломайте кирпичи. Мышь, палец или стрелки.", "high", "очков", 0, 200_000),
    _g("flappy", "Летающая камера", "📷", "arcade", 205, "Нажимайте пробел или касайтесь, чтобы лететь между столбами.", "high", "столбов", 0, 5000),
    _g("stack", "Башня", "🏗️", "arcade", 35, "Нажмите, чтобы уронить блок. Что не попало на башню — обрезается.", "high", "блоков", 0, 1000),
    _g("runner", "Бегун", "🏃", "arcade", 100, "Прыгайте через препятствия: пробел, стрелка вверх или касание.", "high", "м", 0, 100_000),
    _g("reaction", "Реакция", "⚡", "speed", 50, "Нажмите, как только экран станет зелёным. 5 попыток — среднее время.", "low", "мс", 90, 5000),
    _g("math", "Быстрый счёт", "➗", "speed", 250, "30 секунд на примеры. Сколько решите верно?", "high", "верно", 0, 150),
    _g("whack", "Сбой на комплексе", "🔴", "speed", 355, "30 секунд: нажимайте на камеру, как только она «упала» (красная).", "high", "попаданий", 0, 200),
    _g("schulte", "Таблица Шульте", "👁️", "speed", 230, "Нажимайте числа от 1 до 25 по порядку как можно быстрее.", "low", "с", 4, 3600),
    _g("clicker", "Тапалка", "👆", "speed", 15, "10 секунд: сколько раз успеете нажать на кнопку?", "high", "нажатий", 0, 150),
]
BY_KEY = {g["key"]: g for g in CATALOG}
for _game in CATALOG:
    _game["difficulty"] = _game["key"] in {"workshop", "water", "mines", "math", "clicker"}
_MIN_GAP = 2.0                      # не чаще одной отправки результата в 2 секунды на пользователя и игру
_last_submit: dict[tuple[int, str], float] = {}


def category_list() -> list[dict[str, str]]:
    return [{"key": k, "title": t} for k, t in CATEGORIES]


def public_catalog(keys: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Каталог для страницы (без служебных границ); `keys` — только разрешённые игры."""
    return [{k: g[k] for k in ("key", "title", "icon", "cat", "hue", "hint", "better", "unit", "difficulty") if k in g} for g in CATALOG if keys is None or g["key"] in keys]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def settings_of(user_id: int) -> dict[str, Any]:
    """{enabled, games}: games — None (все игры) или список разрешённых ключей."""
    async with get_connection() as conn:
        cur = await conn.execute("select enabled, games from user_games where user_id = ?", (user_id,))
        row = await cur.fetchone()
    if not row:
        return {"enabled": False, "games": None}
    games = None
    if row[1]:
        try:
            games = [k for k in json.loads(row[1]) if k in BY_KEY]
        except ValueError:
            games = None
    return {"enabled": bool(row[0]), "games": games}


async def allowed_keys(user_id: int) -> Optional[list[str]]:
    """Ключи игр, доступных пользователю, либо None, если игры ему не включены."""
    s = await settings_of(user_id)
    if not s["enabled"]:
        return None
    return list(BY_KEY) if s["games"] is None else [k for k in BY_KEY if k in s["games"]]


async def set_access(user_id: int, enabled: bool, games: Optional[list[str]]) -> None:
    clean = None if games is None else [k for k in dict.fromkeys(games) if k in BY_KEY]
    async with get_connection() as conn:
        await conn.execute(
            "insert into user_games (user_id, enabled, games) values (?, ?, ?) on conflict(user_id) do update set enabled = excluded.enabled, games = excluded.games",
            (user_id, int(enabled), None if clean is None else json.dumps(clean)),
        )
        await conn.commit()


async def set_access_bulk(enabled: bool) -> int:
    """Включить или выключить игры сразу всем пользователям (выбор конкретных игр у каждого сохраняется)."""
    async with get_connection() as conn:
        await conn.execute("insert or ignore into user_games (user_id, enabled, games) select id, 0, NULL from users")
        cur = await conn.execute("update user_games set enabled = ?", (int(enabled),))
        await conn.commit()
        return cur.rowcount or 0


async def users_access() -> list[dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "select u.id, u.username, u.role, coalesce(g.enabled, 0), g.games, "
            "(select count(*) from game_scores s where s.user_id = u.id), (select coalesce(sum(plays), 0) from game_scores s where s.user_id = u.id) "
            "from users u left join user_games g on g.user_id = u.id order by u.username collate nocase"
        )
        rows = await cur.fetchall()
    out = []
    for uid, name, role, enabled, games, n_games, plays in rows:
        try:
            lst = None if not games else [k for k in json.loads(games) if k in BY_KEY]
        except ValueError:
            lst = None
        out.append({"id": uid, "username": name, "role": role, "enabled": bool(enabled), "games": lst, "games_played": n_games, "plays": plays})
    return out


class ScoreError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail, self.status_code = detail, status_code


async def submit_score(user_id: int, game: str, score: float) -> dict[str, Any]:
    """Записать результат партии: лучший сохраняется. Возвращает {best, is_record, rank, plays}."""
    meta = BY_KEY.get(game)
    if meta is None:
        raise ScoreError("Такой игры нет", 404)
    keys = await allowed_keys(user_id)
    if keys is None or game not in keys:
        raise ScoreError("Эта игра вам не включена", 403)
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise ScoreError("Результат должен быть числом")
    if value != value or value < meta["min"] or value > meta["max"]:
        raise ScoreError("Недопустимый результат")
    now = time.monotonic()
    key = (user_id, game)
    if now - _last_submit.get(key, -1e9) < _MIN_GAP:
        raise ScoreError("Слишком часто — подождите пару секунд", 429)
    _last_submit[key] = now
    value = round(value, 2)
    low = meta["better"] == "low"
    async with get_connection() as conn:
        cur = await conn.execute("select best from game_scores where user_id = ? and game = ?", (user_id, game))
        row = await cur.fetchone()
        stamp = _now()
        if row is None:
            await conn.execute("insert into game_scores (user_id, game, best, plays, best_at, last_at) values (?, ?, ?, 1, ?, ?)", (user_id, game, value, stamp, stamp))
            best, record = value, True
        else:
            record = value < row[0] if low else value > row[0]
            best = value if record else row[0]
            await conn.execute(
                "update game_scores set best = ?, plays = plays + 1, best_at = case when ? then ? else best_at end, last_at = ? where user_id = ? and game = ?",
                (best, int(record), stamp, stamp, user_id, game),
            )
        await conn.commit()
        cur = await conn.execute("select plays from game_scores where user_id = ? and game = ?", (user_id, game))
        plays = (await cur.fetchone())[0]
    board = await leaderboard(game, user_id, limit=1)
    return {"best": best, "is_record": record, "rank": board["me"]["rank"] if board["me"] else None, "plays": plays}


async def leaderboard(game: str, user_id: Optional[int] = None, limit: int = 20) -> dict[str, Any]:
    """Таблица рекордов по игре: лучшие `limit` пользователей и место/результат самого `user_id`."""
    meta = BY_KEY.get(game)
    if meta is None:
        raise ScoreError("Такой игры нет", 404)
    order = "asc" if meta["better"] == "low" else "desc"
    async with get_connection() as conn:
        cur = await conn.execute(
            f"select s.user_id, u.username, s.best, s.plays, s.best_at from game_scores s join users u on u.id = s.user_id where s.game = ? order by s.best {order}, s.best_at asc",
            (game,),
        )
        rows = await cur.fetchall()
    ranked = [{"rank": i + 1, "user_id": r[0], "username": r[1], "best": r[2], "plays": r[3], "at": r[4]} for i, r in enumerate(rows)]
    me = next((r for r in ranked if r["user_id"] == user_id), None) if user_id is not None else None
    top = [{k: v for k, v in r.items() if k != "user_id"} | {"me": r["user_id"] == user_id} for r in ranked[:limit]]
    return {"game": game, "title": meta["title"], "better": meta["better"], "unit": meta["unit"], "total": len(ranked), "top": top,
            "me": ({"rank": me["rank"], "best": me["best"], "plays": me["plays"]} if me else None)}


async def my_bests(user_id: int) -> dict[str, dict[str, Any]]:
    async with get_connection() as conn:
        cur = await conn.execute("select game, best, plays from game_scores where user_id = ?", (user_id,))
        return {g: {"best": b, "plays": p} for g, b, p in await cur.fetchall()}


async def reset_scores(*, game: Optional[str] = None, user_id: Optional[int] = None) -> int:
    """Сбросить рекорды (администратор): по игре, по пользователю или все. Возвращает, сколько записей удалено."""
    sql, params = "delete from game_scores", []
    where = []
    if game:
        where.append("game = ?")
        params.append(game)
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if where:
        sql += " where " + " and ".join(where)
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.rowcount or 0
