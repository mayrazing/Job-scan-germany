from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
from httpcore._backends.base import SOCKET_OPTION
from pydantic import BaseModel, ConfigDict

from job_scan.ai_config import ReasoningEffort, StoredAiProvider
from job_scan.claude_process import (
    ClaudeInvocation,
    ClaudeOutputLimitExceeded,
    ClaudeProcessError,
    ClaudeRequest,
    ClaudeTimeout,
)

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 8192
_MODEL_LIST_MAX_BYTES = 256 * 1024
_KNOWN_ANTHROPIC_COMPAT_SUFFIXES = (
    "/api/claudecode",
    "/api/anthropic",
    "/apps/anthropic",
    "/api/coding",
    "/claudecode",
    "/anthropic",
    "/step_plan",
    "/coding",
    "/claude",
)


class AnthropicApiError(ClaudeProcessError):
    """Report one safe Anthropic-compatible request failure."""


class AnthropicApiResponseError(AnthropicApiError):
    """Report an unusable Anthropic-compatible response body."""


class OutboundAiUrlError(AnthropicApiError):
    """Report an AI URL that is not a public HTTPS destination."""


class AiModelDiscoveryError(AnthropicApiError):
    """Report failure to read models from an Anthropic-compatible endpoint."""


class _ResponseLimitExceeded(RuntimeError):
    """Stop consuming an upstream response as soon as its byte cap is crossed."""


class _DeadlineNetworkStream(httpcore.NetworkStream):
    """Apply one absolute request deadline to every network operation."""

    def __init__(self, stream: httpcore.NetworkStream, deadline: float) -> None:
        self._stream = stream
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(
            max_bytes,
            timeout=_remaining_timeout(
                self._deadline,
                timeout,
                httpcore.ReadTimeout,
            ),
        )

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(
            buffer,
            timeout=_remaining_timeout(
                self._deadline,
                timeout,
                httpcore.WriteTimeout,
            ),
        )

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        secured = self._stream.start_tls(
            ssl_context,
            server_hostname=server_hostname,
            timeout=_remaining_timeout(
                self._deadline,
                timeout,
                httpcore.ConnectTimeout,
            ),
        )
        return _DeadlineNetworkStream(secured, self._deadline)

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _PublicOnlyNetworkBackend(httpcore.NetworkBackend):
    """Resolve once, reject private results, then connect to that exact IP."""

    def __init__(self, delegate: httpcore.NetworkBackend | None = None) -> None:
        self._delegate = delegate or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        deadline = time.monotonic() + timeout if timeout is not None else None
        address = _resolved_public_addresses(host, port)[0]
        connected = self._delegate.connect_tcp(
            str(address),
            port,
            timeout=(
                _remaining_timeout(deadline, timeout, httpcore.ConnectTimeout)
                if deadline is not None
                else timeout
            ),
            local_address=local_address,
            socket_options=socket_options,
        )
        return (
            _DeadlineNetworkStream(connected, deadline)
            if deadline is not None
            else connected
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for AI providers.")


class _PublicOnlyTransport(httpx.HTTPTransport):
    """Use httpx TLS/HTTP handling with the public-only network backend."""

    def __init__(self) -> None:
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=_PublicOnlyNetworkBackend(),
        )


