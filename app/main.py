"""Веб-приложение: JSON API, прогоны по расписанию и по кнопке, мониторинг, фоновый воркер летсплеев.

Запуск: python -m uvicorn app.main:app
"""

import asyncio
import logging
import mimetypes
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app import export
from app.api import restore_llm_choice, router
from app.catalog import Catalog
from app.config import ROOT, Settings, load_settings
from app.db import Database
from app.gemini import GeminiClient, create_gemini_http_client
from app.gplay import GPlayClient
from app.gpu import idle_loop
from app.letsplay import LetsplayStore, LetsplayWorker, apply_config, letsplay_config, settings_from_env
from app.metacritic import Game, MetacriticClient, create_http_client
from app.pipeline import (
    LetsplayRuns,
    METACRITIC_WORKERS,
    SUMMARY_WORKERS,
    current_day,
    letsplay_loop,
    pending_letsplays,
    run_once,
)
from app.runconfig import SETTING_KEY as RUN_KEY
from app.runconfig import load as load_run
from app.schedule import SETTING_KEY as SCHEDULE_KEY
from app.schedule import load as load_schedule
from app.scheduler import Scheduler
from app.status import ServiceStatus
from app.system import SystemMonitor, WhisperMonitor
from app.transcribe import Transcriber
from app.steam import SteamClient, create_steam_http_client
from app.youtube import YouTubeClient, create_youtube_http_client

log = logging.getLogger(__name__)

WEB = ROOT / "app" / "web"
# Windows берёт типы файлов из реестра и может отдать .js как text/plain: модульные скрипты браузер
# тогда не исполнит. Задаём типы явно
for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("font/woff2", ".woff2")):
    mimetypes.add_type(_type, _ext)


