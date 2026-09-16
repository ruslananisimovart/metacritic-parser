"""Чтение каталога для API: список с поиском, фильтром и сортировкой, карточка игры, похожие игры."""

import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from app.letsplay import timecode
from app.runconfig import LETSPLAY_KEY, LetsplayConfig
from app.runconfig import load_letsplay
from app.similar import GameFeatures, SimilarityIndex

SortField = Literal["metascore", "userscore", "release_date", "title"]
SortOrder = Literal["asc", "desc"]
SIMILAR_LIMIT = 5

_SORT_COLUMNS: dict[str, str] = {
    "metascore": "g.metascore",
    "userscore": "g.userscore",
    "release_date": "g.release_date",
    "title": "g.title COLLATE NOCASE",
}


def _seconds_between(start: str, end: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return 0.0


def _per_game(runs: Sequence[sqlite3.Row]) -> float | None:
    """Среднее время прогона на одну игру по набору завершённых прогонов."""
    selected = sum(r["selected"] for r in runs)
    seconds = sum(_seconds_between(r["started_at"], r["finished_at"]) for r in runs)
    return round(seconds / selected, 1) if selected else None


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# запрос с данными магазинов: у игр без обложки или видео Metacritic они берутся из таблицы media
_WITH_MEDIA = (
    "g.*, m.cover_url AS m_cover_url, m.cover_kind AS m_cover_kind, m.cover_source AS m_cover_source,"
    " m.video_url AS m_video_url, m.video_title AS m_video_title, m.video_poster AS m_video_poster,"
    " m.video_source AS m_video_source, m.video_page AS m_video_page,"
    " m.description AS m_description, m.description_source AS m_description_source,"
    " {complete} AS is_complete"
    " FROM games g LEFT JOIN media m ON m.game_id = g.id"
)


def _description(row: sqlite3.Row) -> dict[str, Any]:
    """Описание карточки: с Metacritic, иначе со страницы магазина (Steam или Google Play)."""
    if (row["description"] or "").strip():
        return {"description": row["description"], "description_source": "metacritic"}
    if row["m_description"]:
        return {"description": row["m_description"], "description_source": row["m_description_source"] or "steam"}
    return {"description": None, "description_source": None}


# фильтры каталога по готовности материалов: у игры есть то, что отмечено; отметки складываются через "и".
# Обложка, описание и видео считаются с Metacritic или из магазинов
HAS_FILTERS: dict[str, str] = {
    "cover": "(COALESCE(g.cover_url, '') <> '' OR EXISTS (SELECT 1 FROM media m WHERE m.game_id = g.id AND m.cover_url IS NOT NULL))",
    "description": "(TRIM(COALESCE(g.description, '')) <> ''"
                   " OR EXISTS (SELECT 1 FROM media m WHERE m.game_id = g.id AND m.description IS NOT NULL))",
    "video": "(COALESCE(g.video_url, '') <> '' OR EXISTS (SELECT 1 FROM media m WHERE m.game_id = g.id AND m.video_url IS NOT NULL))",
    "scores": "(g.metascore IS NOT NULL OR g.userscore IS NOT NULL)",
    "critic_summary": "EXISTS (SELECT 1 FROM review_summaries s WHERE s.game_id = g.id AND s.kind = 'critic')",
    "user_summary": "EXISTS (SELECT 1 FROM review_summaries s WHERE s.game_id = g.id AND s.kind = 'user')",
    "letsplay": "EXISTS (SELECT 1 FROM letsplays l WHERE l.game_id = g.id AND l.status = 'done')",
}
# полный анализ: игра прошла все этапы, что для неё возможны. Обложка, описание, видео, резюме по каждой
# стороне, у которой есть отзывы (и хотя бы одно резюме), найденный летсплей
HAS_FILTERS["complete"] = "(" + " AND ".join((
    HAS_FILTERS["cover"], HAS_FILTERS["description"], HAS_FILTERS["video"], HAS_FILTERS["letsplay"],
    "EXISTS (SELECT 1 FROM review_summaries s WHERE s.game_id = g.id)",
    f"(COALESCE(g.critic_reviews, 0) = 0 OR {HAS_FILTERS['critic_summary']})",
    f"(COALESCE(g.user_reviews, 0) = 0 OR {HAS_FILTERS['user_summary']})",
)) + ")"
# признак полного анализа считается прямо в запросе списка и карточки: значок на обложке
_WITH_MEDIA = _WITH_MEDIA.format(complete=HAS_FILTERS["complete"])


def _cover(row: sqlite3.Row) -> dict[str, Any]:
    """Обложка карточки: с Metacritic, иначе из магазина. kind: portrait (постер), header (широкая шапка
    Steam), icon (квадратная иконка Google Play)."""
    if row["cover_url"]:
        return {"cover_url": row["cover_url"], "cover_source": "metacritic", "cover_kind": "portrait"}
    store = row["m_cover_url"] if "m_cover_url" in row.keys() else None
    if store:
        return {"cover_url": store, "cover_source": row["m_cover_source"] or "steam", "cover_kind": row["m_cover_kind"] or "header"}
    return {"cover_url": None, "cover_source": None, "cover_kind": None}


def _video(row: sqlite3.Row) -> dict[str, Any]:
    """Видео карточки: ролик Metacritic главнее; трейлер из Steam (поток HLS с кадром-заставкой) только там, где его нет."""
    if row["video_url"]:
        return {
            "video_url": row["video_url"], "video_title": row["video_title"], "video_source": "metacritic",
            "video_poster": None, "video_page": row["video_url"],
        }
    if row["m_video_url"]:
        return {
            "video_url": row["m_video_url"], "video_title": row["m_video_title"], "video_source": row["m_video_source"] or "steam",
            "video_poster": row["m_video_poster"], "video_page": row["m_video_page"],
        }
    return {"video_url": None, "video_title": None, "video_source": None, "video_poster": None, "video_page": None}


def _brief(row: sqlite3.Row, platforms: list[str]) -> dict[str, Any]:
    return {
        "slug": row["slug"],
        "title": row["title"],
        **_cover(row),
        "release_date": row["release_date"],
        "metascore": row["metascore"],
        "userscore": row["userscore"],
        "critic_reviews": row["critic_reviews"] or 0,
        "user_reviews": row["user_reviews"] or 0,
        "main_platform": row["main_platform"],
        "platforms": platforms,
        "genres": json.loads(row["genres"]),
        "developers": json.loads(row["developers"]),
        # в списке для значка "летсплей"; в карточке игры летсплей отдаётся целиком отдельным полем
        "letsplay_status": row["letsplay_status"] if "letsplay_status" in row.keys() else None,
        # полный анализ: все этапы, что возможны для игры, пройдены (значок на обложке)
        "complete": bool(row["is_complete"]) if "is_complete" in row.keys() else False,
    }


def _source(raw: dict[str, Any]) -> dict[str, Any]:
    """Отзыв-источник пункта резюме: автор или издание, оценка, дата, цитата и ссылка."""
    score = raw.get("score")
    return {
        "author": raw.get("author") or None,
        "score": float(score) if isinstance(score, (int, float)) else None,
        "date": raw.get("date") or None,
        "quote": str(raw.get("quote") or "").strip(),
        "url": raw.get("url") or None,
    }


def _summary_points(raw: str | None) -> list[dict[str, Any]]:
    """Пункты резюме с источниками. В записях до появления источников пункты лежат строками."""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for point in items if isinstance(items, list) else []:
        if isinstance(point, str):
            text, sources = point.strip(), []
        elif isinstance(point, dict):
            text, sources = str(point.get("text") or "").strip(), point.get("sources") or []
        else:
            continue
        if text:
            out.append({"text": text, "sources": [_source(s) for s in sources if isinstance(s, dict)]})
    return out


def _summary(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "likes": _summary_points(row["likes"]),
        "dislikes": _summary_points(row["dislikes"]),
        "summary": row["summary"],
        "reviews_used": row["reviews_used"],
        "reviews_total": row["reviews_total"],
        # какая модель писала резюме: интерфейс подписывает "Составлено ИИ (модель)"
        "model": row["model"],
        "updated_at": row["updated_at"],
    }


_RUN_COLUMNS = (
    "id, trigger, day, status, selected, created, updated, failed, summaries, media_checked, media_found,"
    " summaries_checked, lp_total, lp_found, lp_not_found, lp_failed, stale_selected, rechecked, error, started_at,"
    " finished_at"
)


# понятная причина неудачи летсплея по тексту ошибки модулей летсплеев; порядок важен, частное раньше общего
_FAILED_REASONS = (
    ("age-restricted", "у ролика возрастное ограничение, звук недоступен без входа в аккаунт"),
    ("live video", "ролик оказался трансляцией"),
    ("audio is too large", "звуковая дорожка ролика слишком большая"),
    ("временный сбой youtube", "YouTube временно не отдал поиск, сервис повторит сам"),
    ("no videos in the search page", "YouTube не отдал результаты поиска"),
    ("ytinitialdata not found", "YouTube не отдал результаты поиска"),
    ("unknown layout", "YouTube поменял страницу поиска"),
    ("заключение не получилось", "модель не смогла составить заключение"),
    ("no speech recognized", "в звуке ролика не распознана речь"),
    ("whisper", "сбой расшифровки Whisper"),
    ("was not downloaded", "звук ролика не скачался"),
    ("yt-dlp", "не удалось скачать звук ролика"),
    ("no transcript", "у ролика не получилось взять ни звук, ни субтитры"),
    ("search", "поиск на YouTube не удался"),
    ("gemini", "модель была недоступна"),
)
_VIDEO_PREFIX = re.compile(r"^[\w-]{11}:\s*")


def _letsplay_reason(status: str, error: str | None) -> str | None:
    if not error:
        return None
    if status == "not_found":
        # отказы по роликам приходят как "id: причина; id: причина", id человеку не нужны
        parts = [_VIDEO_PREFIX.sub("", part.strip()) for part in error.split(";")]
        return "; ".join(dict.fromkeys(part for part in parts if part)) or None
    lowered = error.lower()
    return next((reason for marker, reason in _FAILED_REASONS if marker in lowered), "не удалось обработать ролик")


def _segments(raw: str | None) -> list[dict[str, Any]]:
    """Сегменты расшифровки [начало, конец, текст] в виде [{start, text}] для списка с таймкодами."""
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, list) and len(item) >= 3 and isinstance(item[2], str) and item[2].strip():
            out.append({"start": float(item[0]), "text": item[2].strip()})
    return out