class AiModelOption(BaseModel):
    """Expose one model returned by an upstream model catalog."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    supported_reasoning_efforts: list[ReasoningEffort]


class AnthropicApiInvoker:
    """Call one saved Anthropic-compatible provider and normalize structured output."""

    def __init__(
        self,
        provider: StoredAiProvider,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._provider = provider
        self._client = client

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        """Send one bounded message request and return the existing invocation envelope."""
        _require_public_https(self._provider.base_url)
        if request.allow_web_search:
            return self._invoke_deepseek_web_search(request)
        url = _anthropic_endpoint(self._provider.base_url, "messages")
        payload = {
            "model": self._provider.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": _structured_prompt(request.prompt, request.json_schema),
                }
            ],
            "thinking": {"type": "disabled"},
            "output_config": {"effort": self._provider.reasoning_effort},
        }
        started_at = time.monotonic()
        response = self._post(
            url,
            payload,
            request.timeout_seconds,
            request.max_output_bytes,
        )
        if not response.is_success:
            raise AnthropicApiError(f"AI provider returned HTTP {response.status_code}.")
        structured = _structured_response(response)
        stdout = json.dumps(
            {"structured_output": structured},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return ClaudeInvocation(
            argv=["anthropic-api", self._provider.id, self._provider.model],
            stdout=stdout,
            stderr=b"",
            exit_code=0,
            duration_seconds=time.monotonic() - started_at,
            budget_usd=None,
        )

    def _invoke_deepseek_web_search(
        self,
        request: ClaudeRequest,
    ) -> ClaudeInvocation:
        """Use DeepSeek's server-side Responses web search for one structured request."""
        if urlsplit(self._provider.base_url).hostname != "api.deepseek.com":
            raise AnthropicApiError(
                "This AI provider does not support web search."
            )
        url = _deepseek_responses_endpoint(self._provider.base_url)
        _require_public_https(url)
        output_format = {
            "type": "json_schema",
            "name": "job_scan_output",
            "strict": True,
            "schema": request.json_schema,
        }
        payload = {
            "model": self._provider.model,
            "input": _structured_prompt(request.prompt, request.json_schema),
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "text": {"format": output_format},
            "reasoning": {"effort": self._provider.reasoning_effort},
            "max_output_tokens": _DEFAULT_MAX_TOKENS,
        }
        started_at = time.monotonic()
        request_headers = {
            **_bearer_headers(self._provider.api_key),
            "accept-encoding": "identity",
        }
        response = self._post(
            url,
            payload,
            request.timeout_seconds,
            request.max_output_bytes,
            headers=request_headers,
        )
        if not response.is_success:
            raise AnthropicApiError(
                f"AI provider returned HTTP {response.status_code}."
            )
        search_history = _deepseek_search_only_output(response)
        if search_history is not None:
            response = self._post(
                url,
                {
                    "model": self._provider.model,
                    "input": [
                        *search_history,
                        {
                            "type": "message",
                            "role": "user",
                            "content": (
                                "Stop searching now. Using the evidence already "
                                "gathered, return the required JSON object. If the "
                                "evidence is insufficient, return unknown. Do not call "
                                "any more tools."
                            ),
                        },
                    ],
                    "tool_choice": "none",
                    "text": {"format": output_format},
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": 2048,
                },
                _remaining_invocation_timeout(
                    started_at,
                    request.timeout_seconds,
                ),
                request.max_output_bytes,
                headers=request_headers,
            )
            if not response.is_success:
                raise AnthropicApiError(f"AI provider returned HTTP {response.status_code}.")
        structured = _responses_structured_response(response)
        stdout = json.dumps(
            {"structured_output": structured},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return ClaudeInvocation(
            argv=["deepseek-responses", self._provider.id, self._provider.model],
            stdout=stdout,
            stderr=b"",
            exit_code=0,
            duration_seconds=time.monotonic() - started_at,
            budget_usd=None,
        )

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        max_output_bytes: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = headers or _anthropic_headers(self._provider.api_key)
        try:
            if self._client is not None:
                return _bounded_http_response(
                    self._client,
                    "POST",
                    url,
                    headers=request_headers,
                    payload=payload,
                    timeout=float(timeout_seconds),
                    max_bytes=max_output_bytes,
                )
            with _public_http_client() as client:
                return _bounded_http_response(
                    client,
                    "POST",
                    url,
                    headers=request_headers,
                    payload=payload,
                    timeout=float(timeout_seconds),
                    max_bytes=max_output_bytes,
                )
        except _ResponseLimitExceeded:
            raise ClaudeOutputLimitExceeded(
                "AI response exceeded the configured output limit."
            ) from None
        except httpx.TimeoutException:
            raise ClaudeTimeout("AI provider request timed out.") from None
        except httpx.HTTPError:
            raise AnthropicApiError("Could not reach the AI provider.") from None


class AiModelDiscovery:
    """Read a bounded Anthropic model list for one unsaved or saved provider."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    def discover(self, provider: StoredAiProvider) -> list[AiModelOption]:
        """Return valid unique upstream model rows in their original order."""
        _require_public_https(provider.base_url)
        if self._client is not None:
            return self._discover_with_client(provider, self._client)
        with _public_http_client() as client:
            return self._discover_with_client(provider, client)

    def _discover_with_client(
        self,
        provider: StoredAiProvider,
        client: httpx.Client,
    ) -> list[AiModelOption]:
        for url, headers in _model_discovery_candidates(provider):
            _require_public_https(url)
            try:
                response = _bounded_http_response(
                    client,
                    "GET",
                    url,
                    headers=headers,
                    payload=None,
                    timeout=8.0,
                    max_bytes=_MODEL_LIST_MAX_BYTES,
                )
            except _ResponseLimitExceeded:
                raise AiModelDiscoveryError(
                    "Could not fetch models from this provider."
                ) from None
            except httpx.HTTPError:
                continue
            if not response.is_success:
                continue
            models = _model_options(response)
            if models:
                return models
        raise AiModelDiscoveryError("Could not fetch models from this provider.")


def _model_options(response: httpx.Response) -> list[AiModelOption]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return []
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    models: list[AiModelOption] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display_name = row.get("display_name")
        models.append(
            AiModelOption(
                id=model_id,
                name=display_name if isinstance(display_name, str) else model_id,
                supported_reasoning_efforts=_supported_efforts(row),
            )
        )
    return models


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "accept-encoding": "identity",
        "content-type": "application/json",
    }


def _bearer_headers(api_key: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }


def _model_discovery_candidates(
    provider: StoredAiProvider,
) -> list[tuple[str, dict[str, str]]]:
    primary_url = _anthropic_endpoint(provider.base_url, "models")
    candidates = [(primary_url, _anthropic_headers(provider.api_key))]
    parsed = urlsplit(provider.base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    suffix = next(
        (
            candidate
            for candidate in _KNOWN_ANTHROPIC_COMPAT_SUFFIXES
            if path.endswith(candidate)
        ),
        None,
    )
    if suffix is None:
        return candidates

    bearer_headers = _bearer_headers(provider.api_key)
    candidates.append((primary_url, bearer_headers))
    root_path = path[: -len(suffix)].rstrip("/")
    root_url = urlunsplit((parsed.scheme, parsed.netloc, root_path, "", ""))
    candidates.extend(
        [
            (_anthropic_endpoint(root_url, "models"), bearer_headers),
            (f"{root_url.rstrip('/')}/models", bearer_headers),
        ]
    )
    return candidates


def _public_http_client() -> httpx.Client:
    return httpx.Client(
        transport=_PublicOnlyTransport(),
        follow_redirects=False,
        trust_env=False,
    )


def _bounded_http_response(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
    max_bytes: int,
) -> httpx.Response:
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + timeout
    with client.stream(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=timeout,
    ) as streamed:
        for chunk in streamed.iter_bytes():
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout("AI provider request timed out.")
            size += len(chunk)
            if size > max_bytes:
                raise _ResponseLimitExceeded
            chunks.append(chunk)
        return httpx.Response(
            status_code=streamed.status_code,
            headers=streamed.headers,
            content=b"".join(chunks),
            request=streamed.request,
        )


def _anthropic_endpoint(base_url: str, resource: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    suffix = f"/{resource}" if path.endswith("/v1") else f"/v1/{resource}"
    return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))


def _deepseek_responses_endpoint(base_url: str) -> str:
    """Return DeepSeek's Responses endpoint from its Anthropic-compatible URL."""
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/").removesuffix("/anthropic")
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/responses", "", ""))


def _structured_prompt(prompt: str, schema: dict[str, Any]) -> str:
    compact_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{prompt}\n\n"
        "Return only one JSON object. Do not use Markdown fences or commentary. "
        f"The JSON must match this schema exactly: {compact_schema}"
    )


