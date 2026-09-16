"""Прогон: выбор игр по правилам дня, загрузка данных с Metacritic и резюме отзывов.

Сколько игр брать, добирать ли давно не проверявшиеся игры базы и когда пересобирать резюме,
задаётся в интерфейсе и приходит сюда объектом RunConfig (app/runconfig.py).

Внутри прогона работа идёт через очереди: сначала воркеры Metacritic, затем воркеры
Gemini; их состояние видно на мониторинге (app/status.py). Летсплеи считает отдельный
фоновый воркер (letsplay_loop): один ролик занимает минуты, и прогон не должен их ждать.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.db import Database, stale_border
from app.fetch import FetchError
from app.gemini import GeminiClient, GeminiError
from app.letsplay import (
    DONE,
    FAILED,
    NOT_FOUND,
    LetsplayStore,
    LetsplayWorker,
    Result,
    apply_config,
    game_from_row,
    letsplay_config,
    process_game,
)
from app.metacritic import FINDER_MAX_LIMIT, Game, ListedGame, MetacriticClient, ParseError, ReviewKind
from app.runconfig import RunConfig
from app.status import ServiceStatus
from app.gplay import GPlayClient, GPlayError
from app.steam import RETRY_AFTER as MEDIA_RETRY_AFTER
from app.steam import SteamClient, SteamError
from app.summaries import fetch_sample, needs_summary, review_total, summarize

log = logging.getLogger(__name__)

GAMES_PER_RUN = 20
SEE_ALL_PAGE_SIZE = 50
SEE_ALL_MAX_PAGES = 20
MAX_FAILURES_PER_DAY = 3
METACRITIC_WORKERS = 3
SUMMARY_WORKERS = 2
REVIEW_KINDS: tuple[ReviewKind, ...] = ("critic", "user")
# как часто воркер летсплеев проверяет, не заработала ли модель
LLM_WAIT_SECONDS = 30
# как часто пустая очередь летсплеев пересматривает базу: возвращает игры, отложенные из-за отказа YouTube
LETSPLAY_RESCAN_SECONDS = 300
# магазины: игра свежее этого срока повторяется раз в сутки, а не раз в неделю; и сколько игр базы
# с пробелами прогон добирает сверх своих
FRESH_GAME_DAYS = 30
MEDIA_RETRY_FRESH = timedelta(days=1)
MEDIA_BACKLOG = 20

SummaryOutcome = Literal["saved", "skipped", "failed"]


@dataclass(frozen=True)
class Candidate:
    game: ListedGame
    source: str


@dataclass(frozen=True)
class RunResult:
    run_id: int
    selected: int
    created: int
    updated: int
    failed: int
    summaries: int
    summary_failed: int
    # игры, для которых магазины (Steam, Google Play) дали обложку, трейлер или оба
    media: int = 0


def current_day(tz: ZoneInfo, now: datetime | None = None) -> date:
    return (now or datetime.now(tz)).astimezone(tz).date()


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


async def select_candidates(
    mc: MetacriticClient, db: Database, day: date, limit: int = GAMES_PER_RUN
) -> list[Candidate]:
    """Сначала New Releases, затем SEE ALL с начала списка.

    Пропускает игры, обработанные сегодня, и игры, которые сегодня уже упали
    MAX_FAILURES_PER_DAY раз. SEE ALL читается с начала, а не с сохранённого
    offset: при другом offset API перетасовывает игры с одинаковой датой.
    """
    skip = db.processed_slugs(day) | db.exhausted_slugs(day, MAX_FAILURES_PER_DAY)
    picked: dict[str, Candidate] = {}

    def take(games: Iterable[ListedGame], source: str) -> None:
        for game in games:
            if len(picked) == limit:
                return
            if game.slug not in skip and game.slug not in picked:
                picked[game.slug] = Candidate(game, source)

    # блок New Releases отдаётся одним запросом, а finder не принимает limit больше 50:
    # остаток нормы добирается страницами SEE ALL
    take((await mc.new_releases(min(limit, FINDER_MAX_LIMIT))).games, "new_releases")
    offset = 0
    for _ in range(SEE_ALL_MAX_PAGES):
        if len(picked) == limit:
            break
        page = await mc.see_all(offset, SEE_ALL_PAGE_SIZE)
        take(page.games, "see_all")
        offset += len(page.games)
        if not page.games or offset >= page.total:
            break
    return list(picked.values())


def select_stale(
    db: Database, day: date, limit: int, taken: set[str] | None = None, days: int = 7, tz: ZoneInfo | None = None
) -> list[Candidate]:
    """Старые игры, которым пора обновиться: обновлены days дней назад и раньше, счёт по дням сервиса
    (обновлённые 16.09 при сроке 7 дней созревают 23.09 с полуночи). Самые давние первыми.

    Так обновляются игры, которые больше не попадают в списки новинок Metacritic, но успели
    набрать новые отзывы. Обработанные сегодня, сегодня же упавшие и уже выбранные в этом
    прогоне пропускаются.
    """
    skip = db.exhausted_slugs(day, MAX_FAILURES_PER_DAY) | (taken or set())
    picked: list[Candidate] = []
    border = stale_border(day, days, tz or ZoneInfo("UTC"))
    for game_id, slug, title in db.stale_games(day, limit + len(skip), border):
        if slug in skip:
            continue
        picked.append(Candidate(ListedGame(id=game_id, slug=slug, title=title, release_date=None), "stale"))
        if len(picked) == limit:
            break
    return picked


async def select_for_run(
    mc: MetacriticClient, db: Database, day: date, run: RunConfig, tz: ZoneInfo | None = None
) -> list[Candidate]:
    """Что берём в прогон: новинки Metacritic (до run.games), всегда.

    С включённым обновлением старых игр к ним добавляются до run.stale_games игр базы, которым пора
    обновиться по сроку run.stale_days (счёт по дням сервиса), самые давние первыми.
    """
    candidates = await select_candidates(mc, db, day, run.games)
    if run.stale:
        # обновление старых идёт сверх новинок своей квотой: иначе при полном списке новинок
        # старые игры не обновлялись бы никогда
        candidates += select_stale(db, day, run.stale_games, {c.game.slug for c in candidates}, run.stale_days, tz)
    return candidates


async def _metacritic_worker(
    name: str,
    queue: "asyncio.Queue[Candidate]",
    mc: MetacriticClient,
    db: Database,
    run_id: int,
    day: date,
    status: ServiceStatus,
    saved: list[tuple[Game, bool]],
    on_saved: Callable[[Game], None] | None,
    previewed: set[str],
) -> None:
    while not queue.empty():
        candidate = queue.get_nowait()
        slug = candidate.game.slug
        status.worker_busy(name, "metacritic", candidate.game.title, "metacritic")
        try:
            game = await mc.game(slug)
            # игра с предварительной карточкой уже есть в базе, но для счётчиков прогона она новая
            created = db.save_game(
                game, run_id=run_id, day=day, slug=slug, source=candidate.source,
                created=True if slug in previewed else None,
            )
        except (FetchError, ParseError, sqlite3.Error) as e:
            error = _describe(e)
            log.warning("game %s failed: %s", slug, error)
        except Exception as e:
            # граница обработки одной игры: неожиданная ошибка не должна ронять весь прогон
            error = _describe(e)
            log.exception("game %s failed unexpectedly", slug)
        else:
            saved.append((game, created))
            # игра загрузилась: её прежние ошибки загрузки решены и из списка ошибок уходят
            _resolve(db, slug, ("metacritic",))
            if on_saved is not None:
                on_saved(game)
            recheck = candidate.source == "stale"
            status.run_add(
                processed=1, created=int(created and not recheck), updated=int(not created and not recheck), rechecked=int(recheck)
            )
            status.worker_done(name, "metacritic")
            continue
        if slug in previewed:
            # подробности не загрузились: предварительная карточка не должна остаться в каталоге
            db.drop_listed(slug)
        db.record_failure(run_id=run_id, day=day, slug=slug, stage="metacritic", error=error)
        status.run_add(processed=1, failed=1)
        status.worker_done(name, "metacritic", error)


async def _summarize(
    mc: MetacriticClient, gemini: GeminiClient, db: Database, run_id: int, day: date, game: Game, kind: ReviewKind,
    change: float,
) -> tuple[SummaryOutcome, str | None]:
    total = review_total(game, kind)
    if not needs_summary(db.summary_total(game.id, kind), total, change):
        return "skipped", None
    try:
        sample = await fetch_sample(mc, game.slug, kind)
        reviews = sample.reviews
        if not reviews:
            log.info("%s: %s reviews have no text yet", game.slug, kind)
            return "skipped", None
        summary = await summarize(gemini, game, kind, reviews, total, sample.counts)
    except (FetchError, ParseError, GeminiError) as e:
        error = _describe(e)
        log.warning("%s summary for %s failed: %s", kind, game.slug, error)
    except Exception as e:
        # граница этапа резюме: ошибка не должна откатывать уже сохранённые данные игры
        error = _describe(e)
        log.exception("%s summary for %s failed unexpectedly", kind, game.slug)
    else:
        db.save_summary(
            run_id=run_id,
            game_id=game.id,
            kind=kind,
            likes=[point.model_dump() for point in summary.likes],
            dislikes=[point.model_dump() for point in summary.dislikes],
            summary=summary.summary,
            reviews_used=len(reviews),
            reviews_total=total,
            model=gemini.model,
        )
        _resolve(db, game.slug, (f"summary_{kind}",))
        return "saved", None
    db.record_failure(run_id=run_id, day=day, slug=game.slug, stage=f"summary_{kind}", error=error, game_failed=False)
    return "failed", error


@dataclass(frozen=True)
class StoredGame:
    """Игра из базы в объёме, нужном магазинам: название, компании, дата и что уже есть."""

    id: int
    slug: str
    title: str
    developers: list[str]
    publishers: list[str]
    release_date: date | None
    cover_url: str | None
    video_url: str | None
    description: str | None


def game_from_db_row(row: sqlite3.Row) -> StoredGame:
    release = row["release_date"]
    return StoredGame(
        id=row["id"],
        slug=row["slug"],
        title=row["title"],
        developers=json.loads(row["developers"] or "[]"),
        publishers=json.loads(row["publishers"] or "[]"),
        release_date=date.fromisoformat(release) if release else None,
        cover_url=row["cover_url"],
        video_url=row["video_url"],
        description=row["description"],
    )


def media_retry_after(game: Game, day: date) -> timedelta:
    """Через сколько повторять запрос к магазинам, если нужного там не нашлось: у свежих игр страница
    в магазине появляется с задержкой в дни, поэтому им повтор раз в сутки, остальным раз в неделю."""
    if game.release_date is not None and abs((day - game.release_date).days) <= FRESH_GAME_DAYS:
        return MEDIA_RETRY_FRESH
    return MEDIA_RETRY_AFTER


def media_needs(db: Database, game: Game, day: date | None = None) -> bool:
    """Нужно ли спрашивать магазины про игру: нет обложки, видео или описания с Metacritic и срок повтора вышел."""
    cover, video, description = not game.cover_url, not game.video_url, not (game.description or "").strip()
    return (cover or video or description) and db.media_due(
        game.id, cover=cover, video=video, description=description,
        retry_after=media_retry_after(game, day or date.today()),
    )


async def find_media(steam: SteamClient, gplay: GPlayClient | None, game: Game) -> tuple[dict[str, str | None], list[str]]:
    """Обложка, трейлер и описание из магазинов: сначала Steam (всё три), затем Google Play (иконка
    и описание), если после Steam обложки или описания всё ещё нет. Возвращает поля для save_media
    и заметки для журнала. Описание магазина нужно только играм, у которых его нет на Metacritic."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    want_description = not (game.description or "").strip()
    found, reason = await steam.find_media(game)
    if found:
        notes.append(
            f"steam appid {found.appid} ({found.name}): cover {found.cover_kind or 'none'},"
            f" trailer {'yes' if found.video_url else 'none'}, description {'yes' if found.description else 'none'}"
        )
        if found.cover_url:
            fields.update(cover_url=found.cover_url, cover_kind=found.cover_kind, cover_source="steam", cover_page=found.store_url)
        if found.video_url:
            fields.update(
                video_url=found.video_url, video_title=found.video_title, video_poster=found.video_poster,
                video_source="steam", video_page=found.store_url,
            )
        if want_description and found.description:
            fields.update(description=found.description, description_source="steam")
    else:
        notes.append(f"steam: not found ({reason})")
    if gplay is not None and (not fields.get("cover_url") or (want_description and not fields.get("description"))):
        icon, reason = await gplay.find_cover(game)
        if icon:
            notes.append(f"google play {icon.package} ({icon.name}): icon, description {'yes' if icon.description else 'none'}")
            if not fields.get("cover_url"):
                fields.update(cover_url=icon.cover_url, cover_kind="icon", cover_source="gplay", cover_page=icon.store_url)
            if want_description and not fields.get("description") and icon.description:
                fields.update(description=icon.description, description_source="gplay")
        else:
            notes.append(f"google play: not found ({reason})")
    if not fields:
        # причина остаётся в базе: видно, почему у игры нет материалов, без поиска по логам
        fields["note"] = "; ".join(notes)[:500]
    return fields, notes


