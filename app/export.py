"""Выгрузка собранных данных в CSV: игры, резюме, летсплеи и история прогонов.

Файлы рассчитаны на импорт в таблицы как есть: UTF-8 с BOM (иначе Excel ломает кириллицу),
разделитель запятая, перевод строки CRLF, многострочные тексты в кавычках (RFC 4180).
Списки внутри ячейки (жанры, плюсы, минусы) склеены через "; ".

Читаем отдельным соединением только на чтение: выгрузка не мешает работе сервиса и одинаково
работает как с рабочей базой, так и со снимком базы из хранилища.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sqlite3
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FILES = ("all", "games", "summaries", "letsplays", "runs")
JOIN = "; "
KIND = {"critic": "критики", "user": "игроки"}
TRIGGER = {"schedule": "по расписанию", "manual": "вручную", "cli": "из консоли"}
RUN_STATUS = {"done": "завершён", "failed": "ошибка", "interrupted": "прерван", "running": "идёт"}
LETSPLAY_STATUS = {
    "done": "готов", "queued": "в очереди", "not_found": "не найден", "failed": "ошибка", "skipped": "пропущен",
}
TRANSCRIPT = {"whisper": "Whisper", "captions": "субтитры YouTube"}


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Таблицу летсплеев создаёт свой модуль: в снимке старой базы её может не быть."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _moment(value: Any, tz: ZoneInfo | None) -> str:
    """Время в виде "2026-09-12 14:55" по поясу сервиса: так его понимают и таблицы, и человек."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if tz is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz)
    return parsed.strftime("%Y-%m-%d %H:%M")


