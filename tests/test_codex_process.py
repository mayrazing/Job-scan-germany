from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from job_scan.claude_process import (
    ClaudeOutputLimitExceeded,
    ClaudeRequest,
    ClaudeTimeout,
)
from job_scan.codex_process import (
    CodexModelCatalogError,
    CodexOutputLimitExceeded,
    CodexProcess,
    CodexTimeout,
)

FAKE_CODEX = Path(__file__).parent / "fakes" / "codex"
PRIVATE_PROMPT = "  Lebenslauf: Grüße 🔒\nJD: Python engineer\n  "


def request(**overrides: object) -> ClaudeRequest:
    values: dict[str, object] = {
        "runtime": "codex-cli",
        "prompt": PRIVATE_PROMPT,
        "json_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "timeout_seconds": 5,
        "max_output_bytes": 200_000,
    }
    values.update(overrides)
    return ClaudeRequest.model_validate(values)


def configure_fake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> dict[str, Path]:
    records = {
        name: tmp_path / f"{name.lower()}.txt"
        for name in ("ARGV", "STDIN", "CWD", "SCHEMA_PATH", "SCHEMA_CONTENT")
    }
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    environment_names = {
        "ARGV": "FAKE_CODEX_ARGV_PATH",
        "STDIN": "FAKE_CODEX_STDIN_PATH",
        "CWD": "FAKE_CODEX_CWD_PATH",
        "SCHEMA_PATH": "FAKE_CODEX_SCHEMA_PATH",
        "SCHEMA_CONTENT": "FAKE_CODEX_SCHEMA_CONTENT_PATH",
    }
    for name, path in records.items():
        monkeypatch.setenv(environment_names[name], str(path))
    return records


def test_invoke_uses_safe_noninteractive_argv_and_normalizes_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "success")
    schema = request().json_schema

    invocation = CodexProcess(str(FAKE_CODEX)).invoke(request())

    schema_path = records["SCHEMA_PATH"].read_text()
    expected = [
        str(FAKE_CODEX),
        "--ask-for-approval",
        "never",
        "--disable",
        "shell_tool",
        "--disable",
        "multi_agent",
        "--disable",
        "view_image",
        "-c",
        'web_search="disabled"',
        "-c",
        'model_reasoning_effort="medium"',
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-sol",
        "--output-schema",
        schema_path,
        "-",
    ]
    assert invocation.argv == expected
    assert json.loads(records["ARGV"].read_text()) == expected
    assert records["STDIN"].read_bytes() == PRIVATE_PROMPT.encode("utf-8")
    assert json.loads(records["SCHEMA_CONTENT"].read_text()) == schema
    assert not Path(schema_path).exists()
    assert not Path(records["CWD"].read_text()).exists()
    assert json.loads(invocation.stdout) == {"structured_output": {"result": "ok"}}
    assert invocation.stderr == b"fake codex progress"
    assert invocation.exit_code == 0


def test_web_search_is_enabled_only_for_requests_that_allow_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = CodexProcess(str(FAKE_CODEX)).invoke(
        request(allow_web_search=True)
    )

    config_index = invocation.argv.index('web_search="live"')
    assert invocation.argv[config_index - 1] == "-c"
    assert 'web_search="disabled"' not in invocation.argv


def test_version_and_login_status_use_official_health_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "version")
    process = CodexProcess(str(FAKE_CODEX))

    assert process.version() == "codex-cli 0.149.1"
    assert json.loads(records["ARGV"].read_text()) == [str(FAKE_CODEX), "--version"]

    monkeypatch.setenv("FAKE_CODEX_MODE", "auth")
    status = process.auth_status()

    assert status.authenticated is True
    assert json.loads(records["ARGV"].read_text()) == [
        str(FAKE_CODEX),
        "login",
        "status",
    ]


def test_models_returns_only_visible_catalog_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "models")

    models = CodexProcess(str(FAKE_CODEX)).models()

    assert json.loads(records["ARGV"].read_text()) == [
        str(FAKE_CODEX),
        "debug",
        "models",
    ]
    assert [model.model_dump(mode="json") for model in models] == [
        {
            "id": "gpt-5.6-sol",
            "name": "GPT-5.6-Sol",
            "default_reasoning_effort": "low",
            "supported_reasoning_efforts": ["low", "high", "ultra"],
        }
    ]


def test_models_rejects_malformed_catalog_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "invalid-json")

    with pytest.raises(CodexModelCatalogError, match="invalid model catalog"):
        CodexProcess(str(FAKE_CODEX)).models()


def test_invoke_accepts_codex_catalog_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = CodexProcess(str(FAKE_CODEX)).invoke(request(effort="ultra"))

    assert 'model_reasoning_effort="ultra"' in invocation.argv


def test_timeout_is_bounded_and_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "timeout")

    started = time.monotonic()
    with pytest.raises(CodexTimeout):
        CodexProcess(str(FAKE_CODEX)).invoke(request(timeout_seconds=1))

    assert time.monotonic() - started < 4


def test_codex_limits_keep_existing_business_error_categories() -> None:
    assert issubclass(CodexTimeout, ClaudeTimeout)
    assert issubclass(CodexOutputLimitExceeded, ClaudeOutputLimitExceeded)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_limit_is_enforced_for_both_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, "oversized")
    monkeypatch.setenv("FAKE_CODEX_STREAM", stream)
    monkeypatch.setenv("FAKE_CODEX_OUTPUT_BYTES", "1001")

    with pytest.raises(CodexOutputLimitExceeded, match="1000"):
        CodexProcess(str(FAKE_CODEX)).invoke(request(max_output_bytes=1000))
