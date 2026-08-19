from __future__ import annotations

import json
import socket
from typing import Any

import httpcore
import httpx
import pytest

from job_scan import anthropic_api as anthropic_api_module
from job_scan.ai_config import StoredAiProvider
from job_scan.anthropic_api import (
    AiModelDiscovery,
    AiModelDiscoveryError,
    AnthropicApiError,
    AnthropicApiInvoker,
    AnthropicApiResponseError,
    OutboundAiUrlError,
    _PublicOnlyNetworkBackend,
)
from job_scan.claude_process import (
    ClaudeOutputLimitExceeded,
    ClaudeRequest,
    ClaudeTimeout,
)


class TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class RecordingBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self.hosts: list[str] = []

    def connect_tcp(self, host: str, *_args: Any, **_kwargs: Any) -> Any:
        self.hosts.append(host)
        return object()


def provider(base_url: str = "https://8.8.8.8/anthropic") -> StoredAiProvider:
    return StoredAiProvider(
        id="deepseek",
        display_name="DeepSeek",
        base_url=base_url,
        api_key="sk-private",
        model="deepseek-v4-flash",
        reasoning_effort="low",
    )


def request() -> ClaudeRequest:
    return ClaudeRequest(
        prompt="Return one answer.",
        json_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        model="sonnet",
        effort="medium",
        timeout_seconds=30,
        max_output_bytes=100_000,
    )


def test_invoker_sends_anthropic_request_and_normalizes_json() -> None:
    def respond(sent: httpx.Request) -> httpx.Response:
        assert str(sent.url) == "https://8.8.8.8/anthropic/v1/messages"
        assert sent.headers["x-api-key"] == "sk-private"
        assert sent.headers["anthropic-version"] == "2023-06-01"
        assert sent.headers["accept-encoding"] == "identity"
        payload = json.loads(sent.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["max_tokens"] == 8192
        assert payload["output_config"] == {"effort": "low"}
        assert payload["thinking"] == {"type": "disabled"}
        assert "Return one answer." in payload["messages"][0]["content"]
        assert '"additionalProperties":false' in payload["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": '{"answer":"ok"}'}],
                "model": "deepseek-v4-flash",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))

    result = AnthropicApiInvoker(provider(), client=client).invoke(request())

    assert json.loads(result.stdout) == {"structured_output": {"answer": "ok"}}
    assert result.exit_code == 0
    assert result.argv == ["anthropic-api", "deepseek", "deepseek-v4-flash"]


def test_deepseek_web_search_uses_server_side_responses_tool() -> None:
    def respond(sent: httpx.Request) -> httpx.Response:
        assert str(sent.url) == "https://api.deepseek.com/responses"
        assert sent.headers["authorization"] == "Bearer sk-private"
        assert sent.headers["accept-encoding"] == "identity"
        payload = json.loads(sent.content)
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["tools"] == [{"type": "web_search"}]
        assert payload["tool_choice"] == "auto"
        assert payload["text"]["format"]["schema"] == request().json_schema
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"answer":"searched"}'}
                        ],
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    deepseek = provider("https://api.deepseek.com/anthropic")
    web_request = request().model_copy(update={"allow_web_search": True})

    result = AnthropicApiInvoker(deepseek, client=client).invoke(web_request)

    assert json.loads(result.stdout) == {"structured_output": {"answer": "searched"}}


def test_deepseek_web_search_finishes_one_search_only_response() -> None:
    requests: list[dict[str, Any]] = []
    search_output = [
        {
            "type": "reasoning",
            "id": "reasoning-1",
            "status": "completed",
            "content": [{"type": "reasoning_text", "text": "I found one source."}],
            "summary": [],
        },
        {
            "type": "web_search_call",
            "id": "search-1",
            "status": "completed",
            "action": {
                "type": "search",
                "queries": ["Acme employee count"],
            },
        },
    ]

    def respond(sent: httpx.Request) -> httpx.Response:
        payload = json.loads(sent.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "id": "resp-search",
                    "status": "completed",
                    "output": search_output,
                },
            )
        assert payload["input"][:-1] == search_output
        assert payload["input"][-1] == {
            "type": "message",
            "role": "user",
            "content": (
                "Stop searching now. Using the evidence already gathered, return "
                "the required JSON object. If the evidence is insufficient, return "
                "unknown. Do not call any more tools."
            ),
        }
        assert payload["tool_choice"] == "none"
        assert "tools" not in payload
        assert payload["reasoning"] == {"effort": "low"}
        assert payload["max_output_tokens"] == 2048
        return httpx.Response(
            200,
            json={
                "id": "resp-final",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"answer":"done"}'}],
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    deepseek = provider("https://api.deepseek.com/anthropic")
    web_request = request().model_copy(update={"allow_web_search": True})

    result = AnthropicApiInvoker(deepseek, client=client).invoke(web_request)

    assert json.loads(result.stdout) == {"structured_output": {"answer": "done"}}
    assert len(requests) == 2


def test_deepseek_web_search_stops_after_one_unsuccessful_finish_request() -> None:
    request_count = 0

    def respond(_sent: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "id": f"resp-{request_count}",
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": f"search-{request_count}",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "queries": ["Acme employee count"],
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    deepseek = provider("https://api.deepseek.com/anthropic")
    web_request = request().model_copy(update={"allow_web_search": True})

    with pytest.raises(AnthropicApiResponseError):
        AnthropicApiInvoker(deepseek, client=client).invoke(web_request)

    assert request_count == 2


def test_deepseek_web_search_does_not_finish_after_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0

    def respond(_sent: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "search-1",
                        "status": "completed",
                    }
                ],
            },
        )

    calls = 0

    def monotonic() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls <= 3 else 31.0

    monkeypatch.setattr(anthropic_api_module.time, "monotonic", monotonic)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    deepseek = provider("https://api.deepseek.com/anthropic")
    web_request = request().model_copy(update={"allow_web_search": True})

    with pytest.raises(ClaudeTimeout):
        AnthropicApiInvoker(deepseek, client=client).invoke(web_request)

    assert request_count == 1


