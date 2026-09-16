"""Состояние сервиса в памяти для мониторинга: воркеры, текущий прогон, очередь летсплеев.

Каждое изменение увеличивает версию и будит подписчиков потока /api/events.
Менять состояние можно только из потока event loop.
"""

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# загрузка воркера: доля времени в работе за последние 5 минут
LOAD_WINDOW = 300.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class WorkerState:
    name: str
    kind: str
    state: str = "idle"
    game: str | None = None
    stage: str | None = None
    processed: int = 0
    errors: int = 0
    last_error: str | None = None
    # последняя задача упала: мониторинг показывает ошибку как текущую, а не давнюю
    last_failed: bool = False
    updated_at: str = field(default_factory=_now)


class ServiceStatus:
    def __init__(self) -> None:
        self.workers: dict[str, WorkerState] = {}
        self.run: dict[str, Any] | None = None
        self.letsplay_queue = 0
        self.version = 0
        self._changed = asyncio.Event()
        # когда воркер взял текущую задачу и отрезки работы за окно LOAD_WINDOW
        self._busy_since: dict[str, float] = {}
        self._spans: dict[str, deque[tuple[float, float]]] = {}

    def _mark(self, name: str, busy: bool) -> None:
        now = time.monotonic()
        spans = self._spans.setdefault(name, deque())
        if busy:
            self._busy_since.setdefault(name, now)
        elif name in self._busy_since:
            spans.append((self._busy_since.pop(name), now))
        while spans and spans[0][1] < now - LOAD_WINDOW:
            spans.popleft()

    def _load(self, name: str) -> int:
        now = time.monotonic()
        start = now - LOAD_WINDOW
        busy = sum(end - max(begin, start) for begin, end in self._spans.get(name, ()) if end > start)
        if name in self._busy_since:
            busy += now - max(self._busy_since[name], start)
        return round(100 * min(busy, LOAD_WINDOW) / LOAD_WINDOW)

    def _touch(self) -> None:
        self.version += 1
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait_for_change(self, version: int, timeout: float) -> int:
        """Ждёт изменения после version, но не дольше timeout. Возвращает текущую версию."""
        if self.version != version:
            return self.version
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)
        return self.version

    def worker(self, name: str, kind: str) -> WorkerState:
        """Регистрирует воркер, чтобы мониторинг показывал его и в простое."""
        return self.workers.setdefault(name, WorkerState(name, kind))

    def worker_busy(self, name: str, kind: str, game: str, stage: str) -> None:
        worker = self.worker(name, kind)
        worker.state, worker.game, worker.stage, worker.updated_at = "working", game, stage, _now()
        self._mark(name, True)
        self._touch()

    def worker_waiting(self, name: str, kind: str, reason: str) -> None:
        """Воркер ждёт внешний ресурс (например, модель), reason попадает в stage."""
        worker = self.worker(name, kind)
        worker.state, worker.game, worker.stage, worker.updated_at = "waiting", None, reason, _now()
        self._mark(name, False)
        self._touch()

    def changed(self) -> None:
        """Изменение вне воркеров и прогона (например, выбор модели): подписчики получат новый снимок."""
        self._touch()

    def worker_done(self, name: str, kind: str, error: str | None = None) -> None:
        worker = self.worker(name, kind)
        worker.last_failed = bool(error)
        if error:
            worker.errors += 1
            worker.last_error = error
        else:
            worker.processed += 1
        worker.state, worker.game, worker.stage, worker.updated_at = "idle", None, None, _now()
        self._mark(name, False)
        self._touch()

    def run_started(self, run_id: int, trigger: str) -> None:
        self.run = {
            "id": run_id,
            "trigger": trigger,
            "stage": "selecting",
            "selected": 0,
            "processed": 0,
            "created": 0,
            "updated": 0,
            "rechecked": 0,
            "stale_selected": 0,
            "failed": 0,
            "summaries_total": 0,
            "summaries_done": 0,
            "summaries_saved": 0,
            "summary_failed": 0,
            "media_total": 0,
            "media_done": 0,
            "started_at": _now(),
        }
        self._touch()

    def run_update(self, **values: Any) -> None:
        if self.run is not None:
            self.run.update(values)
            self._touch()

    def run_add(self, **deltas: int) -> None:
        if self.run is not None:
            for key, delta in deltas.items():
                self.run[key] += delta
            self._touch()

    def run_finished(self) -> None:
        self.run = None
        self._touch()

    def set_letsplay_queue(self, size: int) -> None:
        self.letsplay_queue = size
        self._touch()

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run": dict(self.run) if self.run else None,
            "workers": [{**asdict(worker), "load": self._load(worker.name)} for worker in self.workers.values()],
            "letsplay_queue": self.letsplay_queue,
        }
