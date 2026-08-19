from __future__ import annotations

import pytest

from job_scan.normalization import (
    character_ngram_jaccard,
    content_hash,
    normalize_job_url,
    normalize_text,
)


def test_normalize_job_url_removes_tracking_but_keeps_job_id() -> None:
    value = normalize_job_url(
        "https://jobs.example/role/42?utm_source=linkedin&jobId=42#apply"
    )

    assert value == "https://jobs.example/role/42?jobId=42"


def test_normalize_job_url_sorts_remaining_query_pairs_stably() -> None:
    value = normalize_job_url(
        "HTTPS://JOBS.EXAMPLE/role?req=9&source=feed&job=2&job=1&fbclid=x"
    )

    assert value == "https://jobs.example/role?job=1&job=2&req=9"


def test_jaccard_uses_nfkc_lowercase_and_collapsed_space() -> None:
    assert character_ngram_jaccard("ＳＥＮＩＯＲ  Engineer", "senior engineer") == 1.0


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("", "", 1.0), ("", "engineer", 0.0), ("engineer", "", 0.0)],
)
def test_jaccard_has_explicit_empty_string_semantics(
    left: str, right: str, expected: float
) -> None:
    assert character_ngram_jaccard(left, right) == expected


def test_normalize_text_extracts_html_then_removes_normalized_boilerplate() -> None:
    value = normalize_text(
        "<article><h1>ＳＥＮＩＯＲ&nbsp; Engineer</h1><p>Apply   NOW</p></article>",
        boilerplate=("  APPLY now ",),
    )

    assert value == "senior engineer"


def test_content_hash_is_stable_over_equivalent_normalized_content() -> None:
    expected = "sha256:3e0bb7b9b80f6542dd06604e76e5e804919fea1fe44449675f18d0e417f5342c"

    assert content_hash("ＡＣＭＥ", "Senior  Engineer", "Berlin", "<p>Build APIs</p>") == expected
    assert content_hash("acme", "senior engineer", "berlin", "build apis") == expected
