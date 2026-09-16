"""Летсплей игры: выбор ролика, расшифровка речи блогера и заключение по тексту.

Порядок: поиск на YouTube и отсев правилами (app/youtube.py), разметка кандидатов через
Gemini, текст (Whisper или субтитры), заключение через Gemini, запись в таблицу letsplays.
"""

import itertools
import json
import logging
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator

from app.db import Database
from app.gemini import GeminiBadOutput, GeminiClient, GeminiError, GeminiUnavailable
from app.prompts import Prompt
from app.quotes import MIN_QUOTE_CHARS, QUOTE_MATCH_RATIO, fragments, has_ellipsis, shares_phrase
from app.quotes import normalize as _normalize
from app.runconfig import LETSPLAY_KEY, LetsplayConfig, load_letsplay
from app.summaries import as_points, check_language, check_russian, fix_mixed_script
from app.transcribe import TranscribeError, Transcriber
from app.youtube import Candidate, YouTubeBlocked, YouTubeClient, YouTubeError, filter_candidates

log = logging.getLogger(__name__)

QUEUED, DONE, NOT_FOUND, FAILED = "queued", "done", "not_found", "failed"

MAX_POINTS = 5
MAX_MOMENTS = 3
# причина вердикта по ролику: в промпте "не больше пяти слов", в базу больше не пишем
REASON_CHARS = 60
# строка расшифровки с таймкодом: короткие сегменты склеиваются, чтобы таймкодов было не больше, чем мест
LINE_CHARS = 240
# ссылка на момент ставится чуть раньше фразы, иначе ролик начинается с середины слова
MOMENT_LEAD_IN = 2.0
# насколько таймкод модели может отличаться от начала сегмента, когда цитату найти не удалось
TIME_TOLERANCE = 30.0
# во сколько раз лимит размера файла больше для склеенного видео 360p, когда отдельного звука YouTube не дал
MUXED_SIZE_FACTOR = 4
# сколько расшифровки уходит в один запрос к модели, в токенах. У локальной модели контекст мал
# (24-32 тыс. вместе с ответом), а длинный промпт заметно замедляет ответ; у облачной запас больше,
# но один запрос всё равно держим обозримым, иначе модель теряет начало
CHUNK_TOKENS = {"local": 8_000, "cloud": 15_000}
# оценка сверху: кириллица и речь с ошибками распознавания дробятся мельче английского текста
CHARS_PER_TOKEN = 3
# насколько граница части может отойти от идеальной ради паузы блогера (доля от размера части)
SPLIT_SLACK = 0.2
# пункты из двух частей ролика считаются одним, если у них общая большая часть слов
SAME_POINT_RATIO = 0.6

# фильтры безопасности глушим: в летсплеях много брани, а нам нужен разбор речи, а не её оценка
SAFETY = [
    {"category": category, "threshold": "BLOCK_NONE"}
    for category in (
        "HARM_CATEGORY_HARASSMENT",
        "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT",
        "HARM_CATEGORY_DANGEROUS_CONTENT",
    )
]

# у выбора ролика добавок по профилю нет; у заключения облачная модель получает требование к законченной цитате
SELECT_SYSTEM = Prompt(
    head="""Ты отбираешь летсплеи игр на YouTube по списку роликов.
Летсплей: блогер сам играет в эту игру и комментирует происходящее своим голосом (прохождение, первый взгляд, запись стрима с комментариями).
Не летсплей: трейлер, обзор, рецензия, гайд или разбор билдов, нарезка моментов, челлендж и спидран с необычными условиями, прохождение без голоса автора (No Commentary, Silent, Full Game Movie, Longplay), ролик про другую игру или другую часть серии, реакция на чужое видео. Пометки Walkthrough, Gameplay и Full Game сами по себе летсплею не мешают.
Ролик про другую игру отклоняй, даже если в заголовке случайно встречаются буквы или слова из названия: если в заголовке названа другая игра (например, "Sword Art Online" при игре "SSS-FPV"), это не летсплей этой игры. Номер части ("Part 13", "#5") сам по себе не доказывает, что ролик про эту игру.
Не летсплей и официальные ролики самой студии: показы, презентации и трансляции разработчика или издателя игры. Если название канала начинается с имени разработчика или издателя, это официальный ролик.
Если игра вышла только на части платформ (отдельное издание, порт или ремастер для конкретной приставки), подходит ролик именно про это издание или платформу. Ролик, где по заголовку видно другую платформу или исходную версию игры, не подходит.""",
    rules=(
        "суди только по данным ролика, ничего не додумывай",
        "признаки летсплея: нумерация частей (Part 1, #3), реплика или эмоция от первого лица в заголовке,"
        " вопрос в заголовке",
        # пропущенный ролик теряет игре летсплей, а лишний отсеет проверка речи после расшифровки
        "ставь false только при явном признаке из списка исключений; если ролик похож на летсплей, но прямых"
        " признаков голоса в заголовке нет, ставь true: наличие речи сервис проверяет сам при расшифровке",
        "верни все ролики из списка, ни один не пропускай и не добавляй",
        "id возвращай точно так же, как в списке",
        "в поле reason назови признак, по которому решил (нумерация частей, обзор, официальный канал),"
        " не больше пяти слов по-русски",
    ),
)

SELECT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "videos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "STRING", "description": "id ролика из списка, без квадратных скобок"},
                    "letsplay": {"type": "BOOLEAN", "description": "это летсплей именно этой игры"},
                    "reason": {"type": "STRING", "description": "коротко, почему"},
                },
                "required": ["id", "letsplay", "reason"],
            },
        }
    },
    "required": ["videos"],
}

CONCLUSION_SYSTEM = Prompt(
    head="""Ты редактор игрового каталога. По расшифровке речи блогера из летсплея составь заключение на русском языке для карточки игры.
Расшифровка разбита на строки, в начале каждой строки стоит таймкод момента ролика: [ММ:СС] или [Ч:ММ:СС].""",
    rules=(
        "опирайся только на слова блогера, ничего не додумывай и не добавляй знания об игре со стороны",
        "пункт (text) в списках \"хвалит\" и \"ругает\" это вывод по-русски своими словами, а не цитата",
        "к каждому пункту добавь моменты: таймкод строки, из которой взят пункт, и короткую цитату из этой же"
        " строки (3-10 слов, слово в слово, на языке расшифровки); цитата идёт только в момент",
        "таймкод копируй из строки как есть, не считай его сам; если блогер говорит об этом в нескольких местах,"
        " укажи до трёх моментов",
        "цитата должна читаться отдельно от расшифровки и сама подтверждать тезис: бери законченный фрагмент"
        " фразы блогера, не начинай с союза или предлога и не обрывай на них, сохраняй отрицание и условие,"
        " если они есть",
        "если подходящей строки в расшифровке нет, пункт не пиши: пункты без места в расшифровке не нужны",
        "расшифровка может быть неполной (начало и концовка ролика) и с ошибками распознавания: непонятные места"
        " пропускай",
        "в расшифровке вперемешку речь блогера и реплики персонажей игры из катсцен; реплики персонажей это"
        " не мнение блогера: цитаты бери только из его собственных слов, где он обращается к зрителю или"
        " комментирует происходящее",
        "пиши о самой игре: что блогеру нравится и что нет, что он говорит о механиках, сложности, сюжете, графике,"
        " звуке, техническом состоянии",
        "в \"ругает\" относи и недовольство вскользь: цены, требования прокачки, то, что предмет или механика"
        " не дали ожидаемого. Не относи туда его промахи и смерти в бою: это его игра, а не претензия к игре",
        "не пересказывай прохождение по шагам и не раскрывай сюжетные повороты",
        "мат и грубости передавай нейтрально, без цитат",
        "если блогер почти не высказывает мнения об игре, скажи это в summary, а списки оставь пустыми",
        "название игры пиши точно так, как в строке \"Игра\", даже если в расшифровке оно распознано иначе",
        "остальные названия (студии, платформы, фильмы, другие игры) пиши в оригинале латиницей, как в расшифровке:"
        " не переводи их на русский, не заменяй похожими, не угадывай, что блогер имел в виду, и не пиши об ошибках"
        " распознавания",
        "в поле commentary отметь, есть ли в расшифровке впечатления самого блогера об игре. Если там только"
        " диалоги и озвучка самой игры, одно приветствие и прощание или речь без отношения к игре, ставь false",
    ),
    # на тесте 12.09 Gemma перевела фильм Kung Fu Hustle в "Кунг-фу Панда" и дописала, что в расшифровке иначе,
    # хотя правило про названия уже есть: в summary и verdict она его не применяла
    local=(
        "правила про названия действуют и в summary, и в verdict: переноси названия латиницей как в расшифровке,"
        " не переводи их, не поясняй в скобках и не пиши, что в расшифровке распознано иначе",
    ),
)

