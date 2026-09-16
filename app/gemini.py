"""Клиент языковой модели со структурированным JSON-ответом.

Три способа доступа (провайдера), выбор по умолчанию в .env (LLM_PROVIDER), дальше
можно переключать на лету из интерфейса:
- kie: агрегатор kie.ai, модель gemini-3-8-flash, основной путь;
- google: напрямую в Gemini API, модель gemini-3.8-flash;
- local: локальная модель в LM Studio через OpenAI-совместимый /v1/chat/completions.

У kie.ai и Google тело запроса одно, Google-native generateContent. Ответ kie.ai при
stream=false совпадает с ответом Google, отличия: расход кредитов в credits_consumed,
рассуждения в usageMetadata.thinkingTokenCount (у Google thoughtsTokenCount), ошибки в
конверте {"code", "msg"}, иногда при HTTP 200.

У облачных провайдеров может быть два ключа: основной используется всегда, второй
включается, когда основной упёрся в квоту, отозван или сервис на нём сбоит.

Локальная модель делит видеокарту с Whisper: запрос к ней берёт тот же замок, что и
расшифровка, поэтому они работают по очереди, а не одновременно.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.fetch import retry_after_seconds
from app.prompts import Prompt, system_text

log = logging.getLogger(__name__)

PROVIDERS = ("kie", "google", "local")
PROVIDER_TITLES = {"kie": "kie.ai", "google": "Gemini API", "local": "LM Studio"}
REMOTE_URLS = {
    "kie": "https://api.kie.ai/gemini/v1/models/{model}:streamGenerateContent",
    "google": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
}
DEFAULT_MODELS = {"kie": "gemini-3-8-flash", "google": "gemini-3.8-flash", "local": "gemma-4-12b-it-qat"}
DEFAULT_LOCAL_URL = "http://127.0.0.1:1234"
LOCAL_KEY_NAME = "LM Studio"
# резюме и заключения укладываются в 400-700 токенов; лимит обрывает редкое зацикливание
# локальной модели за 15-20 секунд, а не за минуту
LOCAL_MAX_TOKENS = 2048
# первый запрос после простоя загружает модель в видеопамять, это дольше обычного ответа
LOCAL_TIMEOUT = httpx.Timeout(180.0, connect=3.0)
LOCAL_PROBE_SECONDS = 15.0
# lms server start будит фоновую службу LM Studio (Local LLM Service в его настройках); если она
# выключена, команда ждёт минуту и сдаётся, поэтому повторы редкие
LOCAL_START_TIMEOUT = 90
LOCAL_START_RETRY_SECONDS = 300
# контекст локальной модели: самый длинный запрос (заключение по 60 тыс. символов расшифровки)
# около 20 тыс. токенов. При загрузке по запросу LM Studio сам забирает под контекст всю свободную
# видеопамять (Qwen3.5 9B: 233 тыс. токенов, 15,8 ГБ из 16), и Whisper тогда не помещается;
# с 32 тыс. модель занимает около 7,5 ГБ и уживается с Whisper (до 6 ГБ)
LOCAL_CONTEXT = 32768
LOCAL_DEFAULT_PARAMS: dict[str, Any] = {"temperature": 0.2, "max_tokens": LOCAL_MAX_TOKENS}
# отдельные настройки проверенных локальных моделей (ищутся по подстроке в имени); остальные берут общие
LOCAL_PROFILES: dict[str, dict[str, Any]] = {
    # Gemma 4 12B QAT Q4_0: с контекстом 24576 модель и кэш занимают около 8,3 ГБ; сэмплинг рекомендован
    # для Gemma. Рассуждения выключает только reasoning_effort: chat_template_kwargs LM Studio не передаёт.
    # Лимит 1024 из рекомендации мал: резюме по 32 отзывам игроков в него не влезло
    "gemma": {
        "context": 24576,
        "params": {"temperature": 0.3, "top_p": 0.95, "top_k": 64, "min_p": 0, "repeat_penalty": 1.0, "max_tokens": LOCAL_MAX_TOKENS},
    },
}
# по сравнению 2026-09-11 на тех же отзывах и расшифровках, что проверялись у gemini-3.8-flash
LOCAL_NOTES: dict[str, tuple[bool, str]] = {
    "gemma-4-12b-it-qat": (
        True,
        "проверена 2026-09-12: все резюме и заключения по схеме, в том числе длинная немецкая расшифровка, "
        "без иероглифов и зацикливания; с роликами осторожна, сомнительные отклоняет",
    ),
    "qwen/qwen3.5-9b": (
        False,
        "запасная: факты передаёт верно, но бывают ошибки в русских словах и склейка пунктов, "
        "на длинной немецкой расшифровке сбилась, id роликов возвращает в скобках",
    ),
    "openai/gpt-oss-20b": (
        False,
        "не рекомендуется: оставляет пустыми плюсы и минусы в заключениях, додумывает цифры",
    ),
}
DEFAULT_COOLDOWN = 60.0
# кончились кредиты (402 у kie.ai, 429 "credits are depleted" у Google): ждать минуту
# бессмысленно, ключ откладывается на час и сам вернётся в работу после пополнения баланса
BILLING_COOLDOWN = 3600.0
# у kie.ai 455 значит техработы, 501 сбой генерации: повтор может помочь
RETRY_STATUSES = frozenset({455, 500, 501, 502, 503, 504})
_FENCED = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class GeminiError(Exception):
    pass


class GeminiUnavailable(GeminiError):
    """Нет рабочего ключа (все выключены или ждут квоту) либо сервис не ответил после повторов."""


class GeminiRequestError(GeminiError):
    """Запрос отклонён (400, 404, 422): ошибка в запросе или настройках, повтор не поможет."""


class GeminiBadOutput(GeminiError):
    """Пустой ответ, блокировка фильтром или JSON не по схеме."""


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    credits: float = 0.0


@dataclass
class ApiKey:
    name: str
    value: str = field(repr=False)
    disabled: str | None = None
    cooldown_until: float = 0.0
    calls: int = 0
    credits: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    last_error: str | None = None

    def state(self) -> str:
        if self.disabled:
            return "disabled"
        if self.cooldown_until > time.monotonic():
            return "cooldown"
        return "active"

    def add(self, usage: Usage) -> None:
        self.credits += usage.credits
        self.prompt_tokens += usage.prompt_tokens
        self.output_tokens += usage.output_tokens
        self.thinking_tokens += usage.thinking_tokens


M = TypeVar("M", bound=BaseModel)


def _seconds(value: Any) -> float | None:
    if isinstance(value, str) and value.endswith("s"):
        try:
            return max(0.0, float(value[:-1]))
        except ValueError:
            return None
    return None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _error_details(response: httpx.Response) -> tuple[str, str | None, float | None]:
    """Сообщение, причина и задержка повтора. Google, kie.ai и LM Studio пишут ошибки по-разному."""
    delay = retry_after_seconds(response)
    data = _json(response)
    if not isinstance(data, dict):
        return response.text[:200], None, delay
    error = data.get("error")
    if isinstance(error, str):
        # LM Studio: {"error": "текст"}
        return error[:200], None, delay
    if not isinstance(error, dict):
        # конверт kie.ai: {"code": 402, "msg": "...", "data": null}
        return str(data.get("msg") or data)[:200], None, delay
    reason = None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        reason = reason or detail.get("reason")
        if "retryDelay" in detail:
            delay = _seconds(detail["retryDelay"])
    return str(error.get("message") or ""), reason or error.get("type"), delay


def _status(response: httpx.Response) -> int:
    """HTTP-код, а для kie.ai код из тела: он может ответить 200 и положить ошибку в {"code": ...}."""
    if not response.is_success:
        return response.status_code
    data = _json(response)
    if isinstance(data, dict) and "candidates" not in data and isinstance(data.get("code"), int):
        return data["code"] if data["code"] != 200 else response.status_code
    return response.status_code


def _usage(data: Any) -> Usage:
    """Расход по ответу kie.ai или Google."""
    if not isinstance(data, dict):
        return Usage()
    meta = data.get("usageMetadata")
    meta = meta if isinstance(meta, dict) else {}
    credits = data.get("credits_consumed")
    return Usage(
        prompt_tokens=_int(meta.get("promptTokenCount")),
        output_tokens=_int(meta.get("candidatesTokenCount")),
        thinking_tokens=_int(meta.get("thinkingTokenCount")) or _int(meta.get("thoughtsTokenCount")),
        credits=float(credits) if isinstance(credits, (int, float)) else 0.0,
    )


def _usage_local(data: Any) -> Usage:
    """Расход по ответу LM Studio: рассуждения входят в completion_tokens, их вычитаем, как у Google."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return Usage()
    details = usage.get("completion_tokens_details")
    reasoning = _int(details.get("reasoning_tokens")) if isinstance(details, dict) else 0
    return Usage(
        prompt_tokens=_int(usage.get("prompt_tokens")),
        output_tokens=max(0, _int(usage.get("completion_tokens")) - reasoning),
        thinking_tokens=reasoning,
    )


