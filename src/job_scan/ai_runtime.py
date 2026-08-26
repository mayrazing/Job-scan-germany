from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from job_scan.ai_config import (
    AiConfigError,
    AiProviderStore,
    StoredAiProvider,
)
from job_scan.anthropic_api import AnthropicApiInvoker
from job_scan.claude_process import (
    ClaudeInvocation,
    ClaudeProcess,
    ClaudeProcessError,
    ClaudeRequest,
)
from job_scan.codex_process import CodexProcess
from job_scan.locking import FileRWLock
from job_scan.paths import AppPaths


class AiRuntimeConfigError(ClaudeProcessError):
    """Report a selected runtime whose saved configuration is unavailable."""


class AiInvoker(Protocol):
    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation: ...


class AiRuntimeInvoker:
    """Route one model request to a selected local CLI or saved API."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        claude: AiInvoker | None = None,
        codex: AiInvoker | None = None,
        store: AiProviderStore | None = None,
        api_factory: Callable[[StoredAiProvider], AiInvoker] | None = None,
    ) -> None:
        self._claude = claude if claude is not None else ClaudeProcess()
        self._codex = (
            codex if codex is not None else CodexProcess(home=paths.codex_home)
        )
        self._store = store if store is not None else AiProviderStore(paths.ai_config_toml)
        self._api_factory = api_factory or (lambda provider: AnthropicApiInvoker(provider))
        self._usage_lock = FileRWLock(paths.ai_usage_lock_file)

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        """Delegate the request using its validated persisted runtime key."""
        with self._usage_lock.shared():
            if request.runtime == "claude-code":
                return self._claude.invoke(request)
            if request.runtime == "codex-cli":
                return self._codex.invoke(request)
            provider_id = request.runtime.removeprefix("api:")
            try:
                provider = self._store.require(provider_id)
            except AiConfigError:
                raise AiRuntimeConfigError(
                    "Selected AI configuration is missing or invalid."
                ) from None
            if request.runtime_model is not None:
                provider = provider.model_copy(update={"model": request.runtime_model})
            return self._api_factory(provider).invoke(request)
