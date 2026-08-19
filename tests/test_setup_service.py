from __future__ import annotations

import json
from pathlib import Path
from typing import Any, BinaryIO, Self

import pytest
from pydantic import ValidationError

from job_scan import setup_service as setup_service_module
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest, ClaudeTimeout
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    load_config_bytes,
)
from job_scan.paths import AppPaths
from job_scan.prompts import build_profile_prompt
from job_scan.resume import ResumeError
from job_scan.setup_service import (
    SetupAnswers,
    SetupError,
    SetupResult,
    SetupService,
)

RESUME = Path(__file__).parent / "fixtures" / "resume" / "sample.docx"
RESUME_HASH = "sha256:ce8d12508f4b064b099d92d890c66446807a7089e7b11c8efe7a0605cb4297ff"
PROFILE = """# Target roles
Backend Engineer

# Technical skills
Python, SQL

# Experience
Backend delivery

# Languages
English, German B1

# Work authorization and visa
Needs visa sponsorship

# Preferences
Berlin or remote
"""
PROFILE_HASH = "sha256:1e58606166e7ecdb425d39bad0082b7ff2e372d73b140db275b3eef8701b5c1a"
PRIVATE_MARKER = "PRIVATE-RESUME-CONTENT-MUST-NOT-LEAK"


class FakeClaude:
    def __init__(
        self,
        *,
        stdout: bytes | None = None,
        stderr: bytes | None = None,
        exit_code: int = 0,
        error: BaseException | None = None,
        argv: list[str] | None = None,
    ) -> None:
        payload = {"structured_output": {"profile_markdown": PROFILE}}
        self.stdout = stdout if stdout is not None else json.dumps(payload).encode()
        self.stderr = stderr if stderr is not None else PRIVATE_MARKER.encode()
        self.exit_code = exit_code
        self.error = error
        self.argv = argv or ["explicit-local-fake"]
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ClaudeInvocation(
            argv=self.argv,
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            duration_seconds=0.01,
        )


