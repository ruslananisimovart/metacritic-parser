"""Обложка и трейлер из Steam для игр, у которых Metacritic их не отдаёт (свежие инди из SEE ALL).

Магазин Steam отвечает без ключа и входа: поиск по названию и карточка приложения. Игра считается
той же, если название совпадает дословно после нормализации, тип приложения game, и разработчик или
издатель сходятся с Metacritic; если Metacritic компаний не знает, достаточно совпадения года выхода.

Обложка: вертикальный постер library_600x900 (ложится в рамку обложки как есть), а его нет у игр
с новой схемой адресов Steam, тогда горизонтальный header (460x215), который карточка вписывает в рамку
с размытыми полями. Трейлер: первый ролик карточки (Steam помечает главный флагом highlight), поток HLS,
браузер играет его через hls.js. Не нашли, повтор через неделю: у свежих игр страница в Steam появляется
не сразу.
"""

import html
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx

from app.metacritic import Game

log = logging.getLogger(__name__)

SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"
PORTRAIT_URL = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
STORE_URL = "https://store.steampowered.com/app/{appid}/"
RETRY_AFTER = timedelta(days=7)
# сколько совпавших по названию приложений проверяем по карточке: у коротких названий ("Blind Spot")
# в Steam бывает по 4-5 тёзок, нужная игра может стоять последней
MAX_CANDIDATES = 6
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
# юридические хвосты названий компаний, которые Steam и Metacritic пишут по-разному
_COMPANY_TAILS = re.compile(
    r"\b(llc|inc|ltd|limited|gmbh|co|corp|corporation|pty|plc|oy|ab|srl|sarl|sas|kk|s\.?a\.?|s\.?r\.?l\.?)\b\.?",
    re.IGNORECASE,
)
_NOISE = re.compile(r"[™®©]")
_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")
_NON_WORD = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)
# поиск Steam ничего не отдаёт на запрос со знаками-разделителями ("AFK - Army For Keyboard",
# "Chef Hands : Kitchen Mayhem"), а без них находит игру; повторный запрос идёт без этих знаков
_SEARCH_PUNCT = re.compile(r"[-–—:!?.,/]+")


class SteamError(Exception):
    pass


@dataclass(frozen=True)
class SteamMedia:
    appid: int
    name: str
    cover_url: str | None
    cover_kind: str | None  # portrait: вертикальный постер 600x900; header: горизонтальная картинка 460x215
    video_url: str | None  # HLS-плейлист трейлера
    video_title: str | None
    video_poster: str | None  # кадр ролика 600x337
    description: str | None = None  # короткое описание со страницы магазина, текстом без разметки

    @property
    def store_url(self) -> str:
        return STORE_URL.format(appid=self.appid)


def _fold(text: str) -> str:
    """Буквы без диакритики и лигатур: "Göktürk" и "Gokturk" одно слово, "Pokémon" и "Pokemon" тоже."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace("ß", "ss").replace("ø", "o").replace("Ø", "o").replace("đ", "d").replace("ł", "l")


# знаки ™®© убираются до разложения Unicode: NFKD превратил бы ™ в буквы "TM"
def normalize_title(text: str) -> str:
    text = _fold(_NOISE.sub("", text or "")).lower().replace("&", " and ")
    return " ".join(_NON_WORD.sub(" ", text).split())


def normalize_company(text: str) -> str:
    text = _COMPANY_TAILS.sub(" ", _fold(_NOISE.sub("", text or "")).lower().replace("&", " and "))
    return " ".join(_NON_WORD.sub(" ", text).split())


def titles_match(ours: set[str], theirs: str, *, strict: bool) -> bool:
    """Совпало ли название магазина с нашим. strict: дословно после нормализации. Иначе одно внутри
    другого (подзаголовок, "Deluxe Edition"), но короткое не короче 6 знаков: это второй уровень,
    его применяют только при совпавшем разработчике или издателе."""
    if theirs in ours:
        return True
    if strict:
        return False
    return any(len(x) >= 6 and len(theirs) >= 6 and (x in theirs or theirs in x) for x in ours)


def companies_match(ours: list[str], theirs: list[str]) -> bool:
    """Совпала ли хоть одна компания: дословно после нормализации или одно название внутри другого."""
    a = {normalize_company(c) for c in ours if c}
    b = {normalize_company(c) for c in theirs if c}
    a.discard("")
    b.discard("")
    for x in a:
        for y in b:
            if x == y or (len(x) >= 4 and len(y) >= 4 and (x in y or y in x)):
                return True
    return False


def _year(text: str | None) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", text or "")
    return int(match.group(0)) if match else None


_TAGS = re.compile(r"<[^>]+>")
_BREAKS = re.compile(r"<\s*(br|/p|/div|/li|/h\d)\s*/?>", re.IGNORECASE)


def plain_text(raw: Any, limit: int = 2000) -> str | None:
    """Описание магазина текстом: разметка убирается, переносы абзацев остаются, длинное режется по слову."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = html.unescape(_TAGS.sub("", _BREAKS.sub("\n", raw)))
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return text or None


