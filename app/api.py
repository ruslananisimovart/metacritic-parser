"""JSON API: каталог игр, мониторинг, ручной запуск прогона и выбор модели."""

import asyncio
import json
import logging
import math
import os
import secrets
import signal
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app import export, storage
from app.calendar import build as calendar_build
from app.catalog import HAS_FILTERS, Catalog, SortField, SortOrder
from app.config import ROOT
from app.db import Database
from app.gemini import GeminiClient
from app.letsplay import CHUNK_TOKENS
from app.pipeline import MAX_FAILURES_PER_DAY, current_day
from app.runconfig import (
    CHANGES,
    LETSPLAY_KEY,
    MAX_GAMES,
    MAX_LETSPLAY_MINUTES,
    MAX_RECHECKS,
    MIN_GAMES,
    MIN_LETSPLAY_MINUTES,
    STALE_DAYS,
    LetsplayConfig,
    RunConfig,
)
from app.runconfig import SETTING_KEY as RUN_KEY
from app.runconfig import load as load_run
from app.schedule import SETTING_KEY as SCHEDULE_KEY
from app.schedule import ScheduleConfig, next_runs, rule_text
from app.scheduler import ManualRunRefused, Scheduler
from app.status import ServiceStatus

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

HEARTBEAT_SECONDS = 15
# несколько изменений подряд (игра за игрой) склеиваются в одно событие
COALESCE_SECONDS = 0.3
# ключ в таблице settings: выбор модели из интерфейса переживает перезапуск
LLM_SETTING = "llm"


