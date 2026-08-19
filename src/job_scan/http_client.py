from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from job_scan.normalization import normalize_job_url

JOBSUCHE_API_KEY = "jobboerse-jobsuche"
_ALLOWED_HEADERS = {
    "accept": "Accept",
    "content-type": "Content-Type",
    "if-none-match": "If-None-Match",
    "if-modified-since": "If-Modified-Since",
}
_BLOCKED_STATUS_CODES = {401, 403, 407}
_RETRY_STATUS_CODES = {429, 502, 503, 504}
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_MAX_RETRIES = 2
_MAX_RETRY_DELAY_SECONDS = 10.0
_CACHE_METADATA_ALLOWANCE = 4_096

_Origin: TypeAlias = tuple[str, str | None, int | None]
_OriginPolicy: TypeAlias = frozenset[_Origin] | None


class PublicHttpError(RuntimeError):
    """Base error for client-enforced public HTTP contracts."""


class InvalidPublicUrl(PublicHttpError):
    """Raised when a URL is not an unauthenticated HTTP(S) URL."""


class ResponseTooLarge(PublicHttpError):
    """Raised before a response can exceed the configured byte limit."""


class BlockedResponse(PublicHttpError):
    """Raised when authentication, proxy blocking, or CAPTCHA stops a request."""


class InvalidResponse(PublicHttpError):
    """Raised when a response violates the method's response contract."""


class CacheError(PublicHttpError):
    """Raised when bounded conditional cache data cannot be read or written safely."""


@dataclass(frozen=True)
class _CacheEntry:
    body: bytes
    encoding: str
    etag: str | None
    last_modified: str | None


