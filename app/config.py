import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    timezone: ZoneInfo
    db_path: Path
    metacritic_api_key: str | None
    metacritic_concurrency: int
    metacritic_min_interval: float
    gemini_model: str
    scheduler_enabled: bool
    log_dir: Path | None
    # ключи выбранного провайдера; у локальной модели ключей нет
    gemini_keys: tuple[tuple[str, str], ...] = field(repr=False)
    letsplay_enabled: bool = False
    # файл cookies YouTube (формат Netscape) для yt-dlp и субтитров: без входа YouTube отвечает
    # "Sign in to confirm you're not a bot" с адресов, которые он счёл подозрительными
    youtube_cookies: Path | None = None
    # профиль Chrome с входом в аккаунт YouTube: сервис сам экспортирует из него свежие cookies
    # (app/ytcookies.py), файл выше тогда пишется автоматически
    youtube_profile: Path | None = None
    youtube_cookies_refresh: timedelta = timedelta(hours=6)
    manual_cooldown: timedelta = timedelta(minutes=5)
    gemini_provider: str = "kie"
    # ключи всех облачных провайдеров, чтобы переключаться между ними из интерфейса
    llm_keys: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict, repr=False)
    local_llm_url: str = "http://127.0.0.1:1234"
    local_llm_model: str = "gemma-4-12b-it-qat"
    # сколько секунд простоя видеокарты держать в памяти локальную модель и Whisper; внутри работы
    # паузы между обращениями к карте до полутора минут (загрузка аудио, поиск на YouTube)
    gpu_idle_seconds: int = 120
    # CSV после каждого прогона: data/exports/latest и копии последних прогонов рядом
    export_enabled: bool = True
    export_keep: int = 10


# ключи и модель по умолчанию для каждого способа доступа к модели; у kie.ai в имени модели дефисы
_LLM_KEYS = {"kie": ("KIE_API_KEY_1", "KIE_API_KEY_2"), "google": ("GEMINI_API_KEY_1", "GEMINI_API_KEY_2")}
_LLM_MODELS = {"kie": "gemini-3-8-flash", "google": "gemini-3.8-flash", "local": "gemma-4-12b-it-qat"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _env_timezone(name: str, default: str) -> ZoneInfo:
    key = os.environ.get(name, "").strip() or default
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{name}: unknown time zone {key!r} (on Windows the tzdata package is required)") from None


def _env_path(name: str) -> Path | None:
    """Путь из настройки: относительный считается от папки проекта. Пусто, если не задан."""
    raw = os.environ.get(name, "").strip().strip('"')
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _env_keys(names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    pairs = ((name, os.environ.get(name, "").strip()) for name in names)
    return tuple((name, value) for name, value in pairs if value)


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    db_path = os.environ.get("DB_PATH", "").strip()
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower() or "kie"
    if provider not in _LLM_MODELS:
        raise ValueError(f"LLM_PROVIDER must be kie, google or local, got {provider!r}")
    llm_keys = {name: _env_keys(names) for name, names in _LLM_KEYS.items()}
    local_model = os.environ.get("LOCAL_LLM_MODEL", "").strip() or _LLM_MODELS["local"]
    model = local_model if provider == "local" else os.environ.get("GEMINI_MODEL", "").strip() or _LLM_MODELS[provider]
    profile_dir = _env_path("YOUTUBE_PROFILE")
    return Settings(
        timezone=_env_timezone("TIMEZONE", "Europe/Moscow"),
        db_path=Path(db_path) if db_path else ROOT / "data" / "games.db",
        metacritic_api_key=os.environ.get("METACRITIC_API_KEY", "").strip() or None,
        metacritic_concurrency=_env_int("METACRITIC_CONCURRENCY", 3),
        metacritic_min_interval=_env_float("METACRITIC_MIN_INTERVAL", 0.3),
        gemini_model=model,
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
        log_dir=ROOT / "logs",
        gemini_keys=llm_keys.get(provider, ()),
        letsplay_enabled=_env_bool("LETSPLAY_ENABLED", True),
        youtube_cookies=_env_path("YOUTUBE_COOKIES") or (ROOT / "data" / "www.youtube.com_cookies.txt" if profile_dir else None),
        youtube_profile=profile_dir,
        youtube_cookies_refresh=timedelta(hours=_env_float("YOUTUBE_COOKIES_REFRESH_HOURS", 6)),
        manual_cooldown=timedelta(minutes=_env_int("MANUAL_RUN_COOLDOWN_MINUTES", 5)),
        gemini_provider=provider,
        llm_keys=llm_keys,
        local_llm_url=os.environ.get("LOCAL_LLM_URL", "").strip() or "http://127.0.0.1:1234",
        local_llm_model=local_model,
        gpu_idle_seconds=_env_int("GPU_IDLE_SECONDS", 120),
        export_enabled=_env_bool("EXPORT_ENABLED", True),
        export_keep=_env_int("EXPORT_KEEP", 10),
    )