def _moment(raw: Any, url: str | None) -> dict[str, Any] | None:
    """Момент ролика для интерфейса: подпись, цитата и ссылка на YouTube с переходом к этому времени."""
    if not isinstance(raw, dict) or raw.get("seconds") is None:
        return None
    try:
        seconds = max(0.0, float(raw["seconds"]))
    except (TypeError, ValueError):
        return None
    return {
        "seconds": seconds,
        "label": timecode(seconds),
        "quote": str(raw.get("quote") or "").strip(),
        "url": f"{url}&t={int(seconds)}s" if url else None,
    }


def _points(raw: Any, url: str | None) -> list[dict[str, Any]]:
    """Пункты заключения с моментами. В записях до появления таймкодов пункты лежат строками."""
    out: list[dict[str, Any]] = []
    for point in raw if isinstance(raw, list) else []:
        if isinstance(point, str):
            text, moments = point.strip(), []
        elif isinstance(point, dict):
            text, moments = str(point.get("text") or "").strip(), point.get("moments") or []
        else:
            continue
        if text:
            out.append({"text": text, "moments": [m for m in (_moment(item, url) for item in moments) if m]})
    return out


def _conclusion(raw: str | None, url: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "summary": str(data.get("summary") or ""),
        "verdict": str(data.get("verdict") or ""),
        "pros": _points(data.get("pros"), url),
        "cons": _points(data.get("cons"), url),
    }