class PublicHttpClient:
    """Fetch public HTTP resources with bounded transport, retries, and disk caching."""

    def __init__(
        self,
        cache_dir: Path,
        timeout_seconds: float = 20,
        max_response_bytes: int = 5_000_000,
        min_interval_seconds: float = 0.5,
        user_agent: str = "job-scan-germany/0.1 (+local personal job search)",
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be greater than zero")
        if not math.isfinite(min_interval_seconds) or min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be finite and non-negative")
        if not user_agent.strip():
            raise ValueError("user_agent must not be empty")

        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._min_interval_seconds = min_interval_seconds
        self._user_agent = user_agent
        self._last_request_at: float | None = None
        self._rate_lock = threading.Lock()
        self._sleep: Callable[[float], None] = time.sleep
        self._monotonic: Callable[[], float] = time.monotonic
        self._utcnow: Callable[[], datetime] = lambda: datetime.now(UTC)

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a bounded GET response parsed as a JSON object."""
        payload = self._request("GET", url, params=params, headers=headers, json_body=None)
        return self._parse_json_object(payload.body)

    def get_json_same_origin(
        self,
        url: str,
        *,
        allowed_origin: str,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return JSON while requiring every redirect to remain on one origin."""
        payload = self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            json_body=None,
            allowed_origins=self._validated_origins(allowed_origin),
        )
        return self._parse_json_object(payload.body)

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Return a bounded GET response decoded as text."""
        payload = self._request("GET", url, params=params, headers=headers, json_body=None)
        try:
            return payload.body.decode(payload.encoding, errors="replace")
        except LookupError:
            return payload.body.decode("utf-8", errors="replace")

    def get_text_same_origin(
        self,
        url: str,
        *,
        allowed_origin: str,
        allowed_redirect_origins: tuple[str, ...] = (),
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Return text while requiring redirects to stay on approved origins."""
        payload = self._request(
            "GET",
            url,
            params=params,
            headers=headers,
            json_body=None,
            allowed_origins=self._validated_origins(
                allowed_origin,
                allowed_redirect_origins,
            ),
        )
        try:
            return payload.body.decode(payload.encoding, errors="replace")
        except LookupError:
            return payload.body.decode("utf-8", errors="replace")

    def post_json(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST canonical JSON and return a bounded JSON object response."""
        payload = self._request("POST", url, params=None, headers=headers, json_body=body)
        return self._parse_json_object(payload.body)

    def post_json_same_origin(
        self,
        url: str,
        body: Mapping[str, Any],
        *,
        allowed_origin: str,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST JSON while requiring every redirect to remain on one origin."""
        payload = self._request(
            "POST",
            url,
            params=None,
            headers=headers,
            json_body=body,
            allowed_origins=self._validated_origins(allowed_origin),
        )
        return self._parse_json_object(payload.body)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None,
        headers: Mapping[str, str] | None,
        json_body: Mapping[str, Any] | None,
        allowed_origins: _OriginPolicy = None,
    ) -> _CacheEntry:
        request_url = self._build_request_url(url, params)
        self._require_allowed_origin(request_url, allowed_origins)
        body = self._canonical_json(json_body) if json_body is not None else None
        request_headers = self._filter_headers(
            headers,
            allow_authorization=allowed_origins is not None,
        )
        cache_key = self._cache_key(method, request_url, body, allowed_origins)
        cache_allowed = "Authorization" not in request_headers
        cached = self._read_cache(cache_key) if cache_allowed else None
        if method == "POST" and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        if cached is not None:
            if cached.etag is not None:
                request_headers.setdefault("If-None-Match", cached.etag)
            if cached.last_modified is not None:
                request_headers.setdefault("If-Modified-Since", cached.last_modified)

        response = self._request_with_retries(
            method, request_url, request_headers, body, allowed_origins
        )
        if response.status_code == 304:
            if cached is None:
                raise InvalidResponse("received 304 without a cached response")
            return cached

        response.raise_for_status()
        encoding = response.encoding or "utf-8"
        entry = _CacheEntry(
            body=response.content,
            encoding=encoding,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
        if cache_allowed:
            self._write_cache(cache_key, entry)
        return entry

    def _request_with_retries(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        allowed_origins: _OriginPolicy,
    ) -> httpx.Response:
        for attempt in range(_MAX_RETRIES + 1):
            response = self._request_following_redirects(
                method, url, headers, body, allowed_origins
            )
            encoding = response.encoding or "utf-8"
            if self._is_captcha(response.content, encoding):
                raise BlockedResponse(f"CAPTCHA response blocked for {response.url}")
            if response.status_code in _BLOCKED_STATUS_CODES:
                raise BlockedResponse(
                    f"HTTP {response.status_code} blocked public request to {response.url}"
                )
            if response.status_code not in _RETRY_STATUS_CODES:
                return response
            if attempt == _MAX_RETRIES:
                return response
            self._sleep(self._retry_delay(response.headers.get("Retry-After"), attempt))
        raise AssertionError("retry loop exhausted unexpectedly")

    def _request_following_redirects(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        allowed_origins: _OriginPolicy,
    ) -> httpx.Response:
        current_method = method
        current_url = url
        current_headers = dict(headers)
        current_body = body

        with httpx.Client(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": self._user_agent},
            trust_env=False,
        ) as client:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                self._validate_url(current_url)
                self._require_allowed_origin(current_url, allowed_origins)
                client.cookies.clear()
                self._wait_for_rate_limit()
                with client.stream(
                    current_method,
                    current_url,
                    headers=current_headers,
                    content=current_body,
                ) as streamed:
                    client.cookies.clear()
                    location = streamed.headers.get("Location")
                    if streamed.status_code in _REDIRECT_STATUS_CODES and location is not None:
                        if redirect_count == _MAX_REDIRECTS:
                            raise httpx.TooManyRedirects(
                                "Exceeded maximum of five redirects", request=streamed.request
                            )
                        next_url = urljoin(str(streamed.url), location)
                        self._validate_url(next_url)
                        self._require_allowed_origin(next_url, allowed_origins)
                        if self._origin(current_url) != self._origin(next_url):
                            current_headers.pop("Authorization", None)
                            current_headers.pop("X-API-Key", None)
                        if streamed.status_code == 303 or (
                            streamed.status_code in {301, 302} and current_method == "POST"
                        ):
                            current_method = "GET"
                            current_body = None
                            current_headers.pop("Content-Type", None)
                        current_url = next_url
                        continue

                    content = self._read_bounded(streamed)
                    decoded_headers = streamed.headers.copy()
                    decoded_headers.pop("Content-Encoding", None)
                    decoded_headers.pop("Content-Length", None)
                    decoded_headers.pop("Transfer-Encoding", None)
                    return httpx.Response(
                        streamed.status_code,
                        headers=decoded_headers,
                        content=content,
                        request=streamed.request,
                        extensions=streamed.extensions,
                    )
        raise AssertionError("redirect loop exhausted unexpectedly")

    def _read_bounded(self, response: httpx.Response) -> bytes:
        content_length = (
            None
            if response.headers.get("Content-Encoding")
            else response.headers.get("Content-Length")
        )
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > self._max_response_bytes:
                raise ResponseTooLarge(
                    f"response exceeds configured limit of {self._max_response_bytes} bytes"
                )

        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._max_response_bytes:
                raise ResponseTooLarge(
                    f"response exceeds configured limit of {self._max_response_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = self._monotonic()
            if self._last_request_at is not None:
                remaining = self._min_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at = self._monotonic()

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        delay = float(2**attempt)
        if retry_after is not None:
            try:
                delay = float(retry_after.strip())
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    delay = (retry_at - self._utcnow()).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    pass
        if not math.isfinite(delay):
            delay = _MAX_RETRY_DELAY_SECONDS
        return max(0.0, min(delay, _MAX_RETRY_DELAY_SECONDS))

    def _build_request_url(
        self, url: str, params: Mapping[str, str | int] | None
    ) -> str:
        self._validate_url(url)
        try:
            request_url = httpx.URL(url)
            if params is not None:
                request_url = request_url.copy_merge_params(params)
        except (TypeError, ValueError) as error:
            raise InvalidPublicUrl(f"invalid public URL: {url!r}") from error
        normalized = normalize_job_url(str(request_url))
        self._validate_url(normalized)
        return normalized

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as error:
            raise InvalidPublicUrl(f"invalid public URL: {url!r}") from error
        if parts.scheme.lower() not in {"http", "https"}:
            raise InvalidPublicUrl(f"URL scheme must be http or https: {url!r}")
        if parts.hostname is None:
            raise InvalidPublicUrl(f"URL must include a host: {url!r}")
        if parts.username is not None or parts.password is not None:
            raise InvalidPublicUrl(f"URL userinfo is forbidden: {url!r}")
        if port is not None and not 1 <= port <= 65_535:
            raise InvalidPublicUrl(f"URL port is out of range: {url!r}")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            return
        if (
            not address.is_global
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise InvalidPublicUrl(f"URL host must be a public address: {url!r}")

    @classmethod
    def _validated_origin(cls, url: str) -> tuple[str, str | None, int | None]:
        cls._validate_url(url)
        return cls._origin(url)

    @classmethod
    def _validated_origins(
        cls,
        allowed_origin: str,
        allowed_redirect_origins: tuple[str, ...] = (),
    ) -> frozenset[_Origin]:
        return frozenset(
            cls._validated_origin(origin)
            for origin in (allowed_origin, *allowed_redirect_origins)
        )

    @classmethod
    def _require_allowed_origin(
        cls,
        url: str,
        allowed_origins: _OriginPolicy,
    ) -> None:
        if allowed_origins is not None and cls._origin(url) not in allowed_origins:
            raise InvalidPublicUrl(f"URL must stay on configured origin: {url!r}")

    @staticmethod
    def _origin(url: str) -> tuple[str, str | None, int | None]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        port = parts.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return scheme, parts.hostname, port

    @staticmethod
    def _filter_headers(
        headers: Mapping[str, str] | None,
        *,
        allow_authorization: bool = False,
    ) -> dict[str, str]:
        filtered: dict[str, str] = {}
        for name, value in (headers or {}).items():
            normalized_name = name.lower()
            if normalized_name == "authorization":
                if allow_authorization:
                    filtered["Authorization"] = value
                continue
            if normalized_name == "x-api-key":
                if value == JOBSUCHE_API_KEY:
                    filtered["X-API-Key"] = value
                continue
            canonical_name = _ALLOWED_HEADERS.get(normalized_name)
            if canonical_name is not None:
                filtered[canonical_name] = value
        return filtered

    @staticmethod
    def _canonical_json(body: Mapping[str, Any]) -> bytes:
        try:
            return json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise InvalidResponse("request body must contain JSON-compatible values") from error

    @staticmethod
    def _cache_key(
        method: str,
        url: str,
        body: bytes | None,
        allowed_origins: _OriginPolicy = None,
    ) -> str:
        payload = json.dumps(
            {
                "body": body.decode("utf-8") if body is not None else None,
                "method": method,
                "origin_policy": (
                    sorted(allowed_origins) if allowed_origins is not None else None
                ),
                "url": url,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _read_cache(self, cache_key: str) -> _CacheEntry | None:
        path = self._cache_dir / f"{cache_key}.json"
        if not path.exists():
            return None
        encoded_limit = ((self._max_response_bytes + 2) // 3) * 4
        file_limit = encoded_limit + _CACHE_METADATA_ALLOWANCE
        try:
            with path.open("rb") as cache_file:
                raw = cache_file.read(file_limit + 1)
            if len(raw) > file_limit:
                raise CacheError(f"cache entry exceeds bounded size: {path}")
            payload = json.loads(raw)
            encoded_body = payload["body"]
            if not isinstance(encoded_body, str):
                raise TypeError("cache body is not text")
            body = base64.b64decode(encoded_body, validate=True)
            if len(body) > self._max_response_bytes:
                raise CacheError(f"cached response exceeds configured limit: {path}")
            encoding = payload.get("encoding", "utf-8")
            etag = payload.get("etag")
            last_modified = payload.get("last_modified")
            if not isinstance(encoding, str):
                raise TypeError("cache encoding is not text")
            if etag is not None and not isinstance(etag, str):
                raise TypeError("cache ETag is not text")
            if last_modified is not None and not isinstance(last_modified, str):
                raise TypeError("cache Last-Modified is not text")
            return _CacheEntry(body, encoding, etag, last_modified)
        except CacheError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise CacheError(f"invalid cache entry: {path}") from error

    def _write_cache(self, cache_key: str, entry: _CacheEntry) -> None:
        payload = json.dumps(
            {
                "body": base64.b64encode(entry.body).decode("ascii"),
                "encoding": entry.encoding,
                "etag": entry.etag,
                "last_modified": entry.last_modified,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded_limit = ((self._max_response_bytes + 2) // 3) * 4
        if len(payload) > encoded_limit + _CACHE_METADATA_ALLOWANCE:
            raise CacheError("serialized cache entry exceeds bounded size")

        path = self._cache_dir / f"{cache_key}.json"
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._cache_dir, prefix=f".{cache_key}.", suffix=".tmp"
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as cache_file:
                cache_file.write(payload)
                cache_file.flush()
                os.fsync(cache_file.fileno())
            os.replace(temporary_path, path)
        except OSError as error:
            raise CacheError(f"could not write cache entry: {path}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _is_captcha(body: bytes, encoding: str) -> bool:
        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
        if "captcha" in title:
            return True
        return soup.select_one(
            "form[id*='captcha' i], form[action*='captcha' i], "
            "input[name*='captcha' i], .g-recaptcha, .h-captcha"
        ) is not None

    @staticmethod
    def _parse_json_object(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidResponse("response is not valid JSON") from error
        if not isinstance(value, dict):
            raise InvalidResponse("response must be a JSON object")
        return value