async def _media(
    steam: SteamClient, gplay: GPlayClient | None, db: Database, run_id: int, day: date, game: Game
) -> tuple[bool, str | None]:
    """Нашлось ли что-то в магазинах и текст ошибки. "В магазине игры нет" это не ошибка, а результат поиска."""
    try:
        fields, notes = await find_media(steam, gplay, game)
    except (SteamError, GPlayError) as e:
        error = _describe(e)
        log.warning("media for %s failed: %s", game.slug, error)
    except Exception as e:
        # граница поиска в магазинах: их сбой не должен ронять прогон
        error = _describe(e)
        log.exception("media for %s failed unexpectedly", game.slug)
    else:
        db.save_media(game.id, **fields)
        log.info("media for %s: %s", game.slug, "; ".join(notes))
        # магазины ответили (нашли или нет): прежние сбои запроса решены; trailer и cover это записи старых прогонов
        _resolve(db, game.slug, ("media", "steam", "trailer", "cover"))
        return any(key != "note" for key in fields), None
    db.record_failure(run_id=run_id, day=day, slug=game.slug, stage="media", error=error, game_failed=False)
    return False, error


def _resolve(db: Database, slug: str, stages: tuple[str, ...]) -> None:
    """Этап прошёл успешно: его ошибки по этой игре уходят из списка ошибок мониторинга."""
    try:
        if db.resolve_failures(slug, stages):
            log.info("failures for %s resolved: %s", slug, ", ".join(stages))
    except sqlite3.Error:
        log.exception("cannot resolve failures for %s", slug)