def _next_search(row: sqlite3.Row, recheck: LetsplayConfig) -> str | None:
    """Когда игра без летсплея пойдёт в поиск снова по правилу из настроек; None, если повторный поиск
    выключен или лимит на игру исчерпан."""
    if row["status"] != "not_found" or not recheck.recheck:
        return None
    searches = row["searches"] if "searches" in row.keys() else 0
    if recheck.recheck_max and searches > recheck.recheck_max:
        return None
    moment = datetime.fromisoformat(row["updated_at"]) + timedelta(days=recheck.recheck_days)
    return moment.isoformat(timespec="seconds")


def _letsplay(
    row: sqlite3.Row | None, *, whisper_window: float, head_seconds: float, tail_seconds: float, max_attempts: int,
    recheck: LetsplayConfig,
) -> dict[str, Any] | None:
    if row is None:
        return None
    duration = row["duration"]
    coverage = row["coverage"] if "coverage" in row.keys() else None
    if coverage:
        partial = coverage == "edges"
    else:
        # старые записи: Whisper тогда всегда брал только начало и концовку длинного ролика
        partial = row["transcript_source"] == "whisper" and bool(duration) and duration > whisper_window
    return {
        "status": row["status"],
        "url": row["url"],
        "video_id": row["video_id"],
        "title": row["title"],
        "channel": row["channel"],
        "views": row["views"],
        "duration": duration,
        "published": row["published"],
        "language": row["language"],
        "transcript_source": row["transcript_source"],
        # Whisper расшифровывает у длинного ролика только начало и концовку
        "partial": partial,
        "coverage": {"head_minutes": round(head_seconds / 60), "tail_minutes": round(tail_seconds / 60)} if partial else None,
        "conclusion": _conclusion(row["conclusion"], row["url"]),
        "segments": _segments(row["segments"]) if row["status"] == "done" else [],
        "attempts": row["attempts"],
        "max_attempts": max_attempts,
        "error": row["error"],
        "reason": _letsplay_reason(row["status"], row["error"]),
        "updated_at": row["updated_at"],
        # для "летсплея нет": когда следующий поиск, и включён ли он вообще
        "next_search_at": _next_search(row, recheck),
        "recheck": recheck.recheck,
    }


