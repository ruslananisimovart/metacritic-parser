"""Видеокарта: общий замок и сторож простоя.

Локальная модель и Whisper делят одну карту, поэтому работают по очереди под общим замком.
Пока работы нет, держать их в видеопамяти незачем: сторож выгружает обе после простоя, и до
следующего прогона карта свободна. Следующая задача загрузит модели заново, это несколько секунд.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.gemini import GeminiClient
    from app.transcribe import Transcriber

log = logging.getLogger(__name__)

CHECK_SECONDS = 15.0


class GpuLock:
    """Замок видеокарты, который помнит, когда её отпустили в последний раз."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._free_since = time.monotonic()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def idle_seconds(self) -> float:
        """Сколько карта простаивает. Пока идёт работа, ноль."""
        return 0.0 if self._lock.locked() else time.monotonic() - self._free_since

    async def __aenter__(self) -> "GpuLock":
        await self._lock.acquire()
        return self

    async def __aexit__(self, *error: Any) -> None:
        # отметка ставится до release: следующий ждущий не должен увидеть старое время простоя
        self._free_since = time.monotonic()
        self._lock.release()


def _loaded(llm: "GeminiClient", transcriber: "Transcriber") -> bool:
    return bool(getattr(transcriber, "loaded", False)) or llm.local_loaded


async def release(lock: GpuLock, llm: "GeminiClient", transcriber: "Transcriber") -> list[str]:
    """Выгружает обе модели из видеопамяти под замком. Возвращает то, что удалось выгрузить."""
    async with lock:
        released: list[str] = []
        if getattr(transcriber, "loaded", False):
            # выгрузка идёт в потоке: сборка мусора у CTranslate2 занимает заметное время
            await asyncio.to_thread(transcriber.unload)
            released.append(f"whisper {transcriber.model_name}")
        released += await llm.release_local()
        return released


async def idle_loop(lock: GpuLock, llm: "GeminiClient", transcriber: "Transcriber", idle_seconds: float) -> None:
    """Освобождает видеопамять, когда карту не трогали idle_seconds.

    Сторож берёт тот же замок, что и работа с картой, поэтому выгрузка не может совпасть
    с запросом к модели или расшифровкой.
    """
    while True:
        await asyncio.sleep(min(CHECK_SECONDS, idle_seconds))
        if lock.idle_seconds < idle_seconds or not _loaded(llm, transcriber):
            continue
        try:
            released = await release(lock, llm, transcriber)
        except Exception:
            # сбой выгрузки не должен ронять сторож: попробуем на следующем круге
            log.warning("cannot free video memory after idle", exc_info=True)
            continue
        if released:
            log.info("GPU idle %.0fs: unloaded %s", idle_seconds, ", ".join(released))
