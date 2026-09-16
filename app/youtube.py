"""YouTube без официального API: поиск летсплеев, субтитры и аудиодорожка.

Поиск идёт по обычной странице результатов: данные лежат в ytInitialData внутри HTML.
Ключ YouTube Data API здесь не нужен, а его квота (100 единиц за поиск при 10 000 в сутки)
не покрыла бы до 480 игр в день.
"""

import asyncio
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.fetch import RETRY_STATUSES, FetchError, get_text

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.youtube.com/results"
# тот же поиск, которым пользуется сама страница: работает, когда HTML приходит без данных
API_URL = "https://www.youtube.com/youtubei/v1/search"
API_CLIENT = {"clientName": "WEB", "clientVersion": "2.20240401.00.00", "hl": "en", "gl": "US"}
WATCH_URL = "https://www.youtube.com/watch?v="
# повторы поиска при отказе YouTube: паузы растут, последним идёт запасной путь через API
SEARCH_ATTEMPTS = 3
BLOCK_DELAYS = (10.0, 30.0, 90.0)
# после отказа ждут все запросы клиента, а не только повтор: он один на сервис
BLOCK_COOLDOWN = 60.0
# проверка на робота при загрузке ролика или субтитров: адрес помечен, повторные запросы только
# продлевают блокировку, поэтому пауза длиннее, чем при отказе поиска
BOT_CHECK_COOLDOWN = 300.0
BOT_CHECK_MARK = "Sign in to confirm"
# sp: фильтр "только видео" (без каналов, плейлистов и Shorts) с сортировкой и без неё.
# Одной сортировки по просмотрам мало: по запросу "Baldur's Gate 3 let's play" она отдаёт
# просто самые популярные ролики YouTube, а летсплеи самой игры остаются за двадцаткой.
SEARCH_SORTS = {"views": "CAMSAhAB", "relevance": "EgIQAQ=="}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# у роликов с автодубляжем несколько дорожек, оригинальная помечена language_preference 10.
# Без PO Token YouTube не отдаёт отдельные аудиодорожки веб-клиенту (остаётся только склеенный 360p, формат 18):
# тогда берём его, звук из него Whisper читает так же, файл лишь больше
AUDIO_FORMAT = (
    "worstaudio[acodec=opus][format_note*=original]/worstaudio[format_note*=original]/"
    "worstaudio[acodec=opus]/worstaudio/18/worst[height<=480]/worst"
)
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_UNITS = {
    "second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365,
    "s": 0, "m": 0, "h": 0, "d": 1, "w": 7, "mo": 30, "y": 365,
}
# "4 years ago", "Streamed 2 years ago", "11 months ago", в сокращённой вёрстке "4y ago"
_AGE = re.compile(r"(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?|mo|[smhdwy])\b", re.IGNORECASE)
_NOISE = re.compile(r"[™®©]")
_INITIAL_DATA = re.compile(r"""(?:\bytInitialData|\[\s*["']ytInitialData["']\s*\])\s*=\s*(?=\{)""")


class YouTubeError(Exception):
    pass


class YouTubeBlocked(YouTubeError):
    """YouTube временно не отдал выдачу: страница без данных, проверка на робота, 429 или сеть.

    Это не "летсплея нет", а "мы не смогли посмотреть", поэтому попытку игры такой отказ
    не тратит: сервис повторяет поиск позже (app/letsplay.py).
    """


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    channel: str | None
    duration: int | None
    views: int | None
    published: str | None
    was_live: bool

    @property
    def url(self) -> str:
        return f"{WATCH_URL}{self.video_id}"


@dataclass(frozen=True)
class Captions:
    language: str
    generated: bool
    text: str
    segments: list[tuple[float, float, str]]


@dataclass(frozen=True)
class AudioTrack:
    video_id: str
    duration: int
    language: str | None
    format_id: str
    filesize: int | None
    title: str | None
    channel: str | None
    views: int | None
    age_limit: int
    live_status: str | None
    embeddable: bool
    # выбранный формат со звуком и видео вместе: отдельной аудиодорожки YouTube не дал
    muxed: bool = False


