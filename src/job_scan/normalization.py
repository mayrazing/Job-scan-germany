from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_WHITESPACE = re.compile(r"\s+")
_TRACKING_QUERY_KEYS = {"gclid", "fbclid", "ref", "referrer", "source"}


def _normalized_html_text(value: str) -> str:
    """Extract visible HTML text and apply deterministic text normalization."""
    extracted = BeautifulSoup(value, "html.parser").get_text(" ")
    normalized = unicodedata.normalize("NFKC", extracted).lower()
    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_text(value: str, boilerplate: Sequence[str] = ()) -> str:
    """Normalize text and remove caller-supplied normalized boilerplate blocks."""
    normalized = _normalized_html_text(value)
    for block in boilerplate:
        normalized_block = _normalized_html_text(block)
        if normalized_block:
            normalized = normalized.replace(normalized_block, " ")
    return _WHITESPACE.sub(" ", normalized).strip()


def character_ngram_jaccard(left: str, right: str, n: int = 5) -> float:
    """Return Jaccard similarity over normalized character n-gram sets."""
    if n <= 0:
        raise ValueError("n must be greater than zero")

    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left and not normalized_right:
        return 1.0
    if not normalized_left or not normalized_right:
        return 0.0

    def ngrams(value: str) -> set[str]:
        if len(value) < n:
            return {value}
        return {value[index : index + n] for index in range(len(value) - n + 1)}

    left_ngrams = ngrams(normalized_left)
    right_ngrams = ngrams(normalized_right)
    return len(left_ngrams & right_ngrams) / len(left_ngrams | right_ngrams)


def normalize_job_url(url: str) -> str:
    """Remove known tracking query parameters and canonicalize URL ordering."""
    parts = urlsplit(url)
    hostname = parts.hostname
    if hostname is None:
        normalized_netloc = parts.netloc.lower()
    else:
        normalized_host = hostname.lower()
        if ":" in normalized_host:
            normalized_host = f"[{normalized_host}]"
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        port = f":{parts.port}" if parts.port is not None else ""
        normalized_netloc = f"{userinfo}{normalized_host}{port}"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_query_key(key)
    ]
    query_pairs.sort()
    return urlunsplit(
        (parts.scheme.lower(), normalized_netloc, parts.path, urlencode(query_pairs), "")
    )


def _is_tracking_query_key(key: str) -> bool:
    normalized_key = key.lower()
    return normalized_key.startswith("utm_") or normalized_key in _TRACKING_QUERY_KEYS


def content_hash(company: str, title: str, location: str, description: str) -> str:
    """Hash normalized job content with an unambiguous canonical encoding."""
    payload = json.dumps(
        [
            normalize_text(company),
            normalize_text(title),
            normalize_text(location),
            normalize_text(description),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