# пункт списка вместе с местами в ролике: время берётся у сегмента расшифровки, цитата нужна для сверки
_POINT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "text": {"type": "STRING", "description": "тезис по-русски своими словами, не цитата"},
        "moments": {
            "type": "ARRAY",
            "minItems": 1,
            "maxItems": 3,
            "description": "места в расшифровке, где блогер это говорит",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "time": {
                        "type": "STRING",
                        "description": "таймкод строки расшифровки, например 12:34; нужен для привязки цитаты,"
                        " в карточку идёт время сегмента",
                    },
                    "quote": {"type": "STRING", "description": "цитата из этой строки, 3-10 слов, слово в слово"},
                },
                "required": ["time", "quote"],
            },
        },
    },
    "required": ["text", "moments"],
}

CONCLUSION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "commentary": {
            "type": "BOOLEAN",
            "description": "в расшифровке есть впечатления самого блогера об игре, а не только озвучка игры",
        },
        "summary": {"type": "STRING", "description": "о чём ролик и как блогер играет, 2-4 предложения"},
        "pros": {"type": "ARRAY", "items": _POINT_SCHEMA, "maxItems": 5, "description": "что блогер хвалит"},
        "cons": {"type": "ARRAY", "items": _POINT_SCHEMA, "maxItems": 5, "description": "что блогер ругает"},
        "verdict": {
            "type": "STRING",
            "description": "общее впечатление блогера от той части игры, которую он прошёл в ролике, 1-2 предложения",
        },
    },
    "required": ["commentary", "summary", "pros", "cons", "verdict"],
}

# длинный ролик разбирается по частям; общий текст и вывод пишутся по собранным пунктам
MERGE_SYSTEM = Prompt(
    head="""Ты редактор игрового каталога. Летсплей был длинным, и заключение по нему собиралось по частям.
Тебе даны выводы по каждой части и итоговые списки "хвалит" и "ругает". Напиши общий текст для карточки игры на русском языке.""",
    rules=(
        "опирайся только на переданные выводы и пункты, ничего не додумывай и не добавляй знания об игре со стороны",
        "summary: о чём ролик и как блогер играет от начала до конца, 2-4 предложения без пересказа прохождения по шагам",
        "verdict: общее впечатление блогера от игры по всему ролику, 1-2 предложения; если мнение менялось по ходу,"
        " скажи об этом",
        "названия игр, студий и платформ пиши так, как они даны, не переводи и не заменяй похожими",
        "не раскрывай сюжетные повороты",
    ),
)