def _node_text(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    runs = node.get("runs")
    if isinstance(runs, list):
        return "".join(r.get("text", "") for r in runs if isinstance(r, dict))
    return None


def _find_all(node: Any, key: str, found: list[Any]) -> None:
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                found.append(value)
            _find_all(value, key, found)
    elif isinstance(node, list):
        for value in node:
            _find_all(value, key, found)


def _duration_seconds(text: str | None) -> int | None:
    """"1:05:39" в секунды. У трансляций и Shorts длительности может не быть."""
    if not text:
        return None
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _views(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace(" ", " ").strip()
    match = re.match(r"([\d.]+)\s*([KMB]?)", cleaned, re.IGNORECASE)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return int(number * {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2).lower()])


def published_after(text: str | None, today: date) -> date | None:
    """Самая поздняя возможная дата публикации: "4 years ago" значит не позже, чем 4 года назад."""
    if not text:
        return None
    match = _AGE.search(text)
    if not match:
        return None
    unit = match.group(2).lower()
    if len(unit) > 2 and unit.endswith("s"):
        unit = unit[:-1]
    return today - timedelta(days=int(match.group(1)) * _UNITS.get(unit, 0))


def page_hint(html: str) -> str:
    """Почему страница без данных: это видно по её содержимому и помогает читать журнал."""
    lowered = html[:20000].lower()
    if re.search(r"captcha|unusual traffic|not a robot|verify you're human", lowered):
        return "проверка на робота"
    if "consent.youtube.com" in lowered or "before you continue" in lowered:
        return "страница согласия"
    if len(html) < 20000:
        return f"короткая страница ({len(html)} байт)"
    return "страница без данных выдачи"


def extract_initial_data(html: str) -> Any:
    """JSON выдачи со страницы. Обычно это "var ytInitialData = {...}", но YouTube
    иногда отдаёт ту же запись как window["ytInitialData"] = {...}."""
    for match in _INITIAL_DATA.finditer(html):
        try:
            data, _ = json.JSONDecoder().raw_decode(html, match.end())
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise YouTubeBlocked(f"ytInitialData not found in the search page: {page_hint(html)}")


def _from_renderer(item: Any) -> Candidate | None:
    """Ролик из videoRenderer: основная вёрстка выдачи."""
    if not isinstance(item, dict):
        return None
    video_id = item.get("videoId")
    title = _node_text(item.get("title"))
    if not video_id or not title:
        return None
    published = _node_text(item.get("publishedTimeText"))
    return Candidate(
        video_id=video_id,
        title=title,
        channel=_node_text(item.get("ownerText")),
        duration=_duration_seconds(_node_text(item.get("lengthText"))),
        views=_views(_node_text(item.get("viewCountText")) or _node_text(item.get("shortViewCountText"))),
        published=published,
        was_live=bool(published and "streamed" in published.lower()),
    )


def _lockup_texts(node: Any) -> list[str]:
    """Строки из блока новой вёрстки: подписи лежат в "content", значки поверх обложки в "text"."""
    found: list[Any] = []
    for key in ("content", "text"):
        _find_all(node, key, found)
    return [value for value in found if isinstance(value, str) and value.strip()]


def _from_lockup(item: Any) -> Candidate | None:
    """Ролик из lockupViewModel: новая вёрстка, в которую YouTube переводит выдачу.

    Плейлисты и каналы пропускаем: у них contentType не VIDEO, а contentId не id ролика.
    """
    if not isinstance(item, dict) or item.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
        return None
    video_id = item.get("contentId")
    wrapper = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    metadata = wrapper.get("lockupMetadataViewModel") if isinstance(wrapper.get("lockupMetadataViewModel"), dict) else {}
    node = metadata.get("title") if isinstance(metadata.get("title"), dict) else {}
    title = _node_text(node) or node.get("content")
    if not isinstance(video_id, str) or not isinstance(title, str) or not title:
        return None
    # длительность лежит значком поверх обложки, остальное строками подписи
    duration = next(
        (seconds for text in _lockup_texts(item.get("contentImage")) if (seconds := _duration_seconds(text))), None
    )
    rows = _lockup_texts(metadata)
    views = next((_views(text) for text in rows if "view" in text.lower()), None)
    published = next((text for text in rows if "ago" in text.lower()), None)
    channel = next((text for text in rows if text != title and "view" not in text.lower()
                    and "ago" not in text.lower()), None)
    return Candidate(
        video_id=video_id,
        title=title,
        channel=channel,
        duration=duration,
        views=views,
        published=published,
        was_live=bool(published and "streamed" in published.lower()),
    )


def parse_results(data: Any) -> list[Candidate]:
    """Ролики из ответа выдачи: обе вёрстки сразу. Пустой ответ поиска даёт пустой список."""
    renderers: list[Any] = []
    lockups: list[Any] = []
    _find_all(data, "videoRenderer", renderers)
    _find_all(data, "lockupViewModel", lockups)
    candidates: list[Candidate] = []
    seen: set[str] = set()

    def take(candidate: Candidate | None) -> None:
        if candidate is None or candidate.video_id in seen:
            return
        seen.add(candidate.video_id)
        candidates.append(candidate)

    for item in renderers:
        take(_from_renderer(item))
    for item in lockups:
        take(_from_lockup(item))
    if not candidates and lockups:
        # данные есть, но ролики в незнакомой вёрстке: пустой список записал бы все игры
        # в not_found, поэтому это отказ с повтором, а не "летсплеев нет"
        raise YouTubeBlocked("search results use an unknown layout (lockupViewModel)")
    return candidates


def parse_search(html: str) -> list[Candidate]:
    """Ролики со страницы поиска. Страница "ничего не найдено" даёт пустой список."""
    return parse_results(extract_initial_data(html))


def normalize(text: str) -> list[str]:
    """Слова названия без торговых знаков, с римскими цифрами в виде обычных."""
    # апостроф пишут по-разному: "Baldur's Gate 3" в заголовках роликов часто "Baldurs Gate 3"
    cleaned = _NOISE.sub(" ", text.lower().replace("'", "").replace("’", ""))
    words = re.findall(r"\w+", cleaned, re.UNICODE)
    return [_ROMAN.get(word, word) for word in words if word not in {"the", "a", "an"}]


def title_matches(game_title: str, video_title: str) -> bool:
    """Все значимые слова названия игры должны встречаться в заголовке ролика.

    Отсекает и чужие игры (Hades вместо Hades 2), и ролики, где слово из названия попало
    случайно, только если название состоит из нескольких слов.
    """
    game_words = normalize(game_title)
    video_list = normalize(video_title)
    video_words = set(video_list)
    if not game_words:
        return False
    if not all(word in video_words for word in game_words):
        return False
    # короткие названия и аббревиатуры ("SSS-FPV") складываются из случайных кусков чужих заголовков
    # ("No SSs Run ... FPV"): им нужно название целиком, слова подряд
    if len(game_words) <= 2 or any(len(word) <= 3 for word in game_words):
        n = len(game_words)
        return any(video_list[i:i + n] == game_words for i in range(len(video_list) - n + 1))
    return True


def filter_candidates(
    candidates: list[Candidate],
    *,
    game_title: str,
    release_date: date | None,
    today: date,
    min_duration: int,
    max_duration: int,
    years_before_release: int = 2,
) -> list[Candidate]:
    """Оставляет ролики, которые вообще могут быть летсплеем этой игры. max_duration 0: без верхней границы."""
    result: list[Candidate] = []
    for candidate in candidates:
        if candidate.duration is None or candidate.duration < min_duration:
            continue
        if max_duration > 0 and candidate.duration > max_duration:
            continue
        if not title_matches(game_title, candidate.title):
            continue
        if release_date:
            published = published_after(candidate.published, today)
            # ролик не может быть прохождением игры, которая вышла много позже: у свежей игры
            # в выдаче оказываются старые ролики, где слово из названия попало случайно
            if published and published < release_date - timedelta(days=365 * years_before_release):
                continue
        result.append(candidate)
    return sorted(result, key=lambda c: c.views or 0, reverse=True)


def create_youtube_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True)