def _items(raw: Any) -> list[str]:
    """Список строк из JSON: пункты резюме и заключения лежат строками или объектами с полем text."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else []
        except ValueError:
            return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        text = item.strip() if isinstance(item, str) else str((item or {}).get("text") or "").strip()
        if text:
            out.append(text)
    return out


def _joined(raw: Any) -> str:
    return JOIN.join(_items(raw))


def _conclusion(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _minutes(seconds: Any) -> str:
    return "" if not seconds else str(round(float(seconds) / 60, 1))


def _games(conn: sqlite3.Connection, tz: ZoneInfo | None) -> tuple[list[str], Iterator[list[Any]]]:
    letsplays = _has_table(conn, "letsplays")
    media = _has_table(conn, "media")
    sql = f"""
        SELECT g.slug, g.title, g.release_date, g.rating, g.genres, g.developers, g.publishers, g.main_platform,
               g.metascore, g.critic_reviews, g.userscore, g.user_reviews, g.first_seen_at, g.updated_at,
               {"COALESCE(g.video_url, m.video_url) AS video, COALESCE(g.cover_url, m.cover_url) AS cover_url,"
                if media else "g.video_url AS video, g.cover_url,"}
               {"l.status AS letsplay, l.url AS letsplay_url," if letsplays else "NULL AS letsplay, NULL AS letsplay_url,"}
               (SELECT COUNT(*) FROM review_summaries r WHERE r.game_id = g.id) AS summaries
        FROM games g
        {"LEFT JOIN media m ON m.game_id = g.id" if media else ""}
        {"LEFT JOIN letsplays l ON l.game_id = g.id" if letsplays else ""}
        ORDER BY g.metascore IS NULL, g.metascore DESC, g.title
    """
    header = [
        "slug", "название", "дата выхода", "возрастной рейтинг", "жанры", "разработчики", "издатели",
        "основная платформа", "оценка критиков", "рецензий", "оценка игроков", "оценок", "резюме",
        "летсплей", "ссылка Metacritic", "ссылка на летсплей", "видео", "обложка", "добавлена", "обновлена",
    ]
    rows = (
        [
            row["slug"], row["title"], row["release_date"] or "", row["rating"] or "",
            _joined(row["genres"]), _joined(row["developers"]), _joined(row["publishers"]),
            row["main_platform"] or "", row["metascore"] if row["metascore"] is not None else "",
            row["critic_reviews"], row["userscore"] if row["userscore"] is not None else "", row["user_reviews"],
            row["summaries"], LETSPLAY_STATUS.get(row["letsplay"], row["letsplay"] or ""),
            f"https://www.metacritic.com/game/{row['slug']}/", row["letsplay_url"] or "", row["video"] or "",
            row["cover_url"] or "", _moment(row["first_seen_at"], tz), _moment(row["updated_at"], tz),
        ]
        for row in conn.execute(sql)
    )
    return header, rows


def _all(conn: sqlite3.Connection, tz: ZoneInfo | None) -> tuple[list[str], Iterator[list[Any]]]:
    """Всё об игре одной строкой: карточка, оба резюме и летсплей. Для тех, кому удобнее один лист."""
    letsplays = _has_table(conn, "letsplays")
    lp_columns = (
        "l.status AS lp_status, l.url AS lp_url, l.channel AS lp_channel, l.views AS lp_views,"
        " l.duration AS lp_duration, l.language AS lp_language, l.transcript_source AS lp_source,"
        " l.conclusion AS lp_conclusion, l.error AS lp_error"
        if letsplays else
        "NULL AS lp_status, NULL AS lp_url, NULL AS lp_channel, NULL AS lp_views,"
        " NULL AS lp_duration, NULL AS lp_language, NULL AS lp_source, NULL AS lp_conclusion, NULL AS lp_error"
    )
    media = _has_table(conn, "media")
    sql = f"""
        SELECT g.slug, g.title, g.release_date, g.rating, g.genres, g.developers, g.publishers, g.main_platform,
               g.metascore, g.critic_reviews, g.userscore, g.user_reviews, g.updated_at,
               {"COALESCE(g.video_url, m.video_url) AS video, COALESCE(g.cover_url, m.cover_url) AS cover_url,"
                if media else "g.video_url AS video, g.cover_url,"}
               c.likes AS c_likes, c.dislikes AS c_dislikes, c.summary AS c_summary,
               c.reviews_used AS c_used, c.reviews_total AS c_total, c.model AS c_model,
               u.likes AS u_likes, u.dislikes AS u_dislikes, u.summary AS u_summary,
               u.reviews_used AS u_used, u.reviews_total AS u_total, u.model AS u_model,
               {lp_columns}
        FROM games g
        {"LEFT JOIN media m ON m.game_id = g.id" if media else ""}
        LEFT JOIN review_summaries c ON c.game_id = g.id AND c.kind = 'critic'
        LEFT JOIN review_summaries u ON u.game_id = g.id AND u.kind = 'user'
        {"LEFT JOIN letsplays l ON l.game_id = g.id" if letsplays else ""}
        ORDER BY g.metascore IS NULL, g.metascore DESC, g.title
    """
    header = [
        "slug", "название", "дата выхода", "возрастной рейтинг", "жанры", "разработчики", "издатели",
        "основная платформа", "оценка критиков", "рецензий", "оценка игроков", "оценок",
        "плюсы (критики)", "минусы (критики)", "вывод (критики)", "отзывов критиков",
        "плюсы (игроки)", "минусы (игроки)", "вывод (игроки)", "отзывов игроков", "модель",
        "летсплей", "ссылка на летсплей", "канал", "просмотры", "длительность (мин)", "язык", "расшифровка",
        "о чём ролик", "итог ролика", "блогер хвалит", "блогер ругает", "ошибка летсплея",
        "ссылка Metacritic", "видео", "обложка", "обновлена",
    ]

    def used(row: sqlite3.Row, prefix: str) -> str:
        total = row[f"{prefix}_total"]
        return "" if total is None else f"{row[f'{prefix}_used']} из {total}"

    def build() -> Iterator[list[Any]]:
        for row in conn.execute(sql):
            done = _conclusion(row["lp_conclusion"])
            yield [
                row["slug"], row["title"], row["release_date"] or "", row["rating"] or "",
                _joined(row["genres"]), _joined(row["developers"]), _joined(row["publishers"]),
                row["main_platform"] or "", row["metascore"] if row["metascore"] is not None else "",
                row["critic_reviews"], row["userscore"] if row["userscore"] is not None else "", row["user_reviews"],
                _joined(row["c_likes"]), _joined(row["c_dislikes"]), row["c_summary"] or "", used(row, "c"),
                _joined(row["u_likes"]), _joined(row["u_dislikes"]), row["u_summary"] or "", used(row, "u"),
                row["c_model"] or row["u_model"] or "",
                LETSPLAY_STATUS.get(row["lp_status"], row["lp_status"] or ""), row["lp_url"] or "",
                row["lp_channel"] or "", row["lp_views"] if row["lp_views"] is not None else "",
                _minutes(row["lp_duration"]), row["lp_language"] or "",
                TRANSCRIPT.get(row["lp_source"], row["lp_source"] or ""),
                str(done.get("summary") or ""), str(done.get("verdict") or ""),
                _joined(done.get("pros")), _joined(done.get("cons")), row["lp_error"] or "",
                f"https://www.metacritic.com/game/{row['slug']}/", row["video"] or "", row["cover_url"] or "",
                _moment(row["updated_at"], tz),
            ]

    return header, build()


def _summaries(conn: sqlite3.Connection, tz: ZoneInfo | None) -> tuple[list[str], Iterator[list[Any]]]:
    sql = """
        SELECT g.slug, g.title, s.kind, s.likes, s.dislikes, s.summary, s.reviews_used, s.reviews_total,
               s.model, s.updated_at
        FROM review_summaries s
        JOIN games g ON g.id = s.game_id
        ORDER BY g.title, s.kind
    """
    header = [
        "slug", "название", "резюме по", "плюсы", "минусы", "вывод",
        "отзывов использовано", "отзывов всего", "модель", "обновлено",
    ]
    rows = (
        [
            row["slug"], row["title"], KIND.get(row["kind"], row["kind"]),
            _joined(row["likes"]), _joined(row["dislikes"]), row["summary"] or "",
            row["reviews_used"], row["reviews_total"], row["model"] or "", _moment(row["updated_at"], tz),
        ]
        for row in conn.execute(sql)
    )
    return header, rows


def _letsplays(conn: sqlite3.Connection, tz: ZoneInfo | None) -> tuple[list[str], Iterator[list[Any]]]:
    header = [
        "slug", "название", "статус", "ссылка", "канал", "просмотры", "длительность (мин)", "опубликован",
        "язык", "расшифровка", "попыток", "о чём ролик", "итог", "блогер хвалит", "блогер ругает",
        "ошибка", "обновлено",
    ]
    if not _has_table(conn, "letsplays"):
        return header, iter(())
    sql = """
        SELECT g.slug, g.title, l.status, l.url, l.channel, l.views, l.duration, l.published, l.language,
               l.transcript_source, l.attempts, l.conclusion, l.error, l.updated_at
        FROM letsplays l
        JOIN games g ON g.id = l.game_id
        ORDER BY g.title
    """

    def build() -> Iterator[list[Any]]:
        for row in conn.execute(sql):
            done = _conclusion(row["conclusion"])
            yield [
                row["slug"], row["title"], LETSPLAY_STATUS.get(row["status"], row["status"]), row["url"] or "",
                row["channel"] or "", row["views"] if row["views"] is not None else "", _minutes(row["duration"]),
                row["published"] or "", row["language"] or "",
                TRANSCRIPT.get(row["transcript_source"], row["transcript_source"] or ""), row["attempts"],
                str(done.get("summary") or ""), str(done.get("verdict") or ""),
                _joined(done.get("pros")), _joined(done.get("cons")),
                row["error"] or "", _moment(row["updated_at"], tz),
            ]

    return header, build()


def _runs(conn: sqlite3.Connection, tz: ZoneInfo | None) -> tuple[list[str], Iterator[list[Any]]]:
    sql = """
        SELECT id, trigger, day, status, selected, created, updated, failed, summaries, error,
               started_at, finished_at
        FROM runs ORDER BY id DESC
    """
    header = [
        "прогон", "запуск", "день", "статус", "выбрано игр", "новых", "обновлено", "ошибок", "резюме",
        "начало", "конец", "длительность (с)", "ошибка",
    ]

    def seconds(row: sqlite3.Row) -> str:
        if not row["finished_at"]:
            return ""
        try:
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["finished_at"])
        except ValueError:
            return ""
        return str(round((end - start).total_seconds()))

    rows = (
        [
            row["id"], TRIGGER.get(row["trigger"], row["trigger"]), row["day"],
            RUN_STATUS.get(row["status"], row["status"]), row["selected"], row["created"], row["updated"],
            row["failed"], row["summaries"], _moment(row["started_at"], tz), _moment(row["finished_at"], tz),
            seconds(row), row["error"] or "",
        ]
        for row in conn.execute(sql)
    )
    return header, rows


_DATASETS = {"all": _all, "games": _games, "summaries": _summaries, "letsplays": _letsplays, "runs": _runs}


def _dump(handle: Any, header: list[str], rows: Iterable[list[Any]]) -> int:
    writer = csv.writer(handle, lineterminator="\r\n")
    writer.writerow(header)
    count = 0
    for row in rows:
        writer.writerow(row)
        count += 1
    return count


def _write(path: Path, header: list[str], rows: Iterable[list[Any]]) -> int:
    # utf-8-sig и CRLF: так файл открывается и в Google Таблицах, и в Excel без плясок с кодировкой
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        return _dump(handle, header, rows)


def filename(name: str, tz: ZoneInfo | None = None, suffix: str = "csv") -> str:
    """Имя для скачивания: skytec-games-2026-09-14.csv."""
    stamp = (datetime.now(tz) if tz else datetime.now()).strftime("%Y-%m-%d")
    return f"skytec-{name}-{stamp}.{suffix}"


def render(source: Path, name: str, tz: ZoneInfo | None = None) -> bytes:
    """Один файл в память, как он лёг бы на диск: с BOM и CRLF."""
    conn = _connect(source)
    try:
        buffer = io.StringIO(newline="")
        _dump(buffer, *_DATASETS[name](conn, tz))
    finally:
        conn.close()
    return buffer.getvalue().encode("utf-8-sig")


def archive(source: Path, tz: ZoneInfo | None = None) -> bytes:
    """Все четыре файла одним zip: внутри те же имена, что на диске."""
    conn = _connect(source)
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for name in FILES:
                text = io.StringIO(newline="")
                _dump(text, *_DATASETS[name](conn, tz))
                zip_file.writestr(f"{name}.csv", text.getvalue().encode("utf-8-sig"))
    finally:
        conn.close()
    return buffer.getvalue()


def write(source: Path, target: Path, tz: ZoneInfo | None = None) -> dict[str, int]:
    """Кладёт в target четыре файла и возвращает число строк в каждом (без строки заголовка)."""
    target.mkdir(parents=True, exist_ok=True)
    conn = _connect(source)
    try:
        return {name: _write(target / f"{name}.csv", *_DATASETS[name](conn, tz)) for name in FILES}
    finally:
        conn.close()


def _rotate(root: Path, keep: int) -> None:
    """Оставляет keep последних копий прогонов, остальные удаляет. keep меньше единицы — не трогаем ничего."""
    if keep < 1:
        return
    folders = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("run-")),
        key=lambda p: p.stat().st_mtime,
    )
    for folder in folders[:-keep]:
        shutil.rmtree(folder, ignore_errors=True)


def save_run(
    source: Path, root: Path, *, run_id: int | None = None, tz: ZoneInfo | None = None, keep: int = 10
) -> dict[str, int]:
    """Свежий срез в exports/latest и его копия в папке прогона рядом.

    latest всегда перезаписывается, поэтому ссылка на файл не протухает. Копии прогонов нужны,
    чтобы видеть, каким каталог был раньше; лишние удаляются, остаются keep последних.
    """
    latest = root / "latest"
    counts = write(source, latest, tz)
    if run_id is not None:
        stamp = datetime.now(tz) if tz else datetime.now()
        folder = root / f"run-{run_id}-{stamp.strftime('%Y-%m-%d_%H-%M')}"
        folder.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            shutil.copy2(latest / f"{name}.csv", folder / f"{name}.csv")
        _rotate(root, keep)
    return counts
