"""Сверка цитат из ответа модели с исходным текстом: расшифровкой ролика или отзывами.

Модель называет место (таймкод или номер отзыва) и цитату. Место берётся не со слов модели,
а там, где цитата действительно нашлась: так ссылки ведут туда, где это сказано.
"""

import re

# цитата короче этого для поиска бесполезна
MIN_QUOTE_CHARS = 8
# доля слов цитаты, которая должна найтись в тексте, если точного совпадения нет
QUOTE_MATCH_RATIO = 0.7

_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)
# апостроф внутри слова убираем, а не меняем на пробел: "isn't", "isn’t" и "isnt" (модель или Whisper
# без апострофа) сравниваются одинаково. Кавычки и тире и так уходят в пробел с обеих сторон
_APOSTROPHE = re.compile(r"(?<=\w)['’ʼ`´](?=\w)")


def normalize(text: str) -> str:
    """Нижний регистр, без апострофов внутри слов, знаков препинания и лишних пробелов: так цитата и текст сравнимы."""
    return _NOT_WORD.sub(" ", _APOSTROPHE.sub("", text.lower())).strip()


def usable(quote: str) -> bool:
    return len(normalize(quote)) >= MIN_QUOTE_CHARS


# модель иногда склеивает цитату из разных мест через многоточие: "enemy variety... can't quite compete"
_ELLIPSIS = re.compile(r"\s*(?:\.{3}|…)\s*")


def has_ellipsis(quote: str) -> bool:
    return bool(_ELLIPSIS.search(quote))


def fragments(quote: str) -> list[str]:
    """Куски цитаты между многоточиями, длинные первыми; слишком короткие для поиска бесполезны."""
    return sorted((piece.strip() for piece in _ELLIPSIS.split(quote) if usable(piece)), key=len, reverse=True)


def literal_fragment(quote: str, text: str) -> str | None:
    """Самый длинный кусок цитаты между многоточиями, который дословно есть в тексте, или None."""
    haystack = normalize(text)
    return next((piece for piece in fragments(quote) if normalize(piece) in haystack), None)


def word_share(quote: str, text: str) -> float:
    """Какая доля слов цитаты есть в тексте: модель и распознавание речи расходятся в паре слов."""
    words = set(normalize(quote).split())
    return len(words & set(normalize(text).split())) / len(words) if words else 0.0


# сколько слов цитаты должны идти подряд, чтобы совпадение считалось настоящим: на проверке 12 настоящих
# цитат прошли и при трёх, и при четырёх словах, а выдумка из частых слов отсеивается только при четырёх
PHRASE_WORDS = 4
# служебные слова: кусок вида "this is a" совпадает где угодно и ничего не подтверждает
_STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her his i if in is it its"
    " just like me my not of on or our out she so that the their them then there they this to too up was we were"
    " what when who will with would you your".split()
)


def shares_phrase(quote: str, text: str, size: int = PHRASE_WORDS) -> bool:
    """Есть ли в тексте подряд идущий кусок цитаты, в котором есть хотя бы одно значимое слово.

    Одних общих слов мало: из частых слов ("the", "is", "game") складывается любая выдуманная фраза,
    и она набирает нужную долю совпадения в случайном месте.
    """
    needle = normalize(quote)
    haystack = normalize(text)
    words = needle.split()
    if len(words) <= size:
        return bool(needle) and needle in haystack
    return any(
        not set(window) <= _STOPWORDS and " ".join(window) in haystack
        for window in (words[i : i + size] for i in range(len(words) - size + 1))
    )


def contains(quote: str, text: str) -> bool:
    """Цитата есть в тексте дословно или почти дословно."""
    needle = normalize(quote)
    if not needle:
        return False
    return needle in normalize(text) or (word_share(quote, text) >= QUOTE_MATCH_RATIO and shares_phrase(quote, text))
