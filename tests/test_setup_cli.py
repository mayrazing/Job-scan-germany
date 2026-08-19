from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_scan import cli as cli_module
from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionStore,
    ClaudeRuntimeSelection,
)
from job_scan.cli import app
from job_scan.config import AppConfig
from job_scan.paths import AppPaths
from job_scan.setup_service import SetupAnswers, SetupError, SetupResult

RESUME = Path(__file__).parent / "fixtures" / "resume" / "sample.docx"
RESUME_HASH = "sha256:ce8d12508f4b064b099d92d890c66446807a7089e7b11c8efe7a0605cb4297ff"
PROFILE_HASH = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def valid_input() -> str:
    values = [
        " backend engineer, ,platform engineer ",
        " Berlin, Hamburg, ",
        "",
        "35",
        "42",
        "38",
        "41",
        "Goethe B1",
        "claude-sonnet-4-5",
        "high",
        "8",
        "08:30",
    ]
    return "\n".join(values) + "\n"


def nationwide_input() -> str:
    values = valid_input().splitlines()
    values[1] = ""
    return "\n".join(values) + "\n"


class RecordingService:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.calls: list[tuple[Path, SetupAnswers]] = []

    def run(self, resume_path: Path, answers: SetupAnswers) -> SetupResult:
        self.calls.append((resume_path, answers))
        config = AppConfig(
            resume_path=resume_path.resolve(strict=True),
            resume_sha256=RESUME_HASH,
            profile_sha256=PROFILE_HASH,
            **answers.model_dump(),
        )
        return SetupResult(
            config=config,
            profile_path=self.paths.profile_md,
            profile_hash=PROFILE_HASH,
        )


def install_recording_service(
    monkeypatch: pytest.MonkeyPatch,
) -> list[RecordingService]:
    services: list[RecordingService] = []

    def factory(paths: AppPaths) -> RecordingService:
        service = RecordingService(paths)
        services.append(service)
        return service

    monkeypatch.setattr(cli_module, "_setup_service_factory", factory)
    return services


def test_setup_requires_resume_option() -> None:
    result = CliRunner().invoke(app, ["setup"])

    assert result.exit_code == 2
    assert "--resume" in result.output
    assert "Missing option" in result.output


def test_setup_parses_interactive_values_and_keeps_germany_visa_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = install_recording_service(monkeypatch)
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "job-scan-home"))

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=valid_input(),
    )

    assert result.exit_code == 0, result.output
    assert len(services) == 1
    assert services[0].paths.root == tmp_path / "job-scan-home"
    assert len(services[0].calls) == 1
    resume_path, answers = services[0].calls[0]
    assert resume_path == RESUME
    assert answers.search_terms == ["backend engineer", "platform engineer"]
    assert answers.locations == ["Berlin", "Hamburg"]
    assert answers.posted_within_days == 7
    assert answers.linkedin_limit == 10
    assert answers.indeed_de_limit == 35
    assert answers.stepstone_de_limit == 42
    assert answers.glassdoor_de_limit == 38
    assert answers.simplify_de_limit == 41
    assert answers.german_level == "Goethe B1"
    assert "target_lanes" not in SetupAnswers.model_fields
    assert answers.staffing_penalty == 10
    assert answers.claude.model == "claude-sonnet-4-5"
    assert answers.claude.effort == "high"
    assert answers.claude.batch_size == 8
    assert answers.scheduler.local_time == "08:30"
    assert "max_budget_usd" not in answers.claude.model_fields
    assert "Claude max budget USD" not in result.output
    assert "country" not in SetupAnswers.model_fields
    assert "needs_visa_sponsorship" not in SetupAnswers.model_fields
    config = AppConfig(
        resume_path=RESUME.resolve(strict=True),
        resume_sha256=RESUME_HASH,
        profile_sha256=PROFILE_HASH,
        **answers.model_dump(),
    )
    assert config.country == "DE"
    assert config.needs_visa_sponsorship is True


def test_setup_uses_the_saved_global_ai_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = install_recording_service(monkeypatch)
    paths = AppPaths.from_root(tmp_path / "job-scan-home")
    monkeypatch.setenv("JOB_SCAN_HOME", str(paths.root))
    provider = AiProviderStore(paths.ai_config_toml).create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="secret",
            model="deepseek-chat",
            reasoning_effort="low",
        )
    )
    AiSelectionStore(paths.ai_selection_toml).save(
        AiRuntimeSelection(
            ai_runtime=f"api:{provider.id}",
            claude=ClaudeRuntimeSelection(
                model="opus",
                effort="low",
                thinking_enabled=False,
            ),
        )
    )

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=valid_input(),
    )

    assert result.exit_code == 0, result.output
    answers = services[0].calls[0][1]
    assert answers.ai_runtime == f"api:{provider.id}"
    assert answers.claude.model == "opus"
    assert answers.claude.effort == "low"
    assert answers.claude.thinking_enabled is False
    assert answers.claude.batch_size == 8


def test_setup_accepts_an_empty_daily_scan_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = install_recording_service(monkeypatch)
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "job-scan-home"))
    values = valid_input().splitlines()
    values[-1] = ""

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input="\n".join(values) + "\n",
    )

    assert result.exit_code == 0, result.output
    assert services[0].calls[0][1].scheduler.local_time is None


def test_setup_prints_only_paths_and_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_recording_service(monkeypatch)
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "job-scan-home"))

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=valid_input(),
    )

    assert result.exit_code == 0, result.output
    assert str(tmp_path / "job-scan-home" / "profile.md") in result.output
    assert str(tmp_path / "job-scan-home" / "config.toml") in result.output
    assert PROFILE_HASH in result.output
    assert RESUME_HASH in result.output
    assert "Python" not in result.output
    assert "Backend delivery" not in result.output


def test_setup_accepts_empty_locations_as_germany_wide_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = install_recording_service(monkeypatch)
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "job-scan-home"))

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=nationwide_input(),
    )

    assert result.exit_code == 0, result.output
    answers = services[0].calls[0][1]
    assert answers.locations == []


@pytest.mark.parametrize(
    ("input_text", "field"),
    [
        ("\n", "search terms"),
    ],
)
def test_setup_rejects_empty_required_comma_separated_lists_without_calling_service(
    monkeypatch: pytest.MonkeyPatch,
    input_text: str,
    field: str,
) -> None:
    services = install_recording_service(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=input_text,
    )

    assert result.exit_code == 1
    assert field in result.output.lower()
    assert "Traceback" not in result.output
    assert services == []


def test_setup_service_failure_is_short_safe_and_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_SCAN_HOME", str(tmp_path / "job-scan-home"))

    class FailingService:
        def run(self, _resume_path: Path, _answers: SetupAnswers) -> SetupResult:
            raise SetupError("Profile generation failed safely; retry setup.")

    monkeypatch.setattr(cli_module, "_setup_service_factory", lambda _paths: FailingService())

    result = CliRunner().invoke(
        app,
        ["setup", "--resume", str(RESUME)],
        input=valid_input(),
    )

    assert result.exit_code == 1
    assert "Profile generation failed safely; retry setup." in result.output
    assert "Traceback" not in result.output
    assert "Python" not in result.output
