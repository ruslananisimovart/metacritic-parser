"""Клиент внутреннего JSON API Metacritic (backend.metacritic.com).

API неофициальный, поэтому ответы разбираются pydantic-моделями: если схема
поменяется, упадёт разбор конкретной игры, а в базу не попадут пустые данные.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from app.fetch import NotFound, get_json

log = logging.getLogger(__name__)

API_URL = "https://backend.metacritic.com"
SITE_URL = "https://www.metacritic.com"
GAME_TYPE_ID = 13
REVIEWS_PAGE_LIMIT = 50
FINDER_MAX_LIMIT = 50  # на limit 51 и больше finder отвечает HTTP 400
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": f"{SITE_URL}/",
}

ReviewKind = Literal["critic", "user"]
Sentiment = Literal["all", "positive", "neutral", "negative"]


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class ListedGame:
    id: int
    slug: str
    title: str
    release_date: date | None
    # данные для предварительной карточки каталога, остальное приходит с подробностями игры
    cover_url: str | None = None
    metascore: int | None = None
    critic_reviews: int = 0
    genres: tuple[str, ...] = ()
    description: str | None = None
    rating: str | None = None


@dataclass(frozen=True)
class Listing:
    total: int
    games: list[ListedGame]


@dataclass
class PlatformScores:
    name: str
    slug: str
    is_main: bool
    metascore: int | None
    critic_reviews: int
    userscore: float | None = None
    user_reviews: int = 0


@dataclass
class Game:
    id: int
    slug: str
    title: str
    description: str | None
    release_date: date | None
    rating: str | None
    genres: list[str]
    developers: list[str]
    publishers: list[str]
    cover_url: str | None
    video_url: str | None
    video_title: str | None
    metascore: int | None
    critic_reviews: int
    platforms: list[PlatformScores]

    @property
    def url(self) -> str:
        return f"{SITE_URL}/game/{self.slug}/"

    @property
    def main_platform(self) -> PlatformScores | None:
        """Платформа, которую Metacritic показывает на странице игры; к ней относится metascore."""
        return next((p for p in self.platforms if p.is_main), None)

    @property
    def userscore(self) -> float | None:
        main = self.main_platform
        return main.userscore if main else None


@dataclass(frozen=True)
class Review:
    kind: ReviewKind
    text: str
    score: float | None
    source: str | None
    published: date | None
    platform: str | None
    spoiler: bool
    url: str | None = None


@dataclass(frozen=True)
class Reviews:
    total: int
    items: list[Review]


def _loose_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _none_to_list(value: Any) -> Any:
    return [] if value is None else value


T = TypeVar("T")
LooseDate = Annotated[date | None, BeforeValidator(_loose_date)]
NullableList = Annotated[list[T], BeforeValidator(_none_to_list)]


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ScoreSummary(_ApiModel):
    score: float | None = None
    review_count: int | None = Field(None, alias="reviewCount")
    sentiment: str | None = None


class _Image(_ApiModel):
    type_name: str | None = Field(None, alias="typeName")
    bucket_type: str | None = Field(None, alias="bucketType")
    bucket_path: str | None = Field(None, alias="bucketPath")


class _Name(_ApiModel):
    name: str


class _Company(_ApiModel):
    name: str
    type_name: str | None = Field(None, alias="typeName")


class _Production(_ApiModel):
    companies: NullableList[_Company] = []


class _Video(_ApiModel):
    title: str | None = None
    embed_url: str | None = Field(None, alias="embedUrl")


class _Platform(_ApiModel):
    name: str
    slug: str
    is_lead: bool = Field(False, alias="isLeadPlatform")
    critic: _ScoreSummary | None = Field(None, alias="criticScoreSummary")


class _Product(_ApiModel):
    id: int
    slug: str
    title: str
    description: str | None = None
    release_date: LooseDate = Field(None, alias="releaseDate")
    platform: str | None = None
    rating: str | None = None
    genres: NullableList[_Name] = []
    production: _Production | None = None
    images: NullableList[_Image] = []
    video: _Video | None = None
    critic: _ScoreSummary | None = Field(None, alias="criticScoreSummary")
    platforms: NullableList[_Platform] = []


class _ListItem(_ApiModel):
    id: int
    slug: str
    title: str
    release_date: LooseDate = Field(None, alias="releaseDate")
    # в списке уже есть обложка, оценка критиков, жанры и описание: этого хватает на карточку каталога,
    # пока подробности игры не загрузились
    image: _Image | None = None
    critic: _ScoreSummary | None = Field(None, alias="criticScoreSummary")
    genres: NullableList[_Name] = []
    description: str | None = None
    rating: str | None = None


class _Listing(_ApiModel):
    total: int | None = Field(None, alias="totalResults")
    items: NullableList[_ListItem] = []


class _Review(_ApiModel):
    quote: str | None = None
    # у рецензии критика ссылка на полный текст в издании, у отзыва игрока её нет
    url: str | None = None
    score: float | None = None
    review_date: LooseDate = Field(None, alias="date")
    platform: str | None = None
    publication: str | None = Field(None, alias="publicationName")
    author: str | None = None
    spoiler: bool | None = None


class _ReviewList(_ApiModel):
    total: int | None = Field(None, alias="totalResults")
    items: NullableList[_Review] = []


M = TypeVar("M", bound=_ApiModel)


def _validate(model: type[M], value: Any, what: str) -> M:
    try:
        return model.model_validate(value)
    except ValidationError as e:
        first = e.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise ParseError(f"{what}: {e.error_count()} validation error(s), first at {location}: {first['msg']}") from e


def _data(payload: Any, what: str, *, item: bool) -> Any:
    data = payload.get("data") if isinstance(payload, dict) else None
    if item:
        data = data.get("item") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise ParseError(f"{what}: unexpected response shape")
    return data


def _metascore(summary: _ScoreSummary | None) -> int | None:
    return round(summary.score) if summary and summary.score is not None else None


def _review_count(summary: _ScoreSummary | None) -> int:
    return (summary.review_count or 0) if summary else 0


def _cover_url(images: list[_Image]) -> str | None:
    by_type = {image.type_name: image for image in images if image.bucket_path}
    image = by_type.get("cardImage") or by_type.get("mainImage") or next(iter(by_type.values()), None)
    if image is None or image.bucket_path is None:
        return None
    path = image.bucket_path if image.bucket_path.startswith("/") else f"/{image.bucket_path}"
    return f"{SITE_URL}/a/img/{image.bucket_type or 'catalog'}{path}"


def _companies(production: _Production | None, role: str) -> list[str]:
    if production is None:
        return []
    return list(dict.fromkeys(c.name for c in production.companies if c.type_name == role))


def _main_platform_slug(product: _Product) -> str | None:
    # isLeadPlatform бывает у нескольких платформ сразу (brigandine-abyss: PS5 и Switch 2),
    # а страница игры и сводная оценка в карточке относятся к платформе из поля platform
    for candidates in (
        [p for p in product.platforms if p.name == product.platform],
        [p for p in product.platforms if p.is_lead],
        product.platforms,
    ):
        if candidates:
            return candidates[0].slug
    return None


def parse_listing(payload: Any) -> Listing:
    data = _validate(_Listing, _data(payload, "listing", item=False), "listing")
    games = [
        ListedGame(
            id=i.id,
            slug=i.slug,
            title=i.title,
            release_date=i.release_date,
            cover_url=_cover_url([i.image] if i.image else []),
            metascore=_metascore(i.critic),
            critic_reviews=_review_count(i.critic),
            genres=tuple(dict.fromkeys(g.name for g in i.genres)),
            description=(i.description or "").strip() or None,
            rating=i.rating,
        )
        for i in data.items
    ]
    return Listing(total=data.total or 0, games=games)


def parse_game(payload: Any) -> Game:
    product = _validate(_Product, _data(payload, "product", item=True), "product")
    main_slug = _main_platform_slug(product)
    platforms = [
        PlatformScores(
            name=p.name,
            slug=p.slug,
            is_main=p.slug == main_slug,
            metascore=_metascore(p.critic),
            critic_reviews=_review_count(p.critic),
        )
        for p in product.platforms
    ]
    video = product.video
    return Game(
        id=product.id,
        slug=product.slug,
        title=product.title,
        description=(product.description or "").strip() or None,
        release_date=product.release_date,
        rating=product.rating,
        genres=list(dict.fromkeys(g.name for g in product.genres)),
        developers=_companies(product.production, "Developer"),
        publishers=_companies(product.production, "Publisher"),
        cover_url=_cover_url(product.images),
        video_url=video.embed_url if video else None,
        video_title=video.title if video else None,
        metascore=_metascore(product.critic),
        critic_reviews=_review_count(product.critic),
        platforms=platforms,
    )


def parse_user_score(payload: Any) -> tuple[float | None, int]:
    summary = _validate(_ScoreSummary, _data(payload, "user score", item=True), "user score")
    count = summary.review_count or 0
    # Пока отзывов мало, Metacritic оценку не считает (на сайте "tbd"), а API
    # отдаёт score 0 и sentiment null. Настоящая оценка всегда идёт с sentiment.
    score = summary.score if count and summary.sentiment else None
    return score, count


def _parse_review_page(payload: Any, kind: ReviewKind) -> tuple[int, int, list[Review]]:
    """Возвращает общее число отзывов, число записей на странице и отзывы с непустым текстом."""
    page = _validate(_ReviewList, _data(payload, "reviews", item=False), "reviews")
    reviews = [
        Review(
            kind=kind,
            text=r.quote.strip(),
            score=r.score,
            source=(r.publication if kind == "critic" else r.author) or None,
            published=r.review_date,
            platform=r.platform,
            spoiler=bool(r.spoiler),
            # у части изданий ссылка приходит с пробелом в начале
            url=(r.url or "").strip() or None,
        )
        for r in page.items
        if r.quote and r.quote.strip()
    ]
    return page.total or 0, len(page.items), reviews


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True)


class MetacriticClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        concurrency: int = 3,
        min_interval: float = 0.3,
        api_key: str | None = None,
    ) -> None:
        self._http = http
        self._semaphore = asyncio.Semaphore(concurrency)
        self._pacing = asyncio.Lock()
        self._min_interval = min_interval
        self._next_start = 0.0
        self._api_key = api_key

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        if self._api_key:
            query["apiKey"] = self._api_key
        async with self._semaphore:
            async with self._pacing:
                wait = self._next_start - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_start = time.monotonic() + self._min_interval
            return await get_json(self._http, f"{API_URL}{path}", params=query or None)

    async def _finder(self, params: dict[str, Any]) -> Listing:
        if not 1 <= params["limit"] <= FINDER_MAX_LIMIT:
            raise ValueError(f"finder limit must be 1..{FINDER_MAX_LIMIT}, got {params['limit']}")
        return parse_listing(await self._get("/finder/metacritic/web", params))

    async def new_releases(self, limit: int = 20) -> Listing:
        """Блок New Releases со страницы /game/: только игры с Metascore, новые сверху."""
        params = {"sortBy": "-releaseDate", "metaScoreMin": 1, "mcoTypeId": GAME_TYPE_ID, "offset": 0, "limit": limit}
        return await self._finder(params)

    async def see_all(self, offset: int, limit: int = 20) -> Listing:
        """Список SEE ALL с сортировкой "новые", включая игры без оценок."""
        params = {
            "sortBy": "-releaseDate",
            "productType": "games",
            "mcoTypeId": GAME_TYPE_ID,
            "offset": offset,
            "limit": limit,
        }
        return await self._finder(params)

    async def game(self, slug: str) -> Game:
        game = parse_game(await self._get(f"/games/metacritic/{quote(slug, safe='')}/web"))
        results = await asyncio.gather(
            *(self._user_score(slug, p.slug) for p in game.platforms), return_exceptions=True
        )
        for platform, result in zip(game.platforms, results):
            if isinstance(result, BaseException):
                raise result
            platform.userscore, platform.user_reviews = result
        return game

    async def _user_score(self, slug: str, platform_slug: str) -> tuple[float | None, int]:
        path = f"/reviews/metacritic/user/games/{quote(slug, safe='')}/platform/{quote(platform_slug, safe='')}/stats/web"
        try:
            return parse_user_score(await self._get(path))
        except NotFound:
            log.warning("no user score stats for %s on %s", slug, platform_slug)
            return None, 0

    async def reviews(
        self,
        slug: str,
        kind: ReviewKind,
        *,
        limit: int,
        sentiment: Sentiment = "all",
        sort: str = "score",
    ) -> Reviews:
        # API может отдать меньше, чем попросили (критиков не больше 10 за раз),
        # поэтому листаем по фактическому числу записей на странице
        path = f"/reviews/metacritic/{kind}/games/{quote(slug, safe='')}/web"
        collected: list[Review] = []
        offset = total = 0
        while len(collected) < limit:
            params = {
                "offset": offset,
                "limit": min(limit - len(collected), REVIEWS_PAGE_LIMIT),
                "filterBySentiment": sentiment,
                "sort": sort,
            }
            total, page_size, page = _parse_review_page(await self._get(path, params), kind)
            collected.extend(page)
            offset += page_size
            if page_size == 0 or offset >= total:
                break
        return Reviews(total=total, items=collected[:limit])
