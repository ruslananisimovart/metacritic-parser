"""Расшифровка речи из аудио летсплея через faster-whisper на видеокарте.

Модель грузится при первой задаче (около 4,5 ГБ VRAM) и выгружается сторожем простоя из
`app/gpu.py`, когда расшифровок давно не было. Расшифровка идёт в отдельном потоке:
она занимает видеокарту целиком, а веб-интерфейс и мониторинг в это время должны отвечать.
"""

import asyncio
import gc
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.gpu import GpuLock

log = logging.getLogger(__name__)

# сегменты с такими признаками Whisper выдумывает на музыке, тишине и зацикленных повторах
MAX_NO_SPEECH = 0.6
MAX_COMPRESSION = 2.4
SAMPLE_RATE = 16000


class TranscribeError(Exception):
    pass


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    language: str
    text: str
    segments: list[Segment]
    audio_seconds: float
    took_seconds: float


def _prepare_cuda_libraries() -> None:
    """DLL из pip-пакета nvidia-cublas-cu12 не видны CTranslate2 на Windows без правки PATH.

    Без этого первый же вызов на видеокарте падает с "Library cublas64_12.dll is not found".
    В Docker на образе nvidia/cuda библиотеки лежат в системных путях, и правка не нужна.
    """
    if sys.platform != "win32":
        return
    try:
        import nvidia.cublas
    except ImportError:
        return
    bin_dir = Path(nvidia.cublas.__path__[0]) / "bin"
    path = os.environ.get("PATH", "")
    if bin_dir.is_dir() and str(bin_dir) not in path:
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{path}"
        log.debug("added %s to PATH for cuBLAS", bin_dir)


def windows(duration: float, head: float, tail: float, full: bool = False) -> list[tuple[float, float]]:
    """Куски ролика для расшифровки.

    Обычно ролик расшифровывается целиком (full): заключение по всему рассказу блогера, а не по
    краям. Начало и концовка остаются запасным вариантом для роликов длиннее разумного предела.
    """
    if duration <= 0:
        raise TranscribeError(f"unknown video duration: {duration}")
    if full or duration <= head + tail:
        return [(0.0, duration)]
    return [(0.0, head), (duration - tail, duration)]


def decode_window(path: Path, start: float, end: float) -> Any:
    """Кусок [start, end) в моно 16 кГц float32.

    Перемотка вместо чтения файла целиком: у ролика на два часа полное декодирование
    заняло бы лишнюю память и время, а нужны только начало и концовка.
    """
    import av
    import numpy as np

    chunks: list[Any] = []
    try:
        with av.open(str(path)) as container:
            stream = container.streams.audio[0]
            if start:
                container.seek(int(start / stream.time_base), stream=stream)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(stream):
                moment = float(frame.pts * stream.time_base) if frame.pts is not None else 0.0
                if moment + frame.samples / frame.sample_rate < start:
                    continue
                if moment >= end:
                    break
                for resampled in resampler.resample(frame):
                    chunks.append(resampled.to_ndarray().reshape(-1))
            for resampled in resampler.resample(None):
                chunks.append(resampled.to_ndarray().reshape(-1))
    except Exception as e:
        raise TranscribeError(f"cannot decode {path.name}: {type(e).__name__}: {e}") from e
    if not chunks:
        raise TranscribeError(f"no audio in {path.name} at [{start:.0f}, {end:.0f}]")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def whisper_language(code: str | None, supported: Iterable[str]) -> str | None:
    """Код дорожки YouTube в код Whisper: "en-US" и "pt-BR" дают "en" и "pt".

    yt-dlp отдаёт язык с регионом, а faster-whisper на таком коде падает с ValueError.
    Незнакомый Whisper код даёт None, и тогда язык определяется по звуку.
    """
    if not code:
        return None
    short = code.replace("_", "-").split("-")[0].strip().lower()
    return short if short in set(supported) else None


def clean(segments: list[Segment]) -> list[Segment]:
    """Убирает подряд идущие повторы одной и той же фразы."""
    result: list[Segment] = []
    for segment in segments:
        if result and result[-1].text.casefold() == segment.text.casefold():
            continue
        result.append(segment)
    return result


