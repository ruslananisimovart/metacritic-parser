"""Расписание прогонов: режимы, ближайшие запуски и подпись правила.

Время запуска задаётся в поясе сервиса (по умолчанию Москва), в базе и API моменты хранятся в UTC.
Режимы: каждый час в заданную минуту, каждый день, каждые N дней от даты отсчёта, раз в месяц.
"""

import calendar
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MODES = ("hourly", "daily", "days", "monthly")
MAX_EVERY_DAYS = 60
SETTING_KEY = "schedule"
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class ScheduleConfig:
    enabled: bool = True
    mode: str = "hourly"
    # ЧЧ:ММ в поясе сервиса; у почасового режима берутся только минуты
    time: str = "00:00"
    every: int = 2
    month_day: int = 1
    # дата отсчёта для режимов "каждый день" и "каждые N дней"; без неё отсчёт от сегодняшнего дня
    start: str | None = None

    def validated(self) -> "ScheduleConfig":
        if self.mode not in MODES:
            raise ScheduleError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not _TIME.match(self.time):
            raise ScheduleError(f"time must be HH:MM, got {self.time!r}")
        if not 1 <= self.every <= MAX_EVERY_DAYS:
            raise ScheduleError(f"every must be 1..{MAX_EVERY_DAYS}, got {self.every}")
        if not 1 <= self.month_day <= 31:
            raise ScheduleError(f"month_day must be 1..31, got {self.month_day}")
        if self.start is not None:
            try:
                date.fromisoformat(self.start)
            except ValueError:
                raise ScheduleError(f"start must be YYYY-MM-DD, got {self.start!r}") from None
        hours, minutes = self.time.split(":")
        return ScheduleConfig(
            enabled=self.enabled, mode=self.mode, time=f"{int(hours):02d}:{minutes}",
            every=self.every, month_day=self.month_day, start=self.start,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(raw: str | None) -> ScheduleConfig:
    """Расписание из таблицы settings; битая или устаревшая запись даёт расписание по умолчанию."""
    if not raw:
        return ScheduleConfig()
    try:
        data = json.loads(raw)
        return ScheduleConfig(**{k: v for k, v in data.items() if k in ScheduleConfig.__dataclass_fields__}).validated()
    except (ValueError, TypeError):
        return ScheduleConfig()


def _clock(config: ScheduleConfig) -> time:
    hours, minutes = config.time.split(":")
    return time(int(hours), int(minutes))


def _month_moment(year: int, month: int, config: ScheduleConfig, tz: ZoneInfo) -> datetime:
    # в коротком месяце 31-е число превращается в последнее
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    day = min(config.month_day, calendar.monthrange(year, month)[1])
    return datetime.combine(date(year, month, day), _clock(config), tz)


def _anchor(config: ScheduleConfig, local: datetime) -> tuple[datetime, int]:
    """Точка отсчёта и шаг в днях для режимов "каждый день" и "каждые N дней"."""
    step = 1 if config.mode == "daily" else config.every
    start = date.fromisoformat(config.start) if config.start else local.date()
    return datetime.combine(start, _clock(config), local.tzinfo), step


def next_runs(config: ScheduleConfig, now: datetime, tz: ZoneInfo, count: int = 3) -> list[datetime]:
    """Ближайшие запуски строго после now, в поясе tz."""
    local = now.astimezone(tz)
    out: list[datetime] = []
    if config.mode == "hourly":
        moment = local.replace(minute=_clock(config).minute, second=0, microsecond=0)
        if moment <= local:
            moment += timedelta(hours=1)
        return [moment + timedelta(hours=i) for i in range(count)]
    if config.mode == "monthly":
        for shift in range(0, 26):
            moment = _month_moment(local.year, local.month + shift, config, tz)
            if moment > local:
                out.append(moment)
            if len(out) == count:
                break
        return out
    anchor, step = _anchor(config, local)
    if anchor > local:
        moment = anchor
    else:
        passed = (local.date() - anchor.date()).days // step
        moment = anchor + timedelta(days=passed * step)
        while moment <= local:
            moment += timedelta(days=step)
    return [moment + timedelta(days=i * step) for i in range(count)]


def previous_run(config: ScheduleConfig, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Последний плановый запуск не позже now: по нему видно, что запуск пропущен, пока сервис был выключен."""
    local = now.astimezone(tz)
    if config.mode == "hourly":
        moment = local.replace(minute=_clock(config).minute, second=0, microsecond=0)
        return moment if moment <= local else moment - timedelta(hours=1)
    if config.mode == "monthly":
        for shift in range(0, -26, -1):
            moment = _month_moment(local.year, local.month + shift, config, tz)
            if moment <= local:
                return moment
        return None
    anchor, step = _anchor(config, local)
    if anchor > local:
        return None
    moment = anchor + timedelta(days=(local.date() - anchor.date()).days // step * step)
    if moment > local:
        moment -= timedelta(days=step)
    return moment if moment >= anchor else None


def rule_text(config: ScheduleConfig) -> str:
    """Короткая подпись правила для меню и мониторинга."""
    at = config.time
    if config.mode == "hourly":
        return f"раз в час, в :{at[3:]}"
    if config.mode == "daily":
        return f"раз в сутки, в {at}"
    if config.mode == "days":
        return f"раз в {config.every} дн., в {at}"
    return f"раз в месяц, {config.month_day}-го в {at}"
