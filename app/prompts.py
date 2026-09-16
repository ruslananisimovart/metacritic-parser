"""Системные промпты моделей: общее ядро правил и добавки по профилю модели.

Облачные модели (Gemini через kie.ai или Gemini API) и локальные (LM Studio) получают одно ядро:
от него зависят формат ответа и сверка сервисом. В добавку правило уходит, когда помогает одному
классу моделей и мешает другому: тонкие требования к качеству облачная модель выполняет, а небольшую
локальную они сбивают. Вариант выбирает GeminiClient.build_request по провайдеру вызова, поэтому смена
модели из интерфейса посреди запроса не смешает вариант промпта и провайдера.
"""

from dataclasses import dataclass

CLOUD, LOCAL = "cloud", "local"


def profile(provider: str) -> str:
    return LOCAL if provider == "local" else CLOUD


@dataclass(frozen=True)
class Prompt:
    """head: роль и задача; rules: правила для всех моделей; cloud и local: добавки профиля в конец списка."""

    head: str
    rules: tuple[str, ...]
    cloud: tuple[str, ...] = ()
    local: tuple[str, ...] = ()

    def text(self, provider: str) -> str:
        extra = self.local if profile(provider) == LOCAL else self.cloud
        return self._render((*self.rules, *extra))

    def common(self) -> str:
        """Общая часть без добавок профиля: для выгрузки, чтобы не показывать один текст дважды."""
        return self._render(self.rules)

    def _render(self, rules: tuple[str, ...]) -> str:
        return f"{self.head}\nПравила:\n" + ";\n".join(f"- {rule}" for rule in rules) + "."

    @property
    def split(self) -> bool:
        """Отличаются ли варианты для облачных и локальных моделей."""
        return self.cloud != self.local


def system_text(system: "str | Prompt", provider: str) -> str:
    return system.text(provider) if isinstance(system, Prompt) else system