MERGE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING", "description": "о чём ролик и как блогер играет, 2-4 предложения"},
        "verdict": {"type": "STRING", "description": "общее впечатление блогера от игры, 1-2 предложения"},
    },
    "required": ["summary", "verdict"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS letsplays (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    video_id TEXT,
    url TEXT,
    title TEXT,
    channel TEXT,
    views INTEGER,
    duration INTEGER,
    published TEXT,
    language TEXT,
    transcript_source TEXT,
    transcript TEXT,
    segments TEXT,
    conclusion TEXT,
    candidates TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL
);
"""
# колонки добавлены позже схемы: в старых базах их дописывает _migrate
LATER_COLUMNS = {
    "retry_at": "ALTER TABLE letsplays ADD COLUMN retry_at TEXT",
    "coverage": "ALTER TABLE letsplays ADD COLUMN coverage TEXT",
    # сколько раз игру искали и летсплея не нашли: лимит повторных поисков из настроек
    "searches": "ALTER TABLE letsplays ADD COLUMN searches INTEGER NOT NULL DEFAULT 0",
    # ролики, проверенные и отклонённые для игры (нет речи, мало речи, нет мнения): JSON-список id
    "rejected_videos": "ALTER TABLE letsplays ADD COLUMN rejected_videos TEXT",
}
# текст TranscribeError, когда Whisper не нашёл в звуке голоса
NO_SPEECH = "no speech recognized"
# отказы по содержанию ролика (не сбои): нет речи, мало речи, нет мнения блогера; и префикс "id: " в тексте ошибки
_CONTENT_REJECT = re.compile(
    r"no speech recognized|нет речи блогера|мало речи блогера|нет мнения блогера|captions: нет субтитров|мало речи",
    re.IGNORECASE
)
_VIDEO_ID_PREFIX = re.compile(r"^([\w-]{11}):")


class Verdict(BaseModel):
    id: str
    letsplay: bool
    reason: str = ""

    @field_validator("id")
    @classmethod
    def _bare_id(cls, value: str) -> str:
        # Qwen возвращает id вместе со скобками из списка роликов: "[oni1]" вместо "oni1"
        return value.strip().strip("[]").strip()

    @field_validator("reason")
    @classmethod
    def _short_reason(cls, value: str) -> str:
        # причина нужна для разбора в базе, а не для показа: длинную обрезаем, а не отвергаем весь ответ.
        # maxLength в схеме не ставим: поддержку поля у kie.ai не проверяли, отказ уронил бы весь выбор
        return value.strip()[:REASON_CHARS]


class Selection(BaseModel):
    videos: list[Verdict]


class Moment(BaseModel):
    """Место в ролике. time и quote приходят от модели, seconds проставляется сверкой с расшифровкой."""

    time: str = ""
    quote: str = ""
    seconds: float | None = None


class Point(BaseModel):
    text: str
    moments: list[Moment] = []

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        # те же сбои локальных моделей, что в резюме отзывов: иероглифы и латиница внутри русских слов
        text = fix_mixed_script(check_language(value.strip()))
        if not text:
            raise ValueError("empty point")
        return check_russian(text)

    @field_validator("moments")
    @classmethod
    def _moments(cls, moments: list["Moment"]) -> list["Moment"]:
        return moments[:MAX_MOMENTS]


class Conclusion(BaseModel):
    summary: str
    pros: list[Point]
    cons: list[Point]
    verdict: str
    commentary: bool = True

    @property
    def has_opinion(self) -> bool:
        """Блогер говорит об игре сам: иначе ролик не подходит под "рассказ блогера" из задания."""
        return self.commentary and bool(self.pros or self.cons)

    @field_validator("pros", "cons", mode="before")
    @classmethod
    def _points(cls, points: Any) -> list[dict[str, Any]]:
        """Приводит пункты к объектам: модель может вернуть строку, а в старых записях так лежат все пункты.

        Склеенные через '", "' пункты разрезаются; моменты остаются у первой части, остальным их не выдумываем.
        """
        return as_points(points, "moments")[:MAX_POINTS]

    @field_validator("summary", "verdict")
    @classmethod
    def _required(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("empty text")
        return fix_mixed_script(check_language(text.strip()))


class Digest(BaseModel):
    """Общий текст по частям длинного ролика: те же требования к языку, что у заключения."""

    summary: str
    verdict: str

    @field_validator("summary", "verdict")
    @classmethod
    def _required(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("empty text")
        return fix_mixed_script(check_language(text.strip()))


@dataclass(frozen=True)
class GameRef:
    """Данные игры, которые нужны для поиска и промптов."""

    id: int
    title: str
    release_date: date | None = None
    genres: list[str] = field(default_factory=list)
    developers: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VideoText:
    """Текст ролика и сколько звука он покрывает: по этому видно, говорит ли блогер.

    whole: ролик расшифрован целиком, а не только начало и концовка.
    """

    source: str
    language: str
    segments: list[tuple[float, float, str]]
    text: str
    audio_seconds: float
    whole: bool = True

    @property
    def chars_per_minute(self) -> float:
        return len(self.text) / max(self.audio_seconds / 60, 1.0)


@dataclass(frozen=True)
class Settings:
    workdir: Path
    queries: tuple[str, ...] = ("{title} let's play", "{title} walkthrough part 1")
    # какие ролики берём: от десяти минут до двух часов; верхняя граница меняется из интерфейса,
    # 0 значит без ограничения
    min_duration: int = 10 * 60
    max_duration: int = 2 * 60 * 60
    # до этой длины ролик расшифровывается целиком, дальше только начало и концовка: иначе
    # десятичасовой стрим занял бы видеокарту на час
    full_max_seconds: int = 4 * 60 * 60
    max_candidates: int = 10
    # сколько роликов за проход доходят до модели (или срываются на загрузке), прежде чем сдаться;
    # ролик без речи блогера отсеивается до модели и попыткой не считается
    max_text_attempts: int = 3
    # у летсплея 450-600 символов речи на минуту; у прохождения без комментариев, где блогер
    # только здоровается и прощается, около 15 (Aggelos 2: 677 символов на 50 минут)
    min_chars_per_minute: int = 100
    # неудачи повторяются при каждом прогоне и в фоне каждые retry_minutes, но не больше
    # max_attempts_per_game раз в сутки: у ролика без речи новый заход ничего не изменит
    max_attempts_per_game: int = 3
    retry_minutes: int = 30
    # повторный поиск для игр без летсплея (из интерфейса, LetsplayConfig): включён ли, через сколько
    # дней, не больше скольких раз на игру (0 без ограничения)
    recheck: bool = True
    recheck_days: int = 30
    recheck_max: int = 3
    max_audio_bytes: int = 150 * 2**20
    transcript_source: str = "whisper"  # whisper или captions: что пробуем первым
    transcript_max_chars: int = 60_000
    # через сколько повторять поиск после отказа YouTube: попытка при этом не тратится
    defer_minutes: int = 15


@dataclass
class Result:
    status: str
    candidate: Candidate | None = None
    language: str | None = None
    source: str | None = None
    transcript: str = ""
    segments: list[tuple[float, float, str]] = field(default_factory=list)
    conclusion: Conclusion | None = None
    error: str | None = None
    # отказ YouTube, а не итог по игре: попытку не тратим и повторяем поиск позже
    temporary: bool = False
    # full: ролик расшифрован целиком, edges: только начало и концовка
    coverage: str | None = None
    # решение модели по каждому кандидату (id -> вердикт): сохраняется вместе с кандидатами
    verdicts: dict[str, Verdict] = field(default_factory=dict)
    # ролики, которые скачали и признали непригодными: дописываются к списку пропуска игры
    rejected_ids: list[str] = field(default_factory=list)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def settings_from_env(root: Path) -> Settings:
    """Настройки летсплеев из .env. Значения по умолчанию проверены на живых роликах."""
    workdir = os.environ.get("LETSPLAY_WORKDIR", "").strip()
    source = os.environ.get("LETSPLAY_TRANSCRIPT_SOURCE", "").strip().lower() or "whisper"
    if source not in ("whisper", "captions"):
        raise ValueError(f"LETSPLAY_TRANSCRIPT_SOURCE must be whisper or captions, got {source!r}")
    return Settings(
        workdir=Path(workdir) if workdir else root / "data" / "audio",
        min_duration=int(_env("LETSPLAY_MIN_MINUTES", 10) * 60),
        max_duration=int(_env("LETSPLAY_MAX_MINUTES", 120) * 60),
        full_max_seconds=int(_env("LETSPLAY_FULL_MAX_MINUTES", 240) * 60),
        max_candidates=int(_env("LETSPLAY_MAX_CANDIDATES", 10)),
        max_text_attempts=int(_env("LETSPLAY_MAX_TEXT_ATTEMPTS", 3)),
        min_chars_per_minute=int(_env("LETSPLAY_MIN_CHARS_PER_MINUTE", 100)),
        max_audio_bytes=int(_env("LETSPLAY_MAX_AUDIO_MB", 150) * 2**20),
        transcript_source=source,
        transcript_max_chars=int(_env("LETSPLAY_TEXT_MAX_CHARS", 60_000)),
        max_attempts_per_game=int(_env("LETSPLAY_MAX_ATTEMPTS", 3)),
        retry_minutes=int(_env("LETSPLAY_RETRY_MINUTES", 30)),
        defer_minutes=int(_env("LETSPLAY_DEFER_MINUTES", 15)),
    )


def letsplay_config(db: Database, base: Settings) -> LetsplayConfig:
    """Границы длительности: запись из интерфейса, а без неё значения из .env."""
    raw = db.get_setting(LETSPLAY_KEY)
    if raw:
        return load_letsplay(raw)
    return LetsplayConfig(min_minutes=base.min_duration // 60, max_minutes=base.max_duration // 60)


def apply_config(base: Settings, config: LetsplayConfig) -> Settings:
    """Настройки воркера с границами и правилом повторного поиска из интерфейса; остальное из .env."""
    return replace(
        base, min_duration=config.min_minutes * 60, max_duration=config.max_minutes * 60,
        recheck=config.recheck, recheck_days=config.recheck_days, recheck_max=config.recheck_max,
    )


def build_select_prompt(game: GameRef, candidates: list[Candidate]) -> str:
    lines = [f"Игра: {game.title}"]
    if game.release_date:
        lines.append(f"Дата выхода: {game.release_date.isoformat()}")
    if game.genres:
        lines.append(f"Жанры: {', '.join(game.genres)}")
    if game.developers:
        lines.append(f"Разработчик: {', '.join(game.developers)}")
    if game.platforms:
        lines.append(f"Платформы: {', '.join(game.platforms)}")
    lines.append("")
    lines.append("Ролики:")
    for candidate in candidates:
        minutes = f"{candidate.duration // 60} мин" if candidate.duration else "длительность неизвестна"
        views = f"{candidate.views:,} просмотров".replace(",", " ") if candidate.views else "просмотры неизвестны"
        stream = ", запись стрима" if candidate.was_live else ""
        channel = candidate.channel or "канал неизвестен"
        lines.append(f"[{candidate.video_id}] {candidate.title} | {channel} | {minutes} | {views}{stream}")
    return "\n".join(lines)


def timecode(seconds: float) -> str:
    """Секунды в подпись момента: 12:34 или 1:02:03."""
    total = int(max(0.0, seconds))
    hours, minutes, rest = total // 3600, (total % 3600) // 60, total % 60
    return f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes:02d}:{rest:02d}"


def parse_timecode(value: str) -> float | None:
    """Таймкод из ответа модели в секунды. Понимает 12:34, 1:02:03 и просто число секунд."""
    parts = value.strip().strip("[]").replace(",", ":").split(":")
    if not 1 <= len(parts) <= 3 or not all(part.strip().isdigit() for part in parts):
        return None
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _clean_segments(segments: Sequence[Sequence[Any]] | None) -> list[tuple[float, float, str]]:
    """Сегменты расшифровки в едином виде: и кортежи из Whisper, и списки из JSON базы."""
    out: list[tuple[float, float, str]] = []
    for segment in segments or []:
        if len(segment) < 3 or not isinstance(segment[2], str) or not segment[2].strip():
            continue
        try:
            start, end = float(segment[0]), float(segment[1])
        except (TypeError, ValueError):
            continue
        out.append((start, end, segment[2].strip()))
    return out


def transcript_with_timecodes(segments: Sequence[Sequence[Any]] | None, max_chars: int) -> str:
    """Расшифровка строками вида "[12:34] текст". Короткие сегменты склеиваются в одну строку."""
    lines: list[str] = []
    block: list[str] = []
    start: float | None = None
    for segment_start, _end, text in _clean_segments(segments):
        if start is None:
            start = segment_start
        block.append(text)
        if sum(len(part) for part in block) >= LINE_CHARS:
            lines.append(f"[{timecode(start)}] {' '.join(block)}")
            block, start = [], None
    if block and start is not None:
        lines.append(f"[{timecode(start)}] {' '.join(block)}")
    # обрезаем по строкам: половина строки без таймкода модели не нужна
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept)


def _window(quote: str, text: str) -> str:
    """Кусок сегмента длиной с цитату, где больше всего её слов: в карточке слова блогера, а не пересказ модели."""
    tokens = text.split()
    wanted = _normalize(quote).split()
    size = max(1, min(len(wanted), len(tokens)))
    wanted_set = set(wanted)
    best, best_score = 0, -1
    for i in range(len(tokens) - size + 1):
        score = len(wanted_set & set(_normalize(" ".join(tokens[i : i + size])).split()))
        if score > best_score:
            best, best_score = i, score
    return " ".join(tokens[best : best + size]).strip(" ,;:-")


def _segment_start(offsets: list[tuple[int, float]], position: int) -> float:
    """Начало сегмента, в который попадает позиция в склеенной расшифровке."""
    start = offsets[0][1]
    for offset, segment_start in offsets:
        if offset > position:
            break
        start = segment_start
    return start


def _locate(
    quote: str,
    segments: list[tuple[float, float, str]],
    haystack: str,
    offsets: list[tuple[int, float]],
    near: float | None = None,
) -> tuple[float, str] | None:
    """Начало сегмента, где звучит цитата, и её текст для карточки: сперва точное совпадение, потом по словам.

    Фраза может звучать в ролике не раз: из совпадений берётся ближайшее к таймкоду модели (near).
    При совпадении по словам цитата модели расходится с расшифровкой, поэтому в карточку идёт кусок
    самого сегмента: там всегда слова блогера.
    """
    needle = _normalize(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    starts: list[float] = []
    position = haystack.find(needle)
    while position >= 0:
        starts.append(_segment_start(offsets, position))
        position = haystack.find(needle, position + 1)
    if starts:
        start = min(starts, key=lambda s: abs(s - near)) if near is not None else starts[0]
        return start, quote.strip()
    # распознавание и цитата могут разойтись в паре слов, поэтому ищем сегмент с наибольшим совпадением
    words = set(needle.split())
    fitting: list[tuple[float, tuple[float, float, str]]] = []
    for segment in segments:
        ratio = len(words & set(_normalize(segment[2]).split())) / len(words)
        # мало общих слов: нужен ещё и подряд идущий кусок цитаты, иначе из частых слов совпадёт что угодно
        if ratio >= QUOTE_MATCH_RATIO and shares_phrase(quote, segment[2]):
            fitting.append((ratio, segment))
    if not fitting:
        return None
    distance = (lambda segment: abs(segment[0] - near)) if near is not None else (lambda segment: 0.0)
    _ratio, best = max(fitting, key=lambda item: (item[0], -distance(item[1])))
    return best[0], _window(quote, best[2])


def _locate_quote(
    quote: str,
    segments: list[tuple[float, float, str]],
    haystack: str,
    offsets: list[tuple[int, float]],
    near: float | None = None,
) -> tuple[float, str] | None:
    """Как _locate, но склеенную через многоточие цитату ищет по кускам: время и текст берутся у самого
    длинного найденного куска, склейка из разных мест ролика в карточку не попадает."""
    if not has_ellipsis(quote):
        return _locate(quote, segments, haystack, offsets, near)
    return next(
        (found for piece in fragments(quote) if (found := _locate(piece, segments, haystack, offsets, near))), None
    )


def attach_moments(conclusion: Conclusion, segments: Sequence[Sequence[Any]] | None) -> tuple[int, int]:
    """Сверяет моменты с расшифровкой и проставляет им время. Возвращает (подтверждено, отброшено).

    Время берём у сегмента, а не у модели: цитата тут проверка, что момент действительно есть в ролике.
    """
    clean = _clean_segments(segments)
    if not clean:
        for point in [*conclusion.pros, *conclusion.cons]:
            point.moments = []
        return 0, 0
    haystack_parts, offsets, position = [], [], 0
    for start, _end, text in clean:
        normalized = _normalize(text)
        offsets.append((position, start))
        haystack_parts.append(normalized)
        position += len(normalized) + 1
    haystack = " ".join(haystack_parts)
    first, last = clean[0][0], clean[-1][1]

    found = dropped = 0
    for point in [*conclusion.pros, *conclusion.cons]:
        verified: list[Moment] = []
        for moment in point.moments:
            # цитата есть, но в расшифровке её нет: момент выдуман, таймкоду модели тут верить нельзя
            has_quote = len(_normalize(moment.quote)) >= MIN_QUOTE_CHARS
            start = None
            said = parse_timecode(moment.time)
            if has_quote and (located := _locate_quote(moment.quote, clean, haystack, offsets, said)):
                start, moment.quote = located
            if start is None and not has_quote:
                # цитаты модель не дала: остаётся таймкод, если он попадает в расшифрованную часть
                if said is not None and first - TIME_TOLERANCE <= said <= last + TIME_TOLERANCE:
                    start = min(
                        (s for s, _e, _t in clean if abs(s - said) <= TIME_TOLERANCE or s <= said <= _e),
                        key=lambda s: abs(s - said),
                        default=None,
                    )
            if start is None:
                dropped += 1
                continue
            moment.seconds = max(0.0, start - MOMENT_LEAD_IN)
            moment.time = timecode(moment.seconds)
            verified.append(moment)
            found += 1
        # два момента в одной минуте это одно и то же место
        unique: dict[int, Moment] = {}
        for moment in sorted(verified, key=lambda m: m.seconds or 0.0):
            unique.setdefault(int((moment.seconds or 0.0) // 5), moment)
        point.moments = list(unique.values())[:MAX_MOMENTS]
    return found, dropped


def chunk_chars(provider: str, ceiling: int | None = None) -> int:
    """Предел расшифровки на один запрос в символах: по типу модели, но не больше настройки."""
    budget = CHUNK_TOKENS["local" if provider == "local" else "cloud"] * CHARS_PER_TOKEN
    return min(budget, ceiling) if ceiling else budget


def split_segments(
    segments: Sequence[Sequence[Any]] | None, max_chars: int
) -> list[list[tuple[float, float, str]]]:
    """Расшифровка на примерно равные части, границы по самым длинным паузам блогера.

    Число частей считается от всего объёма, поэтому части выходят одного размера: иначе последней
    достаётся пара фраз, и заключение по ней пустое. Граница ищется в окне вокруг идеальной точки
    и ставится на самую длинную паузу между сегментами: там блогер играет молча, мысль закончена.
    """
    clean = _clean_segments(segments)
    if not clean:
        return []
    sizes = [len(text) + 1 for _start, _end, text in clean]
    total = sum(sizes)
    count = max(1, math.ceil(total / max(1, max_chars)))
    if count == 1 or len(clean) < 2:
        return [clean]
    target = total / count
    slack = target * SPLIT_SLACK
    cumulative = list(itertools.accumulate(sizes))
    gaps = [0.0] + [max(0.0, clean[i][0] - clean[i - 1][1]) for i in range(1, len(clean))]
    bounds: list[int] = []
    first = 1
    for k in range(1, count):
        if first >= len(clean):
            break
        ideal = target * k
        # cumulative[i - 1]: сколько символов уйдёт в части до границы перед сегментом i
        near = [i for i in range(first, len(clean)) if abs(cumulative[i - 1] - ideal) <= slack]
        if near:
            index = max(near, key=lambda i: (round(gaps[i], 1), -abs(cumulative[i - 1] - ideal)))
        else:
            index = min(range(first, len(clean)), key=lambda i: abs(cumulative[i - 1] - ideal))
        bounds.append(index)
        first = index + 1
    parts: list[list[tuple[float, float, str]]] = []
    previous = 0
    for index in bounds:
        parts.append(clean[previous:index])
        previous = index
    parts.append(clean[previous:])
    return [part for part in parts if part]


def _same_point(first: str, second: str) -> bool:
    """Один и тот же тезис, сказанный разными словами: большая часть слов общая, и их хотя бы два."""
    a, b = set(_normalize(first).split()), set(_normalize(second).split())
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 2 and shared / min(len(a), len(b)) >= SAME_POINT_RATIO


def merge_points(parts: Sequence[Conclusion], kind: str) -> list[Point]:
    """Пункты "хвалит" или "ругает" из частей ролика: одинаковые по смыслу склеиваются,
    их моменты собираются вместе. Остаются пункты с наибольшим числом мест в ролике."""
    merged: list[Point] = []
    for conclusion in parts:
        for point in getattr(conclusion, kind):
            twin = next((known for known in merged if _same_point(known.text, point.text)), None)
            if twin is None:
                merged.append(point.model_copy(deep=True))
                continue
            seen = {(moment.time, moment.quote) for moment in twin.moments}
            twin.moments.extend(moment for moment in point.moments if (moment.time, moment.quote) not in seen)
            twin.moments = twin.moments[:MAX_MOMENTS]
    merged.sort(key=lambda point: -len(point.moments))
    return merged[:MAX_POINTS]


def build_conclusion_prompt(
    game: GameRef,
    candidate: Candidate,
    transcript: str,
    partial: bool,
    *,
    segments: Sequence[Sequence[Any]] | None = None,
    max_chars: int = 60_000,
    part: tuple[int, int] | None = None,
) -> str:
    body = transcript_with_timecodes(segments, max_chars) if segments else transcript[:max_chars]
    if part and part[1] > 1:
        coverage = (
            f"Расшифровка: часть {part[0]} из {part[1]}, ролик разобран по порядку от начала до конца."
            " Пиши только о том, что блогер говорит в этой части."
        )
    else:
        coverage = "Расшифровка: начало и концовка ролика." if partial else "Расшифровка: ролик целиком."
    parts = [
        f"Игра: {game.title}",
        f"Ролик: {candidate.title}",
        f"Канал: {candidate.channel or 'неизвестен'}",
        coverage,
        "",
        body,
    ]
    return "\n".join(parts)


def build_merge_prompt(
    game: GameRef, candidate: Candidate, parts: Sequence[Conclusion], pros: Sequence[Point], cons: Sequence[Point]
) -> str:
    lines = [f"Игра: {game.title}", f"Ролик: {candidate.title}", f"Частей ролика: {len(parts)}", ""]
    for n, conclusion in enumerate(parts, 1):
        lines.append(f"Часть {n}. {conclusion.summary} Вывод по части: {conclusion.verdict}")
    lines.append("")
    lines.append("Хвалит: " + ("; ".join(point.text for point in pros) or "ничего"))
    lines.append("Ругает: " + ("; ".join(point.text for point in cons) or "ничего"))
    return "\n".join(lines)


def _candidate_row(candidate: Candidate, verdict: Verdict | None) -> dict[str, Any]:
    """Кандидат вместе с решением модели: по базе видно, почему ролик выбран или отклонён."""
    row: dict[str, Any] = {
        "id": candidate.video_id, "title": candidate.title, "views": candidate.views,
        "channel": candidate.channel, "duration": candidate.duration, "was_live": candidate.was_live,
    }
    if verdict is not None:
        row["letsplay"] = verdict.letsplay
        row["reason"] = verdict.reason
    return row


class LetsplayStore:
    """Таблица letsplays. Схема создаётся здесь, db.py остаётся за обязательной частью."""

    def __init__(self, db: Database) -> None:
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(letsplays)")}
        for name, statement in LATER_COLUMNS.items():
            if name not in columns:
                self.conn.execute(statement)
        self.conn.commit()
        self._reclassify_content_failures()
        self._restore_rejected_videos()

    def _restore_rejected_videos(self) -> int:
        """Записи "летсплея нет", сделанные до списка отклонённых роликов: ролики отказа есть только в тексте
        ошибки. Без списка следующий проход скачал бы их снова, поэтому id переносятся из текста."""
        rows = self.conn.execute(
            "SELECT game_id, error FROM letsplays WHERE status = ? AND error IS NOT NULL AND rejected_videos IS NULL",
            (NOT_FOUND,),
        ).fetchall()
        changed = 0
        for row in rows:
            parts = [part.strip() for part in row["error"].split(";") if part.strip()]
            ids = [m.group(1) for part in parts if _CONTENT_REJECT.search(part) and (m := _VIDEO_ID_PREFIX.match(part))]
            if ids:
                self.conn.execute("UPDATE letsplays SET rejected_videos = ? WHERE game_id = ?", (json.dumps(ids), row["game_id"]))
                changed += 1
        if changed:
            self.conn.commit()
            log.info("letsplay: rejected videos restored from error text for %d games", changed)
        return changed

    def _reclassify_content_failures(self) -> int:
        """Записи прежней логики: сбоем считался и итог "все ролики отпали по содержанию" (нет речи,
        мало речи, нет мнения). Теперь это "летсплея нет": такие записи переводятся в not_found,
        отклонённые ролики запоминаются, и игра идёт по правилу повторного поиска, а не по лимиту сбоев."""
        rows = self.conn.execute("SELECT game_id, error FROM letsplays WHERE status = ? AND error IS NOT NULL", (FAILED,)).fetchall()
        changed = 0
        for row in rows:
            parts = [part.strip() for part in row["error"].split(";") if part.strip()]
            if not parts or not all(_CONTENT_REJECT.search(part) for part in parts):
                continue
            ids = [m.group(1) for m in (_VIDEO_ID_PREFIX.match(part) for part in parts) if m]
            self.conn.execute(
                "UPDATE letsplays SET status = ?, rejected_videos = ?, searches = MAX(searches, 1) WHERE game_id = ?",
                (NOT_FOUND, json.dumps(ids) if ids else None, row["game_id"]),
            )
            changed += 1
        if changed:
            self.conn.commit()
            log.info("letsplay: %d records reclassified from failed to not_found (all videos rejected by content)", changed)
        return changed

    def get(self, game_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM letsplays WHERE game_id = ?", (game_id,)
        ).fetchone()
        return row

    def needs_search(self, game_id: int, settings: "Settings", now: datetime | None = None) -> bool:
        """Поиск делается один раз на игру; неудача не откладывается надолго.

        failed (сбой): повтор через retry_minutes (и при каждом прогоне), но не больше max_attempts_per_game
        раз в сутки: счётчик попыток сбрасывается с новым днём (save). not_found (летсплея нет): повторный
        поиск по настройкам из интерфейса: через recheck_days дней, не больше recheck_max раз на игру,
        а с выключенным recheck не ищется вовсе. Отказ YouTube к попыткам не относится: у такой записи
        стоит retry_at, и она возвращается в работу через несколько минут, сколько бы раз он ни отказывал.
        """
        row = self.get(game_id)
        if row is None:
            return True
        if row["status"] == DONE:
            return False
        moment = now or datetime.now(timezone.utc)
        retry_at = row["retry_at"] if "retry_at" in row.keys() else None
        if retry_at:
            return moment >= datetime.fromisoformat(retry_at)
        updated = datetime.fromisoformat(row["updated_at"])
        waited = moment - updated
        if row["status"] == FAILED:
            if row["attempts"] >= settings.max_attempts_per_game and updated.date() == moment.date():
                return False
            return waited >= timedelta(minutes=settings.retry_minutes)
        # прошлый проход сдался раньше, чем проверил все найденные летсплеи: доделываем сразу, не ждём срока
        if self.untried_letsplays(row):
            return True
        if not settings.recheck:
            return False
        searches = row["searches"] if "searches" in row.keys() else 0
        if settings.recheck_max and searches > settings.recheck_max:
            return False
        return waited >= timedelta(days=settings.recheck_days)

    def next_search_at(self, row: sqlite3.Row, settings: "Settings") -> datetime | None:
        """Когда игра без летсплея (not_found) пойдёт в поиск снова; None, если повторный поиск выключен
        или лимит на игру исчерпан. Для карточки игры."""
        if row["status"] != NOT_FOUND:
            return None
        if self.untried_letsplays(row):
            return datetime.fromisoformat(row["updated_at"])
        if not settings.recheck:
            return None
        searches = row["searches"] if "searches" in row.keys() else 0
        if settings.recheck_max and searches > settings.recheck_max:
            return None
        return datetime.fromisoformat(row["updated_at"]) + timedelta(days=settings.recheck_days)

    @staticmethod
    def untried_letsplays(row: sqlite3.Row) -> list[str]:
        """Ролики, которые модель признала летсплеями, но проход до них не дошёл и не отклонил их.

        Только у записи "летсплея нет", где все проверенные ролики отпали по содержанию: у сбоя свой повтор.
        """
        if row["status"] != NOT_FOUND or not row["candidates"] or not row["error"]:
            return []
        parts = [part.strip() for part in row["error"].split(";") if part.strip()]
        if not parts or not all(_VIDEO_ID_PREFIX.match(part) and _CONTENT_REJECT.search(part) for part in parts):
            return []
        try:
            items = json.loads(row["candidates"])
            done = set(json.loads(row["rejected_videos"] or "[]"))
        except ValueError:
            return []
        return [item["id"] for item in items
                if isinstance(item, dict) and item.get("letsplay") and item.get("id") not in done]

    def rejected_videos(self, game_id: int) -> list[str]:
        row = self.get(game_id)
        if row is None or "rejected_videos" not in row.keys() or not row["rejected_videos"]:
            return []
        try:
            data = json.loads(row["rejected_videos"])
        except ValueError:
            return []
        return [str(item) for item in data] if isinstance(data, list) else []

    def save(self, game_id: int, result: Result, candidates: list[Candidate], *, defer_minutes: int = 15) -> None:
        """Итог по игре. При отказе YouTube (result.temporary) попытка не тратится, а ставится
        время следующего захода: иначе одна минута неработающего поиска съедала бы лимит попыток."""
        candidate = result.candidate
        conclusion = result.conclusion.model_dump() if result.conclusion else None
        retry_at = (
            (datetime.now(timezone.utc) + timedelta(minutes=defer_minutes)).isoformat(timespec="seconds")
            if result.temporary else None
        )
        # список пропуска копится между заходами: прежние отклонённые ролики плюс новые
        skipped = list(dict.fromkeys([*self.rejected_videos(game_id), *result.rejected_ids]))
        params = {
            "game_id": game_id,
            "status": result.status,
            "rejected_videos": json.dumps(skipped) if skipped else None,
            "video_id": candidate.video_id if candidate else None,
            "url": candidate.url if candidate else None,
            "title": candidate.title if candidate else None,
            "channel": candidate.channel if candidate else None,
            "views": candidate.views if candidate else None,
            "duration": candidate.duration if candidate else None,
            "published": candidate.published if candidate else None,
            "language": result.language,
            "transcript_source": result.source,
            "transcript": result.transcript or None,
            "segments": json.dumps(result.segments, ensure_ascii=False) if result.segments else None,
            "conclusion": json.dumps(conclusion, ensure_ascii=False) if conclusion else None,
            "candidates": json.dumps(
                [_candidate_row(c, result.verdicts.get(c.video_id)) for c in candidates], ensure_ascii=False
            ),
            "error": result.error,
            "retry_at": retry_at,
            "coverage": result.coverage,
            "now": _utc_now(),
        }
        columns = [k for k in params if k not in ("game_id", "now")]
        spent = 0 if result.temporary else 1
        # поиск без летсплея считается отдельно: по нему работает лимит повторных поисков
        searched = 1 if result.status == NOT_FOUND else 0
        searches = "searches = letsplays.searches + 1" if result.status == NOT_FOUND else "searches = letsplays.searches"
        # попытки считаются за сутки: с новым днём счёт начинается заново, и игра снова в работе
        grows = (
            "attempts = letsplays.attempts" if result.temporary
            else "attempts = CASE WHEN date(letsplays.updated_at) < date(excluded.updated_at) THEN 1"
                 " ELSE letsplays.attempts + 1 END"
        )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                f"INSERT INTO letsplays (game_id, {', '.join(columns)}, attempts, searches, updated_at) "
                f"VALUES (:game_id, {', '.join(':' + c for c in columns)}, {spent}, {searched}, :now) "
                f"ON CONFLICT (game_id) DO UPDATE SET "
                f"{', '.join(f'{c} = excluded.{c}' for c in columns)}, "
                f"{grows}, {searches}, updated_at = excluded.updated_at",
                params,
            )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")


class LetsplayWorker:
    """Один летсплей за раз: сеть, видеокарта и вызовы LLM идут по очереди."""

    def __init__(
        self,
        youtube: YouTubeClient,
        gemini: GeminiClient | None,
        transcriber: Transcriber,
        settings: Settings,
    ) -> None:
        self.youtube = youtube
        self.gemini = gemini  # поиск и расшифровка работают и без него, для ручных проверок
        self.transcriber = transcriber
        self.settings = settings

    def _llm(self) -> GeminiClient:
        if self.gemini is None:
            raise GeminiUnavailable("LLM client is not configured")
        return self.gemini

    async def find_candidates(self, game: GameRef, today: date | None = None) -> list[Candidate]:
        """Ролики, которые прошли отсев правилами. Второй запрос идёт, только если первый пуст."""
        for query in range(len(self.settings.queries)):
            candidates = await self.search_query(game, query, today)
            if candidates:
                return candidates
        return []

    async def search_query(
        self, game: GameRef, query: int, today: date | None = None, exclude: Iterable[str] = ()
    ) -> list[Candidate]:
        """Выдача одного запроса из settings.queries после отсева правилами, без уже просмотренных роликов."""
        today = today or datetime.now(timezone.utc).date()
        text = self.settings.queries[query].format(title=game.title)
        # две выдачи: по просмотрам (популярное) и по релевантности (ролики именно этой игры).
        # У малоизвестных игр выдача по просмотрам бывает пустой, а по релевантности нет,
        # поэтому сбой или пустота одной выдачи не отменяет вторую.
        found: dict[str, Candidate] = {}
        errors: list[str] = []
        blocked = 0
        for sort in ("views", "relevance"):
            try:
                items = await self.youtube.search(text, sort=sort)
            except YouTubeError as e:
                blocked += isinstance(e, YouTubeBlocked)
                log.warning("letsplay %s: search %r (%s) failed: %s", game.title, text, sort, e)
                errors.append(f"{sort}: {e}")
                continue
            for item in items:
                found.setdefault(item.video_id, item)
        if len(errors) == 2:
            # обе выдачи не пришли: если это отказ YouTube, игра ждёт повтора, а не помечается неудачной
            message = f"search {text!r} failed: {'; '.join(errors)}"
            raise (YouTubeBlocked if blocked else YouTubeError)(message)
        seen = set(exclude)
        candidates = filter_candidates(
            [item for item in found.values() if item.video_id not in seen],
            game_title=game.title,
            release_date=game.release_date,
            today=today,
            min_duration=self.settings.min_duration,
            max_duration=self.settings.max_duration,
        )
        return candidates[: self.settings.max_candidates]

    async def judge(self, game: GameRef, candidates: list[Candidate]) -> dict[str, Verdict]:
        """Решение модели по каждому ролику: летсплей ли это и почему (id -> вердикт)."""
        selection = await self._llm().generate(
            system=SELECT_SYSTEM,
            prompt=build_select_prompt(game, candidates),
            schema=SELECT_SCHEMA,
            output=Selection,
            safety=SAFETY,
        )
        wanted = {candidate.video_id for candidate in candidates}
        verdicts = {verdict.id: verdict for verdict in selection.videos if verdict.id in wanted}
        # чужие id отбрасываем; пропущенный ролик остаётся без вердикта и не выбирается
        invented = {verdict.id for verdict in selection.videos} - wanted
        missing = wanted - set(verdicts)
        if invented or missing:
            log.warning("letsplay %s: model skipped %d videos and returned %d unknown ids",
                        game.title, len(missing), len(invented))
        return verdicts

    async def choose(
        self, game: GameRef, candidates: list[Candidate], verdicts: dict[str, Verdict] | None = None
    ) -> list[Candidate]:
        """Оставляет настоящие летсплеи в порядке убывания просмотров. Без вердиктов спрашивает модель."""
        if verdicts is None:
            verdicts = await self.judge(game, candidates)
        chosen = [c for c in candidates if (verdict := verdicts.get(c.video_id)) is not None and verdict.letsplay]
        log.info("letsplay %s: %d of %d candidates look like letsplays", game.title, len(chosen), len(candidates))
        return chosen

    async def text_for(self, candidate: Candidate, hint: str | None = None) -> VideoText:
        """Текст ролика. Порядок источников задан настройкой, hint (название игры) помогает Whisper."""
        order = ["whisper", "captions"] if self.settings.transcript_source == "whisper" else ["captions", "whisper"]
        errors: list[str] = []
        for source in order:
            try:
                if source == "captions":
                    captions = await self.youtube.captions(candidate.video_id)
                    if captions:
                        # субтитры покрывают ролик целиком
                        heard = candidate.duration or captions.segments[-1][1]
                        return VideoText("captions", captions.language, captions.segments, captions.text, float(heard))
                    errors.append("captions: нет субтитров")
                    continue
                return await self._whisper(candidate, hint)
            except YouTubeBlocked:
                # YouTube не пускает наш адрес: второй источник спрашивать бесполезно, игра не виновата
                raise
            except (YouTubeError, TranscribeError) as e:
                log.warning("letsplay %s: %s failed: %s", candidate.video_id, source, e)
                errors.append(f"{source}: {e}")
        raise YouTubeError("; ".join(errors) or "no transcript")

    async def _whisper(self, candidate: Candidate, hint: str | None) -> VideoText:
        track = await self.youtube.audio_track(candidate.video_id)
        if track.age_limit:
            raise YouTubeError(f"age-restricted video ({track.age_limit}+), audio is not available without cookies")
        if track.live_status in ("is_live", "is_upcoming"):
            raise YouTubeError(f"live video ({track.live_status})")
        # без отдельной аудиодорожки YouTube отдал склеенный 360p: он в разы тяжелее, лимит на него мягче
        limit = self.settings.max_audio_bytes * (MUXED_SIZE_FACTOR if track.muxed else 1)
        if track.filesize and track.filesize > limit:
            raise YouTubeError(f"audio is too large ({track.filesize / 2**20:.0f} MB)")
        if track.muxed:
            log.info("letsplay %s: no audio-only format, downloading muxed %s", candidate.video_id, track.format_id)
        path = await self.youtube.download_audio(candidate.video_id, self.settings.workdir, max_filesize=limit)
        duration = track.duration or candidate.duration or 0
        whole = self.hears_whole(duration)
        try:
            transcript = await self.transcriber.transcribe(
                path, duration=duration, language=track.language, hint=hint, full=whole,
            )
        finally:
            path.unlink(missing_ok=True)
        segments = [(s.start, s.end, s.text) for s in transcript.segments]
        return VideoText("whisper", transcript.language, segments, transcript.text, transcript.audio_seconds, whole)

    def hears_whole(self, duration: float) -> bool:
        """Ролик расшифровывается целиком, пока не длиннее предела; дальше только начало и концовка."""
        return duration <= self.settings.full_max_seconds

    async def _ask_conclusion(
        self, game: GameRef, candidate: Candidate, text: VideoText, partial: bool,
        segments: Sequence[Sequence[Any]], part: tuple[int, int] | None, max_chars: int,
    ) -> Conclusion:
        return await self._llm().generate(
            system=CONCLUSION_SYSTEM,
            prompt=build_conclusion_prompt(
                game, candidate, text.text, partial, segments=segments, max_chars=max_chars, part=part,
            ),
            schema=CONCLUSION_SCHEMA,
            output=Conclusion,
            safety=SAFETY,
        )

    async def _merge(self, game: GameRef, candidate: Candidate, parts: list[Conclusion]) -> Conclusion:
        """Одно заключение из нескольких: пункты склеиваются кодом, общий текст пишет модель.

        Если модель не справилась с общим текстом, берётся вывод самой содержательной части:
        собранные пункты с моментами важнее, чем идеальный абзац сверху.
        """
        pros, cons = merge_points(parts, "pros"), merge_points(parts, "cons")
        commentary = any(part.commentary for part in parts)
        try:
            digest = await self._llm().generate(
                system=MERGE_SYSTEM,
                prompt=build_merge_prompt(game, candidate, parts, pros, cons),
                schema=MERGE_SCHEMA,
                output=Digest,
                safety=SAFETY,
            )
            summary, verdict = digest.summary, digest.verdict
        except GeminiBadOutput as e:
            log.warning("letsplay %s: общий текст по частям не получился, берём самую полную часть: %s",
                        candidate.video_id, e)
            richest = max(parts, key=lambda part: len(part.pros) + len(part.cons))
            summary, verdict = richest.summary, richest.verdict
        # пункты передаются словарями: приводящий валидатор Conclusion объекты Point считает строками
        return Conclusion(
            summary=summary, verdict=verdict, commentary=commentary,
            pros=[point.model_dump() for point in pros], cons=[point.model_dump() for point in cons],
        )

    async def conclude(self, game: GameRef, candidate: Candidate, text: VideoText, partial: bool) -> Conclusion:
        """Заключение с местами в ролике: модель называет таймкод и цитату, время сверяется с расшифровкой.

        Длинная расшифровка не влезает в один запрос: она режется на равные части по паузам блогера,
        каждая часть разбирается отдельно, потом пункты склеиваются и модель пишет общий текст.
        """
        budget = chunk_chars(self._llm().provider, self.settings.transcript_max_chars)
        pieces = split_segments(text.segments, budget) or [list(text.segments)]
        if len(pieces) == 1:
            conclusion = await self._ask_conclusion(game, candidate, text, partial, pieces[0], None, budget)
        else:
            log.info("letsplay %s: расшифровка %d символов разбита на %d частей по %d символов",
                     candidate.video_id, len(text.text), len(pieces), budget)
            parts = [
                await self._ask_conclusion(game, candidate, text, partial, piece, (n, len(pieces)), budget)
                for n, piece in enumerate(pieces, 1)
            ]
            conclusion = await self._merge(game, candidate, parts)
        confirmed, dropped = attach_moments(conclusion, text.segments)
        log.info("letsplay %s: %d moments confirmed, %d dropped", candidate.video_id, confirmed, dropped)
        return conclusion

    async def process(
        self, game: GameRef, today: date | None = None, skip: Iterable[str] = ()
    ) -> tuple[Result, list[Candidate]]:
        """Полный проход по одной игре. Ошибки возвращаются статусом, а не исключением.

        skip: ролики, уже признанные непригодными для этой игры (нет речи, нет мнения): их не качаем снова.

        Ролик без речи блогера отсеивается сразу после расшифровки и попытку не тратит: проход берёт
        следующий найденный летсплей, а когда найденные кончились, ищет новые по следующему запросу.
        Попытка это ролик, дошедший до модели, или сбой загрузки и расшифровки; их не больше
        max_text_attempts за проход.
        """
        skip = set(skip)
        candidates: list[Candidate] = []  # все просмотренные ролики прохода, с решениями модели
        verdicts: dict[str, Verdict] = {}
        queue: list[Candidate] = []  # летсплеи, которые ещё предстоит проверить
        next_query = 0

        async def more() -> bool:
            """Новые летсплеи в очередь: выдача очередного запроса без уже просмотренных роликов."""
            nonlocal next_query
            while next_query < len(self.settings.queries):
                query, next_query = next_query, next_query + 1
                found = await self.search_query(game, query, today, exclude=[c.video_id for c in candidates])
                if not found:
                    continue
                candidates.extend(found)
                batch = await self.judge(game, found)
                verdicts.update(batch)
                fresh = [c for c in await self.choose(game, found, batch) if c.video_id not in skip]
                if fresh:
                    queue.extend(fresh)
                    return True
            return False

        try:
            if not await more():
                if not candidates:
                    error = "поиск не дал подходящих роликов"
                elif not any(v.letsplay for v in verdicts.values()):
                    error = "среди роликов нет летсплеев"
                else:
                    error = "новых роликов нет: найденные уже проверены и не подошли"
                return Result(status=NOT_FOUND, error=error, verdicts=verdicts), candidates
        except YouTubeBlocked as e:
            # YouTube не отдал выдачу: игра ни в чём не виновата, повторим поиск позже
            return Result(
                status=FAILED, error=f"временный сбой YouTube: {e}", temporary=True, verdicts=verdicts
            ), candidates
        except (YouTubeError, GeminiError) as e:
            return Result(status=FAILED, error=f"{type(e).__name__}: {e}", verdicts=verdicts), candidates

        errors: list[str] = []  # сбои: сеть, загрузка, расшифровка, ответ модели
        rejected: list[str] = []  # ролик оказался не рассказом блогера
        rejected_ids: list[str] = []  # такие ролики запоминаются для игры, повторно их не качаем
        # каналы, чей ролик оказался без речи: остальные части того же прохождения такие же
        silent_channels: set[str] = set()
        attempts = 0

        def reject(candidate: Candidate, reason: str, *, silent: bool = False) -> None:
            log.info("letsplay %s: %s rejected, %s", game.title, candidate.video_id, reason)
            rejected.append(f"{candidate.video_id}: {reason}")
            rejected_ids.append(candidate.video_id)
            if silent and candidate.channel:
                silent_channels.add(candidate.channel)

        while attempts < self.settings.max_text_attempts:
            if not queue:
                try:
                    if not await more():
                        break
                except YouTubeBlocked as e:
                    return Result(
                        status=FAILED, error=f"временный сбой YouTube: {e}", temporary=True, verdicts=verdicts,
                        rejected_ids=rejected_ids,
                    ), candidates
                except (YouTubeError, GeminiError) as e:
                    errors.append(f"поиск новых роликов: {type(e).__name__}: {e}")
                    break
            candidate = queue.pop(0)
            if candidate.channel and candidate.channel in silent_channels:
                reject(candidate, "тот же канал, в его ролике мало речи")
                continue
            try:
                text = await self.text_for(candidate, hint=game.title)
            except YouTubeBlocked as e:
                # проверка на робота при загрузке: попытка не тратится, игра вернётся после паузы
                log.warning("letsplay %s: YouTube blocked the download, retry later: %s", game.title, e)
                return Result(
                    status=FAILED, error=f"временный сбой YouTube: {e}", temporary=True, verdicts=verdicts,
                    rejected_ids=rejected_ids,
                ), candidates
            except (YouTubeError, TranscribeError) as e:
                if NO_SPEECH in str(e):
                    # Whisper прослушал ролик и не нашёл голоса, субтитров нет: это итог по ролику, а не сбой
                    reject(candidate, "в ролике нет речи блогера", silent=True)
                else:
                    attempts += 1
                    errors.append(f"{candidate.video_id}: {e}")
                continue
            # прохождение без комментариев: блогер только здоровается и прощается, заголовок
            # этого не выдаёт, а по плотности речи видно сразу и без вызова модели
            if text.chars_per_minute < self.settings.min_chars_per_minute:
                reject(candidate, f"мало речи блогера ({text.chars_per_minute:.0f} символов в минуту)", silent=True)
                continue
            # ролик длиннее предела расшифрован только по краям: об этом сказано модели и в карточке
            partial = not text.whole
            attempts += 1
            try:
                conclusion = await self.conclude(game, candidate, text, partial)
            except GeminiBadOutput as e:
                errors.append(f"{candidate.video_id}: заключение не получилось: {e}")
                continue
            except GeminiError as e:
                return Result(
                    status=FAILED, error=f"{type(e).__name__}: {e}", candidate=candidate, verdicts=verdicts,
                    rejected_ids=rejected_ids,
                ), candidates
            # речи много, но это диалоги самой игры, или блогер ничего не оценивает
            if not conclusion.has_opinion:
                reject(candidate, "в ролике нет мнения блогера об игре")
                continue
            return (
                Result(
                    status=DONE,
                    candidate=candidate,
                    language=text.language,
                    source=text.source,
                    transcript=text.text,
                    segments=text.segments,
                    conclusion=conclusion,
                    verdicts=verdicts,
                    coverage="full" if text.whole else "edges",
                ),
                candidates,
            )
        if errors:
            # был сбой: повтор позже может дать результат
            return Result(
                status=FAILED, error="; ".join(errors + rejected), verdicts=verdicts, rejected_ids=rejected_ids
            ), candidates
        # все ролики отпали по содержанию: это "летсплея нет", повтор по правилу повторного поиска
        return Result(
            status=NOT_FOUND, error="; ".join(rejected) or "не удалось получить текст", verdicts=verdicts,
            rejected_ids=rejected_ids,
        ), candidates


async def process_game(worker: LetsplayWorker, store: LetsplayStore, game: GameRef, today: date | None = None) -> Result:
    result, candidates = await worker.process(game, today, skip=store.rejected_videos(game.id))
    store.save(game.id, result, candidates, defer_minutes=worker.settings.defer_minutes)
    if result.temporary:
        log.warning("letsplay %s: отложено на %d мин, попытка не потрачена: %s",
                    game.title, worker.settings.defer_minutes, result.error)
    else:
        log.info("letsplay %s: %s", game.title, result.status)
    return result


def game_from_row(row: sqlite3.Row) -> GameRef:
    def as_list(value: Any) -> list[str]:
        try:
            data = json.loads(value or "[]")
        except ValueError:
            return []
        return [str(item) for item in data] if isinstance(data, list) else []

    release = row["release_date"]
    # основная платформа отличает издание для одной приставки от исходной игры
    platform = row["main_platform"] if "main_platform" in row.keys() else None
    return GameRef(
        id=row["id"],
        title=row["title"],
        release_date=date.fromisoformat(release) if release else None,
        genres=as_list(row["genres"]),
        developers=as_list(row["developers"]),
        platforms=[platform] if platform else [],
    )
