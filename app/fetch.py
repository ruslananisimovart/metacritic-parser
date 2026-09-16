"""GET-запросы JSON с таймаутами и повторами для внешних сервисов."""

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{message}: {url}")
        self.url = url
        self.status = status


class NotFound(FetchError):
    pass


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


async def get_response(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> httpx.Response:
    """Повторяет запрос при сетевых ошибках, 429 и 5xx; 404 и прочие 4xx сразу поднимает."""
    full_url = str(httpx.URL(url, params=params))
    for attempt in range(1, attempts + 1):
        delay: float | None = None
        try:
            response = await client.get(url, params=params)
        except httpx.TransportError as e:
            error = FetchError(full_url,f"network error {type(e).__name__}")
        else:
            if response.status_code == 404:
                raise NotFound(full_url, "HTTP 404", 404)
            if response.status_code in RETRY_STATUSES:
                error = FetchError(full_url,f"HTTP {response.status_code}", response.status_code)
                delay = retry_after_seconds(response)
            elif response.is_error:
                raise FetchError(full_url,f"HTTP {response.status_code}", response.status_code)
            else:
                return response

        if attempt == attempts:
            raise error
        if delay is None:
            delay = base_delay * 2 ** (attempt - 1) * random.uniform(0.8, 1.2)
        delay = min(delay, max_delay)
        log.warning("%s, retry %d/%d in %.1fs", error, attempt, attempts - 1, delay)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Any:
    response = await get_response(
        client, url, params=params, attempts=attempts, base_delay=base_delay, max_delay=max_delay
    )
    try:
        return response.json()
    except ValueError:
        raise FetchError(str(response.url), "response is not valid JSON", response.status_code) from None


async def get_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> str:
    """HTML-страницы (поиск YouTube) с теми же таймаутами и повторами, что и JSON."""
    response = await get_response(
        client, url, params=params, attempts=attempts, base_delay=base_delay, max_delay=max_delay
    )
    return response.text
