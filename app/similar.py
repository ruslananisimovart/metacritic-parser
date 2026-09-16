"""Похожие игры по данным из базы: жанры, описание, название, разработчик, платформы."""

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

WEIGHTS = {"genres": 0.4, "description": 0.3, "title": 0.1, "developers": 0.1, "platforms": 0.1}
MIN_SCORE = 0.15

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be but by can for from has have in into is it its of on or that the their them they "
    "this to up was were will with you your".split()
)


@dataclass(frozen=True)
class GameFeatures:
    id: int
    title: str
    genres: frozenset[str]
    developers: frozenset[str]
    platforms: frozenset[str]
    description: str | None


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in _STOPWORDS]


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class SimilarityIndex:
    def __init__(self, games: Iterable[GameFeatures]) -> None:
        self._games = {game.id: game for game in games}
        self._titles = {game.id: frozenset(_words(game.title)) for game in self._games.values()}
        self._vectors = self._tfidf_vectors()

    def _tfidf_vectors(self) -> dict[int, dict[str, float]]:
        docs = {
            game.id: counts
            for game in self._games.values()
            if game.description and (counts := Counter(_words(game.description)))
        }
        doc_freq = Counter(word for counts in docs.values() for word in counts)
        total = len(docs)
        vectors: dict[int, dict[str, float]] = {}
        for game_id, counts in docs.items():
            weights = {
                word: (1 + math.log(count)) * (math.log((1 + total) / (1 + doc_freq[word])) + 1)
                for word, count in counts.items()
            }
            norm = math.sqrt(sum(w * w for w in weights.values()))
            vectors[game_id] = {word: w / norm for word, w in weights.items()}
        return vectors

    def _cosine(self, a: int, b: int) -> float:
        va, vb = self._vectors.get(a), self._vectors.get(b)
        if not va or not vb:
            return 0.0
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(weight * vb.get(word, 0.0) for word, weight in va.items())

    def score(self, a: GameFeatures, b: GameFeatures) -> float:
        return (
            WEIGHTS["genres"] * _jaccard(a.genres, b.genres)
            + WEIGHTS["description"] * self._cosine(a.id, b.id)
            + WEIGHTS["title"] * _jaccard(self._titles[a.id], self._titles[b.id])
            + WEIGHTS["developers"] * _jaccard(a.developers, b.developers)
            + WEIGHTS["platforms"] * _jaccard(a.platforms, b.platforms)
        )

    def similar(self, game_id: int, limit: int) -> list[tuple[int, float]]:
        """До limit самых похожих игр (id, оценка от 0 до 1), слабее MIN_SCORE не показываем."""
        game = self._games.get(game_id)
        if game is None:
            return []
        scored = [
            (other.id, score)
            for other in self._games.values()
            if other.id != game_id and (score := self.score(game, other)) >= MIN_SCORE
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]