def _structured_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise AnthropicApiResponseError("AI provider returned invalid JSON.") from None
    blocks = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(blocks, list):
        raise AnthropicApiResponseError("AI provider returned no message content.")
    text = "".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    try:
        structured = json.loads(text)
    except json.JSONDecodeError:
        raise AnthropicApiResponseError(
            "AI provider did not return the required JSON object."
        ) from None
    if not isinstance(structured, dict):
        raise AnthropicApiResponseError(
            "AI provider did not return the required JSON object."
        )
    return structured


def _responses_structured_response(response: httpx.Response) -> dict[str, Any]:
    """Read one JSON object from DeepSeek Responses output-text blocks."""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        raise AnthropicApiResponseError("AI provider returned invalid JSON.") from None
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list):
        raise AnthropicApiResponseError("AI provider returned no response output.")
    text = "".join(
        content.get("text", "")
        for item in output
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for content in item["content"]
        if isinstance(content, dict)
        and content.get("type") == "output_text"
        and isinstance(content.get("text"), str)
    ).strip()
    try:
        structured = json.loads(text)
    except json.JSONDecodeError:
        raise AnthropicApiResponseError(
            "AI provider did not return the required JSON object."
        ) from None
    if not isinstance(structured, dict):
        raise AnthropicApiResponseError(
            "AI provider did not return the required JSON object."
        )
    return structured