async def _summary_worker(
    name: str,
    queue: "asyncio.Queue[tuple[Game, ReviewKind]]",
    mc: MetacriticClient,
    gemini: GeminiClient,
    db: Database,
    run_id: int,
    day: date,
    status: ServiceStatus,
    outcomes: list[SummaryOutcome],
    change: float,
) -> None:
    while not queue.empty():
        game, kind = queue.get_nowait()
        status.worker_busy(name, "gemini", game.title, f"summary_{kind}")
        outcome, error = await _summarize(mc, gemini, db, run_id, day, game, kind, change)
        outcomes.append(outcome)
        status.run_add(
            summaries_done=1, summaries_saved=int(outcome == "saved"), summary_failed=int(outcome == "failed")
        )
        status.worker_done(name, "gemini", error)


async def run_once(
    mc: MetacriticClient,
    db: Database,
    *,
    day: date,
    trigger: str,
    gemini: GeminiClient | None = None,
    status: ServiceStatus | None = None,
    on_saved: Callable[[Game], None] | None = None,
    run: RunConfig | None = None,
    steam: SteamClient | None = None,
    gplay: GPlayClient | None = None,
    tz: ZoneInfo | None = None,
) -> RunResult:
    """Один прогон. Без клиента Gemini резюме отзывов пропускаются, без клиента Steam обложки и трейлеры
    из магазинов для игр, у которых Metacritic их не отдал (Google Play идёт только после Steam).

    run задаёт настройки из интерфейса: что берём в работу, сколько игр и порог пересборки резюме.
    on_saved вызывается для каждой сохранённой игры: так сервис ставит её в очередь летсплеев.
    """
    run = run or RunConfig()
    status = status or ServiceStatus()
    run_id = db.start_run(trigger, day)
    status.run_started(run_id, trigger)
    candidates: list[Candidate] = []
    previewed: set[str] = set()
    saved: list[tuple[Game, bool]] = []
    outcomes: list[SummaryOutcome] = []
    media_found = 0
    try:
        candidates = await select_for_run(mc, db, day, run, tz)
        stale_count = sum(1 for c in candidates if c.source == "stale")
        db.set_selected(run_id, len(candidates), stale_count)
        status.run_update(stage="metacritic", selected=len(candidates), stale_selected=stale_count)
        log.info("run %d (%s): selected %d games %s", run_id, day, len(candidates), dict(Counter(c.source for c in candidates)))

        # каталог показывает игры сразу: в списке уже есть название, обложка, оценка критиков, жанры и дата,
        # а разработчик, платформы и оценка игроков дозаполняются, когда загрузятся подробности
        previewed = db.save_listed([c.game for c in candidates])
        if previewed:
            log.info("run %d: %d new games are in the catalog from the listing", run_id, len(previewed))

        games: asyncio.Queue[Candidate] = asyncio.Queue()
        for candidate in candidates:
            games.put_nowait(candidate)
        await asyncio.gather(
            *(
                _metacritic_worker(f"metacritic-{n}", games, mc, db, run_id, day, status, saved, on_saved, previewed)
                for n in range(1, METACRITIC_WORKERS + 1)
            )
        )

        if steam is not None:
            # магазины дают обложку, трейлер и описание: спрашиваем про игры прогона, у которых нет
            # хотя бы одного, и добираем до MEDIA_BACKLOG игр базы, у которых срок повтора вышел
            need = [game for game, _ in saved if media_needs(db, game, day)]
            seen = {game.id for game in need}
            for row in db.media_backlog(MEDIA_BACKLOG + len(seen)):
                if row["id"] in seen or len(need) >= len(seen) + MEDIA_BACKLOG:
                    continue
                game = game_from_db_row(row)
                if media_needs(db, game, day):
                    need.append(game)
            status.run_update(stage="media", media_total=len(need))
            for game in need:
                status.worker_busy("media", "media", game.title, "media")
                found, error = await _media(steam, gplay, db, run_id, day, game)
                media_found += int(found)
                status.run_add(media_done=1)
                status.worker_done("media", "media", error)
            db.set_media_counts(run_id, len(need), media_found)

        if gemini is not None:
            tasks: asyncio.Queue[tuple[Game, ReviewKind]] = asyncio.Queue()
            for game, _ in saved:
                for kind in REVIEW_KINDS:
                    tasks.put_nowait((game, kind))
            status.run_update(stage="summaries", summaries_total=tasks.qsize())
            db.set_summaries_checked(run_id, tasks.qsize())
            await asyncio.gather(
                *(
                    _summary_worker(f"gemini-{n}", tasks, mc, gemini, db, run_id, day, status, outcomes, run.change)
                    for n in range(1, SUMMARY_WORKERS + 1)
                )
            )
    except BaseException as e:
        state = "interrupted" if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)) else "failed"
        # прогон оборвался: предварительные карточки без подробностей убираем, иначе в каталоге
        # останутся игры без разработчика и платформ до следующего прогона
        for slug in previewed - {game.slug for game, _ in saved}:
            try:
                db.drop_listed(slug)
            except sqlite3.Error:
                log.warning("cannot drop the preliminary card for %s", slug)
        db.finish_run(run_id, state, error=_describe(e))
        status.run_finished()
        raise
    db.finish_run(run_id, "done")
    # сверка списка ошибок с данными: что за прогон решилось, из списка уходит
    try:
        db.resolve_stale_failures()
    except sqlite3.Error:
        log.exception("cannot resolve stale failures")
    status.run_finished()
    result = RunResult(
        run_id=run_id,
        selected=len(candidates),
        created=sum(1 for _, created in saved if created),
        updated=sum(1 for _, created in saved if not created),
        failed=len(candidates) - len(saved),
        summaries=outcomes.count("saved"),
        summary_failed=outcomes.count("failed"),
        media=media_found,
    )
    log.info("run %d done: %s", run_id, result)
    return result


