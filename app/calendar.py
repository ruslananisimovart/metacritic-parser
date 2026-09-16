"""Календарь мониторинга: по дням месяца что было (прогоны, игры, летсплеи, ошибки) и что будет
(плановые запуски, старые игры на обновление, повторные поиски летсплеев, повторные запросы к магазинам).

Прошлое берётся из базы по местной дате события. Будущее считается из расписания и настроек:
старые игры созревают по правилу stale_border, летсплеи без ролика по сроку повторного поиска,
магазины по сроку повтора. Сегодня это и то и другое: что уже прошло и что ещё запланировано.
"""

import calendar as _cal
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.runconfig import LetsplayConfig, RunConfig
from app.schedule import ScheduleConfig, next_runs, rule_text
from app.steam import RETRY_AFTER as MEDIA_RETRY_AFTER

# сколько плановых запусков вперёд считаем: почасовое расписание даёт до 24 в день на месяц вперёд
MAX_PLANNED = 24 * 45


def _local_day(iso: str, tz: ZoneInfo) -> date:
    return datetime.fromisoformat(iso).astimezone(tz).date()


def month_days(year: int, month: int) -> tuple[date, date]:
    last = _cal.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def build(
    db_path: Path, *, year: int, month: int, tz: ZoneInfo, today: date, now: datetime,
    schedule: ScheduleConfig, run: RunConfig, letsplay: LetsplayConfig, scheduler_active: bool,
) -> dict[str, Any]:
    """Считается в потоке пула, поэтому у календаря своё соединение с базой, как у чтения каталога."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _build(conn, year=year, month=month, tz=tz, today=today, now=now, schedule=schedule, run=run,
                      letsplay=letsplay, scheduler_active=scheduler_active)
    finally:
        conn.close()


def _build(
    conn: sqlite3.Connection, *, year: int, month: int, tz: ZoneInfo, today: date, now: datetime,
    schedule: ScheduleConfig, run: RunConfig, letsplay: LetsplayConfig, scheduler_active: bool,
) -> dict[str, Any]:
    first, last = month_days(year, month)
    days: dict[date, dict[str, Any]] = {
        first + timedelta(days=i): {"runs": 0, "created": 0, "updated": 0, "rechecked": 0, "failed": 0,
                                    "summaries": 0, "letsplays": 0, "letsplays_missing": 0, "errors": 0, "planned": 0,
                                    "stale_due": 0, "letsplay_due": 0, "media_due": 0,
                                    # прогоны дня по времени: факт (время и сколько игр) и план (время)
                                    "run_list": [], "planned_times": []}
        for i in range((last - first).days + 1)
    }
    lo = _iso(datetime.combine(first, datetime.min.time(), tzinfo=tz))
    hi = _iso(datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz))

    # прошлое: прогоны и их счётчики по дню старта
    for row in conn.execute(
        "SELECT started_at, status, created, updated, rechecked, failed FROM runs"
        " WHERE started_at >= ? AND started_at < ? ORDER BY started_at", (lo, hi)
    ):
        cell = days.get(_local_day(row["started_at"], tz))
        if cell is None:
            continue
        cell["runs"] += 1
        cell["created"] += row["created"] or 0
        cell["updated"] += row["updated"] or 0
        cell["rechecked"] += row["rechecked"] or 0
        cell["failed"] += row["failed"] or 0
        cell["run_list"].append({
            "time": datetime.fromisoformat(row["started_at"]).astimezone(tz).strftime("%H:%M"),
            "games": (row["created"] or 0) + (row["updated"] or 0) + (row["rechecked"] or 0),
            "status": row["status"],
        })
    for row in conn.execute("SELECT updated_at FROM review_summaries WHERE updated_at >= ? AND updated_at < ?", (lo, hi)):
        cell = days.get(_local_day(row["updated_at"], tz))
        if cell is not None:
            cell["summaries"] += 1
    # летсплеи по дню итога: найденные и те, где летсплея честно нет
    for row in conn.execute(
        "SELECT status, updated_at FROM letsplays WHERE status IN ('done', 'not_found') AND updated_at >= ? AND updated_at < ?",
        (lo, hi),
    ):
        cell = days.get(_local_day(row["updated_at"], tz))
        if cell is not None:
            cell["letsplays" if row["status"] == "done" else "letsplays_missing"] += 1
    for row in conn.execute("SELECT created_at FROM failures WHERE created_at >= ? AND created_at < ?", (lo, hi)):
        cell = days.get(_local_day(row["created_at"], tz))
        if cell is not None:
            cell["errors"] += 1

    # будущее: плановые запуски по расписанию, начиная с текущего момента
    if schedule.enabled and scheduler_active:
        for moment in next_runs(schedule, now, tz, MAX_PLANNED):
            day = moment.astimezone(tz).date()
            if day > last:
                break
            cell = days.get(day)
            if cell is not None:
                cell["planned"] += 1
                cell["planned_times"].append(moment.astimezone(tz).strftime("%H:%M"))

    # старые игры: в какой день созревают по сроку (обновлённые в день D созревают в D + срок);
    # всё, что созрело до начала месяца или до сегодня, ложится на ближайший показанный день
    if run.stale:
        _spread(
            days, today, last, run.stale_days,
            (row["updated_at"] for row in conn.execute("SELECT updated_at FROM games")), tz, "stale_due",
        )
    # летсплеи без ролика: повторный поиск по сроку из настроек
    if letsplay.recheck:
        rows = conn.execute(
            "SELECT updated_at, searches FROM letsplays WHERE status = 'not_found'"
        ).fetchall()
        due_rows = [r["updated_at"] for r in rows if not letsplay.recheck_max or (r["searches"] or 0) <= letsplay.recheck_max]
        _spread(days, today, last, letsplay.recheck_days, due_rows, tz, "letsplay_due")
    # магазины: игры, у которых чего-то нет, спрашиваются снова через неделю после прошлого запроса
    media_rows = conn.execute(
        "SELECT m.searched_at FROM media m JOIN games g ON g.id = m.game_id"
        " WHERE (g.cover_url IS NULL OR g.cover_url = '') AND m.cover_url IS NULL"
        "    OR (g.video_url IS NULL OR g.video_url = '') AND m.video_url IS NULL"
        "    OR (g.description IS NULL OR trim(g.description) = '') AND m.description IS NULL"
    ).fetchall()
    _spread(days, today, last, MEDIA_RETRY_AFTER.days, (r["searched_at"] for r in media_rows), tz, "media_due")

    # средний темп за две недели: сколько игр, резюме и летсплеев даёт один прогон; для прогноза
    since = _iso(datetime.combine(today - timedelta(days=14), datetime.min.time(), tzinfo=tz))
    done_runs = conn.execute(
        "SELECT COUNT(*) AS n, SUM(created + updated + rechecked) AS games, SUM(summaries) AS summaries FROM runs"
        " WHERE status = 'done' AND started_at >= ?", (since,)
    ).fetchone()
    found = conn.execute(
        "SELECT COUNT(*) FROM letsplays WHERE status = 'done' AND updated_at >= ?", (since,)
    ).fetchone()[0]
    n = done_runs["n"] or 0
    pace = {
        "runs": n,
        "games": round((done_runs["games"] or 0) / n, 1) if n else 0,
        "summaries": round((done_runs["summaries"] or 0) / n, 1) if n else 0,
        "letsplays": round(found / n, 2) if n else 0,
    }
    return {
        "year": year,
        "month": month,
        "today": today.isoformat(),
        "pace": pace,
        "schedule_rule": rule_text(schedule) if schedule.enabled and scheduler_active else None,
        "stale_days": run.stale_days if run.stale else None,
        "days": [{"date": day.isoformat(), **cell} for day, cell in sorted(days.items())],
    }


def _spread(days: dict[date, dict[str, Any]], today: date, last: date, period_days: int,
            stamps: Any, tz: ZoneInfo, key: str) -> None:
    """Раскладывает будущие события по дням: событие в день D + period; всё просроченное ложится на сегодня."""
    counts: dict[date, int] = defaultdict(int)
    for iso in stamps:
        if not iso:
            continue
        due = _local_day(iso, tz) + timedelta(days=period_days)
        if due < today:
            due = today
        if due > last:
            continue
        counts[due] += 1
    for day, n in counts.items():
        cell = days.get(day)
        if cell is not None:
            cell[key] += n


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