def valid_answers() -> SetupAnswers:
    return SetupAnswers(
        ai_runtime="api:deepseek",
        search_terms=["backend engineer", "platform engineer"],
        locations=["Berlin", "Hamburg"],
        german_level="Goethe B1",
        staffing_penalty=10,
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="high",
            batch_size=8,
            timeout_seconds=91,
            max_output_bytes=123_456,
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def app_config() -> AppConfig:
    answers = valid_answers()
    return AppConfig(
        resume_path=RESUME.resolve(strict=True),
        resume_sha256=RESUME_HASH,
        profile_sha256=PROFILE_HASH,
        **answers.model_dump(),
    )


def paths_at(tmp_path: Path) -> AppPaths:
    return AppPaths.from_root(tmp_path / "home")


def assert_old_pair_unchanged(paths: AppPaths) -> None:
    assert paths.profile_md.read_bytes() == b"old profile\n"
    assert paths.config_toml.read_bytes() == b"old config\n"
    assert not list(paths.root.glob(".*.tmp"))
    assert not list(paths.root.glob(".*.bak"))


def seed_old_pair(paths: AppPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.profile_md.write_bytes(b"old profile\n")
    paths.config_toml.write_bytes(b"old config\n")


def test_profile_prompt_contains_resume_preferences_and_strict_markdown_contract() -> None:
    prompt = build_profile_prompt(PRIVATE_MARKER, valid_answers())

    assert PRIVATE_MARKER in prompt
    assert "backend engineer" in prompt
    assert "Berlin" in prompt
    assert "Goethe B1" in prompt
    for heading in (
        "Target roles",
        "Technical skills",
        "Experience",
        "Languages",
        "Work authorization and visa",
        "Preferences",
    ):
        assert heading in prompt
    assert "do not invent, infer, embellish, or silently fill" in prompt.lower()
    assert "resume_sha256" not in prompt
    assert "profile_sha256" not in prompt


def test_setup_uses_exact_schema_and_configured_safe_request_limits(tmp_path: Path) -> None:
    fake = FakeClaude()

    SetupService(paths_at(tmp_path), fake).run(RESUME, valid_answers())

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request.json_schema == {
        "type": "object",
        "properties": {
            "profile_markdown": {"type": "string", "minLength": 1},
        },
        "required": ["profile_markdown"],
        "additionalProperties": False,
    }
    assert request.model == "claude-sonnet-4-5"
    assert request.effort == "high"
    assert request.runtime == "api:deepseek"
    assert request.timeout_seconds == 91
    assert request.max_output_bytes == 123_456
    assert PRIVATE_MARKER not in request.prompt


def test_setup_forwards_disabled_thinking_to_claude_request(tmp_path: Path) -> None:
    fake = FakeClaude()
    answers = valid_answers()
    answers = answers.model_copy(
        update={
            "claude": ClaudeSettings.model_validate(
                {**answers.claude.model_dump(), "thinking_enabled": False}
            )
        }
    )

    SetupService(paths_at(tmp_path), fake).run(RESUME, answers)

    assert fake.requests[0].thinking_enabled is False


def test_setup_publishes_exact_profile_and_round_trippable_config_without_touching_resume(
    tmp_path: Path,
) -> None:
    paths = paths_at(tmp_path)
    before_resume = RESUME.read_bytes()
    before_siblings = {entry.name for entry in RESUME.parent.iterdir()}

    result = SetupService(paths, FakeClaude()).run(RESUME, valid_answers())

    assert paths.profile_md.read_bytes() == PROFILE.encode("utf-8")
    assert result == SetupResult(
        config=app_config(),
        profile_path=paths.profile_md,
        profile_hash=PROFILE_HASH,
    )
    assert load_config(paths.config_toml) == app_config()
    assert result.config.resume_path == RESUME.resolve(strict=True)
    assert result.config.resume_sha256 == RESUME_HASH
    assert result.config.profile_sha256 == PROFILE_HASH
    assert RESUME.read_bytes() == before_resume
    assert {entry.name for entry in RESUME.parent.iterdir()} == before_siblings
    assert PRIVATE_MARKER not in result.model_dump_json()


def test_prepare_builds_profile_without_overwriting_current_setup(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    seed_old_pair(paths)

    prepared = SetupService(paths, FakeClaude()).prepare(RESUME, valid_answers())

    assert paths.profile_md.read_bytes() == b"old profile\n"
    assert paths.config_toml.read_bytes() == b"old config\n"
    assert prepared.profile_bytes == PROFILE.encode("utf-8")
    assert prepared.profile_hash == PROFILE_HASH
    assert prepared.config.resume_sha256 == RESUME_HASH
    assert load_config_bytes(prepared.config_bytes) == prepared.config


def test_setup_reuses_profile_for_same_resume_while_updating_config(
    tmp_path: Path,
) -> None:
    paths = paths_at(tmp_path)
    fake = FakeClaude()
    service = SetupService(paths, fake)
    service.run(RESUME, valid_answers())
    updated_answers = valid_answers().model_copy(update={"posted_within_days": 14})

    result = service.run(RESUME, updated_answers)

    assert len(fake.requests) == 1
    assert paths.profile_md.read_bytes() == PROFILE.encode("utf-8")
    assert result.profile_hash == PROFILE_HASH
    assert result.config.posted_within_days == 14
    assert load_config(paths.config_toml).posted_within_days == 14


def test_setup_publishes_a_round_trippable_config_without_a_schedule_time(
    tmp_path: Path,
) -> None:
    paths = paths_at(tmp_path)
    answers = valid_answers().model_copy(update={"scheduler": SchedulerSettings()})

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.scheduler.local_time is None
    assert load_config(paths.config_toml).scheduler.local_time is None
    assert "local_time" not in paths.config_toml.read_text(encoding="utf-8")


def test_setup_publishes_a_round_trippable_config_without_a_posting_window(
    tmp_path: Path,
) -> None:
    paths = paths_at(tmp_path)
    answers = valid_answers().model_copy(update={"posted_within_days": None})

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.posted_within_days is None
    assert load_config(paths.config_toml).posted_within_days is None


def test_setup_persists_a_custom_linkedin_limit(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["linkedin_limit"] = 75
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.linkedin_limit == 75
    assert load_config(paths.config_toml).linkedin_limit == 75


def test_setup_persists_disabled_arbeitsagentur_source(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["arbeitsagentur_enabled"] = False
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.arbeitsagentur_enabled is False
    assert load_config(paths.config_toml).arbeitsagentur_enabled is False


def test_setup_persists_a_custom_indeed_de_limit(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["indeed_de_limit"] = 35
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.indeed_de_limit == 35
    assert load_config(paths.config_toml).indeed_de_limit == 35


def test_setup_persists_a_custom_stepstone_de_limit(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["stepstone_de_limit"] = 42
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.stepstone_de_limit == 42
    assert load_config(paths.config_toml).stepstone_de_limit == 42


def test_setup_persists_a_custom_glassdoor_de_limit(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["glassdoor_de_limit"] = 38
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.glassdoor_de_limit == 38
    assert load_config(paths.config_toml).glassdoor_de_limit == 38


def test_setup_persists_a_custom_simplify_de_limit(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answer_data = valid_answers().model_dump()
    answer_data["simplify_de_limit"] = 41
    answers = SetupAnswers.model_validate(answer_data)

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.simplify_de_limit == 41
    assert load_config(paths.config_toml).simplify_de_limit == 41


def test_setup_persists_disabled_opencli_switches(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    answers = valid_answers().model_copy(
        update={
            "linkedin_enabled": False,
            "indeed_de_enabled": False,
            "stepstone_de_enabled": False,
            "glassdoor_de_enabled": False,
            "simplify_de_enabled": False,
        }
    )

    result = SetupService(paths, FakeClaude()).run(RESUME, answers)

    assert result.config.linkedin_enabled is False
    assert result.config.indeed_de_enabled is False
    assert result.config.stepstone_de_enabled is False
    assert result.config.glassdoor_de_enabled is False
    assert result.config.simplify_de_enabled is False
    saved = load_config(paths.config_toml)
    assert saved.linkedin_enabled is False
    assert saved.indeed_de_enabled is False
    assert saved.stepstone_de_enabled is False
    assert saved.glassdoor_de_enabled is False
    assert saved.simplify_de_enabled is False


def test_setup_persists_the_actual_selected_api_model(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    fake = FakeClaude(argv=["anthropic-api", "deepseek", "deepseek-chat"])

    result = SetupService(paths, fake).run(RESUME, valid_answers())

    assert result.config.ai_model == "deepseek-chat"
    assert load_config(paths.config_toml).selected_model == "deepseek-chat"


def test_setup_uses_unique_sibling_fsynced_temps_before_exact_pair_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = paths_at(tmp_path)
    created: list[Path] = []
    operations: list[tuple[str, Path | None]] = []
    real_mkstemp = setup_service_module.tempfile.mkstemp
    real_fsync = setup_service_module.os.fsync
    real_replace = setup_service_module.os.replace

    def recording_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return descriptor, name

    def recording_fsync(descriptor: int) -> None:
        operations.append(("fsync", None))
        real_fsync(descriptor)

    def recording_replace(source: Path, target: Path) -> None:
        operations.append(("replace", Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(setup_service_module.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(setup_service_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(setup_service_module.os, "replace", recording_replace)

    SetupService(paths, FakeClaude()).run(RESUME, valid_answers())

    assert len(created) == 2
    assert created[0] != created[1]
    assert all(path.parent == paths.root for path in created)
    assert all(path.name.endswith(".tmp") for path in created)
    assert operations == [
        ("fsync", None),
        ("fsync", None),
        ("replace", paths.profile_md),
        ("replace", paths.config_toml),
    ]
    assert all(not path.exists() for path in created)


@pytest.mark.parametrize(
    ("stdout", "exit_code"),
    [
        (b'{"structured_output":{"profile_markdown":"ignored"}}', 9),
        (b"{not-json", 0),
        (b"[]", 0),
        (b"{}", 0),
        (b'{"structured_output":[]}', 0),
        (b'{"structured_output":{}}', 0),
        (
            b'{"structured_output":{"profile_markdown":"x","extra":true}}',
            0,
        ),
        (b'{"structured_output":{"profile_markdown":7}}', 0),
        (b'{"structured_output":{"profile_markdown":"   "}}', 0),
        (
            json.dumps(
                {
                    "structured_output": {
                        "profile_markdown": PROFILE.replace(
                            "# Languages\nEnglish, German B1\n\n", ""
                        )
                    }
                }
            ).encode(),
            0,
        ),
        (
            json.dumps(
                {
                    "structured_output": {
                        "profile_markdown": PROFILE.replace(
                            "# Target roles\nBackend Engineer",
                            "# Target roles\n\n# Unrelated\nBackend Engineer",
                        )
                    }
                }
            ).encode(),
            0,
        ),
        (b"\xff", 0),
        (
            json.dumps(
                {"structured_output": {"profile_markdown": PROFILE + "\ud800"}}
            ).encode(),
            0,
        ),
    ],
    ids=[
        "nonzero",
        "invalid-json",
        "non-object-result",
        "missing-structured-output",
        "non-object-structured-output",
        "missing-profile",
        "extra-profile-field",
        "wrong-profile-type",
        "blank-profile",
        "missing-heading",
        "empty-heading",
        "invalid-utf8",
        "invalid-profile-unicode",
    ],
)
def test_invalid_claude_results_preserve_old_pair_and_hide_private_streams(
    tmp_path: Path,
    stdout: bytes,
    exit_code: int,
) -> None:
    paths = paths_at(tmp_path)
    seed_old_pair(paths)

    with pytest.raises(SetupError) as captured:
        SetupService(paths, FakeClaude(stdout=stdout, exit_code=exit_code)).run(
            RESUME, valid_answers()
        )

    assert_old_pair_unchanged(paths)
    rendered = f"{captured.value!s}\n{captured.value!r}"
    assert PRIVATE_MARKER not in rendered
    assert "not-json" not in rendered


def test_nonzero_claude_result_reports_known_safe_failure_reason(tmp_path: Path) -> None:
    paths = paths_at(tmp_path)
    fake = FakeClaude(
        exit_code=1,
        stderr=b"Error: configured model is not available for this account",
    )

    with pytest.raises(SetupError) as captured:
        SetupService(paths, fake).run(RESUME, valid_answers())

    assert "configured Claude model is unavailable" in str(captured.value)
    assert not paths.profile_md.exists()
    assert not paths.config_toml.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "missing-resume",
        "claude-timeout",
        "deep-config-validation",
    ],
)
def test_prepublication_failures_preserve_old_pair(
    tmp_path: Path,
    failure: str,
) -> None:
    paths = paths_at(tmp_path)
    seed_old_pair(paths)
    resume_path = RESUME
    answers = valid_answers()
    fake = FakeClaude()
    if failure == "missing-resume":
        resume_path = tmp_path / "missing.docx"
    elif failure == "claude-timeout":
        fake = FakeClaude(error=ClaudeTimeout("safe controlled timeout"))
    else:
        answers.search_terms.clear()

    with pytest.raises((SetupError, ClaudeTimeout, ResumeError)):
        SetupService(paths, fake).run(resume_path, answers)

    assert_old_pair_unchanged(paths)


@pytest.mark.parametrize("write_number", [1, 2])
def test_first_or_second_staging_write_failure_preserves_old_pair_and_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_number: int,
) -> None:
    paths = paths_at(tmp_path)
    seed_old_pair(paths)
    real_fdopen = setup_service_module.os.fdopen
    opened = 0

    class FailingWriter:
        def __init__(self, wrapped: BinaryIO, fail: bool) -> None:
            self.wrapped = wrapped
            self.fail = fail

        def __enter__(self) -> Self:
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self.wrapped.__exit__(*args)

        def write(self, payload: bytes) -> int:
            if self.fail:
                raise OSError("controlled write failure")
            return self.wrapped.write(payload)

        def flush(self) -> None:
            self.wrapped.flush()

        def fileno(self) -> int:
            return self.wrapped.fileno()

    def injected_fdopen(*args: Any, **kwargs: Any) -> FailingWriter:
        nonlocal opened
        opened += 1
        return FailingWriter(real_fdopen(*args, **kwargs), opened == write_number)

    monkeypatch.setattr(setup_service_module.os, "fdopen", injected_fdopen)

    with pytest.raises(SetupError):
        SetupService(paths, FakeClaude()).run(RESUME, valid_answers())

    assert_old_pair_unchanged(paths)


@pytest.mark.parametrize("replace_number", [1, 2])
@pytest.mark.parametrize("old_pair_exists", [False, True])
def test_pair_publication_failure_rolls_back_both_targets_and_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_number: int,
    old_pair_exists: bool,
) -> None:
    paths = paths_at(tmp_path)
    paths.root.mkdir(parents=True)
    if old_pair_exists:
        seed_old_pair(paths)
    real_replace = setup_service_module.os.replace
    replace_calls = 0

    def replace_then_fail(source: Path, target: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        real_replace(source, target)
        if replace_calls == replace_number:
            raise OSError("controlled replace failure after mutation")

    monkeypatch.setattr(setup_service_module.os, "replace", replace_then_fail)

    with pytest.raises(SetupError):
        SetupService(paths, FakeClaude()).run(RESUME, valid_answers())

    if old_pair_exists:
        assert_old_pair_unchanged(paths)
    else:
        assert not paths.profile_md.exists()
        assert not paths.config_toml.exists()
        assert not list(paths.root.glob(".*.tmp"))
        assert not list(paths.root.glob(".*.bak"))


def test_setup_models_forbid_private_or_derived_fields() -> None:
    answers = valid_answers()
    answer_data = answers.model_dump()
    assert set(SetupAnswers.model_fields) == {
        "candidate_name",
        "ai_runtime",
        "search_terms",
        "locations",
        "posted_within_days",
        "arbeitsagentur_enabled",
        "target_companies",
        "linkedin_enabled",
        "linkedin_limit",
        "indeed_de_enabled",
        "indeed_de_limit",
        "stepstone_de_enabled",
        "stepstone_de_limit",
        "glassdoor_de_enabled",
        "glassdoor_de_limit",
        "simplify_de_enabled",
        "simplify_de_limit",
        "minimum_company_size",
        "german_level",
        "staffing_penalty",
        "claude",
        "scheduler",
    }
    for forbidden in (
        "country",
        "needs_visa_sponsorship",
        "resume_path",
        "resume_sha256",
        "profile_sha256",
    ):
        with pytest.raises(ValidationError):
            SetupAnswers.model_validate({**answer_data, forbidden: "forbidden"})

    legacy_answer_data = {**answer_data, "staffing_penalty": 23}
    assert SetupAnswers.model_validate(legacy_answer_data).staffing_penalty == 10

    result_data = {
        "config": app_config(),
        "profile_path": Path("profile.md"),
        "profile_hash": PROFILE_HASH,
    }
    assert set(SetupResult.model_fields) == {
        "config",
        "profile_path",
        "profile_hash",
    }
    for forbidden in ("resume_text", "resume_bytes", "stdout", "stderr"):
        with pytest.raises(ValidationError):
            SetupResult.model_validate({**result_data, forbidden: PRIVATE_MARKER})
