from __future__ import annotations

import gzip
from pathlib import Path

import httpx
import pytest
import respx

from job_scan.http_client import (
    JOBSUCHE_API_KEY,
    BlockedResponse,
    InvalidPublicUrl,
    InvalidResponse,
    PublicHttpClient,
    ResponseTooLarge,
)


def make_client(cache_dir: Path, **overrides: object) -> PublicHttpClient:
    options: dict[str, object] = {
        "cache_dir": cache_dir,
        "min_interval_seconds": 0,
    }
    options.update(overrides)
    return PublicHttpClient(**options)  # type: ignore[arg-type]


@respx.mock
def test_request_uses_configured_timeout(tmp_path: Path) -> None:
    def time_out(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {
            "connect": 1.25,
            "read": 1.25,
            "write": 1.25,
            "pool": 1.25,
        }
        raise httpx.ReadTimeout("fixture timeout", request=request)

    respx.get("https://jobs.example/data").mock(side_effect=time_out)
    client = make_client(tmp_path, timeout_seconds=1.25)

    with pytest.raises(httpx.ReadTimeout, match="fixture timeout"):
        client.get_json("https://jobs.example/data")


@respx.mock
def test_streamed_response_over_limit_raises_typed_error(tmp_path: Path) -> None:
    respx.get("https://jobs.example/data").mock(return_value=httpx.Response(200, content=b"12345"))
    client = make_client(tmp_path, max_response_bytes=4)

    with pytest.raises(ResponseTooLarge, match="4 bytes"):
        client.get_text("https://jobs.example/data")


@respx.mock
def test_gzip_text_response_is_decoded_once(tmp_path: Path) -> None:
    body = "Grüße aus Berlin".encode()
    respx.get("https://jobs.example/text").mock(
        return_value=httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/plain; charset=utf-8"},
        )
    )
    client = make_client(tmp_path)

    assert client.get_text("https://jobs.example/text") == "Grüße aus Berlin"


@respx.mock
def test_gzip_json_response_is_decoded_once(tmp_path: Path) -> None:
    body = b'{"jobs":[{"id":42}]}'
    respx.get("https://jobs.example/json").mock(
        return_value=httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )
    )
    client = make_client(tmp_path)

    assert client.get_json("https://jobs.example/json") == {"jobs": [{"id": 42}]}


@respx.mock
def test_gzip_response_limit_applies_to_decoded_bytes(tmp_path: Path) -> None:
    decoded_body = b"x" * 1_024
    compressed_body = gzip.compress(decoded_body)
    assert len(compressed_body) < 100
    respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(
            200,
            content=compressed_body,
            headers={"Content-Encoding": "gzip"},
        )
    )
    client = make_client(tmp_path, max_response_bytes=100)

    with pytest.raises(ResponseTooLarge, match="100 bytes"):
        client.get_text("https://jobs.example/data")


