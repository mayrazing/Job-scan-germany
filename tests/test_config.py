from pathlib import Path
from typing import Any, Self, TextIO

import pytest
import tomli_w
from pydantic import ValidationError

from job_scan import config as config_module
from job_scan.config import (
    AppConfig,
    ClaudeSettings,
    SchedulerSettings,
    load_config,
    save_config,
)


def valid_config() -> AppConfig:
    return AppConfig(
        resume_path=Path("/tmp/resume.pdf"),
        resume_sha256="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        profile_sha256="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        search_terms=["backend engineer"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="medium",
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


@pytest.fixture
def created_temporary_paths(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    created_paths: list[Path] = []
    real_mkstemp = config_module.tempfile.mkstemp

    def recording_mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        file_descriptor, temporary_name = real_mkstemp(*args, **kwargs)
        created_paths.append(Path(temporary_name))
        return file_descriptor, temporary_name

    monkeypatch.setattr(config_module.tempfile, "mkstemp", recording_mkstemp)
    return created_paths


def test_config_rejects_non_german_country(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config_data = valid_config().model_dump(mode="json", exclude_none=True)
    config_data["country"] = "SE"
    path.write_text(tomli_w.dumps(config_data), encoding="utf-8")

    with pytest.raises(ValidationError) as error:
        load_config(path)

    assert [item["loc"] for item in error.value.errors()] == [("country",)]


def test_existing_config_defaults_to_claude_code_runtime() -> None:
    data = valid_config().model_dump()
    data.pop("ai_runtime", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.ai_runtime == "claude-code"


def test_existing_config_defaults_to_seven_day_posting_window() -> None:
    data = valid_config().model_dump()
    data.pop("posted_within_days", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.posted_within_days == 7


def test_existing_config_defaults_to_arbeitsagentur_enabled() -> None:
    data = valid_config().model_dump()
    data.pop("arbeitsagentur_enabled", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.arbeitsagentur_enabled is True


def test_existing_config_defaults_to_no_target_companies() -> None:
    data = valid_config().model_dump()
    data.pop("target_companies", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.target_companies == []


def test_config_accepts_bosch_as_a_target_company() -> None:
    data = valid_config().model_dump()
    data["target_companies"] = ["bosch"]

    loaded = AppConfig.model_validate(data)

    assert loaded.target_companies == ["bosch"]


def test_config_accepts_connected_target_companies() -> None:
    data = valid_config().model_dump()
    data["target_companies"] = [
        "bosch",
        "telekom",
        "rohde-schwarz",
        "siemens",
        "dhl",
        "thyssenkrupp",
        "dallmeier",
    ]

    loaded = AppConfig.model_validate(data)

    assert loaded.target_companies == [
        "bosch",
        "telekom",
        "rohde-schwarz",
        "siemens",
        "dhl",
        "thyssenkrupp",
        "dallmeier",
    ]


def test_config_rejects_unconnected_target_company() -> None:
    data = valid_config().model_dump()
    data["target_companies"] = ["acme"]

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_existing_config_defaults_to_claude_thinking_enabled() -> None:
    data = valid_config().model_dump()
    data["claude"].pop("thinking_enabled", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.claude.thinking_enabled is True


def test_existing_config_defaults_to_no_company_size_limit() -> None:
    data = valid_config().model_dump()
    data.pop("minimum_company_size", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.minimum_company_size == 0


def test_existing_config_defaults_to_ten_point_staffing_penalty() -> None:
    data = valid_config().model_dump()
    data.pop("staffing_penalty", None)

    loaded = AppConfig.model_validate(data)

    assert loaded.staffing_penalty == 10


@pytest.mark.parametrize("value", [-1, 1, 49, 100, 500, 9999])
def test_config_rejects_unsupported_company_size_thresholds(value: int) -> None:
    data = valid_config().model_dump()
    data["minimum_company_size"] = value

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_accepts_three_day_posting_window() -> None:
    data = valid_config().model_dump()
    data["posted_within_days"] = 3

    loaded = AppConfig.model_validate(data)

    assert loaded.posted_within_days == 3


@pytest.mark.parametrize("value", [2, 4, 30, 100])
def test_config_rejects_unsupported_posting_windows(value: int) -> None:
    data = valid_config().model_dump()
    data["posted_within_days"] = value

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_rejects_empty_search_terms() -> None:
    data = valid_config().model_dump()
    data["search_terms"] = []

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_has_no_target_lanes_field() -> None:
    assert "target_lanes" not in AppConfig.model_fields


def test_config_has_no_companies_field() -> None:
    """Company sources were removed; only keyword discovery sites remain."""
    assert "companies" not in AppConfig.model_fields


def test_config_has_no_radius_or_work_mode_fields() -> None:
    assert "radius_km" not in AppConfig.model_fields
    assert "remote_preference" not in AppConfig.model_fields
    data = valid_config().model_dump()
    data.update(radius_km=50, remote_preference="hybrid")

    loaded = AppConfig.model_validate(data)

    assert "radius_km" not in loaded.model_dump()
    assert "remote_preference" not in loaded.model_dump()


@pytest.mark.parametrize("field", ["resume_sha256", "profile_sha256"])
@pytest.mark.parametrize(
    "invalid_hash",
    ["", "abc", "sha256:ABCDEF", "sha256:" + ("a" * 63), "sha512:" + ("a" * 64)],
)
def test_config_rejects_invalid_persisted_hashes(
    field: str,
    invalid_hash: str,
) -> None:
    data = valid_config().model_dump()
    data[field] = invalid_hash

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("local_time", ["8:30", "08:3", "08:30:00", "24:00", "12:60"])
def test_scheduler_rejects_invalid_local_time(local_time: str) -> None:
    with pytest.raises(ValidationError):
        SchedulerSettings(local_time=local_time)


def test_legacy_config_without_linkedin_limit_defaults_to_10(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data.pop("linkedin_limit")
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    assert load_config(path).linkedin_limit == 10


def test_legacy_config_without_indeed_de_limit_defaults_to_10(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data.pop("indeed_de_limit", None)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    assert load_config(path).indeed_de_limit == 10


def test_legacy_config_without_stepstone_de_limit_defaults_to_10(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data.pop("stepstone_de_limit", None)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    assert load_config(path).stepstone_de_limit == 10


def test_legacy_config_without_glassdoor_de_limit_defaults_to_10(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data.pop("glassdoor_de_limit", None)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    assert load_config(path).glassdoor_de_limit == 10


def test_legacy_config_without_simplify_de_limit_defaults_to_10(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data.pop("simplify_de_limit", None)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    assert load_config(path).simplify_de_limit == 10


def test_legacy_config_derives_opencli_switches_from_zero_limits(
    tmp_path: Path,
) -> None:
    data = valid_config().model_dump(mode="json", exclude_none=True)
    data["linkedin_limit"] = 0
    data["indeed_de_limit"] = 0
    data["stepstone_de_limit"] = 0
    data["glassdoor_de_limit"] = 0
    data["simplify_de_limit"] = 0
    for field in (
        "linkedin_enabled",
        "indeed_de_enabled",
        "stepstone_de_enabled",
        "glassdoor_de_enabled",
        "simplify_de_enabled",
    ):
        data.pop(field, None)
    path = tmp_path / "config.toml"
    path.write_text(tomli_w.dumps(data), encoding="utf-8")

    loaded = load_config(path)

    assert loaded.linkedin_enabled is False
    assert loaded.indeed_de_enabled is False
    assert loaded.stepstone_de_enabled is False
    assert loaded.glassdoor_de_enabled is False
    assert loaded.simplify_de_enabled is False
    assert loaded.linkedin_limit == 10
    assert loaded.indeed_de_limit == 10
    assert loaded.stepstone_de_limit == 10
    assert loaded.glassdoor_de_limit == 10
    assert loaded.simplify_de_limit == 10


def test_config_persists_opencli_switch_separately_from_limit(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = valid_config().model_copy(update={"linkedin_enabled": False, "linkedin_limit": 37})

    save_config(path, config)

    loaded = load_config(path)
    assert loaded.linkedin_enabled is False
    assert loaded.linkedin_limit == 37


@pytest.mark.parametrize("linkedin_limit", [0, 101])
def test_config_rejects_linkedin_limit_outside_opencli_bounds(
    linkedin_limit: int,
) -> None:
    data = valid_config().model_dump()
    data["linkedin_limit"] = linkedin_limit

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("indeed_de_limit", [0, 101])
def test_config_rejects_indeed_de_limit_outside_opencli_bounds(
    indeed_de_limit: int,
) -> None:
    data = valid_config().model_dump()
    data["indeed_de_limit"] = indeed_de_limit

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("stepstone_de_limit", [0, 101])
def test_config_rejects_stepstone_de_limit_outside_opencli_bounds(
    stepstone_de_limit: int,
) -> None:
    data = valid_config().model_dump()
    data["stepstone_de_limit"] = stepstone_de_limit

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("glassdoor_de_limit", [0, 101])
def test_config_rejects_glassdoor_de_limit_outside_opencli_bounds(
    glassdoor_de_limit: int,
) -> None:
    data = valid_config().model_dump()
    data["glassdoor_de_limit"] = glassdoor_de_limit

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


@pytest.mark.parametrize("simplify_de_limit", [0, 101])
def test_config_rejects_simplify_de_limit_outside_opencli_bounds(
    simplify_de_limit: int,
) -> None:
    data = valid_config().model_dump()
    data["simplify_de_limit"] = simplify_de_limit

    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_config_round_trips_without_a_schedule_time(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = valid_config().model_copy(
        update={"scheduler": SchedulerSettings()}
    )

    save_config(path, config)

    assert load_config(path) == config
    assert "local_time" not in path.read_text(encoding="utf-8")


def test_config_round_trips_explicit_no_posting_window(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = valid_config().model_copy(update={"posted_within_days": None})

    save_config(path, config)

    assert load_config(path) == config


def test_save_config_uses_unique_sibling_temporary_files(
    tmp_path: Path,
    created_temporary_paths: list[Path],
) -> None:
    path = tmp_path / "config.toml"
    config = valid_config()

    save_config(path, config)
    save_config(path, config)

    assert load_config(path) == config
    assert len(created_temporary_paths) == 2
    assert created_temporary_paths[0] != created_temporary_paths[1]
    assert all(temporary.parent == path.parent for temporary in created_temporary_paths)
    assert all(
        temporary.name.startswith(f".{path.name}.")
        and temporary.name.endswith(".tmp")
        for temporary in created_temporary_paths
    )
    assert all(not temporary.exists() for temporary in created_temporary_paths)


def test_save_config_fsyncs_before_replacing_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_temporary_paths: list[Path],
) -> None:
    path = tmp_path / "config.toml"
    operations: list[str] = []
    replace_calls: list[tuple[Path, Path]] = []
    real_fsync = config_module.os.fsync
    real_replace = config_module.os.replace

    def recording_fsync(file_descriptor: int) -> None:
        operations.append("fsync")
        real_fsync(file_descriptor)

    def recording_replace(source: Path, target: Path) -> None:
        operations.append("replace")
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(config_module.os, "fsync", recording_fsync)
    monkeypatch.setattr(config_module.os, "replace", recording_replace)

    save_config(path, valid_config())

    assert operations == ["fsync", "replace"]
    assert replace_calls == [(created_temporary_paths[0], path)]
    assert load_config(path) == valid_config()


@pytest.mark.parametrize(
    ("mutation", "target_exists"),
    [
        (lambda config: setattr(config, "country", "SE"), True),
        (lambda config: config.search_terms.clear(), False),
    ],
    ids=["direct-field", "required-list"],
)
def test_save_config_deep_revalidates_before_creating_or_publishing_temp(
    tmp_path: Path,
    created_temporary_paths: list[Path],
    mutation: Any,
    target_exists: bool,
) -> None:
    path = tmp_path / "config.toml"
    if target_exists:
        path.write_text("original", encoding="utf-8")
    config = valid_config()
    mutation(config)

    with pytest.raises(ValidationError):
        save_config(path, config)

    assert created_temporary_paths == []
    if target_exists:
        assert path.read_text(encoding="utf-8") == "original"
    else:
        assert not path.exists()


def test_save_config_cleans_temp_and_preserves_target_when_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_temporary_paths: list[Path],
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("original", encoding="utf-8")
    real_fdopen = config_module.os.fdopen

    class WriteFailingFile:
        def __init__(self, wrapped: TextIO) -> None:
            self.wrapped = wrapped

        def __enter__(self) -> Self:
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self.wrapped.__exit__(*args)

        def write(self, _serialized: str) -> None:
            raise OSError("write failed")

    def failing_fdopen(*args: Any, **kwargs: Any) -> WriteFailingFile:
        return WriteFailingFile(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(config_module.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="write failed"):
        save_config(path, valid_config())

    assert len(created_temporary_paths) == 1
    assert not created_temporary_paths[0].exists()
    assert path.read_text(encoding="utf-8") == "original"


def test_save_config_cleans_temp_and_preserves_target_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_temporary_paths: list[Path],
) -> None:
    path = tmp_path / "config.toml"
    path.write_text("original", encoding="utf-8")

    def failing_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_config(path, valid_config())

    assert len(created_temporary_paths) == 1
    assert not created_temporary_paths[0].exists()
    assert path.read_text(encoding="utf-8") == "original"