def _deepseek_search_only_output(
    response: httpx.Response,
) -> list[dict[str, Any]] | None:
    """Return search history only when DeepSeek omitted its final text message."""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
        return None
    if not any(item.get("type") == "web_search_call" for item in output):
        return None
    text = "".join(
        content.get("text", "")
        for item in output
        if isinstance(item.get("content"), list)
        for content in item["content"]
        if isinstance(content, dict)
        and content.get("type") == "output_text"
        and isinstance(content.get("text"), str)
    ).strip()
    return None if text else output


def _remaining_invocation_timeout(started_at: float, timeout_seconds: int) -> float:
    """Keep a follow-up request inside the original invocation deadline."""
    remaining = timeout_seconds - (time.monotonic() - started_at)
    if remaining <= 0:
        raise ClaudeTimeout("AI provider request timed out.")
    return remaining


def _require_public_https(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise OutboundAiUrlError("AI provider URL must use public HTTPS.")
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
        if not literal.is_global:
            raise OutboundAiUrlError("AI provider URL must use public HTTPS.")
    except ValueError:
        pass
    try:
        port = parsed.port
    except ValueError:
        raise OutboundAiUrlError("AI provider URL contains an invalid port.") from None
    if port is not None and not 1 <= port <= 65535:
        raise OutboundAiUrlError("AI provider URL contains an invalid port.")


def _resolved_public_addresses(
    host: str,
    port: int,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        addresses = list(
            dict.fromkeys(
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
            )
        )
    except (OSError, ValueError):
        raise OutboundAiUrlError("AI provider host could not be resolved.") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise OutboundAiUrlError("AI provider URL must use public HTTPS.")
    return addresses


def _remaining_timeout(
    deadline: float,
    configured: float | None,
    error_type: type[httpcore.TimeoutException],
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise error_type("AI provider request timed out.")
    return min(configured, remaining) if configured is not None else remaining


def _supported_efforts(row: dict[str, Any]) -> list[ReasoningEffort]:
    capabilities = row.get("capabilities")
    effort = capabilities.get("effort") if isinstance(capabilities, dict) else None
    if not isinstance(effort, dict) or effort.get("supported") is not True:
        return []
    supported: list[ReasoningEffort] = []
    for level in ("low", "medium", "high", "xhigh", "max"):
        value = effort.get(level)
        if isinstance(value, dict) and value.get("supported") is True:
            supported.append(level)
    return supported