@respx.mock
def test_429_retries_twice_and_caps_retry_after(tmp_path: Path) -> None:
    route = respx.get("https://jobs.example/data").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "999"}),
            httpx.Response(429, headers={"Retry-After": "12"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client = make_client(tmp_path)
    delays: list[float] = []
    client._sleep = delays.append

    assert client.get_json("https://jobs.example/data") == {"ok": True}
    assert delays == [10.0, 10.0]
    assert route.call_count == 3


@respx.mock
def test_cached_etag_is_conditional_and_304_returns_cached_body(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={"jobs": [1]}, headers={"ETag": '"v1"'})
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    respx.get("https://jobs.example/data").mock(side_effect=respond)
    client = make_client(tmp_path)

    assert client.get_json("https://jobs.example/data") == {"jobs": [1]}
    assert client.get_json("https://jobs.example/data") == {"jobs": [1]}


@respx.mock
def test_caller_auth_cookies_and_unapproved_api_keys_are_removed(tmp_path: Path) -> None:
    seen_headers: httpx.Headers | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = request.headers
        return httpx.Response(200, json={"ok": True})

    respx.get("https://jobs.example/data").mock(side_effect=respond)
    client = make_client(tmp_path)

    result = client.get_json(
        "https://jobs.example/data",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-API-Key": "private-key",
        },
    )

    assert result == {"ok": True}
    assert seen_headers is not None
    assert seen_headers["Accept"] == "application/json"
    assert "Authorization" not in seen_headers
    assert "Cookie" not in seen_headers
    assert "X-API-Key" not in seen_headers


@respx.mock
def test_built_in_jobsuche_api_key_is_allowed(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == JOBSUCHE_API_KEY
        return httpx.Response(200, json={"ok": True})

    respx.get("https://rest.arbeitsagentur.de/jobs").mock(side_effect=respond)
    client = make_client(tmp_path)

    assert client.get_json(
        "https://rest.arbeitsagentur.de/jobs", headers={"X-API-Key": JOBSUCHE_API_KEY}
    ) == {"ok": True}


@respx.mock
def test_response_cookies_are_not_persisted_to_next_request(tmp_path: Path) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "Cookie" not in request.headers
        headers = {"Set-Cookie": "session=secret"} if calls == 1 else {}
        return httpx.Response(200, text=f"call {calls}", headers=headers)

    respx.get("https://jobs.example/data").mock(side_effect=respond)
    client = make_client(tmp_path)

    assert client.get_text("https://jobs.example/data") == "call 1"
    assert client.get_text("https://jobs.example/data") == "call 2"


@pytest.mark.parametrize(
    "url",
    ["ftp://jobs.example/data", "https://user:secret@jobs.example/data"],
)
def test_non_public_url_is_rejected_before_transport(tmp_path: Path, url: str) -> None:
    client = make_client(tmp_path)

    with pytest.raises(InvalidPublicUrl):
        client.get_text(url)


@pytest.mark.parametrize("status_code", [401, 403, 407])
@respx.mock
def test_auth_or_proxy_block_stops_without_retry(tmp_path: Path, status_code: int) -> None:
    route = respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(status_code)
    )
    client = make_client(tmp_path)

    with pytest.raises(BlockedResponse, match=str(status_code)):
        client.get_text("https://jobs.example/data")
    assert route.call_count == 1


@respx.mock
def test_captcha_form_stops_without_retry(tmp_path: Path) -> None:
    route = respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(
            200,
            html="<html><title>Verify</title><form id='captcha-form'></form></html>",
        )
    )
    client = make_client(tmp_path)

    with pytest.raises(BlockedResponse, match="CAPTCHA"):
        client.get_text("https://jobs.example/data")
    assert route.call_count == 1


@respx.mock
def test_retryable_captcha_response_stops_on_first_request(tmp_path: Path) -> None:
    route = respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(
            503,
            html="<html><title>CAPTCHA challenge</title></html>",
        )
    )
    client = make_client(tmp_path)
    delays: list[float] = []
    client._sleep = delays.append

    with pytest.raises(BlockedResponse, match="CAPTCHA"):
        client.get_text("https://jobs.example/data")
    assert route.call_count == 1
    assert delays == []


@respx.mock
def test_non_retryable_error_captcha_is_classified_as_blocked(tmp_path: Path) -> None:
    route = respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(
            404,
            html="<html><form action='/captcha/verify'></form></html>",
        )
    )
    client = make_client(tmp_path)

    with pytest.raises(BlockedResponse, match="CAPTCHA"):
        client.get_text("https://jobs.example/data")
    assert route.call_count == 1


@respx.mock
def test_more_than_five_redirects_are_rejected(tmp_path: Path) -> None:
    respx.get("https://jobs.example/data").mock(
        return_value=httpx.Response(302, headers={"Location": "/data"})
    )
    client = make_client(tmp_path)

    with pytest.raises(httpx.TooManyRedirects):
        client.get_text("https://jobs.example/data")


@respx.mock
def test_json_methods_require_an_object_response(tmp_path: Path) -> None:
    respx.post("https://jobs.example/search").mock(return_value=httpx.Response(200, json=[]))
    client = make_client(tmp_path)

    with pytest.raises(InvalidResponse, match="JSON object"):
        client.post_json("https://jobs.example/search", {"query": "python"})


@respx.mock
def test_same_origin_policy_rejects_cross_origin_redirect_before_second_request(
    tmp_path: Path,
) -> None:
    first = respx.get("https://jobs.example/detail").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/payload"})
    )
    second = respx.get("https://evil.example/payload").mock(
        return_value=httpx.Response(200, text="secret")
    )
    client = make_client(tmp_path)

    with pytest.raises(InvalidPublicUrl, match="configured origin"):
        client.get_text_same_origin(
            "https://jobs.example/detail", allowed_origin="https://jobs.example/jobs"
        )

    assert first.call_count == 1
    assert second.call_count == 0