class GameBrief(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    # откуда обложка: metacritic или steam; steam отдаёт постер (portrait) или широкую шапку (header)
    cover_source: str | None = None
    cover_kind: str | None = None
    # полный анализ: обложка, описание, видео, резюме по сторонам с отзывами, летсплей
    complete: bool = False
    release_date: str | None
    metascore: int | None
    userscore: float | None
    main_platform: str | None
    platforms: list[str]
    genres: list[str]
    developers: list[str]
    # число рецензий и оценок: в каталоге подписи под оценками ("64 рецензии", "1208 оценок")
    critic_reviews: int
    user_reviews: int
    letsplay_status: str | None = None


class GameList(BaseModel):
    total: int
    # сколько игр с каждой отметкой фильтра при тех же поиске и платформе
    facets: dict[str, int] = {}
    items: list[GameBrief]


class PlatformScore(BaseModel):
    name: str
    slug: str
    is_main: bool
    metascore: int | None
    critic_reviews: int
    userscore: float | None
    user_reviews: int


class ReviewSource(BaseModel):
    """Отзыв, на который опирается пункт резюме. У критика ссылка на рецензию, у игрока на список отзывов."""

    author: str | None
    score: float | None
    date: str | None
    quote: str
    url: str | None


class SummaryPoint(BaseModel):
    text: str
    sources: list[ReviewSource]


class SummaryOut(BaseModel):
    likes: list[SummaryPoint]
    dislikes: list[SummaryPoint]
    summary: str
    reviews_used: int
    reviews_total: int
    model: str
    updated_at: str


class Summaries(BaseModel):
    critic: SummaryOut | None
    user: SummaryOut | None


class SimilarGame(BaseModel):
    slug: str
    title: str
    cover_url: str | None
    cover_source: str | None = None
    cover_kind: str | None = None
    metascore: int | None
    userscore: float | None
    score: float


class LetsplayMoment(BaseModel):
    """Место в ролике, где блогер говорит про этот пункт: подпись, цитата и ссылка с переходом к времени."""

    seconds: float
    label: str
    quote: str
    url: str | None


class LetsplayPoint(BaseModel):
    text: str
    moments: list[LetsplayMoment]


class LetsplayConclusion(BaseModel):
    summary: str
    pros: list[LetsplayPoint]
    cons: list[LetsplayPoint]
    verdict: str


class TranscriptSegment(BaseModel):
    start: float
    text: str


class Coverage(BaseModel):
    head_minutes: int
    tail_minutes: int


class LetsplayOut(BaseModel):
    status: str
    url: str | None
    video_id: str | None
    title: str | None
    channel: str | None
    views: int | None
    duration: int | None
    published: str | None
    language: str | None
    transcript_source: str | None
    partial: bool
    coverage: Coverage | None
    conclusion: LetsplayConclusion | None
    segments: list[TranscriptSegment]
    attempts: int
    max_attempts: int
    error: str | None
    reason: str | None
    updated_at: str
    # летсплея нет: когда следующий повторный поиск (None: выключен или лимит исчерпан)
    next_search_at: str | None = None
    recheck: bool = True


class GameCard(GameBrief):
    description: str | None
    # metacritic, steam или gplay: откуда взято описание
    description_source: str | None = None
    rating: str | None
    publishers: list[str]
    video_url: str | None
    video_title: str | None
    # metacritic (страница с плеером) или steam (поток HLS, с кадром-заставкой)
    video_source: str | None
    video_poster: str | None = None
    # куда ведёт "Открыть": страница плеера Metacritic или страница игры в Steam
    video_page: str | None = None
    metacritic_url: str
    updated_at: str
    platform_scores: list[PlatformScore]
    summaries: Summaries
    similar: list[SimilarGame]
    letsplay: LetsplayOut | None


class PlatformCount(BaseModel):
    slug: str
    name: str
    games: int


class RunInfo(BaseModel):
    id: int
    trigger: str
    day: str
    status: str
    selected: int
    created: int
    updated: int
    failed: int
    summaries: int
    media_checked: int = 0
    media_found: int = 0
    summaries_checked: int = 0
    # старые игры на перепроверку: сколько выбрано и сколько загрузилось (в created/updated не входят)
    stale_selected: int = 0
    rechecked: int = 0
    lp_total: int = 0
    lp_found: int = 0
    lp_not_found: int = 0
    lp_failed: int = 0
    error: str | None
    started_at: str
    finished_at: str | None


class RunPage(BaseModel):
    total: int
    items: list[RunInfo]


class RunIn(BaseModel):
    """Настройки прогона из того же окна: сколько новинок, перепроверка каталога (раз в сколько дней
    и сколько игр за прогон), порог пересборки резюме."""

    games: Annotated[int, Field(ge=MIN_GAMES, le=MAX_GAMES)] = 20
    stale: bool = True
    stale_days: Literal[1, 3, 7, 14, 30] = 7
    stale_games: Annotated[int, Field(ge=MIN_GAMES, le=MAX_GAMES)] = 10
    change: float = 0.1

    @field_validator("change")
    @classmethod
    def known_change(cls, value: float) -> float:
        # порог выбирается из готового списка: произвольные доли интерфейс не предлагает
        if not any(abs(value - choice) < 1e-6 for choice in CHANGES):
            raise ValueError(f"change must be one of {CHANGES}")
        return value

    def config(self) -> RunConfig:
        return RunConfig(
            games=self.games, stale=self.stale, stale_days=self.stale_days, stale_games=self.stale_games, change=self.change
        ).validated()


class LetsplayIn(BaseModel):
    """Длительность роликов для летсплеев из панели мониторинга, в минутах; max_minutes 0: без верхней границы."""

    min_minutes: Annotated[int, Field(ge=MIN_LETSPLAY_MINUTES, le=MAX_LETSPLAY_MINUTES)] = 10
    max_minutes: Annotated[int, Field(ge=0, le=MAX_LETSPLAY_MINUTES)] = 120
    # повторный поиск для игр без летсплея: включён ли, через сколько дней, не больше скольких раз (0 без лимита)
    recheck: bool = True
    recheck_days: Literal[1, 7, 14, 30, 90] = 30
    recheck_max: Annotated[int, Field(ge=0, le=MAX_RECHECKS)] = 3

    @model_validator(mode="after")
    def ordered(self) -> "LetsplayIn":
        # верхняя граница ниже нижней означает, что ни один ролик не подойдёт: такое не принимаем
        if self.max_minutes and self.max_minutes < self.min_minutes:
            raise ValueError(f"max_minutes {self.max_minutes} is below min_minutes {self.min_minutes}")
        return self

    def config(self) -> LetsplayConfig:
        return LetsplayConfig(
            min_minutes=self.min_minutes, max_minutes=self.max_minutes,
            recheck=self.recheck, recheck_days=self.recheck_days, recheck_max=self.recheck_max,
        ).validated()


class ScheduleIn(BaseModel):
    """Расписание из интерфейса. Время в поясе сервиса; у почасового режима берутся только минуты."""

    enabled: bool = True
    mode: Literal["hourly", "daily", "days", "monthly"]
    time: Annotated[str, Field(pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")]
    every: Annotated[int, Field(ge=1, le=60)] = 2
    month_day: Annotated[int, Field(ge=1, le=31)] = 1
    start: date | None = None
    # настройки прогона и летсплеев окно сохраняет вместе с расписанием; без них остаются прежние
    run: RunIn | None = None
    letsplay: LetsplayIn | None = None

    def config(self) -> ScheduleConfig:
        return ScheduleConfig(
            enabled=self.enabled, mode=self.mode, time=self.time, every=self.every,
            month_day=self.month_day, start=self.start.isoformat() if self.start else None,
        ).validated()


class Health(BaseModel):
    status: str
    games: int
    last_run: RunInfo | None
    next_run_at: str | None
    run_in_progress: bool


class LlmModel(BaseModel):
    id: str
    recommended: bool
    note: str | None


class LlmProvider(BaseModel):
    id: str
    title: str
    available: bool
    reason: str | None
    default_model: str
    models: list[LlmModel]


class LlmOptions(BaseModel):
    provider: str
    title: str
    model: str
    reason: str | None
    providers: list[LlmProvider]


class LlmChoice(BaseModel):
    provider: Literal["kie", "google", "local"]
    model: Annotated[str, Field(min_length=1, max_length=200)]


def restore_llm_choice(db: Database, llm: GeminiClient) -> None:
    """Выбор из интерфейса важнее .env; битая или устаревшая запись пропускается."""
    raw = db.get_setting(LLM_SETTING)
    if raw is None:
        return
    try:
        choice = json.loads(raw)
        llm.select(choice["provider"], choice["model"])
    except (ValueError, KeyError, TypeError) as e:
        log.warning("stored LLM choice %r is ignored: %s", raw, e)


def get_catalog(request: Request) -> Catalog:
    catalog: Catalog = request.app.state.catalog
    return catalog


CatalogDep = Annotated[Catalog, Depends(get_catalog)]


@router.get("/health", response_model=Health)
def health(request: Request, catalog: CatalogDep) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    next_run = scheduler.next_run_at if scheduler else None
    return {
        "status": "ok",
        **catalog.status(),
        "next_run_at": next_run.isoformat(timespec="seconds") if next_run else None,
        "run_in_progress": bool(scheduler and scheduler.run_in_progress),
    }


@router.get("/games", response_model=GameList)
def list_games(
    catalog: CatalogDep,
    q: Annotated[str | None, Query(max_length=100, description="часть названия игры")] = None,
    platform: Annotated[str | None, Query(max_length=50, description="slug платформы, например pc")] = None,
    sort: SortField = "release_date",
    order: SortOrder = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
    has: Annotated[str | None, Query(
        max_length=200,
        description="через запятую, что должно быть у игры: complete, cover, description, video, scores, critic_summary,"
                    " user_summary, letsplay",
    )] = None,
) -> dict[str, Any]:
    keys = [key for key in (has or "").split(",") if key in HAS_FILTERS]
    return catalog.games(q=q, platform=platform, sort=sort, order=order, limit=limit, offset=offset, has=keys)


@router.get("/games/{slug}", response_model=GameCard)
def game_card(slug: str, catalog: CatalogDep) -> dict[str, Any]:
    game = catalog.game(slug)
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")
    return game


@router.get("/platforms", response_model=list[PlatformCount])
def platforms(catalog: CatalogDep) -> list[dict[str, Any]]:
    return catalog.platforms()


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


def _memory_status(app: FastAPI) -> dict[str, Any]:
    """Часть статуса из памяти процесса. Вызывать в потоке event loop, где её меняют воркеры."""
    status: ServiceStatus = app.state.status
    scheduler = app.state.scheduler
    gemini: GeminiClient | None = app.state.gemini
    now = time.monotonic()
    keys = [
        {
            "name": key.name,
            "state": key.state(),
            "calls": key.calls,
            "credits": round(key.credits, 4),
            "tokens": {
                "prompt": key.prompt_tokens,
                "output": key.output_tokens,
                "thinking": key.thinking_tokens,
            },
            "last_error": key.last_error,
            "cooldown_seconds": max(0, math.ceil(key.cooldown_until - now)),
        }
        for key in (gemini.keys if gemini else [])
    ]
    return {
        **status.snapshot(),
        # интерфейс показывает время в поясе сервиса, а не в поясе браузера
        "timezone": app.state.settings.timezone.key,
        "scheduler": {
            # enabled: планировщик запущен (SCHEDULER_ENABLED); active: расписание не на паузе
            "enabled": scheduler is not None,
            "active": bool(scheduler and scheduler.config.enabled),
            "config": scheduler.config.as_dict() if scheduler else None,
            "rule": rule_text(scheduler.config) if scheduler else None,
            "next_run_at": _iso(scheduler.next_run_at) if scheduler else None,
            "run_in_progress": bool(scheduler and scheduler.run_in_progress),
            "manual_available_at": _iso(scheduler.manual_available_at) if scheduler else None,
            # настройки следующего прогона: источник игр, сколько берём, порог пересборки резюме
            "run": app.state.run_config.as_dict(),
        },
        # какие ролики берём в летсплеи и как их разбираем: окно настроек читает это из статуса
        "letsplay_settings": _letsplay_view(app),
        "llm": {"provider": gemini.provider, "title": gemini.title, "model": gemini.model} if gemini else None,
        "gemini_keys": keys,
        "whisper": app.state.whisper,
        "storage": _storage(app),
    }


async def _full_status(app: FastAPI) -> dict[str, Any]:
    memory = _memory_status(app)
    if memory["llm"] is not None:
        # у LM Studio проверка готовности ходит на сервер, ответ кэшируется на 15 секунд
        memory["llm"]["reason"] = await app.state.gemini.ready()
    # видеокарта, процессор, память, диск: nvidia-smi и системные вызовы в потоке, снимок кэшируется на 5 с
    system = getattr(app.state, "system", None)
    memory["system"] = await asyncio.to_thread(system.snapshot) if system is not None else None
    tracker = getattr(app.state, "transcriber", None)
    if memory["whisper"] is not None and tracker is not None and hasattr(tracker, "snapshot"):
        memory["whisper"] = {**memory["whisper"], **tracker.snapshot()}
    youtube = getattr(app.state, "youtube", None)
    if memory["whisper"] is not None:
        # ход загрузки звука летсплея: этап до расшифровки, обычно самый долгий
        memory["whisper"] = {**memory["whisper"], "download": youtube.download if youtube is not None else None}
    # вход в YouTube из профиля Chrome: когда обновляли cookies и жив ли аккаунт
    memory["youtube"] = (
        {"profile": youtube.has_profile, "cookies": youtube.cookies_state, "blocks": youtube.blocks}
        if youtube is not None else None
    )
    # обновление старых игр по настройкам прогона: скольким пора сегодня и когда наступит очередь следующей
    tz = app.state.settings.timezone
    today = current_day(tz)
    run_cfg = load_run(app.state.db.get_setting(RUN_KEY))
    due, next_at = app.state.db.stale_outlook(today, run_cfg.stale_days, tz)
    memory["stale"] = {"enabled": run_cfg.stale, "days": run_cfg.stale_days, "games": run_cfg.stale_games,
                       "due": due, "next_at": next_at}
    catalog: Catalog = app.state.catalog
    day = today.isoformat()
    stored = await asyncio.to_thread(catalog.monitor, day, MAX_FAILURES_PER_DAY)
    return {**memory, **stored}


@router.get("/status")
async def service_status(request: Request) -> dict[str, Any]:
    """Снимок для мониторинга: воркеры, текущий прогон, модель и её ключи, история прогонов, ошибки."""
    return await _full_status(request.app)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    """Server-Sent Events: полный снимок статуса при каждом изменении и не реже раза в 15 секунд."""
    status: ServiceStatus = request.app.state.status

    async def stream() -> AsyncIterator[str]:
        version = -1
        # при остановке сервиса поток закрывается сам, иначе uvicorn ждал бы открытые соединения
        while not await request.is_disconnected() and not getattr(request.app.state, "stopping", False):
            await status.wait_for_change(version, HEARTBEAT_SECONDS)
            await asyncio.sleep(COALESCE_SECONDS)
            payload = await _full_status(request.app)
            version = payload["version"]
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/runs", status_code=202)
async def start_run(request: Request) -> dict[str, str]:
    """Внеплановый прогон. 409: прогон уже идёт; 429: ручной запуск был недавно (есть Retry-After)."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler is disabled")
    try:
        scheduler.request_run()
    except ManualRunRefused as e:
        if e.retry_after is None:
            raise HTTPException(status_code=409, detail=str(e)) from None
        raise HTTPException(
            status_code=429, detail=str(e), headers={"Retry-After": str(math.ceil(e.retry_after))}
        ) from None
    return {"status": "started"}


@router.post("/runs/stop", status_code=202)
async def stop_run(request: Request) -> dict[str, str]:
    """Остановить текущий прогон: он помечается прерванным, следующий пойдёт по расписанию. 409: прогона нет."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler is disabled")
    if not scheduler.stop_run():
        raise HTTPException(status_code=409, detail="no run in progress")
    log.info("run stop requested from the interface")
    return {"status": "stopping"}


@router.get("/runs", response_model=RunPage)
def run_history(
    catalog: CatalogDep,
    status: Literal["all", "ok", "bad"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """История прогонов постранично: все, успешные или с ошибками."""
    return catalog.runs_page(status, limit, offset)


def _scheduler(request: Request) -> Scheduler:
    scheduler: Scheduler | None = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler is disabled")
    return scheduler


def _run_config(request: Request) -> RunConfig:
    return load_run(request.app.state.db.get_setting(RUN_KEY))


def _letsplay_view(app: FastAPI) -> dict[str, Any]:
    """Границы длительности роликов и то, что интерфейсу нужно для пояснений."""
    return {
        **app.state.letsplay_config.as_dict(),
        "enabled": app.state.settings.letsplay_enabled,
        "full_max_minutes": app.state.letsplay_base.full_max_seconds // 60,
        "chunk_tokens": CHUNK_TOKENS,
    }


def _schedule_view(config: ScheduleConfig, tz: ZoneInfo, run: RunConfig, app: FastAPI) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "config": config.as_dict(),
        "rule": rule_text(config),
        "timezone": tz.key,
        "next": [moment.astimezone(timezone.utc).isoformat(timespec="seconds") for moment in next_runs(config, now, tz, 3)],
        "run": run.as_dict(),
        "run_limits": {"min": MIN_GAMES, "max": MAX_GAMES, "changes": list(CHANGES)},
        # сколько игр каталога ждут перепроверки при каждом варианте интервала и когда устареет следующая:
        # подпись и второй таймер в окне настроек
        "stale_due": {str(days): app.state.db.stale_outlook(current_day(tz), days, tz)[0] for days in STALE_DAYS},
        "stale_next": {str(days): app.state.db.stale_outlook(current_day(tz), days, tz)[1] for days in STALE_DAYS},
        "letsplay": _letsplay_view(app),
    }


@router.get("/calendar")
async def calendar_month(
    request: Request,
    year: Annotated[int, Query(ge=2020, le=2100)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> dict[str, Any]:
    """Календарь мониторинга за месяц: по дням что было (прогоны, игры, летсплеи, ошибки) и что будет
    (плановые запуски, старые игры на обновление, повторные поиски летсплеев, запросы к магазинам)."""
    app = request.app
    tz = app.state.settings.timezone
    scheduler = app.state.scheduler
    schedule = scheduler.config if scheduler else ScheduleConfig(enabled=False)
    return await asyncio.to_thread(
        calendar_build,
        app.state.settings.db_path,
        year=year,
        month=month,
        tz=tz,
        today=current_day(tz),
        now=datetime.now(timezone.utc),
        schedule=schedule,
        run=_run_config(request),
        letsplay=app.state.letsplay_config,
        scheduler_active=bool(scheduler) and schedule.enabled,
    )


@router.get("/schedule")
async def get_schedule(request: Request) -> dict[str, Any]:
    """Расписание прогонов, настройки прогона и летсплеев, три ближайших запуска."""
    scheduler = _scheduler(request)
    return _schedule_view(scheduler.config, scheduler.tz, _run_config(request), request.app)


@router.put("/schedule")
async def put_schedule(body: ScheduleIn, request: Request) -> dict[str, Any]:
    """Новое расписание или пауза (enabled=false). Текущий прогон не прерывается.

    Вместе с расписанием сохраняются настройки прогона и летсплеев: прогон читает их перед
    запуском, воркер летсплеев перед каждой игрой, перезапуск не нужен.
    """
    scheduler = _scheduler(request)
    config = body.config()
    app = request.app
    db = app.state.db
    db.set_setting(SCHEDULE_KEY, json.dumps(config.as_dict()))
    run = _run_config(request)
    if body.run is not None:
        run = body.run.config()
        db.set_setting(RUN_KEY, json.dumps(run.as_dict()))
        app.state.run_config = run
        log.info("run settings changed: games %d, recheck %s, resummarize at %.0f%%", run.games,
                 f"{run.stale_games} games older than {run.stale_days} days" if run.stale else "off", run.change * 100)
    if body.letsplay is not None:
        letsplay = body.letsplay.config()
        db.set_setting(LETSPLAY_KEY, json.dumps(letsplay.as_dict()))
        app.state.letsplay_config = letsplay
        log.info("letsplay settings changed: %d..%s minutes, recheck %s",
                 letsplay.min_minutes, letsplay.max_minutes or "no limit",
                 f"every {letsplay.recheck_days} days, max {letsplay.recheck_max or 'unlimited'}" if letsplay.recheck else "off")
        # новое правило повторного поиска действует сразу: игры, у которых срок уже вышел, встают в очередь
        enqueue = getattr(app.state, "enqueue_pending_letsplays", None)
        if enqueue is not None:
            enqueue()
    scheduler.update(config)
    app.state.status.changed()
    log.info("schedule changed: %s, %s", rule_text(config), "active" if config.enabled else "paused")
    return _schedule_view(config, scheduler.tz, run, app)


@router.post("/schedule/preview")
async def preview_schedule(body: ScheduleIn, request: Request) -> dict[str, Any]:
    """Ближайшие запуски для черновика расписания, без сохранения: окно настройки показывает их сразу."""
    run = body.run.config() if body.run is not None else _run_config(request)
    return _schedule_view(body.config(), _scheduler(request).tz, run, request.app)


def _storage(app: FastAPI) -> dict[str, Any]:
    return storage.state(app.state.db, app.state.storage_root, ROOT)


def _run_active(request: Request) -> bool:
    scheduler = request.app.state.scheduler
    return request.app.state.status.run is not None or bool(scheduler and scheduler.run_in_progress)


class RestoreIn(BaseModel):
    snapshot: str | None = Field(None, description="итерация, имя её папки в хранилище; без неё последняя")
    mode: str = Field("merge", pattern="^(merge|replace)$", description="merge: добавить к каталогу, replace: заменить")


def _drop_letsplay_queue(app: FastAPI) -> None:
    # очередь летсплеев держит id удалённых игр: воркер их пропустил бы, но счётчик врал бы
    queue = getattr(app.state, "letsplay_queue", None)
    if queue is not None:
        while not queue.empty():
            queue.get_nowait()
        app.state.status.set_letsplay_queue(0)


@router.get("/storage")
async def storage_state(request: Request) -> dict[str, Any]:
    """Хранилище: сохранённые итерации с периодом прогонов и объёмом данных, какие из них сейчас в каталоге."""
    return _storage(request.app)


@router.post("/storage/clear")
async def storage_clear(request: Request) -> dict[str, Any]:
    """Опустошить каталог: собранное сохраняется итерацией на диске. 409: идёт прогон или каталог пуст."""
    if _run_active(request):
        raise HTTPException(status_code=409, detail="run is in progress")
    app = request.app
    try:
        manifest = storage.archive(app.state.db, app.state.settings.db_path, app.state.storage_root,
                                   app.state.settings.timezone)
    except storage.StorageError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    _drop_letsplay_queue(app)
    app.state.status.changed()
    log.info("catalog archived as %s: %d games", manifest["id"], manifest["games"])
    return _storage(app)


@router.post("/storage/restore")
async def storage_restore(request: Request, body: RestoreIn | None = None) -> dict[str, Any]:
    """Подгрузить итерацию: добавить к текущему каталогу или заменить его (текущий сперва уходит в хранилище).

    Без тела подгружается последняя итерация с добавлением. 404: такой итерации нет, 409: идёт прогон.
    """
    if _run_active(request):
        raise HTTPException(status_code=409, detail="run is in progress")
    app = request.app
    body = body or RestoreIn()
    try:
        result = storage.restore(app.state.db, app.state.settings.db_path, app.state.storage_root,
                                 app.state.settings.timezone, snapshot=body.snapshot, mode=body.mode)
    except storage.SnapshotNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except storage.StorageError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    if result["saved"]:
        _drop_letsplay_queue(app)
    enqueue = getattr(app.state, "enqueue_pending_letsplays", None)
    if enqueue is not None:
        enqueue()
    app.state.status.changed()
    log.info("catalog restored from %s (%s): %d games, current catalog saved as %s",
             result["snapshot"], result["mode"], result["restored"], result["saved"] or "nothing")
    return {**_storage(app), **result}


def _attachment(data: bytes, name: str, media: str) -> Response:
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/export.zip")
async def export_all(request: Request) -> Response:
    """Все четыре файла одним архивом. Данные берутся из базы на момент запроса."""
    cfg = request.app.state.settings
    data = await asyncio.to_thread(export.archive, cfg.db_path, cfg.timezone)
    return _attachment(data, export.filename("export", cfg.timezone, "zip"), "application/zip")


@router.get("/storage/{snapshot}/export.zip")
async def storage_export(snapshot: str, request: Request) -> Response:
    """Те же четыре файла, но по снимку базы из хранилища. 404: такой итерации нет."""
    app = request.app
    if snapshot not in {item["id"] for item in storage.snapshots(app.state.storage_root)}:
        raise HTTPException(status_code=404, detail=f"unknown snapshot {snapshot!r}")
    db_file = app.state.storage_root / snapshot / "games.db"
    data = await asyncio.to_thread(export.archive, db_file, app.state.settings.timezone)
    return _attachment(data, f"skytec-{snapshot}.zip", "application/zip")


@router.get("/export/{name}.csv")
async def export_csv(name: str, request: Request) -> Response:
    """Один файл: games, summaries, letsplays или runs. 404: другого набора нет."""
    if name not in export.FILES:
        raise HTTPException(status_code=404, detail=f"unknown export {name!r}")
    cfg = request.app.state.settings
    data = await asyncio.to_thread(export.render, cfg.db_path, name, cfg.timezone)
    return _attachment(data, export.filename(name, cfg.timezone), "text/csv; charset=utf-8")


@router.post("/control/shutdown", include_in_schema=False, status_code=202)
async def shutdown(request: Request, x_control_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
    """Штатная остановка из Metacritic_Parser.bat. Токен задаёт скрипт управления при запуске, без него ручки нет (404)."""
    expected = os.environ.get("SERVICE_CONTROL_TOKEN", "")
    if not expected or not secrets.compare_digest(x_control_token or "", expected):
        raise HTTPException(status_code=404, detail="Not Found")
    log.info("shutdown requested by service control")
    request.app.state.stopping = True
    request.app.state.status.changed()  # будит потоки событий, чтобы они закрылись
    # ответ уходит клиенту, затем тот же сигнал, что и Ctrl+C: uvicorn штатно завершает приложение
    asyncio.get_running_loop().call_later(0.3, signal.raise_signal, signal.SIGINT)
    return {"status": "stopping"}


@router.post("/control/youtube-cookies", include_in_schema=False)
async def refresh_youtube_cookies(request: Request, x_control_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    """Обновить cookies YouTube из профиля Chrome прямо сейчас: лаунчер зовёт после входа в аккаунт."""
    expected = os.environ.get("SERVICE_CONTROL_TOKEN", "")
    if not expected or not secrets.compare_digest(x_control_token or "", expected):
        raise HTTPException(status_code=404, detail="Not Found")
    youtube = getattr(request.app.state, "youtube", None)
    if youtube is None or not youtube.has_profile:
        raise HTTPException(status_code=409, detail="профиль Chrome для YouTube не задан")
    logged_in = await youtube.refresh_cookies("после входа в лаунчере")
    request.app.state.status.changed()
    return {"logged_in": logged_in, "cookies": youtube.cookies_state}


def _llm(request: Request) -> GeminiClient:
    llm: GeminiClient | None = request.app.state.gemini
    if llm is None:
        raise HTTPException(status_code=503, detail="LLM client is not started")
    return llm


@router.get("/llm", response_model=LlmOptions)
async def llm_options(request: Request) -> dict[str, Any]:
    """Текущая модель и из чего можно выбрать: kie.ai, Gemini API, модели LM Studio на этом ПК."""
    return await _llm(request).options()


@router.put("/llm", response_model=LlmOptions)
async def choose_llm(choice: LlmChoice, request: Request) -> dict[str, Any]:
    """Смена модели для следующих запросов, текущие запросы доработают на прежней.

    409: у провайдера нет ключа; 503: LM Studio не отвечает; 422: такой модели у провайдера нет.
    """
    llm = _llm(request)
    if choice.provider == "local":
        # выбор локальной модели означает, что она сейчас понадобится: закрытый LM Studio поднимается
        await llm.start_local()
    options = await llm.options()
    provider = next(p for p in options["providers"] if p["id"] == choice.provider)
    if not provider["available"]:
        code = 503 if provider["reason"] == "server_unavailable" else 409
        raise HTTPException(status_code=code, detail=provider["reason"])
    if choice.model not in {model["id"] for model in provider["models"]}:
        raise HTTPException(status_code=422, detail="unknown model")
    previous = f"{llm.provider}/{llm.model}"
    llm.select(choice.provider, choice.model)
    db: Database = request.app.state.db
    db.set_setting(LLM_SETTING, json.dumps({"provider": llm.provider, "model": llm.model}))
    request.app.state.status.changed()
    log.info("LLM switched from %s to %s/%s", previous, llm.provider, llm.model)
    return await llm.options()
