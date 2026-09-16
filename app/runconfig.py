"""Настройки прогона: сколько игр берём, добирать ли старые и когда пересобираем резюме.

Хранятся в таблице settings рядом с расписанием и читаются перед каждым прогоном,
поэтому изменения из интерфейса действуют со следующего запуска.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

SETTING_KEY = "run"
# списки Metacritic читаются постранично, так что потолок не в API, а в длительности прогона:
# на игру уходит около 10 секунд загрузки и два запроса к модели, 500 игр это несколько часов
MAX_GAMES = 500
MIN_GAMES = 1
# доля прироста отзывов, после которой резюме собирается заново; 0 - любой новый отзыв
CHANGES = (0.0, 0.05, 0.1, 0.2, 0.5)


class RunConfigError(ValueError):
    pass


# через сколько дней игра каталога считается устаревшей и перечитывается с Metacritic
STALE_DAYS = (1, 3, 7, 14, 30)


@dataclass(frozen=True)
class RunConfig:
    """games: сколько новинок Metacritic берёт прогон.

    stale: перепроверять игры каталога. Игра перечитывается с Metacritic, когда с её последнего
    обновления прошло stale_days дней; за прогон берётся не больше stale_games таких игр, самые давние
    первыми, сверх новинок. change: при каком приросте отзывов пересобирать резюме.
    """

    games: int = 20
    stale: bool = True
    stale_days: int = 7
    stale_games: int = 10
    change: float = 0.1

    def validated(self) -> "RunConfig":
        if not MIN_GAMES <= self.games <= MAX_GAMES:
            raise RunConfigError(f"games must be {MIN_GAMES}..{MAX_GAMES}, got {self.games}")
        if not MIN_GAMES <= self.stale_games <= MAX_GAMES:
            raise RunConfigError(f"stale_games must be {MIN_GAMES}..{MAX_GAMES}, got {self.stale_games}")
        if int(self.stale_days) not in STALE_DAYS:
            raise RunConfigError(f"stale_days must be one of {STALE_DAYS}, got {self.stale_days}")
        change = min(CHANGES, key=lambda value: abs(value - self.change))
        if abs(change - self.change) > 1e-6:
            raise RunConfigError(f"change must be one of {CHANGES}, got {self.change}")
        return RunConfig(
            games=int(self.games), stale=bool(self.stale), stale_days=int(self.stale_days),
            stale_games=int(self.stale_games), change=change,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load(raw: str | None) -> RunConfig:
    """Настройки из таблицы settings; битая или устаревшая запись даёт значения по умолчанию."""
    if not raw:
        return RunConfig()
    try:
        data = json.loads(raw)
        return RunConfig(**{k: v for k, v in data.items() if k in RunConfig.__dataclass_fields__}).validated()
    except (ValueError, TypeError):
        return RunConfig()


def dump(config: RunConfig) -> str:
    return json.dumps(config.as_dict())


LETSPLAY_KEY = "letsplay"
# длительность ролика в минутах: нижняя граница отсекает трейлеры и нарезки, верхняя стримы на всю ночь
MIN_LETSPLAY_MINUTES = 1
MAX_LETSPLAY_MINUTES = 24 * 60


# через сколько дней повторять поиск для игр без летсплея: варианты из интерфейса
RECHECK_DAYS = (1, 7, 14, 30, 90)
MAX_RECHECKS = 100


@dataclass(frozen=True)
class LetsplayConfig:
    """Какие ролики берём в летсплеи: от min_minutes до max_minutes; max_minutes 0 значит без верхней границы.

    recheck: искать ли повторно игры, у которых летсплей не нашёлся; recheck_days: через сколько дней;
    recheck_max: не больше стольких повторных поисков на игру, 0 без ограничения. Сбои (YouTube, модель,
    Whisper) сюда не входят: они повторяются сами, каждые полчаса и с каждым прогоном, до 3 раз в сутки.
    """

    min_minutes: int = 10
    max_minutes: int = 120
    recheck: bool = True
    recheck_days: int = 30
    recheck_max: int = 3

    def validated(self) -> "LetsplayConfig":
        low, high = int(self.min_minutes), int(self.max_minutes)
        if not MIN_LETSPLAY_MINUTES <= low <= MAX_LETSPLAY_MINUTES:
            raise RunConfigError(f"min_minutes must be {MIN_LETSPLAY_MINUTES}..{MAX_LETSPLAY_MINUTES}, got {low}")
        if high < 0 or high > MAX_LETSPLAY_MINUTES:
            raise RunConfigError(f"max_minutes must be 0..{MAX_LETSPLAY_MINUTES}, got {high}")
        if high and high < low:
            raise RunConfigError(f"max_minutes {high} is below min_minutes {low}")
        days, limit = int(self.recheck_days), int(self.recheck_max)
        if days not in RECHECK_DAYS:
            raise RunConfigError(f"recheck_days must be one of {RECHECK_DAYS}, got {days}")
        if not 0 <= limit <= MAX_RECHECKS:
            raise RunConfigError(f"recheck_max must be 0..{MAX_RECHECKS}, got {limit}")
        return LetsplayConfig(
            min_minutes=low, max_minutes=high, recheck=bool(self.recheck), recheck_days=days, recheck_max=limit
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_letsplay(raw: str | None) -> LetsplayConfig:
    if not raw:
        return LetsplayConfig()
    try:
        data = json.loads(raw)
        fields = LetsplayConfig.__dataclass_fields__
        return LetsplayConfig(**{k: v for k, v in data.items() if k in fields}).validated()
    except (ValueError, TypeError):
        return LetsplayConfig()
