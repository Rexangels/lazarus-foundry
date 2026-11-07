"""Tests for apps.genome.tokens: splitting, stemming and the match rule."""
from __future__ import annotations

import pytest

from apps.genome.tokens import path_tokens, required_overlap, split_words, stem, text_match, tokens


def test_split_words_handles_case_changes_snake_case_and_digits() -> None:
    assert split_words("WebhookDispatcher.post_to_ledger") == ["Webhook", "Dispatcher", "post", "to", "ledger"]
    assert split_words("HTTPServer sha256") == ["HTTP", "Server", "sha"]
    assert split_words("feat/webhook-retry-engine") == ["feat", "webhook", "retry", "engine"]


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (("retry", "retries", "retrying"), "retry"),
        (("currency", "currencies"), "currency"),
        (("convert", "conversion", "converted"), "convert"),
        (("invoice", "invoices", "invoiced", "invoicing"), "invoic"),
        (("dispatch", "dispatcher", "dispatching"), "dispatch"),
        (("balance", "balanced", "balances"), "balanc"),
        (("post", "posting", "postings"), "post"),
        (("webhook", "webhooks"), "webhook"),
    ],
)
def test_stem_maps_word_families_to_one_stem(words: tuple[str, ...], expected: str) -> None:
    assert {stem(word) for word in words} == {expected}


def test_stem_keeps_short_words_and_ss_us_is_endings() -> None:
    assert [stem(word) for word in ("rate", "rates", "status", "class", "analysis", "pdf")] == [
        "rate", "rate", "status", "class", "analysis", "pdf",
    ]


def test_tokens_drop_stopwords_tags_and_one_letter_words() -> None:
    assert tokens("TODO: wire the retry engine into a dispatcher") == {"wire", "retry", "engin", "dispatch"}
    assert tokens(None) == frozenset() and tokens("") == frozenset()


def test_path_tokens_skip_the_top_level_package_and_extension() -> None:
    assert path_tokens("paypulse/ledger.py") == {"ledger"}
    assert path_tokens("src/billing/invoices.py") == {"bill", "invoic"}
    assert path_tokens("README.md") == {"readm"}


def test_text_match_needs_two_shared_stems_unless_the_feature_is_smaller() -> None:
    feature = tokens("PDF Invoice Export")
    assert required_overlap(feature) == 2
    assert text_match(tokens("PDF invoice export"), feature)
    assert not text_match(tokens("Invoice"), feature)  # one word never matches a bigger feature
    assert text_match(tokens("ledger"), tokens("Ledger"))
    assert not text_match(tokens("anything"), frozenset())