@respx.mock
def test_same_origin_policy_allows_same_origin_redirect(tmp_path: Path) -> None:
    respx.get("https://jobs.example/detail").mock(
        return_value=httpx.Response(302, headers={"Location": "/detail/final"})
    )
    respx.get("https://jobs.example/detail/final").mock(
        return_value=httpx.Response(200, text="public detail")
    )
    client = make_client(tmp_path)

    assert client.get_text_same_origin(
        "https://jobs.example/detail", allowed_origin="https://jobs.example/jobs"
    ) == "public detail"


@respx.mock
def test_approved_cross_origin_redirect_does_not_forward_authorization(
    tmp_path: Path,
) -> None:
    respx.get("https://jobs.example/detail").mock(
        return_value=httpx.Response(
            302,
            headers={"Location": "https://trusted.example/detail"},
        )
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, text="public detail")

    respx.get("https://trusted.example/detail").mock(side_effect=respond)
    client = make_client(tmp_path)

    assert client.get_text_same_origin(
        "https://jobs.example/detail",
        allowed_origin="https://jobs.example",
        allowed_redirect_origins=("https://trusted.example",),
        headers={"Authorization": "Bearer public-frontend-token"},
    ) == "public detail"


@respx.mock
def test_same_origin_json_allows_frontend_bearer(
    tmp_path: Path,
) -> None:
    seen_headers: httpx.Headers | None = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_headers
        seen_headers = request.headers
        return httpx.Response(200, json={"jobs": []})

    respx.get("https://jobs.example/search").mock(side_effect=respond)
    client = make_client(tmp_path)

    result = client.get_json_same_origin(
        "https://jobs.example/search",
        allowed_origin="https://jobs.example",
        headers={"Authorization": "Bearer public-frontend-token"},
    )

    assert result == {"jobs": []}
    assert seen_headers is not None
    assert seen_headers["Authorization"] == "Bearer public-frontend-token"


@respx.mock
def test_same_origin_authorized_json_does_not_use_shared_cache(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "If-None-Match" not in request.headers
        return httpx.Response(
            200,
            json={"authorization": request.headers["Authorization"]},
            headers={"ETag": '"shared"'},
        )

    route = respx.get("https://jobs.example/search").mock(side_effect=respond)
    client = make_client(tmp_path)

    assert client.get_json_same_origin(
        "https://jobs.example/search",
        allowed_origin="https://jobs.example",
        headers={"Authorization": "Bearer token-a"},
    ) == {"authorization": "Bearer token-a"}
    assert client.get_json_same_origin(
        "https://jobs.example/search",
        allowed_origin="https://jobs.example",
        headers={"Authorization": "Bearer token-b"},
    ) == {"authorization": "Bearer token-b"}

    assert route.call_count == 2
    assert len(requests) == 2
    assert list(tmp_path.glob("*.json")) == []


@respx.mock
def test_same_origin_bearer_rejects_cross_origin_redirect_before_second_request(
    tmp_path: Path,
) -> None:
    first = respx.get("https://jobs.example/search").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://evil.example/payload"}
        )
    )
    second = respx.get("https://evil.example/payload").mock(
        return_value=httpx.Response(200, json={"secret": True})
    )
    client = make_client(tmp_path)

    with pytest.raises(InvalidPublicUrl, match="configured origin"):
        client.get_json_same_origin(
            "https://jobs.example/search",
            allowed_origin="https://jobs.example",
            headers={"Authorization": "Bearer public-frontend-token"},
        )

    assert first.call_count == 1
    assert second.call_count == 0


def test_same_origin_policy_rejects_userinfo_before_transport(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    with pytest.raises(InvalidPublicUrl, match="userinfo"):
        client.get_text_same_origin(
            "https://user:secret@jobs.example/detail",
            allowed_origin="https://jobs.example/jobs",
        )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.10.20",
        "192.0.2.1",
        "224.0.0.1",
        "[::1]",
        "[fe80::1]",
    ],
)
def test_literal_non_global_addresses_are_rejected_before_transport(
    tmp_path: Path, host: str
) -> None:
    client = make_client(tmp_path)

    with pytest.raises(InvalidPublicUrl, match="public address"):
        client.get_text(f"https://{host}/jobs")


@respx.mock
def test_same_origin_cache_cannot_reuse_unrestricted_cache_entry(tmp_path: Path) -> None:
    route = respx.get("https://jobs.example/detail").mock(
        side_effect=[
            httpx.Response(200, text="unrestricted", headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    client = make_client(tmp_path)

    assert client.get_text("https://jobs.example/detail") == "unrestricted"
    with pytest.raises(InvalidResponse, match="without a cached response"):
        client.get_text_same_origin(
            "https://jobs.example/detail", allowed_origin="https://jobs.example/jobs"
        )
    assert route.call_count == 2
