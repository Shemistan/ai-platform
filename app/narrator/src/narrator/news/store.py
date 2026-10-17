"""Локальное хранилище новостей: library/news/news.db (SQLite).

items — материалы с оценкой агента, runs — журнал сбора по источникам, memo —
что нужно помнить между заходами (channel_id YouTube). Все времена — ISO 8601
в UTC.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from narrator.news.feeds import Item, is_web
from narrator.news.sources import Source

DB_NAME = "news.db"
# Выпуски и их колонки отметок: дайджест дня, обзоры недели и месяца для YouTube.
ISSUES = ("digest", "weekly", "monthly")
# Пометка в runs.error: материалы упавшего источника добрал агент (news add).
RESCUED = "· добрал агент"
# Параметры-метки рекламы и шаринга: от них ссылка не становится другой.
_TRACKING = {"fbclid", "gclid", "ref", "ref_src", "mc_cid", "mc_eid"}

RETENTION_DAYS = 180  # `news prune`: старше — в архив (CMP-016)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    published    TEXT,
    summary      TEXT NOT NULL DEFAULT '',
    collected    TEXT NOT NULL,
    status       TEXT NOT NULL,          -- new | assessed | skipped | old
    kind         TEXT,
    event        TEXT,
    title_ru     TEXT,
    why          TEXT,
    factors      TEXT,                   -- JSON: фактор → 0…1
    authority    REAL,
    score        REAL,
    importance   INTEGER,
    confirmed_by TEXT,
    assessed     TEXT,
    digest       TEXT,                   -- id дайджеста дня, куда попал материал
    weekly       TEXT                    -- id недельного обзора, куда попал материал
    monthly      TEXT,                   -- id месячного обзора, куда попал материал
    foreign_agent TEXT                   -- id записи реестра: источник — канал иноагента
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE INDEX IF NOT EXISTS items_event ON items(event);
CREATE TABLE IF NOT EXISTS runs (
    source TEXT NOT NULL,
    at     TEXT NOT NULL,
    ok     INTEGER NOT NULL,
    found  INTEGER NOT NULL DEFAULT 0,
    new    INTEGER NOT NULL DEFAULT 0,
    error  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS memo (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Ссылка без меток utm_*, фрагмента и хвостового слэша, хост строчными."""
    parts = urlsplit(url.strip())
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = [
        (k, v) for k, v in pairs if not (k.lower().startswith("utm_") or k.lower() in _TRACKING)
    ]
    path = parts.path.rstrip("/") or "/"
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    return urlunsplit((scheme, parts.netloc.lower(), path, urlencode(query), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:12]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        for column in (*ISSUES[1:], "foreign_agent"):  # база до обзоров и до иноагентов
            if column not in columns:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
        self._rescale()

    def _rescale(self) -> None:
        """База со шкалой важности 1…5 → 1…10: пересчитать из сохранённого балла.

        Материал, которому правило подтверждения срезало важность, остаётся
        срезанным и на новой шкале.
        """
        from narrator.news.digest import UNCONFIRMED_CAP, importance

        if self.memo().get("importance_scale") == "10":
            return
        rows = self.db.execute(
            "SELECT id, score, importance FROM items WHERE importance IS NOT NULL"
        ).fetchall()
        for r in rows:
            level = importance(r["score"])
            if r["importance"] < min(5, int(round(r["score"] * 5, 6)) + 1):  # был срезан
                level = min(level, UNCONFIRMED_CAP)
            self.db.execute("UPDATE items SET importance = ? WHERE id = ?", (level, r["id"]))
        self.save_memo({"importance_scale": "10"})

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- сбор -------------------------------------------------------------

    def ever_collected(self, source: str) -> bool:
        row = self.db.execute("SELECT 1 FROM runs WHERE source = ? AND ok = 1", (source,))
        return row.fetchone() is not None

    def add(
        self,
        src: Source,
        items: Iterable[Item],
        now: datetime,
        days: float,
        baseline: bool | None = None,
        agent: str | None = None,
    ) -> int:
        """Кладёт новые материалы источника; возвращает, сколько из них ждут разбора.

        Материал старше `days` сразу `old`. У материала без даты свежесть не
        проверить, поэтому при первом заходе по источнику он тоже `old`:
        первый заход только запоминает, что уже было на странице. baseline=False
        — материалы отобрал агент, и без даты они свежие. agent — id записи
        реестра, если источник — канал иноагента: материал маркируется при публикации.
        """
        cutoff = iso(now - timedelta(days=days))
        if baseline is None:
            baseline = not self.ever_collected(src.id)
        fresh = 0
        for item in items:
            if not is_web(item.url):  # только http(s): ссылку покажет страница новостей
                continue
            key = item_id(item.url)
            if self.db.execute("SELECT 1 FROM items WHERE id = ?", (key,)).fetchone():
                continue
            if item.published:
                status = "new" if item.published >= cutoff else "old"
            else:
                status = "old" if baseline else "new"
            self.db.execute(
                "INSERT INTO items (id, source, title, url, published, summary, collected, status,"
                " foreign_agent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    src.id,
                    item.title,
                    item.url,
                    item.published,
                    item.summary,
                    iso(now),
                    status,
                    agent,
                ),
            )
            fresh += status == "new"
        return fresh

    def log_run(self, source: str, now: datetime, ok: bool, found=0, new=0, error="") -> None:
        self.db.execute(
            "INSERT INTO runs (source, at, ok, found, new, error) VALUES (?, ?, ?, ?, ?, ?)",
            (source, iso(now), int(ok), found, new, error),
        )
        self.db.commit()

    def failed(self) -> dict[str, dict]:
        """Источники, чей последний заход — сбой, который агент ещё не добрал."""
        rows = self.db.execute(
            "SELECT source, at, ok, error FROM runs r1 WHERE rowid ="
            " (SELECT MAX(rowid) FROM runs r2 WHERE r2.source = r1.source)"
        )
        return {r["source"]: dict(r) for r in rows if not r["ok"] and RESCUED not in r["error"]}

    def log_rescue(self, source: str, now: datetime, error: str, found: int, new: int) -> None:
        """Агент добрал материалы упавшего источника: сбой остаётся сбоем, но с пометкой."""
        note = f"{error} {RESCUED}: {found}" if error else f"{RESCUED}: {found}"
        self.log_run(source, now, ok=False, found=found, new=new, error=note)

    def memo(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM memo")}

    def save_memo(self, memo: dict[str, str]) -> None:
        self.db.executemany(
            "INSERT INTO memo (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            memo.items(),
        )
        self.db.commit()

    # --- разбор -----------------------------------------------------------

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM items WHERE status = 'new' ORDER BY collected, source, id"
        ).fetchall()

    def get(self, key: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM items WHERE id = ?", (key,)).fetchone()

    def save_assessment(self, key: str, fields: dict, now: datetime) -> None:
        self.db.execute(
            "UPDATE items SET status = 'assessed', kind = :kind, event = :event,"
            " title_ru = :title_ru, why = :why, factors = :factors, authority = :authority,"
            " score = :score, importance = :importance, confirmed_by = :confirmed_by,"
            " assessed = :assessed WHERE id = :id",
            {
                **fields,
                "factors": json.dumps(fields["factors"], sort_keys=True),
                "assessed": iso(now),
                "id": key,
            },
        )

    def skip(self, keys: Iterable[str], now: datetime) -> int:
        n = 0
        for key in keys:
            cur = self.db.execute(
                "UPDATE items SET status = 'skipped', assessed = ? WHERE id = ? AND status = 'new'",
                (iso(now), key),
            )
            n += cur.rowcount
        return n

    def commit(self) -> None:
        self.db.commit()

    # --- ретенция ---------------------------------------------------------

    def prune(self, now: datetime, days: float = RETENTION_DAYS) -> int:
        """Материалы старше days — в архив (`old`), база сжимается VACUUM (CMP-016).

        Записи не удаляются: по id сбор узнаёт уже виденную ссылку и не
        приносит её снова. Сжатие — только если что-то ушло в архив.
        """
        cutoff = iso(now - timedelta(days=days))
        cur = self.db.execute(
            "UPDATE items SET status = 'old'"
            " WHERE status != 'old' AND coalesce(published, collected) < ?",
            (cutoff,),
        )
        self.db.commit()
        if cur.rowcount:
            self.db.execute("VACUUM")
        return cur.rowcount

    # --- отбор ------------------------------------------------------------

    def assessed_on(
        self, start: datetime, end: datetime, mark: str | None = None, column: str = "digest"
    ) -> list[sqlite3.Row]:
        """Оценённые материалы, опубликованные в [start, end) и не попавшие в другой выпуск.

        Дата — публикации; у материала без неё (страницы без ленты) — сбора.
        """
        _check_column(column)
        return self.db.execute(
            f"SELECT * FROM items WHERE status = 'assessed'"
            f" AND coalesce(published, collected) >= ? AND coalesce(published, collected) < ?"
            f" AND ({column} IS NULL OR {column} = ?)",
            (iso(start), iso(end), mark or ""),
        ).fetchall()

    def assessed_since(
        self, since: datetime, mark: str | None = None, column: str = "digest"
    ) -> list[sqlite3.Row]:
        """Оценённые материалы, собранные после since и не попавшие в другой выпуск.

        column — выпуск из ISSUES: отметки у выпусков свои, поэтому новость
        дня попадает и в обзор недели, и в обзор месяца.
        """
        _check_column(column)
        return self.db.execute(
            f"SELECT * FROM items WHERE status = 'assessed'"
            f" AND (({column} IS NULL AND collected >= ?) OR {column} = ?)",
            (iso(since), mark or ""),
        ).fetchall()

    def mark_issue(self, keys: Iterable[str], mark: str, column: str = "digest") -> None:
        """Отметить материалы выпуском mark; прежние отметки этого выпуска снимаются."""
        _check_column(column)
        self.db.execute(f"UPDATE items SET {column} = NULL WHERE {column} = ?", (mark,))
        self.db.executemany(
            f"UPDATE items SET {column} = ? WHERE id = ?", [(mark, k) for k in keys]
        )
        self.db.commit()

    def issue_sources(self, marks: Iterable[str], column: str = "digest") -> set[str]:
        """Источники материалов, попавших в выпуски marks."""
        _check_column(column)
        marks = list(marks)
        if not marks:
            return set()
        holes = ", ".join("?" * len(marks))
        rows = self.db.execute(
            f"SELECT DISTINCT source FROM items WHERE {column} IN ({holes})", marks
        )
        return {r["source"] for r in rows}

    def assessed(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM items WHERE status = 'assessed'").fetchall()

    def rescore(self, key: str, factors: dict, score: float, importance: int) -> None:
        self.db.execute(
            "UPDATE items SET factors = ?, score = ?, importance = ? WHERE id = ?",
            (json.dumps(factors, sort_keys=True), score, importance, key),
        )

    def source_stats(self) -> dict[str, dict]:
        """По источнику: материалов, оценено, важных (от 7 из 10), в выпусках, последний заход."""
        stats: dict[str, dict] = {}
        for r in self.db.execute(
            "SELECT source, COUNT(*) AS total,"
            " SUM(status IN ('assessed', 'skipped')) AS processed,"
            " SUM(importance >= 7) AS important,"
            " SUM(digest IS NOT NULL) AS issued FROM items GROUP BY source"
        ):
            stats[r["source"]] = {
                "total": r["total"],
                "processed": r["processed"] or 0,
                "important": r["important"] or 0,
                "issued": r["issued"] or 0,
            }
        for r in self.db.execute(
            "SELECT source, at, ok, new, error FROM runs r1 WHERE at ="
            " (SELECT MAX(at) FROM runs r2 WHERE r2.source = r1.source)"
        ):
            stats.setdefault(r["source"], {"total": 0, "processed": 0, "important": 0})
            stats[r["source"]]["last"] = dict(r)
        return stats


def _check_column(column: str) -> None:
    if column not in ISSUES:  # имя колонки идёт в SQL — только известные
        raise ValueError(f"нет такого выпуска: {column}")


def issue_agents(db: Path, mark: str, column: str = "digest") -> list[str]:
    """id иноагентов из реестра, чьи материалы попали в выпуск mark (CMP-012).

    Только чтение: публикация не трогает базу (и не мигрирует её), нет базы
    или колонки — пусто."""
    _check_column(column)
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            f"SELECT DISTINCT foreign_agent FROM items WHERE {column} = ?"
            " AND foreign_agent IS NOT NULL",
            (mark,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return sorted(r[0] for r in rows)


def db_path(root: Path) -> Path:
    return root / "news" / DB_NAME
