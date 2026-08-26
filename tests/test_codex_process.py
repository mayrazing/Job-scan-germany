from __future__ import annotations

import copy
import json
import os
import stat
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
    CodexNotAuthenticated,
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
        for name in (
            "ARGV",
            "STDIN",
            "CWD",
            "SCHEMA_PATH",
            "SCHEMA_CONTENT",
            "CODEX_HOME",
        )
    }
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    environment_names = {
        "ARGV": "FAKE_CODEX_ARGV_PATH",
        "STDIN": "FAKE_CODEX_STDIN_PATH",
        "CWD": "FAKE_CODEX_CWD_PATH",
        "SCHEMA_PATH": "FAKE_CODEX_SCHEMA_PATH",
        "SCHEMA_CONTENT": "FAKE_CODEX_SCHEMA_CONTENT_PATH",
        "CODEX_HOME": "FAKE_CODEX_HOME_PATH",
    }
    for name, path in records.items():
        monkeypatch.setenv(environment_names[name], str(path))
    return records


@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        ("version", "version"),
        ("auth", "auth_status"),
        ("models", "models"),
        ("success", "invoke"),
    ],
)
def test_every_codex_process_uses_job_scan_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    operation: str,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, mode)
    codex_home = tmp_path / "job-scan" / "codex-home"
    process = CodexProcess(str(FAKE_CODEX), home=codex_home)

    if operation == "invoke":
        process.invoke(request())
    else:
        getattr(process, operation)()

    assert records["CODEX_HOME"].read_text() == str(codex_home)
    assert codex_home.is_dir()
    if os.name != "nt":
        assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        ("auth", "auth_status"),
        ("models", "models"),
        ("success", "invoke"),
    ],
)
def test_codex_auth_and_work_use_file_credential_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    operation: str,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, mode)
    process = CodexProcess(
        str(FAKE_CODEX),
        home=tmp_path / "job-scan" / "codex-home",
    )

    if operation == "invoke":
        process.invoke(request())
    else:
        getattr(process, operation)()

    argv = json.loads(records["ARGV"].read_text())
    assert 'cli_auth_credentials_store="file"' in argv


def test_invoke_uses_safe_noninteractive_argv_and_normalizes_structured_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "success")
    schema = request().json_schema

    invocation = CodexProcess(
        str(FAKE_CODEX), home=tmp_path / "codex-home"
    ).invoke(request())

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
        "-c",
        'cli_auth_credentials_store="file"',
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


def test_invoke_makes_every_codex_output_schema_object_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "success")
    schema = {
        "type": "object",
        "properties": {
            "result": {"type": "string"},
            "details": {
                "type": ["object", "null"],
                "properties": {
                    "label": {"type": ["string", "null"]},
                },
                "required": [],
            },
            "choice": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": [],
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["result"],
    }
    codex_request = request(json_schema=schema)
    original_schema = copy.deepcopy(codex_request.json_schema)

    CodexProcess(
        str(FAKE_CODEX), home=tmp_path / "codex-home"
    ).invoke(codex_request)

    written_schema = json.loads(records["SCHEMA_CONTENT"].read_text())
    assert written_schema["required"] == ["result", "details", "choice"]
    assert written_schema["additionalProperties"] is False
    details_schema = written_schema["properties"]["details"]
    assert details_schema["required"] == ["label"]
    assert details_schema["additionalProperties"] is False
    choice_schema = written_schema["properties"]["choice"]["anyOf"][0]
    assert choice_schema["required"] == ["value"]
    assert choice_schema["additionalProperties"] is False
    assert codex_request.json_schema == original_schema


def test_web_search_is_enabled_only_for_requests_that_allow_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = CodexProcess(
        str(FAKE_CODEX), home=tmp_path / "codex-home"
    ).invoke(
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
    process = CodexProcess(str(FAKE_CODEX), home=tmp_path / "codex-home")

    assert process.version() == "codex-cli 0.149.1"
    assert json.loads(records["ARGV"].read_text()) == [str(FAKE_CODEX), "--version"]

    monkeypatch.setenv("FAKE_CODEX_MODE", "auth")
    status = process.auth_status()

    assert status.authenticated is True
    assert json.loads(records["ARGV"].read_text()) == [
        str(FAKE_CODEX),
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "status",
    ]


def test_auth_status_accepts_current_codex_stderr_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "auth-stderr")

    status = CodexProcess(str(FAKE_CODEX), home=tmp_path / "codex-home").auth_status()

    assert status.authenticated is True


def test_auth_failure_points_to_isolated_login_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "nonzero")
    codex_home = tmp_path / "Job Scan" / "codex-home"

    with pytest.raises(CodexNotAuthenticated) as raised:
        CodexProcess(str(FAKE_CODEX), home=codex_home).auth_status()

    message = str(raised.value)
    assert f"CODEX_HOME='{codex_home}'" in message
    assert "cli_auth_credentials_store=\"file\"" in message
    assert "login --device-auth" in message


def test_models_returns_only_visible_catalog_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    records = configure_fake(monkeypatch, tmp_path, "models")

    models = CodexProcess(
        str(FAKE_CODEX), home=tmp_path / "codex-home"
    ).models()

    assert json.loads(records["ARGV"].read_text()) == [
        str(FAKE_CODEX),
        "-c",
        'cli_auth_credentials_store="file"',
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
        CodexProcess(
            str(FAKE_CODEX), home=tmp_path / "codex-home"
        ).models()


def test_invoke_accepts_codex_catalog_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = CodexProcess(
        str(FAKE_CODEX), home=tmp_path / "codex-home"
    ).invoke(request(effort="ultra"))

    assert 'model_reasoning_effort="ultra"' in invocation.argv


def test_timeout_is_bounded_and_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "timeout")

    started = time.monotonic()
    with pytest.raises(CodexTimeout):
        CodexProcess(
            str(FAKE_CODEX), home=tmp_path / "codex-home"
        ).invoke(request(timeout_seconds=1))

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
        CodexProcess(
            str(FAKE_CODEX), home=tmp_path / "codex-home"
        ).invoke(request(max_output_bytes=1000))