def pick_movie(movies: Any) -> dict[str, Any] | None:
    """Главный ролик карточки: с флагом highlight, иначе первый; нужен поток HLS."""
    items = [m for m in movies if isinstance(m, dict) and isinstance(m.get("hls_h264"), str)] if isinstance(movies, list) else []
    if not items:
        return None
    return next((m for m in items if m.get("highlight")), items[0])


def create_steam_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True)


class SteamClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _json(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = await self._http.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SteamError(f"{url}: {type(e).__name__}: {e}") from e

    async def search(self, title: str) -> list[dict[str, Any]]:
        data = await self._json(SEARCH_URL, {"term": title, "l": "english", "cc": "US"})
        items = data.get("items") if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    async def details(self, appid: int) -> dict[str, Any] | None:
        data = await self._json(DETAILS_URL, {"appids": appid, "l": "english", "cc": "US"})
        entry = data.get(str(appid)) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not entry.get("success"):
            return None
        payload = entry.get("data")
        return payload if isinstance(payload, dict) else None

    async def portrait_exists(self, appid: int) -> bool:
        try:
            response = await self._http.head(PORTRAIT_URL.format(appid=appid))
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def find_media(self, game: Game) -> tuple[SteamMedia | None, str]:
        """Обложка и трейлер из Steam и причина, если игры там нет (для журнала)."""
        # Metacritic дописывает к повторяющимся названиям уточнение в скобках ("BLIND SPOT (Kyle Simpson)"),
        # в Steam игра называется без него
        plain = _PAREN_TAIL.sub("", game.title).strip()
        wanted = {normalize_title(game.title), normalize_title(plain)} - {""}
        if not wanted:
            return None, "пустое название"
        ours = [*game.developers, *game.publishers]
        term = plain or game.title
        found = await self.search(term)
        cleaned = " ".join(_SEARCH_PUNCT.sub(" ", term).split())
        if not found and cleaned and cleaned != term:
            found = await self.search(cleaned)
        # дословные совпадения первыми, затем частичные (подзаголовок, издание): те принимаются только
        # при совпавшем разработчике или издателе, поэтому без компаний Metacritic их не рассматриваем
        exact = [item for item in found if titles_match(wanted, normalize_title(str(item.get("name", ""))), strict=True)]
        loose = [item for item in found if item not in exact and ours
                 and titles_match(wanted, normalize_title(str(item.get("name", ""))), strict=False)]
        if not exact and not loose:
            return None, "в Steam нет игры с таким названием"
        # тёзки: сначала те, у кого название совпало дословно, с регистром ("BLIND SPOT" среди "Blind Spot")
        exact.sort(key=lambda item: str(item.get("name", "")).strip() != plain)
        hits = [*exact, *loose]
        reasons: list[str] = []
        for item in hits[:MAX_CANDIDATES]:
            try:
                appid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            data = await self.details(appid)
            if data is None:
                reasons.append(f"{appid}: карточка недоступна")
                continue
            if data.get("type") != "game":
                reasons.append(f"{appid}: это {data.get('type')}, не игра")
                continue
            theirs = [*(data.get("developers") or []), *(data.get("publishers") or [])]
            if ours:
                if not companies_match(ours, theirs):
                    reasons.append(f"{appid}: разработчик и издатель не сходятся ({', '.join(theirs) or 'нет'})")
                    continue
            else:
                release = (data.get("release_date") or {}).get("date") if isinstance(data.get("release_date"), dict) else None
                year = _year(release)
                if game.release_date is None or year is None or year != game.release_date.year:
                    reasons.append(f"{appid}: у Metacritic нет компаний, а год выхода не совпал ({release or 'нет'})")
                    continue
            name = str(data.get("name") or item.get("name"))
            cover_url, cover_kind = None, None
            if await self.portrait_exists(appid):
                cover_url, cover_kind = PORTRAIT_URL.format(appid=appid), "portrait"
            elif isinstance(data.get("header_image"), str) and data["header_image"]:
                cover_url, cover_kind = data["header_image"], "header"
            movie = pick_movie(data.get("movies"))
            return SteamMedia(
                appid,
                name,
                cover_url,
                cover_kind,
                movie["hls_h264"] if movie else None,
                (str(movie.get("name")) or None) if movie else None,
                (movie.get("thumbnail") if isinstance(movie.get("thumbnail"), str) else None) if movie else None,
                plain_text(data.get("short_description")) or plain_text(data.get("about_the_game")),
            ), ""
        return None, "; ".join(reasons) or "совпадений нет"
