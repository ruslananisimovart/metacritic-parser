"""SQLite-хранилище: игры с платформами, прогоны, отметки "обработано сегодня" и ошибки."""

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from app.metacritic import Game, ListedGame

# прогон, который так долго не отмечался, считается брошенным (процесс упал или ПК выключили)
STALE_RUN_AFTER = timedelta(minutes=10)

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT,
    release_date TEXT,
    rating TEXT,
    genres TEXT NOT NULL,
    developers TEXT NOT NULL,
    publishers TEXT NOT NULL,
    cover_url TEXT,
    video_url TEXT,
    video_title TEXT,
    metascore INTEGER,
    critic_reviews INTEGER NOT NULL,
    userscore REAL,
    user_reviews INTEGER NOT NULL,
    main_platform TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS game_platforms (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    is_main INTEGER NOT NULL,
    metascore INTEGER,
    critic_reviews INTEGER NOT NULL,
    userscore REAL,
    user_reviews INTEGER NOT NULL,
    PRIMARY KEY (game_id, slug)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    summaries INTEGER NOT NULL DEFAULT 0,
    -- игры, для которых спрашивали магазины, и сколько из них что-то получили
    media_checked INTEGER NOT NULL DEFAULT 0,
    media_found INTEGER NOT NULL DEFAULT 0,
    -- сколько резюме проверили (по два на игру) и летсплеи этой итерации: сколько ушло в очередь и чем кончились
    summaries_checked INTEGER NOT NULL DEFAULT 0,
    -- старые игры каталога: сколько взято на перепроверку (stale_selected) и сколько из них загрузилось (rechecked)
    stale_selected INTEGER NOT NULL DEFAULT 0,
    rechecked INTEGER NOT NULL DEFAULT 0,
    lp_total INTEGER NOT NULL DEFAULT 0,
    lp_found INTEGER NOT NULL DEFAULT 0,
    lp_not_found INTEGER NOT NULL DEFAULT 0,
    lp_failed INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS processed (
    day TEXT NOT NULL,
    slug TEXT NOT NULL,
    game_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (day, slug)
);

CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    day TEXT NOT NULL,
    slug TEXT NOT NULL,
    stage TEXT NOT NULL,
    error TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS failures_day_slug ON failures (day, slug);

-- обложка и трейлер из магазинов (Steam, Google Play) для игр, у которых их нет на Metacritic;
-- запись с пустыми полями значит "искали, не нашли". *_page: страница игры в магазине для ссылки "Открыть"
CREATE TABLE IF NOT EXISTS media (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    cover_url TEXT,
    cover_kind TEXT,
    cover_source TEXT,
    cover_page TEXT,
    video_url TEXT,
    video_title TEXT,
    video_poster TEXT,
    video_source TEXT,
    video_page TEXT,
    description TEXT,
    description_source TEXT,
    -- почему магазины ничего не дали (для журнала и разбора)
    note TEXT,
    searched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_summaries (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    likes TEXT NOT NULL,
    dislikes TEXT NOT NULL,
    summary TEXT NOT NULL,
    reviews_used INTEGER NOT NULL,
    reviews_total INTEGER NOT NULL,
    model TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (game_id, kind)
);

-- настройки, которые меняются из интерфейса и должны пережить перезапуск (выбор модели)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_GAME_FIELDS = (
    "id", "slug", "title", "description", "release_date", "rating", "genres", "developers", "publishers",
    "cover_url", "video_url", "video_title", "metascore", "critic_reviews", "userscore", "user_reviews",
    "main_platform",
)
_UPSERT_GAME = f"""
INSERT INTO games ({", ".join(_GAME_FIELDS)}, first_seen_at, updated_at)
VALUES ({", ".join(":" + f for f in _GAME_FIELDS)}, :now, :now)
ON CONFLICT (id) DO UPDATE SET
    {", ".join(f"{f} = excluded.{f}" for f in _GAME_FIELDS[1:])},
    updated_at = excluded.updated_at
"""


class RunInProgress(Exception):
    def __init__(self, run_id: int) -> None:
        super().__init__(f"run {run_id} is already in progress")
        self.run_id = run_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stale_border(day: date, days: int, tz: ZoneInfo) -> str:
    """Граница "пора обновить" по дням сервиса: игра, обновлённая в день D, созревает в день D + days,
    независимо от часа. Игры с updated_at раньше этой отметки (UTC ISO) считаются старыми по сроку.
    Пример: срок 7 дней, сегодня 23.09: обновлённые 16.09 и раньше пора, обновлённые 17.09 ещё нет."""
    first_fresh_day = day - timedelta(days=days - 1)
    return _ts(datetime.combine(first_fresh_day, time.min, tzinfo=tz))


def _ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # транзакции открываем явно через BEGIN IMMEDIATE, поэтому autocommit-режим драйвера
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate_failures()
        # трейлеры с YouTube, отдельная таблица обложек и таблица steam заменены таблицей media;
        # старые записи не нужны: магазины спрашиваются заново в ближайший прогон
        self.conn.executescript("DROP TABLE IF EXISTS trailers; DROP TABLE IF EXISTS covers; DROP TABLE IF EXISTS steam;")
        self._add_columns("media", {"description": "TEXT", "description_source": "TEXT", "note": "TEXT"})
        self._add_columns("runs", {
            name: "INTEGER NOT NULL DEFAULT 0"
            for name in ("media_checked", "media_found", "summaries_checked", "lp_total", "lp_found", "lp_not_found", "lp_failed",
                         "rechecked", "stale_selected")
        })

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        """Новые колонки в существующей таблице: база, созданная прежней версией, дополняется на месте."""
        present = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in present:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    def _migrate_failures(self) -> None:
        """В старой базе run_id у ошибок обязателен, а сбои летсплеев происходят вне прогона:
        таблица пересобирается с необязательным run_id, записи сохраняются."""
        columns = self.conn.execute("PRAGMA table_info(failures)").fetchall()
        if not any(col["name"] == "run_id" and col["notnull"] for col in columns):
            return
        self.conn.executescript(
            "BEGIN;"
            "CREATE TABLE failures_new ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER REFERENCES runs(id), day TEXT NOT NULL,"
            " slug TEXT NOT NULL, stage TEXT NOT NULL, error TEXT NOT NULL, created_at TEXT NOT NULL);"
            "INSERT INTO failures_new (id, run_id, day, slug, stage, error, created_at)"
            " SELECT id, run_id, day, slug, stage, error, created_at FROM failures;"
            "DROP TABLE failures;"
            "ALTER TABLE failures_new RENAME TO failures;"
            "CREATE INDEX IF NOT EXISTS failures_day_slug ON failures (day, slug);"
            "COMMIT;"
        )

    def close(self) -> None:
        self.conn.close()

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, _ts(utc_now())),
            )

    def delete_setting(self, key: str) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def clear_catalog(self) -> None:
        """Очищает базу до состояния чистого сервиса: игры, отметки дня, ошибки и история прогонов.

        Платформы, резюме, летсплеи и записи Steam уходят каскадом за играми. Настройки (модель, расписание)
        остаются: они про сам сервис, а не про собранные данные.
        """
        with self._transaction() as conn:
            conn.execute("DELETE FROM games")
            conn.execute("DELETE FROM processed")
            conn.execute("DELETE FROM failures")
            conn.execute("DELETE FROM runs")

    def restore_catalog(self, source: Path, tables: Sequence[str]) -> int:
        """Переносит строки из снимка базы, существующие не затирает. Возвращает число вернувшихся игр."""
        before = self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        # ATTACH нельзя внутри транзакции, поэтому он снаружи
        self.conn.execute("ATTACH DATABASE ? AS archive", (str(source),))
        try:
            with self._transaction() as conn:
                for table in tables:
                    current = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
                    stored = {row[1] for row in conn.execute(f"PRAGMA archive.table_info({table})")}
                    # снимок мог сделать сервис со схемой старше или новее: берём общие колонки
                    columns = ", ".join(name for name in current if name in stored)
                    if columns:
                        conn.execute(
                            f"INSERT OR IGNORE INTO main.{table} ({columns}) SELECT {columns} FROM archive.{table}"
                        )
        finally:
            self.conn.execute("DETACH DATABASE archive")
        return int(self.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] - before)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def start_run(self, trigger: str, day: date, now: datetime | None = None) -> int:
        """Регистрирует прогон. Брошенные прогоны помечает interrupted, при живом поднимает RunInProgress."""
        now = now or utc_now()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running' AND updated_at < ?",
                (_ts(now), _ts(now - STALE_RUN_AFTER)),
            )
            running = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchone()
            if running:
                raise RunInProgress(running["id"])
            cursor = conn.execute(
                "INSERT INTO runs (trigger, day, status, started_at, updated_at) VALUES (?, ?, 'running', ?, ?)",
                (trigger, day.isoformat(), _ts(now), _ts(now)),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def set_selected(self, run_id: int, selected: int, stale: int = 0) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET selected = ?, stale_selected = ?, updated_at = ? WHERE id = ?",
                (selected, stale, _ts(utc_now()), run_id),
            )

    def stale_outlook(self, day: date, days: int, tz: ZoneInfo) -> tuple[int, str | None]:
        """Обновление старых игр: сколько игр пора обновить сегодня и когда наступит очередь следующей
        (UTC ISO, полночь дня по поясу сервиса), если сегодня некого. Для подсказок в интерфейсе."""
        due = self.stale_due(stale_border(day, days, tz))
        if due:
            return due, None
        oldest = self.conn.execute("SELECT MIN(updated_at) FROM games").fetchone()[0]
        if not oldest:
            return 0, None
        oldest_day = datetime.fromisoformat(oldest).astimezone(tz).date()
        due_day = oldest_day + timedelta(days=days)
        return 0, _ts(datetime.combine(due_day, time.min, tzinfo=tz))

    def finish_run(self, run_id: int, status: str, error: str | None = None) -> None:
        now = _ts(utc_now())
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (status, error, now, now, run_id),
            )

    def last_run_id(self) -> int | None:
        value = self.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        return int(value) if value is not None else None

    def set_summaries_checked(self, run_id: int, checked: int) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE runs SET summaries_checked = ? WHERE id = ?", (checked, run_id))

    def letsplay_count(self, run_id: int, **deltas: int) -> None:
        """Счётчики летсплеев итерации: lp_total (ушло в очередь), lp_found, lp_not_found, lp_failed."""
        allowed = {"total": "lp_total", "lp_total": "lp_total", "lp_found": "lp_found",
                   "lp_not_found": "lp_not_found", "lp_failed": "lp_failed"}
        sets = ", ".join(f"{allowed[k]} = MAX(0, {allowed[k]} + ?)" for k in deltas)
        with self._transaction() as conn:
            conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*deltas.values(), run_id))

    def set_media_counts(self, run_id: int, checked: int, found: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE runs SET media_checked = ?, media_found = ?, updated_at = ? WHERE id = ?",
                (checked, found, _ts(utc_now()), run_id),
            )

    def save_listed(self, games: Sequence[ListedGame]) -> set[str]:
        """Предварительные карточки из списка Metacritic: каталог показывает игры сразу, не дожидаясь
        подробностей. Уже известные игры не трогает, возвращает slug'и добавленных."""
        now = _ts(utc_now())
        added: set[str] = set()
        with self._transaction() as conn:
            for game in games:
                if conn.execute("SELECT 1 FROM games WHERE id = ? OR slug = ?", (game.id, game.slug)).fetchone():
                    continue
                conn.execute(
                    "INSERT INTO games (id, slug, title, description, release_date, rating, genres, developers,"
                    " publishers, cover_url, metascore, critic_reviews, user_reviews, first_seen_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, 0, ?, ?)",
                    (
                        game.id,
                        game.slug,
                        game.title,
                        game.description,
                        game.release_date.isoformat() if game.release_date else None,
                        game.rating,
                        json.dumps(list(game.genres), ensure_ascii=False),
                        game.cover_url,
                        game.metascore,
                        game.critic_reviews,
                        now,
                        now,
                    ),
                )
                added.add(game.slug)
        return added

    def drop_listed(self, slug: str) -> None:
        """Убирает предварительную карточку: подробности игры так и не загрузились."""
        with self._transaction() as conn:
            conn.execute("DELETE FROM games WHERE slug = ?", (slug,))

    def save_game(
        self, game: Game, *, run_id: int, day: date, slug: str, source: str, created: bool | None = None
    ) -> bool:
        """Сохраняет игру и её платформы и отмечает slug обработанным за день. True, если игра новая.

        created передаётся, когда игру уже видели в этом прогоне предварительной карточкой: строка в базе
        есть, но для счётчиков прогона игра всё равно новая."""
        now = _ts(utc_now())
        main = game.main_platform
        params = {
            "id": game.id,
            "slug": game.slug,
            "title": game.title,
            "description": game.description,
            "release_date": game.release_date.isoformat() if game.release_date else None,
            "rating": game.rating,
            "genres": json.dumps(game.genres, ensure_ascii=False),
            "developers": json.dumps(game.developers, ensure_ascii=False),
            "publishers": json.dumps(game.publishers, ensure_ascii=False),
            "cover_url": game.cover_url,
            "video_url": game.video_url,
            "video_title": game.video_title,
            "metascore": game.metascore,
            "critic_reviews": game.critic_reviews,
            "userscore": main.userscore if main else None,
            "user_reviews": main.user_reviews if main else 0,
            "main_platform": main.name if main else None,
            "now": now,
        }
        platforms = [
            (game.id, p.slug, p.name, int(p.is_main), p.metascore, p.critic_reviews, p.userscore, p.user_reviews)
            for p in game.platforms
        ]
        with self._transaction() as conn:
            if created is None:
                created = conn.execute("SELECT 1 FROM games WHERE id = ?", (game.id,)).fetchone() is None
            conn.execute(_UPSERT_GAME, params)
            conn.execute("DELETE FROM game_platforms WHERE game_id = ?", (game.id,))
            conn.executemany("INSERT INTO game_platforms VALUES (?, ?, ?, ?, ?, ?, ?, ?)", platforms)
            conn.execute(
                "INSERT OR IGNORE INTO processed (day, slug, game_id, run_id, source, processed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day.isoformat(), slug, game.id, run_id, source, now),
            )
            # created и updated про игры из новинок Metacritic; игры, взятые на перепроверку (source stale),
            # считаются отдельно в rechecked, чтобы из цифр прогона было видно, откуда пришла игра
            recheck = source == "stale"
            conn.execute(
                "UPDATE runs SET created = created + ?, updated = updated + ?, rechecked = rechecked + ?, updated_at = ?"
                " WHERE id = ?",
                (int(created and not recheck), int(not created and not recheck), int(recheck), now, run_id),
            )
        return created

    def record_failure(
        self, *, run_id: int | None, day: date, slug: str, stage: str, error: str, game_failed: bool = True
    ) -> None:
        """game_failed=False для ошибок дополнительных этапов: игра при этом сохранена.

        run_id=None для сбоев вне прогона (летсплеи идут фоновым воркером): счётчик прогона не трогается.
        """
        now = _ts(utc_now())
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO failures (run_id, day, slug, stage, error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, day.isoformat(), slug, stage, error, now),
            )
            if run_id is not None:
                conn.execute(
                    "UPDATE runs SET failed = failed + ?, updated_at = ? WHERE id = ?", (int(game_failed), now, run_id)
                )

    def resolve_failures(self, slug: str, stages: Sequence[str]) -> int:
        """Убирает ошибки игры по этапам, которые с тех пор прошли успешно: в мониторинге остаются
        только нерешённые. Счётчики прогонов не трогаются, история прогонов остаётся как была."""
        marks = ", ".join("?" * len(stages))
        with self._transaction() as conn:
            cursor = conn.execute(f"DELETE FROM failures WHERE slug = ? AND stage IN ({marks})", (slug, *stages))
        return int(cursor.rowcount or 0)

    def resolve_stale_failures(self) -> int:
        """Сверка списка ошибок с данными: ошибка уходит, если после неё этап по игре прошёл успешно
        (летсплей найден или честно не найден, игра перечитана, резюме сохранено, магазины ответили).
        Зовётся при старте и после прогона: покрывает то, что случилось, пока сервис не работал."""
        # сравнение нестрогое: итог этапа и запись об ошибке ложатся в базу в одну секунду, и ">" их не видел
        rules = (
            ("stage = 'letsplay' AND EXISTS (SELECT 1 FROM games g JOIN letsplays l ON l.game_id = g.id"
             " WHERE g.slug = failures.slug AND l.status IN ('done', 'not_found') AND l.updated_at >= failures.created_at)"),
            ("stage = 'metacritic' AND EXISTS (SELECT 1 FROM games g"
             " WHERE g.slug = failures.slug AND g.updated_at >= failures.created_at)"),
            ("stage IN ('summary_critic', 'summary_user') AND EXISTS (SELECT 1 FROM games g"
             " JOIN review_summaries s ON s.game_id = g.id AND s.kind = substr(failures.stage, 9)"
             " WHERE g.slug = failures.slug AND s.updated_at >= failures.created_at)"),
            ("stage IN ('media', 'steam', 'trailer', 'cover') AND EXISTS (SELECT 1 FROM games g JOIN media m ON m.game_id = g.id"
             " WHERE g.slug = failures.slug AND m.searched_at >= failures.created_at)"),
        )
        removed = 0
        with self._transaction() as conn:
            for rule in rules:
                removed += conn.execute(f"DELETE FROM failures WHERE {rule}").rowcount or 0
        return removed

    def summary_total(self, game_id: int, kind: str) -> int | None:
        """Сколько отзывов было, когда строилось текущее резюме; None, если резюме нет."""
        row = self.conn.execute(
            "SELECT reviews_total FROM review_summaries WHERE game_id = ? AND kind = ?", (game_id, kind)
        ).fetchone()
        return row["reviews_total"] if row else None

    def save_summary(
        self,
        *,
        run_id: int | None,
        game_id: int,
        kind: str,
        # пункты объектами {text, sources}; в записях до появления источников лежат строки
        likes: list[dict[str, object]],
        dislikes: list[dict[str, object]],
        summary: str,
        reviews_used: int,
        reviews_total: int,
        model: str,
    ) -> None:
        now = _ts(utc_now())
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_summaries
                    (game_id, kind, likes, dislikes, summary, reviews_used, reviews_total, model, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (game_id, kind) DO UPDATE SET
                    likes = excluded.likes, dislikes = excluded.dislikes, summary = excluded.summary,
                    reviews_used = excluded.reviews_used, reviews_total = excluded.reviews_total,
                    model = excluded.model, updated_at = excluded.updated_at
                """,
                (
                    game_id,
                    kind,
                    json.dumps(likes, ensure_ascii=False),
                    json.dumps(dislikes, ensure_ascii=False),
                    summary,
                    reviews_used,
                    reviews_total,
                    model,
                    now,
                ),
            )
            # резюме, пересобранное из консоли, к прогону не относится
            if run_id is not None:
                conn.execute("UPDATE runs SET summaries = summaries + 1, updated_at = ? WHERE id = ?", (now, run_id))

    def processed_slugs(self, day: date) -> set[str]:
        rows = self.conn.execute("SELECT slug FROM processed WHERE day = ?", (day.isoformat(),))
        return {row["slug"] for row in rows}

    def stale_games(self, day: date, limit: int, border: str) -> list[tuple[int, str, str]]:
        """Старые игры, которым пора обновиться: обновлены до border (см. stale_border), самые давние первыми.

        Обработанные сегодня пропускаются, иначе прогон брал бы те же игры по кругу.
        """
        rows = self.conn.execute(
            "SELECT g.id, g.slug, g.title FROM games g"
            " WHERE g.updated_at < ? AND g.slug NOT IN (SELECT slug FROM processed WHERE day = ?)"
            " ORDER BY g.updated_at ASC, g.id ASC LIMIT ?",
            (border, day.isoformat(), limit),
        ).fetchall()
        return [(row["id"], row["slug"], row["title"]) for row in rows]

    def stale_due(self, border: str) -> int:
        """Сколько старых игр пора обновить: обновлены до border. Для подписей в интерфейсе."""
        return int(self.conn.execute("SELECT COUNT(*) FROM games WHERE updated_at < ?", (border,)).fetchone()[0])

    def exhausted_slugs(self, day: date, max_failures: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT slug FROM failures WHERE day = ? AND stage = 'metacritic' GROUP BY slug HAVING COUNT(*) >= ?",
            (day.isoformat(), max_failures),
        )
        return {row["slug"] for row in rows}

    def media_due(
        self, game_id: int, *, cover: bool, video: bool, description: bool, retry_after: timedelta,
        now: datetime | None = None,
    ) -> bool:
        """Нужно ли спрашивать магазины: игру ещё не искали, или того, чего не хватает (обложки, трейлера,
        описания), в прошлый раз не нашли дольше retry_after назад."""
        row = self.conn.execute(
            "SELECT cover_url, video_url, description, searched_at FROM media WHERE game_id = ?", (game_id,)
        ).fetchone()
        if row is None:
            return True
        if (not cover or row["cover_url"]) and (not video or row["video_url"]) and (not description or row["description"]):
            return False
        return (now or utc_now()) - datetime.fromisoformat(row["searched_at"]) >= retry_after

    def media_backlog(self, limit: int) -> list[sqlite3.Row]:
        """Игры базы без обложки, видео или описания с Metacritic, которых давно (или ещё ни разу)
        не спрашивали в магазинах: самые давние первыми. Срок повтора проверяет вызывающий."""
        return self.conn.execute(
            """
            SELECT g.*, m.searched_at AS media_searched_at FROM games g LEFT JOIN media m ON m.game_id = g.id
            WHERE (g.cover_url IS NULL OR g.cover_url = '' OR g.video_url IS NULL OR g.video_url = ''
                   OR g.description IS NULL OR trim(g.description) = '')
              AND (m.game_id IS NULL
                   OR (g.cover_url IS NULL OR g.cover_url = '') AND m.cover_url IS NULL
                   OR (g.video_url IS NULL OR g.video_url = '') AND m.video_url IS NULL
                   OR (g.description IS NULL OR trim(g.description) = '') AND m.description IS NULL)
            ORDER BY m.searched_at IS NOT NULL, m.searched_at, g.id LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def save_media(self, game_id: int, **fields: str | None) -> None:
        """Обложка, трейлер и описание из магазинов; пустые поля значат "искали, не нашли", это тоже
        запоминается для паузы до повтора. fields: cover_url, cover_kind, cover_source, cover_page, video_url,
        video_title, video_poster, video_source, video_page, description, description_source."""
        columns = (
            "cover_url", "cover_kind", "cover_source", "cover_page",
            "video_url", "video_title", "video_poster", "video_source", "video_page",
            "description", "description_source", "note",
        )
        unknown = set(fields) - set(columns)
        if unknown:
            raise ValueError(f"unknown media fields: {sorted(unknown)}")
        values = [fields.get(name) for name in columns]
        assignments = ", ".join(f"{name} = excluded.{name}" for name in (*columns, "searched_at"))
        with self._transaction() as conn:
            conn.execute(
                f"INSERT INTO media (game_id, {', '.join(columns)}, searched_at) VALUES (?, {', '.join('?' * len(columns))}, ?)"
                f" ON CONFLICT (game_id) DO UPDATE SET {assignments}",
                (game_id, *values, _ts(utc_now())),
            )

    def last_run_started(self) -> datetime | None:
        value = self.conn.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
        return datetime.fromisoformat(value) if value else None

    def run(self, run_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