class WebAssets(StaticFiles):
    """Статика интерфейса: браузер каждый раз сверяет ETag, поэтому после правок не держит старый код."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def setup_logging(log_dir: Path) -> None:
    root = logging.getLogger()
    if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(log_dir / "service.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or load_settings()
        if cfg.log_dir is not None:
            setup_logging(cfg.log_dir)
        db = Database(cfg.db_path)
        try:
            # таблица letsplays нужна карточке игры, даже когда воркер летсплеев выключен
            store = LetsplayStore(db)
            # ошибки, которые успели решиться (например, пока сервис стоял), из списка уходят
            stale = db.resolve_stale_failures()
            if stale:
                log.info("%d resolved failures removed from the error list", stale)
            # модель Whisper грузится при расшифровке и выгружается после простоя: здесь видеокарта не занимается;
            # обёртка считает расшифровки для мониторинга, интерфейс у неё тот же
            transcriber = WhisperMonitor(Transcriber.from_env())
            status = ServiceStatus()
            app.state.transcriber = transcriber
            app.state.system = SystemMonitor(cfg.db_path)
            app.state.catalog = Catalog(
                cfg.db_path,
                whisper_window=transcriber.head_seconds + transcriber.tail_seconds,
                head_seconds=transcriber.head_seconds,
                tail_seconds=transcriber.tail_seconds,
                max_attempts=settings_from_env(ROOT).max_attempts_per_game,
            )
            app.state.status = status
            app.state.settings = cfg
            app.state.db = db
            # настройки прогона для мониторинга; сам прогон перечитывает их из базы
            app.state.run_config = load_run(db.get_setting(RUN_KEY))
            # длительность роликов для летсплеев: запись из базы, иначе значения из .env
            app.state.letsplay_base = settings_from_env(ROOT)
            app.state.letsplay_config = letsplay_config(db, app.state.letsplay_base)
            app.state.scheduler = None
            app.state.stopping = False
            app.state.gemini = None
            app.state.whisper = None
            # "Опустошить" складывает снимки базы сюда, рядом с самой базой
            app.state.storage_root = cfg.db_path.parent / "storage"
            # CSV со свежими данными после каждого прогона
            app.state.export_root = cfg.db_path.parent / "exports"
            app.state.youtube = None
            app.state.letsplay_queue = None
            app.state.enqueue_pending_letsplays = None
            tasks: list[asyncio.Task[None]] = []
            async with (
                create_http_client() as mc_http,
                create_gemini_http_client() as gemini_http,
                create_youtube_http_client() as youtube_http,
                create_steam_http_client() as steam_http,
            ):
                steam = SteamClient(steam_http)
                # Google Play ходит с теми же браузерными заголовками, что и Steam
                gplay = GPlayClient(steam_http)
                llm = GeminiClient(
                    gemini_http,
                    cfg.gemini_keys,
                    cfg.gemini_model,
                    provider=cfg.gemini_provider,
                    other_keys=cfg.llm_keys,
                    local_url=cfg.local_llm_url,
                    local_model=cfg.local_llm_model,
                    local_ttl=cfg.gpu_idle_seconds,
                    # локальная модель и Whisper делят видеокарту: общий замок, работают по очереди
                    gpu_lock=transcriber.gpu,
                )
                # между прогонами модели не держат видеопамять: после простоя обе выгружаются
                tasks.append(asyncio.create_task(
                    idle_loop(transcriber.gpu, llm, transcriber, cfg.gpu_idle_seconds), name="gpu-idle"))
                restore_llm_choice(db, llm)
                app.state.gemini = llm
                reason = await llm.ready(fresh=True)
                if reason is None:
                    log.info("LLM: %s, model %s", llm.title, llm.model)
                else:
                    log.warning("LLM: %s, model %s is not ready (%s), summaries and letsplays wait for it",
                                llm.title, llm.model, reason)

                # YouTube нужен только летсплеям: трейлеры берутся с Metacritic, иначе из Steam
                youtube = YouTubeClient(youtube_http, cookies=cfg.youtube_cookies, profile=cfg.youtube_profile)
                app.state.youtube = youtube
                if cfg.letsplay_enabled and youtube.has_profile:
                    # cookies из профиля Chrome: при старте и дальше по таймеру, чтобы сессия не протухала
                    async def cookies_loop() -> None:
                        await youtube.refresh_cookies("старт сервиса")
                        while True:
                            await asyncio.sleep(cfg.youtube_cookies_refresh.total_seconds())
                            await youtube.refresh_cookies("по таймеру")

                    tasks.append(asyncio.create_task(cookies_loop(), name="youtube-cookies"))
                letsplays: asyncio.Queue[int] | None = None
                if cfg.letsplay_enabled:
                    worker = LetsplayWorker(youtube, llm, transcriber, apply_config(app.state.letsplay_base, app.state.letsplay_config))
                    queue: asyncio.Queue[int] = asyncio.Queue()
                    letsplays = queue
                    # летсплеи в очереди приписываются прогону: "Текущий прогон" считает только свою итерацию
                    lp_runs = LetsplayRuns(db)
                    app.state.letsplay_runs = lp_runs

                    def enqueue_pending() -> None:
                        # при старте, после прогона, смены настроек и "Подгрузить из хранилища": игры,
                        # которым нужен поиск летсплея по актуальному правилу повтора
                        worker.settings = apply_config(app.state.letsplay_base, letsplay_config(db, app.state.letsplay_base))
                        pending = pending_letsplays(db, store, worker)
                        for game_id in pending:
                            queue.put_nowait(game_id)
                        lp_runs.assign(pending)
                        status.set_letsplay_queue(queue.qsize())

                    enqueue_pending()
                    app.state.letsplay_queue = queue
                    app.state.enqueue_pending_letsplays = enqueue_pending
                    app.state.whisper = {
                        "model": transcriber.model_name,
                        "device": transcriber.device,
                        "compute_type": transcriber.compute_type,
                    }
                    tasks.append(asyncio.create_task(
                        letsplay_loop(letsplays, db, worker, store, status, tz=cfg.timezone, runs=lp_runs), name="letsplay"
                    ))

                if cfg.scheduler_enabled:
                    mc = MetacriticClient(
                        mc_http,
                        concurrency=cfg.metacritic_concurrency,
                        min_interval=cfg.metacritic_min_interval,
                        api_key=cfg.metacritic_api_key,
                    )
                    for n in range(1, METACRITIC_WORKERS + 1):
                        status.worker(f"metacritic-{n}", "metacritic")
                    for n in range(1, SUMMARY_WORKERS + 1):
                        status.worker(f"gemini-{n}", "gemini")
                    status.worker("media", "media")

                    def enqueue_letsplay(game: Game) -> None:
                        if letsplays is not None:
                            letsplays.put_nowait(game.id)
                            if status.run is not None:
                                app.state.letsplay_runs.assign([game.id], status.run["id"])
                            status.set_letsplay_queue(letsplays.qsize())

                    async def job(trigger: str) -> None:
                        # модель могли сменить из интерфейса или она перестала отвечать: без неё прогон
                        # сохраняет игры, а резюме достроит следующий прогон
                        reason = await llm.ready(fresh=True, start=True)
                        if reason is not None:
                            log.warning("LLM %s %s is not ready (%s), review summaries are skipped in this run",
                                        llm.title, llm.model, reason)
                        # настройки прогона читаются перед каждым запуском: смена из интерфейса
                        # действует со следующего прогона, перезапуск сервиса не нужен
                        result = await run_once(
                            mc,
                            db,
                            day=current_day(cfg.timezone),
                            trigger=trigger,
                            gemini=None if reason else llm,
                            status=status,
                            on_saved=enqueue_letsplay,
                            run=load_run(db.get_setting(RUN_KEY)),
                            steam=steam,
                            gplay=gplay,
                            tz=cfg.timezone,
                        )
                        # неудавшиеся летсплеи навёрстываются с каждым прогоном: в очередь возвращаются
                        # игры, у которых срок повтора вышел, вслед за играми самого прогона
                        if app.state.enqueue_pending_letsplays is not None:
                            app.state.enqueue_pending_letsplays()
                        if cfg.export_enabled:
                            # выгрузка идёт в потоке и своим соединением: прогон она не задерживает,
                            # а её ошибка (нет места, занят файл) не должна ронять сам прогон
                            try:
                                counts = await asyncio.to_thread(
                                    export.save_run, cfg.db_path, app.state.export_root,
                                    run_id=result.run_id, tz=cfg.timezone, keep=cfg.export_keep,
                                )
                                log.info("export: %s", ", ".join(f"{name} {n}" for name, n in counts.items()))
                            except (OSError, sqlite3.Error) as e:
                                log.warning("export failed: %s", e)

                    # расписание настраивается из интерфейса и хранится в базе; по умолчанию раз в час
                    scheduler = Scheduler(
                        job,
                        db.last_run_started,
                        load_schedule(db.get_setting(SCHEDULE_KEY)),
                        cfg.timezone,
                        manual_cooldown=cfg.manual_cooldown,
                    )
                    app.state.scheduler = scheduler
                    tasks.append(asyncio.create_task(scheduler.run_forever(), name="scheduler"))
                try:
                    yield
                finally:
                    # отмена прерывает текущий прогон (run_once помечает его interrupted);
                    # расшифровка в отдельном потоке отменой не прерывается и доработает сама
                    for task in tasks:
                        task.cancel()
                    for task in tasks:
                        with suppress(asyncio.CancelledError):
                            await task
                    # модель, которую сервис сам загрузил в LM Studio, выгружается: после остановки
                    # видеопамять свободна; LM Studio недоступен или завис, остановку это не держит
                    with suppress(Exception):
                        await asyncio.wait_for(llm.release_local(), 15)
                    log.info("service stopped")
        finally:
            db.close()

    app = FastAPI(title="Metacritic parser", lifespan=lifespan)
    app.include_router(router)
    app.mount("/assets", WebAssets(directory=WEB / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # экраны переключаются через #/..., сервер всегда отдаёт одну страницу
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