def _credits(response: httpx.Response) -> float:
    return _usage(_json(response)).credits


def _answer_json(text: str) -> str:
    """JSON из ответа модели; изредка она оборачивает его в ```json ... ```, хотя просили чистый JSON."""
    text = text.strip()
    fenced = _FENCED.fullmatch(text)
    return fenced.group(1) if fenced else text


def _validate(text: str, finish: Any, cut_off: str, output: type[M]) -> M:
    if not text.strip():
        raise GeminiBadOutput(f"empty answer, finish reason {finish}")
    try:
        return output.model_validate_json(_answer_json(text))
    except ValidationError as e:
        if finish == cut_off:
            raise GeminiBadOutput(f"answer cut off by the output token limit (finish reason {finish})") from e
        raise GeminiBadOutput(
            f"answer does not match schema: {e.error_count()} validation error(s), finish reason {finish}"
        ) from e


def _parse_output(response: httpx.Response, output: type[M]) -> M:
    """Ответ kie.ai или Google."""
    data = _json(response)
    if data is None:
        raise GeminiBadOutput("response is not JSON")
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not candidates:
        feedback = (data.get("promptFeedback") if isinstance(data, dict) else None) or {}
        raise GeminiBadOutput(f"no candidates, blockReason={feedback.get('blockReason')}")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    return _validate(text, candidate.get("finishReason"), "MAX_TOKENS", output)


