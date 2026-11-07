"""Tokenizer and light stemmer used to match evidence to features.

Deliberately small and explicit so its behavior can be read and tested:

1. ``split_words`` splits on anything that is not a letter or digit, then
   splits identifiers at case changes (``WebhookDispatcher`` -> ``Webhook``,
   ``Dispatcher``; ``HTTPServer`` -> ``HTTP``, ``Server``). Pure digit runs are
   dropped.
2. Words are lowercased; stopwords (``STOPWORDS``) and one-letter words are dropped.
3. ``stem`` maps each word to a stem: an explicit ``IRREGULAR_STEMS`` table first,
   then at most one plural rule, at most one verb-suffix rule and a final
   silent ``e`` rule (see ``stem``). Both sides of every comparison are
   stemmed the same way, so a stem only has to be consistent, not a real word:
   ``retries``/``retry`` -> ``retry``, ``currencies``/``currency`` ->
   ``currency``, ``conversion``/``convert`` -> ``convert``,
   ``invoice``/``invoices``/``invoiced`` -> ``invoic``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

STOPWORDS: frozenset[str] = frozenset({
    # English function words
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been", "before",
    "but", "by", "can", "cannot", "do", "does", "each", "for", "from", "has", "have", "if", "in", "into",
    "is", "it", "its", "just", "may", "must", "no", "not", "of", "on", "only", "or", "our", "per", "should",
    "so", "still", "than", "that", "the", "then", "these", "this", "those", "to", "up", "via", "was", "we",
    "when", "where", "which", "while", "will", "with", "yet", "you", "your",
    # Code and repository noise
    "cls", "def", "error", "errors", "exception", "false", "init", "lib", "md", "module", "none", "py",
    "return", "self", "src", "true", "txt",
    # Tags, branch prefixes and test words that name no feature
    "bugfix", "chore", "feat", "feature", "features", "fix", "fixme", "hack", "hotfix", "test", "tests",
    "todo", "wip", "xxx",
})

# Applied before any rule; keys are lowercase words, values their stems.
IRREGULAR_STEMS: dict[str, str] = {
    "conversion": "convert",
    "conversions": "convert",
    "converter": "convert",
    "converters": "convert",
    "dispatcher": "dispatch",
    "dispatchers": "dispatch",
}

_WORD_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9]+")
# Upper-case runs before a capitalized word (HTTP|Server), capitalized or lower words, digits.
_CASE_RE: re.Pattern[str] = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_words(text: str) -> list[str]:
    """Words of ``text`` in order, split at non-alphanumerics and case changes; digit runs dropped."""
    words: list[str] = []
    for chunk in _WORD_RE.findall(text):
        words.extend(part for part in _CASE_RE.findall(chunk) if not part.isdigit())
    return words


def stem(word: str) -> str:
    """Light, deterministic stem of a lowercase ``word`` (see the module docstring)."""
    if word in IRREGULAR_STEMS:
        return IRREGULAR_STEMS[word]
    # Plurals: retries -> retry, classes -> class, invoices -> invoice.
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif word.endswith("sses"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    # Verb suffixes: dispatching -> dispatch, issued -> issu(e).
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    # Silent e, so invoice/invoiced and balance/balanced share a stem.
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


def tokens(text: str | None) -> frozenset[str]:
    """The set of stems in ``text`` (empty for None)."""
    if not text:
        return frozenset()
    result: set[str] = set()
    for word in split_words(text):
        lowered = word.lower()
        if len(lowered) < 2 or lowered in STOPWORDS:
            continue
        stemmed = stem(lowered)
        if len(stemmed) >= 2 and stemmed not in STOPWORDS:
            result.add(stemmed)
    return frozenset(result)


def tokens_of(texts: Iterable[str | None]) -> frozenset[str]:
    """The union of ``tokens`` over several texts."""
    result: set[str] = set()
    for text in texts:
        result |= tokens(text)
    return frozenset(result)


def path_tokens(path: str) -> frozenset[str]:
    """Stems naming a source file: its stem plus every directory below the top-level one.

    The top-level directory is usually the package or project name
    (``paypulse/ledger.py`` -> ``{"ledger"}``), which every module shares, so it
    would match everything.
    """
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts:
        return frozenset()
    name = parts[-1].rsplit(".", 1)[0] if "." in parts[-1] else parts[-1]
    return tokens_of([name, *parts[1:-1]])


def required_overlap(feature: frozenset[str], minimum: int = 2) -> int:
    """Shared stems a match needs: ``minimum``, or all of the feature's stems when it has fewer.

    Deliberately asymmetric: a one-word item (a symbol ``Invoice``, a test
    ``test_invoice``) cannot match a feature by a single shared word unless the
    feature itself is a single word, so ``Invoice`` never counts as evidence for
    "PDF Invoice Export".
    """
    return min(minimum, len(feature))


def text_match(item: frozenset[str], feature: frozenset[str], minimum: int = 2) -> bool:
    """True when ``item`` shares at least ``required_overlap(feature)`` stems (and at least one) with ``feature``."""
    required = required_overlap(feature, minimum)
    return required >= 1 and len(item & feature) >= required