def pending_letsplays(db: Database, store: LetsplayStore, worker: LetsplayWorker) -> list[int]:
    """Игры, которым нужен поиск летсплея. Ставятся в очередь при старте сервиса.

    Сначала игры с оценкой критиков, затем новые: у свежих игр из SEE ALL без оценок
    летсплеев почти не бывает, а у игр из New Releases они обычно есть.
    """
    settings = worker.settings
    rows = db.conn.execute(
        "SELECT id FROM games ORDER BY metascore IS NULL, first_seen_at DESC, id DESC"
    ).fetchall()
    return [
        row["id"]
        for row in rows
        if store.needs_search(row["id"], settings)
    ]


async def _wait_for_llm(llm: GeminiClient | None, status: ServiceStatus, name: str) -> None:
    """Без рабочей модели ролик не выбрать и заключение не написать: игра упала бы впустую
    и потратила одну из попыток. Воркер ждёт, пока модель заработает (ключ, баланс, LM Studio)."""
    if llm is None or (reason := await llm.ready(fresh=True, start=True)) is None:
        return
    log.warning("letsplay worker waits for LLM %s %s: %s", llm.title, llm.model, reason)
    status.worker_waiting(name, "letsplay", f"llm_{reason}")
    while (reason := await llm.ready(fresh=True, start=True)) is not None:
        await asyncio.sleep(LLM_WAIT_SECONDS)
    log.info("LLM %s %s is ready, letsplay worker continues", llm.title, llm.model)