def _parse_local(response: httpx.Response, output: type[M]) -> M:
    """Ответ LM Studio в формате OpenAI chat completions."""
    data = _json(response)
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices[0], dict):
        raise GeminiBadOutput("no choices in the local model answer")
    choice = choices[0]
    text = (choice.get("message") or {}).get("content") or ""
    return _validate(text, choice.get("finish_reason"), "length", output)


def _json_schema(node: Any) -> Any:
    """Схема Gemini (типы OBJECT, STRING) в обычную JSON Schema для OpenAI-совместимого сервера.

    Обязательные строковые поля получают minLength 1: локальные модели (Gemma 4) иначе оставляют их
    пустыми (verdict: ""), а грамматика LM Studio с minLength пустую строку уже не пропустит.
    """
    if isinstance(node, list):
        return [_json_schema(value) for value in node]
    if not isinstance(node, dict):
        return node
    out = {
        key: (value.lower() if key == "type" and isinstance(value, str) else _json_schema(value))
        for key, value in node.items()
        if key != "propertyOrdering"
    }
    properties = out.get("properties")
    if isinstance(properties, dict):
        for name in out.get("required") or []:
            prop = properties.get(name)
            if isinstance(prop, dict) and prop.get("type") == "string":
                prop.setdefault("minLength", 1)
    return out


def _local_profile(model: str) -> tuple[int, dict[str, Any]]:
    """Контекст и параметры запроса для локальной модели."""
    name = model.lower()
    for marker, profile in LOCAL_PROFILES.items():
        if marker in name:
            return profile["context"], profile["params"]
    return LOCAL_CONTEXT, LOCAL_DEFAULT_PARAMS


def _local_reasoning(model: str) -> str:
    # Qwen3.5 по умолчанию рассуждает и тратит на это весь лимит ответа, у GPT-OSS рассуждения
    # не выключаются, только уменьшаются; остальные модели параметр игнорируют
    return "low" if "gpt-oss" in model.lower() else "none"


def create_gemini_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))


def _find_lms() -> str | None:
    """Консольная утилита LM Studio: LMS_PATH из .env, PATH или установка в профиле пользователя."""
    explicit = os.environ.get("LMS_PATH", "").strip()
    if explicit:
        return explicit
    found = shutil.which("lms")
    if found:
        return found
    default = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    return str(default) if default.is_file() else None


class GeminiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        keys: Sequence[tuple[str, str]],
        model: str,
        *,
        provider: str = "kie",
        other_keys: Mapping[str, Sequence[tuple[str, str]]] | None = None,
        local_url: str = DEFAULT_LOCAL_URL,
        local_model: str = DEFAULT_MODELS["local"],
        local_ttl: int = 120,
        gpu_lock: asyncio.Lock | None = None,
        lms_path: str | None = None,
        concurrency: int = 2,
        attempts: int = 4,
        base_delay: float = 2.0,
        max_wait: float = 60.0,
    ) -> None:
        """keys относятся к provider, ключи остальных облачных провайдеров в other_keys."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown LLM provider {provider!r}, expected one of {PROVIDERS}")
        pools = {name: list(values) for name, values in (other_keys or {}).items()}
        pools[provider] = list(keys)
        self._keys = {name: [ApiKey(n, v) for n, v in pools.get(name, ())] for name in REMOTE_URLS}
        self._keys["local"] = [ApiKey(LOCAL_KEY_NAME, "")]
        self._models = {name: [DEFAULT_MODELS[name]] for name in REMOTE_URLS}
        if provider in REMOTE_URLS and model not in self._models[provider]:
            self._models[provider].insert(0, model)
        self.provider = provider
        self.model = model
        self._local_url = local_url.rstrip("/")
        self._local_model = local_model
        self._local_ttl = local_ttl
        self._local_cache: tuple[float, list[str] | None] = (float("-inf"), None)
        self._local_probe: asyncio.Task[list[str] | None] | None = None
        # копии моделей, которые загрузил сам сервис: при остановке они выгружаются из LM Studio
        self._loaded_local: set[str] = set()
        self._gpu_lock = gpu_lock
        self._lms = lms_path or _find_lms()
        self._local_start_at = float("-inf")
        self._local_start_lock = asyncio.Lock()
        self._http = http
        self._semaphore = asyncio.Semaphore(concurrency)
        self._attempts = attempts
        self._base_delay = base_delay
        self._max_wait = max_wait

    @property
    def keys(self) -> list[ApiKey]:
        """Ключи текущего провайдера; у локальной модели один условный ключ для счётчиков."""
        return self._keys[self.provider]

    @property
    def title(self) -> str:
        return PROVIDER_TITLES[self.provider]

    def select(self, provider: str, model: str) -> None:
        """Провайдер и модель для следующих запросов. Список допустимых моделей проверяет API по options()."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown LLM provider {provider!r}")
        model = model.strip()
        if not model:
            raise ValueError("model is empty")
        if provider in REMOTE_URLS:
            if not self._keys[provider]:
                raise ValueError(f"no API key for provider {provider}")
            if model not in self._models[provider]:
                self._models[provider].append(model)
        self.provider, self.model = provider, model

    async def local_models(self, *, fresh: bool = False) -> list[str] | None:
        """Модели, которые LM Studio готов отдать (скачанные), или None, если сервер не отвечает."""
        checked, cached = self._local_cache
        now = time.monotonic()
        if not fresh and now - checked < LOCAL_PROBE_SECONDS:
            return cached
        try:
            response = await self._http.get(f"{self._local_url}/v1/models", timeout=3.0)
            data = response.json() if response.is_success else None
        except (httpx.HTTPError, ValueError):
            data = None
        models = None
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            models = [
                item["id"]
                for item in data["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str) and "embed" not in item["id"].lower()
            ]
        self._local_cache = (now, models)
        return models

    def _local_cached(self) -> list[str] | None:
        """Последний известный список моделей LM Studio; устаревший обновляется в фоне.

        Мониторинг спрашивает готовность на каждое событие, а отказ соединения с закрытым портом
        на Windows приходит только через ~2 с: ждать его в потоке событий нельзя.
        """
        checked, cached = self._local_cache
        stale = time.monotonic() - checked >= LOCAL_PROBE_SECONDS
        if stale and (self._local_probe is None or self._local_probe.done()):
            self._local_probe = asyncio.create_task(self.local_models(fresh=True))
        return cached

    async def start_local(self) -> bool:
        """Поднимает сервер LM Studio командой lms server start, если он не отвечает. True, если сервер отвечает.

        Зовётся, только когда модель нужна для работы: прогон, воркер летсплеев, выбор модели в интерфейсе.
        Старт сервиса и открытый мониторинг LM Studio не будят, в простое он не запускается.
        """
        async with self._local_start_lock:
            if await self.local_models(fresh=True) is not None:
                return True
            if self._lms is None or time.monotonic() - self._local_start_at < LOCAL_START_RETRY_SECONDS:
                return False
            self._local_start_at = started = time.monotonic()
            log.info("LM Studio is not running, starting it: %s server start", self._lms)
            code, output = await asyncio.to_thread(self._run_lms, "server", "start")
            if await self.local_models(fresh=True) is None:
                log.warning(
                    "LM Studio did not start (lms exit %s: %s); turn on Local LLM Service in LM Studio settings",
                    code, output[-300:] or "no output",
                )
                return False
            log.info("LM Studio server started in %.1fs", time.monotonic() - started)
            return True

    def _run_lms(self, *args: str) -> tuple[int | None, str]:
        # в потоке, а не через asyncio: цикл событий uvicorn на Windows может не поддерживать подпроцессы
        try:
            done = subprocess.run(
                [str(self._lms), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=LOCAL_START_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return None, f"no answer in {LOCAL_START_TIMEOUT}s"
        except OSError as e:
            return None, str(e)
        return done.returncode, " ".join((done.stdout + done.stderr).split())

    async def ready(self, *, fresh: bool = False, start: bool = False) -> str | None:
        """None, если модель можно звать прямо сейчас, иначе код причины для мониторинга и логов.

        fresh=True спрашивает LM Studio сразу (старт, начало прогона); без него берётся последний ответ.
        start=True ещё и поднимает закрытый LM Studio: так делают только те, кому модель нужна для работы.
        """
        if self.provider == "local":
            models = await self.local_models() if fresh else self._local_cached()
            if models is None and start and await self.start_local():
                models = await self.local_models()
            if models is None:
                return "server_unavailable"
            return None if self.model in models else "model_not_found"
        keys = self._keys[self.provider]
        if not keys:
            return "no_key"
        usable = [k for k in keys if not k.disabled]
        if not usable:
            return "keys_disabled"
        if min(k.cooldown_until for k in usable) - time.monotonic() > self._max_wait:
            return "keys_resting"
        return None

    async def options(self) -> dict[str, Any]:
        """Текущий выбор и всё, из чего можно выбрать, для интерфейса."""
        local = await self.local_models(fresh=True)
        providers = []
        for name in PROVIDERS:
            if name == "local":
                models, reason, default = local or [], None if local is not None else "server_unavailable", self._local_model
            else:
                models, reason, default = self._models[name], None if self._keys[name] else "no_key", self._models[name][0]
            providers.append(
                {
                    "id": name,
                    "title": PROVIDER_TITLES[name],
                    "available": reason is None,
                    "reason": reason,
                    "default_model": default,
                    "models": [self._model_info(name, model) for model in models],
                }
            )
        return {
            "provider": self.provider,
            "title": self.title,
            "model": self.model,
            "reason": await self.ready(fresh=True),
            "providers": providers,
        }

    def _model_info(self, provider: str, model: str) -> dict[str, Any]:
        if provider != "local":
            return {"id": model, "recommended": True, "note": None}
        recommended, note = LOCAL_NOTES.get(model, (False, None))
        return {"id": model, "recommended": recommended, "note": note}

    def build_request(
        self,
        key: ApiKey,
        *,
        system: str | Prompt,
        prompt: str,
        schema: dict[str, Any],
        safety: list[dict[str, Any]] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """Адрес, заголовки и тело запроса ровно в том виде, в каком они уходят в модель.

        Системный промпт с добавками по профилю раскрывается здесь, по провайдеру этого вызова.
        """
        provider = provider or self.provider
        model = model or self.model
        system = system_text(system, provider)
        if provider == "local":
            body: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "strict": True, "schema": _json_schema(schema)},
                },
                **_local_profile(model)[1],
                "reasoning_effort": _local_reasoning(model),
                "stream": False,
                # действует, только если LM Studio загрузил модель сам по запросу (без родного API);
                # модель, загруженную сервисом, выгружает сторож простоя из app/gpu.py
                "ttl": self._local_ttl,
            }
            return f"{self._local_url}/v1/chat/completions", {}, body
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
        }
        if safety:
            # расшифровки летсплеев бывают с матом, иначе фильтр возвращает пустой ответ
            body["safetySettings"] = safety
        if provider == "kie":
            # у kie.ai по умолчанию stream=true и ответ приходит кусками
            body["stream"] = False
            headers = {"Authorization": f"Bearer {key.value}"}
        else:
            headers = {"x-goog-api-key": key.value}
        return REMOTE_URLS[provider].format(model=model), headers, body

    async def generate(
        self,
        *,
        system: str | Prompt,
        prompt: str,
        schema: dict[str, Any],
        output: type[M],
        safety: list[dict[str, Any]] | None = None,
    ) -> M:
        async with self._semaphore:
            # провайдер и модель фиксируются на весь вызов: переключение из интерфейса
            # действует на следующие запросы и не смешивает форматы внутри одного
            provider, model = self.provider, self.model
            local = provider == "local"
            previous: ApiKey | None = None
            bad_output_seen = False
            last_error = "no attempts"
            for attempt in range(1, self._attempts + 1):
                key = await self._pick_key(provider, avoid=previous)
                previous = key
                key.calls += 1
                url, headers, body = self.build_request(
                    key, system=system, prompt=prompt, schema=schema, safety=safety, provider=provider, model=model
                )
                started = time.monotonic()
                try:
                    response = await self._post(local, url, headers, body)
                except httpx.TransportError as e:
                    if local:
                        # сервер пропал: следующая проверка готовности спросит его заново
                        self._local_cache = (float("-inf"), None)
                    last_error = self._fail(key, f"network error {type(e).__name__}")
                    await self._backoff(attempt)
                    continue

                status = _status(response)
                if status < 300:
                    # токены и кредиты списываются и за ответ, который потом не пройдёт схему
                    usage = _usage_local(_json(response)) if local else _usage(_json(response))
                    key.add(usage)
                    log.info(
                        "LLM %s %s: %.1fs, tokens in %d, out %d, thinking %d, credits %.4f",
                        key.name, model, time.monotonic() - started, usage.prompt_tokens,
                        usage.output_tokens, usage.thinking_tokens, usage.credits,
                    )
                    try:
                        return _parse_local(response, output) if local else _parse_output(response, output)
                    except GeminiBadOutput as e:
                        last_error = self._fail(key, str(e))
                        if bad_output_seen:
                            raise
                        bad_output_seen = True
                        continue

                message, reason, delay = _error_details(response)
                last_error = self._fail(key, f"HTTP {status} {reason or ''}: {message}".replace(" :", ":"))
                if status == 402 or (status == 429 and "credits are depleted" in message.lower()):
                    key.cooldown_until = time.monotonic() + BILLING_COOLDOWN
                elif status == 429:
                    key.cooldown_until = time.monotonic() + (DEFAULT_COOLDOWN if delay is None else delay)
                elif not local and (status in (401, 403) or reason == "API_KEY_INVALID"):
                    key.disabled = last_error
                    log.error("LLM key %s disabled until restart", key.name)
                elif status in RETRY_STATUSES:
                    await self._backoff(attempt)
                else:
                    raise GeminiRequestError(last_error)
            raise GeminiUnavailable(f"no answer after {self._attempts} attempts, last error: {last_error}")

    async def _post(self, local: bool, url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        if not local:
            return await self._http.post(url, json=body, headers=headers)
        if self._gpu_lock is None:
            await self._ensure_local_model(body["model"])
            return await self._http.post(url, json=body, headers=headers, timeout=LOCAL_TIMEOUT)
        async with self._gpu_lock:
            await self._ensure_local_model(body["model"])
            return await self._http.post(url, json=body, headers=headers, timeout=LOCAL_TIMEOUT)

    async def _ensure_local_model(self, model: str) -> None:
        """Держит в LM Studio ровно одну языковую модель, нужную, с контекстом LOCAL_CONTEXT.

        Лишние модели и копию с большим контекстом выгружает. Вызывается под замком видеокарты,
        поэтому загрузка не совпадает с расшифровкой Whisper. LM Studio без родного API
        (/api/v1/models) пропускаем: там остаётся загрузка по запросу.
        """
        response = await self._http.get(f"{self._local_url}/api/v1/models", timeout=5.0)
        data = _json(response) if response.is_success else None
        entries = data.get("models") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return
        limit, _ = _local_profile(model)
        loaded = False
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "llm":
                continue
            for instance in entry.get("loaded_instances") or []:
                if not isinstance(instance, dict):
                    continue
                config = instance.get("config")
                context = _int(config.get("context_length")) if isinstance(config, dict) else 0
                if entry.get("key") == model and 0 < context <= limit and not loaded:
                    # копия могла остаться от сервиса, который упал и не убрал за собой: раз она
                    # в работе, она считается своей и уходит из видеопамяти после простоя
                    self._loaded_local.add(str(instance.get("id")))
                    loaded = True
                    continue
                await self._local_admin("unload", {"instance_id": instance.get("id")})
                self._loaded_local.discard(str(instance.get("id")))
                log.info("LM Studio: unloaded %s (context %d) to leave video memory for Whisper", instance.get("id"), context)
        if not loaded:
            started = time.monotonic()
            # parallel 1: запросы и так идут по одному, а при нескольких слотах llama.cpp может делить
            # контекст между ними, и длинная расшифровка перестала бы помещаться
            # ttl здесь не передать: /api/v1/models/load отвечает 400 на незнакомый ключ
            data = await self._local_admin("load", {"model": model, "context_length": limit, "parallel": 1})
            self._loaded_local.add(str(data.get("instance_id") or model))
            log.info("LM Studio: loaded %s with context %d in %.1fs", model, limit, time.monotonic() - started)
    @property
    def local_loaded(self) -> bool:
        """Есть ли в LM Studio модель, которую загрузил сам сервис."""
        return bool(self._loaded_local)

    async def release_local(self) -> list[str]:
        """Выгружает из LM Studio модели, которые загрузил сам сервис: после простоя и при остановке.

        Возвращает выгруженные копии. Следующий запрос загрузит модель заново через _ensure_local_model.
        """
        released: list[str] = []
        for instance in list(self._loaded_local):
            try:
                await self._local_admin("unload", {"instance_id": instance})
                released.append(instance)
                log.info("LM Studio: unloaded %s", instance)
            except (GeminiError, httpx.HTTPError) as e:
                log.warning("LM Studio: cannot unload %s: %s", instance, e)
            self._loaded_local.discard(instance)
        return released

    async def _local_admin(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(f"{self._local_url}/api/v1/models/{action}", json=body, timeout=LOCAL_TIMEOUT)
        if not response.is_success:
            message, _, _ = _error_details(response)
            raise GeminiUnavailable(f"LM Studio cannot {action} {body}: HTTP {response.status_code} {message}")
        data = _json(response)
        return data if isinstance(data, dict) else {}

    async def _pick_key(self, provider: str, avoid: ApiKey | None) -> ApiKey:
        keys = self._keys[provider]
        if not keys:
            raise GeminiUnavailable(f"no API key for provider {provider}")
        while True:
            usable = [k for k in keys if not k.disabled]
            if not usable:
                details = "; ".join(f"{k.name}: {k.disabled}" for k in keys)
                raise GeminiUnavailable(f"all LLM keys are disabled ({details})")
            now = time.monotonic()
            ready = [k for k in usable if k.cooldown_until <= now]
            if ready:
                # основной ключ первый в списке; повтор после ошибки по возможности идёт на другой ключ
                return next((k for k in ready if k is not avoid), ready[0])
            wait = min(k.cooldown_until for k in usable) - now
            if wait > self._max_wait:
                reasons = "; ".join(f"{k.name}: {k.last_error}" for k in usable)
                raise GeminiUnavailable(f"all LLM keys are resting for {wait:.0f}s ({reasons})")
            log.info("all LLM keys hit the quota, waiting %.1fs", wait)
            await asyncio.sleep(wait)

    def _fail(self, key: ApiKey, error: str) -> str:
        key.last_error = error
        log.warning("LLM key %s: %s", key.name, error)
        return error

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(30.0, self._base_delay * 2 ** (attempt - 1)))