class YouTubeClient:
    """Поиск, субтитры и аудио. Блокирующие библиотеки вызываются в отдельном потоке."""

    def __init__(
        self, http: httpx.AsyncClient, *, min_interval: float = 3.0, attempts: int = SEARCH_ATTEMPTS,
        cookies: Path | None = None, profile: Path | None = None,
    ) -> None:
        self._http = http
        self._pacing = asyncio.Lock()
        self._min_interval = min_interval
        self._attempts = max(1, attempts)
        # cookies залогиненного аккаунта (Netscape): с ними YouTube отдаёт ролик и субтитры и тем адресам,
        # которым без входа отвечает "Sign in to confirm you're not a bot"
        self._cookies: Path | None = None
        # профиль Chrome сервиса: cookies выгружаются из него сами (app/ytcookies.py), файл выше
        # переписывается при старте, по таймеру и при отказе YouTube
        self._profile = profile
        self._cookies_path = cookies
        self._refresh_lock = threading.Lock()
        self._refreshed_at = 0.0
        # состояние последнего обновления cookies для мониторинга
        self.cookies_state: dict[str, Any] | None = None
        if cookies is not None:
            if cookies.is_file():
                self._cookies = cookies
                log.info("youtube: cookies from %s", cookies)
            elif profile is None:
                log.warning("youtube: cookies file %s not found, requests go without login", cookies)
        self._next_start = 0.0
        # до этого момента ждут все запросы клиента: YouTube отказал, давить на него нельзя
        self._blocked_until = 0.0
        # сколько раз YouTube отказывал за время работы сервиса: видно в журнале
        self.blocks = 0
        # ход текущей загрузки звука для мониторинга; None, пока ничего не качается
        self.download: dict[str, Any] | None = None

    async def _wait_turn(self) -> None:
        async with self._pacing:
            now = time.monotonic()
            wait = max(self._next_start - now, self._blocked_until - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_start = time.monotonic() + self._min_interval

    def _hold(self, seconds: float) -> None:
        """Пауза для всех запросов клиента: поиск летсплеев и трейлеров идут через него же."""
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    @property
    def has_profile(self) -> bool:
        return self._profile is not None and self._cookies_path is not None

    def _refresh_sync(self, reason: str, min_age: float = 0.0) -> bool:
        """Выгружает cookies из профиля Chrome. True, если вход в аккаунт жив.

        Блокирует поток на 5-20 секунд, поэтому из event loop зовётся через refresh_cookies.
        min_age: не повторять, если обновляли меньше этих секунд назад (отказ на каждый ролик не должен
        запускать Chrome снова и снова).
        """
        if not self.has_profile:
            return False
        from app import ytcookies

        with self._refresh_lock:
            if min_age and time.monotonic() - self._refreshed_at < min_age:
                return bool(self.cookies_state and self.cookies_state.get("logged_in"))
            self._refreshed_at = time.monotonic()
            state: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reason": reason}
            try:
                report = ytcookies.export_cookies(self._profile, self._cookies_path)  # type: ignore[arg-type]
            except ytcookies.NotLoggedIn as e:
                self._cookies = self._cookies_path if self._cookies_path.is_file() else None  # type: ignore[union-attr]
                state.update(logged_in=False, error=str(e))
                log.warning("youtube cookies (%s): %s", reason, e)
            except Exception as e:
                state.update(logged_in=bool(self.cookies_state and self.cookies_state.get("logged_in")), error=f"{type(e).__name__}: {e}")
                log.warning("youtube cookies (%s) failed: %s", reason, state["error"])
            else:
                self._cookies = report.path
                state.update(logged_in=True, count=report.count, error=None)
                log.info("youtube cookies (%s): %d cookies, account is logged in", reason, report.count)
            self.cookies_state = state
            return bool(state.get("logged_in"))

    async def refresh_cookies(self, reason: str, min_age: float = 0.0) -> bool:
        return await asyncio.to_thread(self._refresh_sync, reason, min_age)

    async def search(self, query: str, sort: str = "views") -> list[Candidate]:
        """Ролики по запросу. При отказе YouTube повторяет с паузой, затем пробует внутренний API.

        Отказ, который не удалось обойти, поднимается как YouTubeBlocked: игре он попытку не тратит.
        """
        last: YouTubeBlocked | None = None
        for attempt in range(1, self._attempts + 1):
            await self._wait_turn()
            try:
                html = await get_text(
                    self._http,
                    SEARCH_URL,
                    params={"search_query": query, "sp": SEARCH_SORTS[sort], "hl": "en", "gl": "US"},
                    attempts=2,
                )
            except FetchError as e:
                if e.status is not None and e.status not in RETRY_STATUSES:
                    raise YouTubeError(f"search request failed: {e}") from e
                last = YouTubeBlocked(f"search request failed: {e}")
            else:
                try:
                    candidates = parse_results(extract_initial_data(html))
                except YouTubeBlocked as e:
                    last = e
                else:
                    log.info("youtube search %r (%s): %d videos", query, sort, len(candidates))
                    return candidates
            self.blocks += 1
            delay = BLOCK_DELAYS[min(attempt, len(BLOCK_DELAYS)) - 1]
            self._hold(delay)
            log.warning("youtube отказал на %r (%s), попытка %d из %d, пауза %.0f с: %s",
                        query, sort, attempt, self._attempts, delay, last)
        try:
            candidates = await self._search_api(query, sort)
        except YouTubeError as e:
            self._hold(BLOCK_COOLDOWN)
            raise YouTubeBlocked(f"{last}; внутренний API тоже не ответил: {e}") from last
        log.info("youtube search %r (%s): %d videos через внутренний API", query, sort, len(candidates))
        return candidates

    async def _search_api(self, query: str, sort: str) -> list[Candidate]:
        """Запасной путь: тот же поиск через youtubei, которым пользуется сама страница.

        Он не зависит от вёрстки HTML и выручает, когда страница приходит без данных.
        """
        await self._wait_turn()
        body = {"context": {"client": API_CLIENT}, "query": query, "params": SEARCH_SORTS[sort]}
        try:
            response = await self._http.post(API_URL, json=body)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise YouTubeError(f"youtubei search failed: {type(e).__name__}: {e}") from e
        return parse_results(data)

    async def captions(self, video_id: str, prefer: str | None = None) -> Captions | None:
        """Субтитры на языке речи: авторские лучше автоматических. Нет субтитров: None."""
        return await asyncio.to_thread(self._captions, video_id, prefer)

    def _captions(self, video_id: str, prefer: str | None) -> Captions | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError as e:
            raise YouTubeError(f"youtube-transcript-api is not installed: {e}") from e
        session = None
        if self._cookies:
            import http.cookiejar
            import requests
            jar = http.cookiejar.MozillaCookieJar(str(self._cookies))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError) as e:
                log.warning("youtube: cannot read cookies %s: %s", self._cookies, e)
            else:
                session = requests.Session()
                session.cookies = jar
        try:
            tracks = list(YouTubeTranscriptApi(http_client=session).list(video_id))
        except Exception as e:
            # RequestBlocked и IpBlocked: YouTube не пускает наш адрес к субтитрам, и cookies тут не
            # помогают (библиотека ходит от имени мобильного клиента). Субтитры запасной источник,
            # поэтому это не отказ, а "субтитров нет": расшифровка идёт через yt-dlp и Whisper
            if type(e).__name__ in ("RequestBlocked", "IpBlocked"):
                log.warning("youtube: captions for %s blocked (%s)", video_id, type(e).__name__)
                return None
            log.info("no caption list for %s: %s", video_id, type(e).__name__)
            return None
        if not tracks:
            return None
        # язык автоматической дорожки совпадает с языком речи, поэтому по нему выбираем авторскую
        generated = [t for t in tracks if t.is_generated]
        spoken = prefer or (generated[0].language_code if generated else None)
        same_language = [
            t for t in tracks if spoken and t.language_code.split("-")[0] == spoken.split("-")[0]
        ]
        manual = [t for t in same_language if not t.is_generated]
        track = (manual or same_language or generated or tracks)[0]
        try:
            snippets = track.fetch().to_raw_data()
        except Exception as e:
            log.warning("cannot fetch captions for %s: %s: %s", video_id, type(e).__name__, e)
            return None
        segments = [
            (round(float(s["start"]), 1), round(float(s["start"]) + float(s.get("duration", 0)), 1), s["text"].strip())
            for s in snippets
            if s.get("text", "").strip()
        ]
        if not segments:
            return None
        return Captions(
            language=track.language_code,
            generated=track.is_generated,
            text=" ".join(s[2] for s in segments),
            segments=segments,
        )

    async def audio_track(self, video_id: str) -> AudioTrack:
        return await asyncio.to_thread(self._audio_track, video_id)

    def _audio_track(self, video_id: str) -> AudioTrack:
        info = self._extract(video_id, download=False)
        return AudioTrack(
            video_id=video_id,
            duration=int(info.get("duration") or 0),
            language=info.get("language"),
            format_id=str(info.get("format_id") or ""),
            filesize=info.get("filesize") or info.get("filesize_approx"),
            title=info.get("title"),
            channel=info.get("uploader") or info.get("channel"),
            views=info.get("view_count"),
            age_limit=int(info.get("age_limit") or 0),
            live_status=info.get("live_status"),
            embeddable=bool(info.get("playable_in_embed", True)),
            muxed=str(info.get("vcodec") or "none") != "none",
        )

    async def download_audio(self, video_id: str, target: Path, *, max_filesize: int | None = None) -> Path:
        """Скачивает оригинальную аудиодорожку целиком: кусочная загрузка через ffmpeg упирается
        в ограничение скорости YouTube (5 секунд аудио читались 19 секунд)."""
        return await asyncio.to_thread(self._download_audio, video_id, target, max_filesize)

    def _download_audio(self, video_id: str, target: Path, max_filesize: int | None) -> Path:
        target.mkdir(parents=True, exist_ok=True)
        self.download = {"video_id": video_id, "downloaded": 0, "total": None, "percent": None, "speed": None}

        def progress(event: dict[str, Any]) -> None:
            # yt-dlp зовёт из своего потока, мониторинг этот словарь только читает
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            done = event.get("downloaded_bytes") or 0
            self.download = {
                "video_id": video_id,
                "downloaded": done,
                "total": total,
                "percent": round(100 * done / total, 1) if total else None,
                "speed": event.get("speed"),
            }

        options: dict[str, Any] = {
            "outtmpl": str(target / "%(id)s.%(ext)s"),
            "http_chunk_size": 10 * 2**20,  # без разбивки YouTube режет скорость соединения
            "socket_timeout": 30,
            "retries": 3,
            "progress_hooks": [progress],
        }
        if max_filesize:
            options["max_filesize"] = max_filesize
        try:
            self._extract(video_id, download=True, options=options)
        finally:
            self.download = None
        files = [p for p in target.glob(f"{video_id}.*") if p.suffix not in (".part", ".json")]
        if not files:
            raise YouTubeError(f"audio for {video_id} was not downloaded (too large or unavailable)")
        return files[0]

    def _extract(self, video_id: str, *, download: bool, options: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return self._extract_once(video_id, download=download, options=options)
        except YouTubeBlocked:
            # проверка на робота при наличии профиля Chrome: чаще всего протухли cookies, обновляем их
            # из профиля и пробуем ещё раз; без входа в профиле повтор бессмыслен
            if not self.has_profile:
                raise
            if not self._refresh_sync("отказ YouTube", min_age=60.0):
                raise YouTubeBlocked(
                    "YouTube требует вход, а в профиле Chrome сервиса входа нет: пункт \"Войти в YouTube\" в лаунчере"
                ) from None
            self._blocked_until = 0.0
            return self._extract_once(video_id, download=download, options=options)

    def _extract_once(self, video_id: str, *, download: bool, options: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            import yt_dlp
        except ImportError as e:
            raise YouTubeError(f"yt-dlp is not installed: {e}") from e
        params: dict[str, Any] = {"format": AUDIO_FORMAT, "quiet": True, "noprogress": True, "no_warnings": True}
        if self._cookies:
            # yt-dlp переписывает файл cookies по завершении и теряет часть cookies входа, после чего
            # YouTube снова требует вход. Работаем с копией, оригинал остаётся как выгружен из браузера
            work = self._cookies.with_name(self._cookies.name + ".work")
            try:
                shutil.copyfile(self._cookies, work)
                params["cookiefile"] = str(work)
            except OSError as e:
                log.warning("youtube: cannot copy cookies to %s: %s", work, e)
                params["cookiefile"] = str(self._cookies)
        params.update(options or {})
        if not download:
            params["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(params) as ydl:
                info = ydl.extract_info(f"{WATCH_URL}{video_id}", download=download)
        except Exception as e:
            if BOT_CHECK_MARK in str(e):
                # проверка на робота: адрес помечен YouTube, игра не виновата; все запросы ждут
                self.blocks += 1
                self._hold(BOT_CHECK_COOLDOWN)
                raise YouTubeBlocked(f"yt-dlp: YouTube требует вход ({BOT_CHECK_MARK}...)") from e
            raise YouTubeError(f"yt-dlp failed for {video_id}: {type(e).__name__}: {e}") from e
        if not isinstance(info, dict):
            raise YouTubeError(f"yt-dlp returned no data for {video_id}")
        return info