class Catalog:
    def __init__(
        self,
        db_path: Path,
        whisper_window: float = 40 * 60,
        *,
        head_seconds: float | None = None,
        tail_seconds: float = 0.0,
        max_attempts: int = 3,
    ) -> None:
        self._db_path = db_path
        self._whisper_window = whisper_window
        self._head_seconds = whisper_window - tail_seconds if head_seconds is None else head_seconds
        self._tail_seconds = tail_seconds
        self._max_attempts = max_attempts
        self._index_lock = threading.Lock()
        self._index: SimilarityIndex | None = None
        self._index_key: tuple[Any, ...] | None = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # отдельное соединение на запрос: эндпоинты работают в пуле потоков, а пишет в базу прогон
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = ON")
            yield conn
        finally:
            conn.close()

    def games(
        self, *, q: str | None, platform: str | None, sort: SortField, order: SortOrder, limit: int, offset: int,
        has: Sequence[str] = (),
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if q and q.strip():
            where.append("g.title LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(q.strip())}%")
        if platform:
            where.append("EXISTS (SELECT 1 FROM game_platforms p WHERE p.game_id = g.id AND p.slug = ?)")
            params.append(platform)
        # числа в меню фильтров: сколько игр с каждой отметкой при тех же поиске и платформе
        base = list(where)
        where.extend(HAS_FILTERS[key] for key in has if key in HAS_FILTERS)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        column = _SORT_COLUMNS[sort]
        direction = "ASC" if order == "asc" else "DESC"
        # игры без оценки в конце при любом направлении, при равенстве по названию
        order_by = f"{column} IS NULL, {column} {direction}, g.title COLLATE NOCASE, g.id"
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM games g {clause}", params).fetchone()[0]
            facets = {
                key: conn.execute(f"SELECT COUNT(*) FROM games g WHERE {' AND '.join([*base, condition])}", params).fetchone()[0]
                for key, condition in HAS_FILTERS.items()
            }
            rows = conn.execute(
                f"SELECT l.status AS letsplay_status, {_WITH_MEDIA} LEFT JOIN letsplays l ON l.game_id = g.id "
                f"{clause} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            names = self._platform_names(conn, [row["id"] for row in rows])
        return {
            "total": total,
            "facets": facets,
            "items": [_brief(row, names.get(row["id"], [])) for row in rows],
        }

    def game(self, slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(f"SELECT {_WITH_MEDIA} WHERE g.slug = ?", (slug,)).fetchone()
            if row is None:
                return None
            platforms = conn.execute(
                "SELECT * FROM game_platforms WHERE game_id = ? ORDER BY is_main DESC, name", (row["id"],)
            ).fetchall()
            summaries = {
                summary["kind"]: summary
                for summary in conn.execute("SELECT * FROM review_summaries WHERE game_id = ?", (row["id"],))
            }
            similar = self._similar(conn, row["id"])
            letsplay = conn.execute("SELECT * FROM letsplays WHERE game_id = ?", (row["id"],)).fetchone()
            # правило повторного поиска из окна "Расписание и прогон": для даты следующего поиска
            setting = conn.execute("SELECT value FROM settings WHERE key = ?", (LETSPLAY_KEY,)).fetchone()
        recheck = load_letsplay(setting["value"] if setting else None)
        return {
            **_brief(row, [p["name"] for p in platforms]),
            "letsplay_status": letsplay["status"] if letsplay else None,
            **_description(row),
            "rating": row["rating"],
            "publishers": json.loads(row["publishers"]),
            **_video(row),
            "metacritic_url": f"https://www.metacritic.com/game/{row['slug']}/",
            "updated_at": row["updated_at"],
            "platform_scores": [
                {
                    "name": p["name"],
                    "slug": p["slug"],
                    "is_main": bool(p["is_main"]),
                    "metascore": p["metascore"],
                    "critic_reviews": p["critic_reviews"],
                    "userscore": p["userscore"],
                    "user_reviews": p["user_reviews"],
                }
                for p in platforms
            ],
            "summaries": {kind: _summary(summaries.get(kind)) for kind in ("critic", "user")},
            "similar": similar,
            "letsplay": _letsplay(
                letsplay,
                whisper_window=self._whisper_window,
                head_seconds=self._head_seconds,
                tail_seconds=self._tail_seconds,
                max_attempts=self._max_attempts,
                recheck=recheck,
            ),
        }

    def platforms(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, MIN(name) AS name, COUNT(*) AS games FROM game_platforms "
                "GROUP BY slug ORDER BY games DESC, name"
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            run = conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return {"games": games, "last_run": dict(run) if run else None}

    def monitor(self, day: str, max_failures: int) -> dict[str, Any]:
        """Данные мониторинга из базы: история прогонов, ошибки, счётчики за день, летсплеи."""
        with self._connect() as conn:
            games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
            runs = [dict(r) for r in conn.execute(f"SELECT {_RUN_COLUMNS} FROM runs ORDER BY id DESC LIMIT 20")]
            runs_total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            # список ошибок и счётчик "за сутки" считают одно и то же окно: последние 24 часа
            failures = [
                dict(r)
                for r in conn.execute(
                    "SELECT f.created_at, f.slug, g.title, f.stage, f.error FROM failures f "
                    "LEFT JOIN games g ON g.slug = f.slug WHERE f.created_at >= ? ORDER BY f.id DESC LIMIT 20",
                    ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"),),
                )
            ]
            by_source = {
                r["source"]: r["n"]
                for r in conn.execute(
                    "SELECT source, COUNT(*) AS n FROM processed WHERE day = ? GROUP BY source", (day,)
                )
            }
            skipped = [
                r["slug"]
                for r in conn.execute(
                    "SELECT slug FROM failures WHERE day = ? AND stage = 'metacritic' "
                    "GROUP BY slug HAVING COUNT(*) >= ?",
                    (day, max_failures),
                )
            ]
            letsplays = {
                r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM letsplays GROUP BY status")
            }
            summaries = {
                r["kind"]: r["n"] for r in conn.execute("SELECT kind, COUNT(*) AS n FROM review_summaries GROUP BY kind")
            }
            failures_today = conn.execute("SELECT COUNT(*) FROM failures WHERE day = ?", (day,)).fetchone()[0]
            timing_sql = (
                "SELECT started_at, finished_at, selected FROM runs "
                "WHERE status = 'done' AND finished_at IS NOT NULL AND selected > 0 ORDER BY id DESC LIMIT 10 OFFSET ?"
            )
            timing = conn.execute(timing_sql, (0,)).fetchall()
            timing_prev = conn.execute(timing_sql, (10,)).fetchall()
            recent = self._recent_games(conn)
            # показатели мониторинга за сутки и за сутки до них: из них интерфейс считает изменение
            now = datetime.now(timezone.utc)
            day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
            two_days_ago = (now - timedelta(days=2)).isoformat(timespec="seconds")

            def count(sql: str, *args: Any) -> int:
                return int(conn.execute(sql, args).fetchone()[0])

            kpi = {
                "processed_24h": count("SELECT COUNT(*) FROM processed WHERE processed_at >= ?", day_ago),
                "processed_prev_24h": count(
                    "SELECT COUNT(*) FROM processed WHERE processed_at >= ? AND processed_at < ?", two_days_ago, day_ago
                ),
                "summaries_24h": count("SELECT COUNT(*) FROM review_summaries WHERE updated_at >= ?", day_ago),
                "failures_24h": count("SELECT COUNT(*) FROM failures WHERE created_at >= ?", day_ago),
                "failures_prev_24h": count(
                    "SELECT COUNT(*) FROM failures WHERE created_at >= ? AND created_at < ?", two_days_ago, day_ago
                ),
            }
        kpi["seconds_per_game_prev"] = _per_game(timing_prev)
        return {
            "games": games,
            "today": {
                "day": day,
                "new_releases": by_source.get("new_releases", 0),
                "see_all": by_source.get("see_all", 0),
                "skipped_until_tomorrow": skipped,
            },
            "runs": runs,
            "runs_total": runs_total,
            "failures": failures,
            "failures_today": failures_today,
            "letsplays": letsplays,
            "summaries": {"critic": summaries.get("critic", 0), "user": summaries.get("user", 0)},
            # время прогона целиком (Metacritic, трейлеры, резюме) на одну игру, по последним прогонам
            "seconds_per_game": _per_game(timing),
            "kpi": kpi,
            "recent": recent,
        }

    def runs_page(self, status: str, limit: int, offset: int) -> dict[str, Any]:
        """История прогонов постранично: все, успешные (и идущий) или с ошибками и прерванные."""
        where = {
            "all": "",
            "ok": "WHERE status IN ('done', 'running')",
            "bad": "WHERE status IN ('failed', 'interrupted')",
        }[status]
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM runs {where}").fetchone()[0]
            rows = conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs {where} ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return {"total": total, "items": [dict(row) for row in rows]}

    @staticmethod
    def _recent_games(conn: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
        """Игры последнего прогона для блока "Последние проверки": что сохранилось и на чём упало."""
        last = conn.execute("SELECT id, started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return []
        failed: dict[str, list[str]] = {}
        for row in conn.execute("SELECT slug, stage FROM failures WHERE run_id = ? ORDER BY id", (last["id"],)):
            failed.setdefault(row["slug"], []).append(row["stage"])
        rows = conn.execute(
            "SELECT p.slug, p.processed_at AS at, g.title, g.metascore, g.userscore, g.critic_reviews, g.user_reviews, "
            "(SELECT COUNT(*) FROM review_summaries s WHERE s.game_id = g.id) AS summaries, l.status AS letsplay_status "
            "FROM processed p JOIN games g ON g.id = p.game_id LEFT JOIN letsplays l ON l.game_id = g.id "
            "WHERE p.run_id = ? ORDER BY p.processed_at DESC, g.title LIMIT ?",
            (last["id"], limit),
        ).fetchall()
        items = [{**dict(row), "failed_stages": failed.pop(row["slug"], [])} for row in rows]
        # игра, упавшая на загрузке с Metacritic, в processed не попадает: показываем её отдельно
        for slug, stages in failed.items():
            if "metacritic" not in stages or len(items) >= limit:
                continue
            title = conn.execute("SELECT title FROM games WHERE slug = ?", (slug,)).fetchone()
            items.append({
                "slug": slug, "at": last["started_at"], "title": title["title"] if title else None, "metascore": None,
                "userscore": None, "critic_reviews": 0, "user_reviews": 0, "summaries": 0, "letsplay_status": None,
                "failed_stages": stages,
            })
        return items

    @staticmethod
    def _platform_names(conn: sqlite3.Connection, game_ids: Sequence[int]) -> dict[int, list[str]]:
        if not game_ids:
            return {}
        marks = ", ".join("?" * len(game_ids))
        names: dict[int, list[str]] = {}
        rows = conn.execute(
            f"SELECT game_id, name FROM game_platforms WHERE game_id IN ({marks}) ORDER BY is_main DESC, name",
            list(game_ids),
        )
        for row in rows:
            names.setdefault(row["game_id"], []).append(row["name"])
        return names

    def _similar(self, conn: sqlite3.Connection, game_id: int) -> list[dict[str, Any]]:
        pairs = self._similarity_index(conn).similar(game_id, SIMILAR_LIMIT)
        if not pairs:
            return []
        ids = [other_id for other_id, _ in pairs]
        marks = ", ".join("?" * len(ids))
        rows = {row["id"]: row for row in conn.execute(f"SELECT {_WITH_MEDIA} WHERE g.id IN ({marks})", ids)}
        return [
            {
                "slug": rows[other_id]["slug"],
                "title": rows[other_id]["title"],
                **_cover(rows[other_id]),
                "metascore": rows[other_id]["metascore"],
                "userscore": rows[other_id]["userscore"],
                "score": round(score, 3),
            }
            for other_id, score in pairs
            if other_id in rows
        ]

    def _similarity_index(self, conn: sqlite3.Connection) -> SimilarityIndex:
        count, last_update = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM games").fetchone()
        key = (count, last_update)
        with self._index_lock:
            if self._index is None or self._index_key != key:
                self._index = SimilarityIndex(self._features(conn))
                self._index_key = key
            return self._index

    @staticmethod
    def _features(conn: sqlite3.Connection) -> list[GameFeatures]:
        platforms: dict[int, set[str]] = {}
        for row in conn.execute("SELECT game_id, slug FROM game_platforms"):
            platforms.setdefault(row["game_id"], set()).add(row["slug"])
        return [
            GameFeatures(
                id=row["id"],
                title=row["title"],
                genres=frozenset(json.loads(row["genres"])),
                developers=frozenset(json.loads(row["developers"])),
                platforms=frozenset(platforms.get(row["id"], ())),
                description=row["description"],
            )
            for row in conn.execute("SELECT id, title, genres, developers, description FROM games")
        ]
