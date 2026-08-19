from __future__ import annotations

from pathlib import Path

from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_runtime import AiRuntimeInvoker
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest
from job_scan.locking import FileRWLock, LockUnavailable
from job_scan.paths import AppPaths


class RecordingInvoker:
    def __init__(self, label: str) -> None:
        self.label = label
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        return ClaudeInvocation(
            argv=[self.label],
            stdout=b'{"structured_output":{"ok":true}}',
            stderr=b"",
            exit_code=0,
            duration_seconds=0.01,
        )


def request(runtime: str, *, runtime_model: str | None = None) -> ClaudeRequest:
    return ClaudeRequest(
        runtime=runtime,
        prompt="Return JSON.",
        json_schema={"type": "object"},
        model="sonnet",
        effort="medium",
        timeout_seconds=30,
        max_output_bytes=1000,
        runtime_model=runtime_model,
    )


def test_runtime_delegates_default_request_to_claude_cli(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    cli = RecordingInvoker("cli")
    runtime = AiRuntimeInvoker(paths, claude=cli)

    result = runtime.invoke(request("claude-code"))

    assert result.argv == ["cli"]
    assert cli.requests[0].runtime == "claude-code"


def test_runtime_loads_selected_provider_for_api_request(tmp_path: Path) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AiProviderStore(paths.ai_config_toml)
    saved = store.create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="sk-test",
            model="deepseek-chat",
            reasoning_effort="low",
        )
    )
    api = RecordingInvoker("api")
    selected = []

    def api_factory(provider):
        selected.append(provider)
        return api

    runtime = AiRuntimeInvoker(paths, claude=RecordingInvoker("cli"), api_factory=api_factory)

    result = runtime.invoke(
        request(f"api:{saved.id}", runtime_model="scan-snapshot-model")
    )

    assert result.argv == ["api"]
    assert selected[0].id == "deepseek"
    assert selected[0].api_key == "sk-test"
    assert selected[0].model == "scan-snapshot-model"
    assert api.requests[0].runtime == "api:deepseek"


def test_runtime_holds_the_ai_usage_lock_for_the_complete_invocation(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")

    class LockCheckingInvoker(RecordingInvoker):
        def invoke(self, ai_request: ClaudeRequest) -> ClaudeInvocation:
            try:
                with FileRWLock(paths.ai_usage_lock_file).exclusive(blocking=False):
                    raise AssertionError("AI configuration lock was writable during use")
            except LockUnavailable:
                pass
            return super().invoke(ai_request)

    runtime = AiRuntimeInvoker(paths, claude=LockCheckingInvoker("cli"))

    result = runtime.invoke(request("claude-code"))

    assert result.argv == ["cli"]