class LetsplayRuns:
    """Какому прогону принадлежит летсплей в очереди: блок "Текущий прогон" считает летсплеи только своей
    итерации (игры прогона и добранные после него старые), а не весь каталог.

    Игра приписывается последнему прогону; если она уже числилась за прежним, переходит к новому.
    Итог записывается в счётчики прогона, когда воркер закончил игру. Отказ YouTube итогом не считается:
    игра остаётся за прогоном до следующего захода.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._owner: dict[int, int] = {}

    def assign(self, game_ids: Iterable[int], run_id: int | None = None) -> None:
        run_id = run_id if run_id is not None else self._db.last_run_id()
        if run_id is None:
            return
        for game_id in game_ids:
            old = self._owner.get(game_id)
            if old == run_id:
                continue
            try:
                if old is not None:
                    self._db.letsplay_count(old, total=-1)
                self._db.letsplay_count(run_id, total=1)
            except sqlite3.Error:
                log.exception("cannot count letsplay for run %d", run_id)
                continue
            self._owner[game_id] = run_id

    def skipped(self, game_id: int) -> None:
        """Игра из очереди оказалась не нужна (уже готова или срок не вышел): из счёта прогона уходит."""
        run_id = self._owner.pop(game_id, None)
        if run_id is not None:
            with contextlib.suppress(sqlite3.Error):
                self._db.letsplay_count(run_id, total=-1)

    def finished(self, game_id: int, outcome: str) -> None:
        run_id = self._owner.pop(game_id, None)
        if run_id is not None:
            with contextlib.suppress(sqlite3.Error):
                self._db.letsplay_count(run_id, **{outcome: 1})


async def letsplay_loop(
    queue: "asyncio.Queue[int]", db: Database, worker: LetsplayWorker, store: LetsplayStore, status: ServiceStatus,
    tz: ZoneInfo | None = None, runs: LetsplayRuns | None = None,
) -> None:
    """Фоновый воркер летсплеев: одна игра за раз из очереди.

    Одну игру можно поставить в очередь несколько раз: перед обработкой проверяется,
    нужен ли поиск, поэтому готовые и недавно проверенные игры пропускаются.

    Когда очередь пуста, воркер раз в LETSPLAY_RESCAN_SECONDS смотрит базу заново: так
    возвращаются игры, отложенные из-за отказа YouTube, и перезапуск сервиса для этого не нужен.
    """
    name = "letsplay"
    status.worker(name, "letsplay")
    settings = worker.settings
    while True:
        try:
            game_id = await asyncio.wait_for(queue.get(), timeout=LETSPLAY_RESCAN_SECONDS)
        except asyncio.TimeoutError:
            # пустая очередь: возвращаем игры, у которых срок повтора вышел (границы длительности берём свежие)
            worker.settings = apply_config(settings, letsplay_config(db, settings))
            waiting = pending_letsplays(db, store, worker)
            for pending in waiting:
                queue.put_nowait(pending)
            if runs is not None:
                runs.assign(waiting)
            status.set_letsplay_queue(queue.qsize())
            if waiting:
                log.info("letsplay: в очередь вернулись %d игр, ждавших повтора", len(waiting))
            continue
        status.set_letsplay_queue(queue.qsize())
        row = db.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        # границы длительности могли поменять в интерфейсе: берём свежие перед каждой игрой
        worker.settings = apply_config(settings, letsplay_config(db, settings))
        current = worker.settings
        if row is None or not store.needs_search(game_id, current):
            if runs is not None:
                runs.skipped(game_id)
            continue
        game = game_from_row(row)
        await _wait_for_llm(worker.gemini, status, name)
        status.worker_busy(name, "letsplay", game.title, "letsplay")
        try:
            result = await process_game(worker, store, game)
        except Exception as e:
            # граница воркера: сбой одной игры не должен останавливать очередь. Запись failed
            # нужна, чтобы сработали лимит попыток и повтор не чаще раза в сутки
            error = _describe(e)
            log.exception("letsplay for %s failed unexpectedly", game.title)
            try:
                store.save(game.id, Result(status=FAILED, error=error), [])
            except sqlite3.Error:
                log.exception("cannot save failed letsplay for %s", game.title)
            _record_letsplay_failure(db, row["slug"], error, tz)
            if runs is not None:
                runs.finished(game_id, "lp_failed")
            status.worker_done(name, "letsplay", error)
        else:
            if runs is not None and not result.temporary:
                runs.finished(game_id, {DONE: "lp_found", NOT_FOUND: "lp_not_found"}.get(result.status, "lp_failed"))
            if result.status == FAILED and result.error:
                _record_letsplay_failure(db, row["slug"], result.error, tz)
            else:
                # летсплей найден или его честно нет: прежние сбои YouTube по игре решены
                _resolve(db, row["slug"], ("letsplay",))
            status.worker_done(name, "letsplay", result.error if result.status == FAILED else None)


def _record_letsplay_failure(db: Database, slug: str, error: str, tz: ZoneInfo | None) -> None:
    """Сбой летсплея в общий список ошибок мониторинга. Летсплеи идут вне прогона, поэтому без run_id."""
    try:
        db.record_failure(
            run_id=None, day=current_day(tz) if tz else date.today(), slug=slug, stage="letsplay", error=error,
            game_failed=False,
        )
    except sqlite3.Error:
        log.exception("cannot record letsplay failure for %s", slug)
