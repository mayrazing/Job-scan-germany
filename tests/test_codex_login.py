from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from job_scan.codex_login import CodexLoginWorkflow

FAKE_CODEX = Path(__file__).parent / "fakes" / "codex"


def _wait_for_state(workflow: object, expected: str) -> object:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = workflow.snapshot()
        if snapshot.state == expected:
            return snapshot
        time.sleep(0.01)
    pytest.fail(f"Codex login did not reach {expected}")


def test_device_login_publishes_code_and_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_path = tmp_path / "argv.json"
    home_path = tmp_path / "codex-home"
    release_path = tmp_path / "release"
    monkeypatch.setenv("FAKE_CODEX_MODE", "device-login")
    monkeypatch.setenv("FAKE_CODEX_ARGV_PATH", str(argv_path))
    monkeypatch.setenv("FAKE_CODEX_LOGIN_RELEASE_PATH", str(release_path))
    workflow = CodexLoginWorkflow(home_path, binary=str(FAKE_CODEX))

    workflow.start()
    pending = _wait_for_state(workflow, "pending")

    assert pending.verification_url == "https://auth.openai.com/codex/device"
    assert pending.user_code == "TEST-9YWCE"
    assert json.loads(argv_path.read_text()) == [
        str(FAKE_CODEX),
        "-c",
        'cli_auth_credentials_store="file"',
        "login",
        "--device-auth",
    ]
    release_path.touch()
    assert _wait_for_state(workflow, "succeeded").error is None
    workflow.close()


def test_device_login_can_be_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODE", "device-login")
    monkeypatch.setenv(
        "FAKE_CODEX_LOGIN_RELEASE_PATH",
        str(tmp_path / "never-release"),
    )
    workflow = CodexLoginWorkflow(
        tmp_path / "codex-home",
        binary=str(FAKE_CODEX),
    )
    workflow.start()
    _wait_for_state(workflow, "pending")

    cancelled = workflow.cancel()

    assert cancelled.state == "cancelled"
    workflow.close()