def test_non_deepseek_anthropic_provider_rejects_web_search() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("unsupported provider reached transport")
        )
    )
    web_request = request().model_copy(update={"allow_web_search": True})

    with pytest.raises(AnthropicApiError, match="does not support web search"):
        AnthropicApiInvoker(provider(), client=client).invoke(web_request)


def test_invoker_rejects_private_destination_before_transport() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: pytest.fail("private destination reached transport")
        )
    )

    with pytest.raises(OutboundAiUrlError):
        AnthropicApiInvoker(provider("https://127.0.0.1/anthropic"), client=client).invoke(
            request()
        )


def test_invoker_rejects_non_json_model_text() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"content": [{"type": "text", "text": "not json"}]},
            )
        )
    )

    with pytest.raises(AnthropicApiResponseError):
        AnthropicApiInvoker(provider(), client=client).invoke(request())


def test_invoker_stops_streaming_when_output_limit_is_exceeded() -> None:
    stream = TrackingStream([b"123456", b"789012", b"must-not-be-read"])
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=stream)
        )
    )
    bounded_request = request().model_copy(update={"max_output_bytes": 10})

    with pytest.raises(ClaudeOutputLimitExceeded):
        AnthropicApiInvoker(provider(), client=client).invoke(bounded_request)

    assert stream.yielded == 2


def test_invoker_stops_streaming_at_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = TrackingStream(
        [
            b'{"content":',
            b'[{"type":"text",',
            b'"text":"{\\"answer\\":\\"ok\\"}"}]}',
        ]
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=stream)
        )
    )
    ticks = iter([0.0, 0.0, 1.0, 31.0])
    monkeypatch.setattr(anthropic_api_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(ClaudeTimeout):
        AnthropicApiInvoker(provider(), client=client).invoke(request())

    assert stream.yielded == 2


def test_network_backend_connects_to_the_single_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegate = RecordingBackend()
    resolution_count = 0

    def resolve(host: str, port: int, **_kwargs: Any):
        nonlocal resolution_count
        resolution_count += 1
        address = "8.8.8.8" if resolution_count == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(anthropic_api_module.socket, "getaddrinfo", resolve)

    _PublicOnlyNetworkBackend(delegate=delegate).connect_tcp(
        "provider.example",
        443,
        timeout=30.0,
    )

    assert resolution_count == 1
    assert delegate.hosts == ["8.8.8.8"]


def test_model_discovery_reads_anthropic_data_rows() -> None:
    def respond(sent: httpx.Request) -> httpx.Response:
        assert str(sent.url) == "https://8.8.8.8/anthropic/v1/models"
        assert sent.headers["x-api-key"] == "sk-private"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-v4-flash",
                        "display_name": "DeepSeek V4 Flash",
                        "capabilities": {
                            "effort": {
                                "supported": True,
                                "low": {"supported": True},
                                "medium": {"supported": True},
                            }
                        },
                    },
                    {"id": "deepseek-chat"},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))

    models = AiModelDiscovery(client=client).discover(provider())

    assert [model.model_dump() for model in models] == [
        {
            "id": "deepseek-v4-flash",
            "name": "DeepSeek V4 Flash",
            "supported_reasoning_efforts": ["low", "medium"],
        },
        {
            "id": "deepseek-chat",
            "name": "deepseek-chat",
            "supported_reasoning_efforts": [],
        },
    ]


def test_model_discovery_falls_back_to_deepseek_root_models_with_bearer() -> None:
    requests: list[tuple[str, str | None, str | None]] = []

    def respond(sent: httpx.Request) -> httpx.Response:
        requests.append(
            (
                str(sent.url),
                sent.headers.get("x-api-key"),
                sent.headers.get("authorization"),
            )
        )
        if str(sent.url) == "https://8.8.8.8/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "deepseek-v4-flash",
                            "object": "model",
                            "owned_by": "deepseek",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(respond))

    models = AiModelDiscovery(client=client).discover(provider())

    assert [model.id for model in models] == ["deepseek-v4-flash"]
    assert requests == [
        ("https://8.8.8.8/anthropic/v1/models", "sk-private", None),
        (
            "https://8.8.8.8/anthropic/v1/models",
            None,
            "Bearer sk-private",
        ),
        ("https://8.8.8.8/v1/models", None, "Bearer sk-private"),
        ("https://8.8.8.8/models", None, "Bearer sk-private"),
    ]


def test_model_discovery_stops_streaming_after_catalog_limit() -> None:
    stream = TrackingStream(
        [b"x" * 200_000, b"y" * 100_000, b"must-not-be-read"]
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=stream)
        )
    )

    with pytest.raises(AiModelDiscoveryError):
        AiModelDiscovery(client=client).discover(provider())

    assert stream.yielded == 2
