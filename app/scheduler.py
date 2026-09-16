"""Запуск прогонов по расписанию и по кнопке."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import RunInProgress
from app.schedule import ScheduleConfig, next_runs, previous_run

log = logging.getLogger(__name__)

# если прогон упал ещё до записи в базу, время последнего прогона не сдвинется,
# и без этой паузы планировщик пытался бы запускаться снова без остановки
MIN_GAP = timedelta(minutes=5)
MANUAL_COOLDOWN = timedelta(minutes=5)
# пропущенный за время простоя запуск догоняем при старте, если до следующего планового ещё далеко
CATCH_UP_BEFORE = timedelta(minutes=15)
# таймер может сработать на долю секунды раньше срока, и запись прогона ляжет "до" планового слота;
# запуск не раньше этого допуска считается запуском в слот, а не пропуском
SLOT_TOLERANCE = timedelta(minutes=1)


class ManualRunRefused(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Scheduler:
    """Прогоны по расписанию (app/schedule.py) и внеплановые по кнопке.

    После перезапуска сервиса пропущенный плановый запуск выполняется сразу; с пустой базой
    первый прогон стартует без ожидания. Смена расписания и кнопка будят планировщик раньше срока.
    На паузе плановых запусков нет, ручной запуск работает.
    """

    def __init__(
        self,
        job: Callable[[str], Awaitable[object]],
        last_started: Callable[[], datetime | None],
        config: ScheduleConfig,
        tz: ZoneInfo,
        min_gap: timedelta = MIN_GAP,
        manual_cooldown: timedelta = MANUAL_COOLDOWN,
    ) -> None:
        self._job = job
        self._last_started = last_started
        self._config = config
        self._tz = tz
        self._min_gap = min_gap
        self._manual_cooldown = manual_cooldown
        self._not_before: datetime | None = None
        self._manual = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_manual: datetime | None = None
        self.next_run_at: datetime | None = None
        self.run_in_progress = False
        # задача текущего прогона: отмена по кнопке "Остановить" обрывает её, а не сам планировщик
        self._current: asyncio.Task[object] | None = None
        self._stop_requested = False

    @property
    def config(self) -> ScheduleConfig:
        return self._config

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    def update(self, config: ScheduleConfig) -> None:
        """Новое расписание действует сразу: текущий прогон доработает, следующий посчитается по нему."""
        self._config = config
        self._wake.set()

    @property
    def manual_available_at(self) -> datetime | None:
        """Когда снова можно нажать кнопку; None, если уже можно."""
        if self._last_manual is None:
            return None
        moment = self._last_manual + self._manual_cooldown
        return moment if moment > datetime.now(timezone.utc) else None

    def request_run(self) -> None:
        """Внеплановый прогон по кнопке: не во время прогона и не чаще раза в manual_cooldown.

        Интервал защищает лимиты модели и Metacritic от случайных повторных нажатий.
        """
        if self.run_in_progress or self._manual.is_set():
            raise ManualRunRefused("run is already in progress")
        available = self.manual_available_at
        if available is not None:
            wait = (available - datetime.now(timezone.utc)).total_seconds()
            raise ManualRunRefused("manual run was requested recently", retry_after=wait)
        self._last_manual = datetime.now(timezone.utc)
        self._manual.set()

    def stop_run(self) -> bool:
        """Остановка текущего прогона по кнопке: текущий шаг обрывается, прогон помечается прерванным
        (run_once ловит отмену). Расписание при этом не меняется. False, если прогона нет."""
        task = self._current
        if task is None or task.done():
            return False
        self._stop_requested = True
        task.cancel()
        return True

    def _due(self, now: datetime, first: bool) -> datetime | None:
        config = self._config
        if not config.enabled:
            return None
        due = next_runs(config, now, self._tz, 1)[0].astimezone(timezone.utc)
        if first:
            last = self._last_started()
            missed = previous_run(config, now, self._tz)
            # журнал прогонов (runs) говорит, закрыт ли последний плановый слот: догоняем только
            # действительно пропущенный за время простоя, а не тот, что стартовал секундой раньше срока
            if last is None or (missed is not None and last < missed - SLOT_TOLERANCE and due - now > CATCH_UP_BEFORE):
                due = now
        if self._not_before is not None and due < self._not_before:
            due = self._not_before
        return due

    async def run_forever(self) -> None:
        first = True
        while True:
            now = datetime.now(timezone.utc)
            self.next_run_at = self._due(now, first)
            first = False
            if self.next_run_at is None:
                log.info("schedule is paused, runs only by button")
            else:
                log.info("next run at %s", self.next_run_at.isoformat(timespec="seconds"))
            delay = None if self.next_run_at is None else max(0.0, (self.next_run_at - now).total_seconds())
            reason = await self._sleep(delay)
            if reason == "config":
                continue
            if reason == "time" and self.next_run_at is not None:
                # таймер asyncio может проснуться чуть раньше срока: запуск строго не раньше слота,
                # иначе запись в журнале ляжет "до" него и после перезапуска слот сочтут пропущенным
                early = (self.next_run_at - datetime.now(timezone.utc)).total_seconds()
                if early > 0:
                    await asyncio.sleep(early)
            trigger = "manual" if reason == "manual" else "schedule"

            self.run_in_progress = True
            self._stop_requested = False
            self._current = asyncio.create_task(self._job(trigger), name="run")
            try:
                await self._current
            except asyncio.CancelledError:
                # остановка по кнопке отменяет задачу прогона, остановка сервиса отменяет сам планировщик:
                # тогда прогон отменяется следом и дожидается, чтобы он успел пометиться прерванным
                if not self._stop_requested:
                    self._current.cancel()
                    with contextlib.suppress(BaseException):
                        await self._current
                    raise
                log.info("%s run stopped by user", trigger)
            except RunInProgress as e:
                log.info("%s run skipped: %s", trigger, e)
            except Exception:
                # граница планировщика: упавший прогон не должен останавливать расписание
                log.exception("%s run failed", trigger)
            finally:
                self._current = None
                self.run_in_progress = False
                self._not_before = datetime.now(timezone.utc) + self._min_gap

    async def _sleep(self, delay: float | None) -> str:
        """Ждёт планового времени. Возвращает "manual" (кнопка), "config" (новое расписание) или "time"."""
        manual = asyncio.ensure_future(self._manual.wait())
        wake = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({manual, wake}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            manual.cancel()
            wake.cancel()
        if self._manual.is_set():
            self._manual.clear()
            return "manual"
        if self._wake.is_set():
            self._wake.clear()
            return "config"
        return "time"