class Transcriber:
    """Одна модель на процесс и одна задача на видеокарте за раз; после простоя модель выгружается."""

    def __init__(
        self,
        *,
        model: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "float16",
        batch_size: int = 16,
        head_seconds: float = 35 * 60,
        tail_seconds: float = 5 * 60,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.batch_size = batch_size
        self.head_seconds = head_seconds
        self.tail_seconds = tail_seconds
        self._model: Any = None
        self._pipeline: Any = None
        # общий с локальной моделью: сервис передаёт его в GeminiClient, по нему же считается простой
        self.gpu = GpuLock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def unload(self) -> None:
        """Убирает модель из видеопамяти. Вызывается под замком видеокарты, не во время расшифровки.

        CTranslate2 отдаёт память при удалении модели, но BatchedInferencePipeline держит на неё
        ссылку, поэтому снимаются обе, а сборка мусора добирает циклические ссылки.
        """
        if self._model is None:
            return
        self._pipeline = None
        self._model = None
        gc.collect()

    def load(self) -> None:
        """Загружает модель. Вызывается лениво, первый раз занимает около секунды из кэша."""
        if self._model is not None:
            return
        _prepare_cuda_libraries()
        try:
            from faster_whisper import BatchedInferencePipeline, WhisperModel
        except ImportError as e:
            raise TranscribeError(f"faster-whisper is not installed: {e}") from e
        log.info("loading whisper %s on %s (%s)", self.model_name, self.device, self.compute_type)
        try:
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        except Exception as e:
            raise TranscribeError(f"cannot load whisper model: {type(e).__name__}: {e}") from e
        self._pipeline = BatchedInferencePipeline(model=self._model)

    def detect_language(self, audio: Any) -> tuple[str, float]:
        self.load()
        language, probability, _ = self._model.detect_language(
            audio=audio, vad_filter=True, language_detection_segments=4
        )
        return language, probability

    def _run(
        self, path: Path, duration: float, language: str | None, hint: str | None = None, full: bool = False
    ) -> Transcript:
        import time

        self.load()
        started = time.monotonic()
        requested = language
        language = whisper_language(requested, self._model.supported_languages)
        if requested and not language:
            log.info("language %r is unknown to whisper, detecting it from audio", requested)
        segments: list[Segment] = []
        audio_seconds = 0.0
        try:
            for start, end in windows(duration, self.head_seconds, self.tail_seconds, full):
                audio = decode_window(path, start, end)
                audio_seconds += len(audio) / SAMPLE_RATE
                if not language:
                    language, probability = self.detect_language(audio)
                    log.info("detected language %s (p=%.2f) in %s", language, probability, path.name)
                # название игры как подсказка: без неё редкие названия распознаются как похожие
                # слова (Aggelos 2 превращался в "Ageless 2") и ошибка уходит в заключение
                raw, _ = self._pipeline.transcribe(
                    audio,
                    language=language,
                    batch_size=self.batch_size,
                    vad_filter=True,
                    task="transcribe",
                    hotwords=hint or None,
                )
                for segment in raw:
                    text = segment.text.strip()
                    if (
                        not text
                        or segment.no_speech_prob > MAX_NO_SPEECH
                        or segment.compression_ratio > MAX_COMPRESSION
                    ):
                        continue
                    segments.append(Segment(round(start + segment.start, 1), round(start + segment.end, 1), text))
        except TranscribeError:
            raise
        except Exception as e:
            # ошибка faster-whisper или CTranslate2 (язык, память видеокарты) должна увести
            # сценарий к субтитрам, а не уронить обработку игры
            raise TranscribeError(f"whisper failed on {path.name}: {type(e).__name__}: {e}") from e
        segments = clean(segments)
        if not segments:
            raise TranscribeError(f"no speech recognized in {path.name}")
        took = time.monotonic() - started
        log.info(
            "transcribed %s: %.1f min of audio in %.1fs, %d segments",
            path.name, audio_seconds / 60, took, len(segments),
        )
        return Transcript(
            language=language or "",
            text=" ".join(s.text for s in segments),
            segments=segments,
            audio_seconds=audio_seconds,
            took_seconds=took,
        )

    @classmethod
    def from_env(cls) -> "Transcriber":
        def number(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError:
                raise ValueError(f"{name} must be a number, got {raw!r}") from None

        return cls(
            model=os.environ.get("WHISPER_MODEL", "").strip() or "large-v3-turbo",
            device=os.environ.get("WHISPER_DEVICE", "").strip() or "cuda",
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "").strip() or "float16",
            batch_size=int(number("WHISPER_BATCH_SIZE", 16)),
            head_seconds=number("LETSPLAY_HEAD_MINUTES", 35) * 60,
            tail_seconds=number("LETSPLAY_TAIL_MINUTES", 5) * 60,
        )

    async def transcribe(
        self, path: Path, *, duration: float, language: str | None = None, hint: str | None = None,
        full: bool = True,
    ) -> Transcript:
        """Расшифровывает ролик целиком (full) или только начало и концовку.

        language берётся из метаданных дорожки, hint (название игры) подсказывает модели
        написание редких слов.
        """
        async with self.gpu:
            return await asyncio.to_thread(self._run, path, duration, language, hint, full)
