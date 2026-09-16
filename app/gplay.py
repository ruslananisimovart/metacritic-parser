"""Обложка из Google Play для игр, которых нет в Steam (мобильные релизы из SEE ALL).

Официального API у Play нет: поиск и страница приложения читаются как HTML. Из страницы берутся только
устойчивые вещи: meta-теги og:title и og:image (иконка 512x512) и имя разработчика из ссылки
/store/apps/dev. Игра считается той же по тем же правилам, что и в Steam: название совпадает дословно
после нормализации, разработчик сходится с компаниями Metacritic, а если Metacritic компаний не знает,
совпадает год выхода. Трейлер из Play не берётся: это ролик YouTube, а он в браузере без входа не играет.
"""

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.metacritic import Game
from app.steam import _PAREN_TAIL, _year, companies_match, normalize_title, plain_text, titles_match

log = logging.getLogger(__name__)

SEARCH_URL = "https://play.google.com/store/search"
DETAILS_URL = "https://play.google.com/store/apps/details"
# сколько найденных приложений открываем по странице
MAX_CANDIDATES = 4
_PACKAGE = re.compile(r"/store/apps/details\?id=([A-Za-z0-9_.]+)")
_OG = re.compile(r'<meta property="og:(title|image)" content="([^"]*)"')
_DEVELOPER = re.compile(r'href="/store/apps/dev(?:eloper)?\?id=[^"]*"[^>]*>(?:<span[^>]*>)?([^<]{1,100})<')
_DATE = re.compile(r'"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, (?:19|20)\d{2})"')
_TITLE_TAIL = re.compile(r"\s*-\s*Apps on Google Play\s*$")
# полное описание приложения лежит в блоке data-g-id="description"; короткое в meta description
_DESCRIPTION = re.compile(r'data-g-id="description"[^>]*>(.*?)</div>', re.DOTALL)
_META_DESCRIPTION = re.compile(r'<meta (?:name|itemprop)="description" content="([^"]*)"')


class GPlayError(Exception):
    pass


@dataclass(frozen=True)
class GPlayMedia:
    package: str
    name: str
    cover_url: str  # иконка, квадрат
    description: str | None = None

    @property
    def store_url(self) -> str:
        return f"{DETAILS_URL}?id={self.package}"


def _icon(url: str) -> str:
    """Иконка нужного размера: параметр после "=" задаёт размер и скругление, s0-br30 это оригинал со скруглением."""
    base = url.split("=", 1)[0]
    return f"{base}=w512-h512"


class GPlayClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _page(self, url: str, params: dict[str, Any]) -> str:
        try:
            response = await self._http.get(url, params=params)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as e:
            raise GPlayError(f"{url}: {type(e).__name__}: {e}") from e

    async def search(self, title: str) -> list[str]:
        """Пакеты приложений из выдачи, в порядке страницы, без повторов."""
        page = await self._page(SEARCH_URL, {"q": title, "c": "apps", "hl": "en", "gl": "US"})
        seen: list[str] = []
        for match in _PACKAGE.finditer(page):
            package = match.group(1)
            if package not in seen:
                seen.append(package)
        return seen

    async def details(self, package: str) -> dict[str, Any] | None:
        page = await self._page(DETAILS_URL, {"id": package, "hl": "en", "gl": "US"})
        meta = {key: html.unescape(value) for key, value in _OG.findall(page)}
        if "title" not in meta:
            return None
        developer = _DEVELOPER.search(page)
        full = _DESCRIPTION.search(page)
        short = _META_DESCRIPTION.search(page)
        return {
            "name": _TITLE_TAIL.sub("", meta["title"]).strip(),
            "icon": meta.get("image") or None,
            "developer": html.unescape(developer.group(1)).strip() if developer else None,
            "years": sorted({_year(d) for d in _DATE.findall(page) if _year(d)}),
            "description": plain_text(full.group(1) if full else None) or plain_text(short.group(1) if short else None),
        }

    async def find_cover(self, game: Game) -> tuple[GPlayMedia | None, str]:
        """Обложка из Play и причина, если игры там нет (для журнала)."""
        plain = _PAREN_TAIL.sub("", game.title).strip()
        wanted = {normalize_title(game.title), normalize_title(plain)} - {""}
        if not wanted:
            return None, "пустое название"
        packages = await self.search(plain or game.title)
        if not packages:
            return None, "в Google Play ничего не нашлось"
        reasons: list[str] = []
        ours = [*game.developers, *game.publishers]
        for package in packages[:MAX_CANDIDATES]:
            data = await self.details(package)
            if data is None:
                reasons.append(f"{package}: страница не разобралась")
                continue
            # частичное совпадение названия (подзаголовок, издание) принимается только вместе с разработчиком
            if not titles_match(wanted, normalize_title(data["name"]), strict=not ours):
                reasons.append(f"{package}: это {data['name']!r}")
                continue
            if ours:
                if not data["developer"] or not companies_match(ours, [data["developer"]]):
                    reasons.append(f"{package}: разработчик не сходится ({data['developer'] or 'нет'})")
                    continue
            elif game.release_date is None or game.release_date.year not in data["years"]:
                reasons.append(f"{package}: у Metacritic нет компаний, а год выхода не совпал")
                continue
            if not data["icon"]:
                reasons.append(f"{package}: у страницы нет иконки")
                continue
            return GPlayMedia(package, data["name"], _icon(data["icon"]), data["description"]), ""
        return None, "; ".join(reasons) or "совпадений нет"
