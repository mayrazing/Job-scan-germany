from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.sources import linkedin as linkedin_module
from job_scan.sources import run_source
from job_scan.sources.linkedin import LinkedinAdapter


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Java Backend Engineer"],
        "locations": [],
        "german_level": "A2",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(local_time="08:00"),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def fake_opencli(
    tmp_path: Path,
    payload: object,
    *,
    expected_limit: str = "2",
    expected_date_posted: str | None = "week",
) -> Path:
    executable = tmp_path / "opencli"
    expected_args = [
        "linkedin",
        "search",
        "Java Backend Engineer",
        "--location",
        "Germany",
        "--limit",
        expected_limit,
        "--details",
    ]
    if expected_date_posted is not None:
        expected_args.extend(["--date-posted", expected_date_posted])
    expected_args.extend(["-f", "json", "--site-session", "persistent"])
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import sys",
            f"expected = {expected_args!r}",
            "if sys.argv[1:] != expected:",
            "    print(json.dumps({'actual': sys.argv[1:]}), file=sys.stderr)",
            "    raise SystemExit(78)",
            f"print({json.dumps(json.dumps(payload))})",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def exiting_opencli(tmp_path: Path, exit_code: int) -> Path:
    executable = tmp_path / "opencli"
    executable.write_text(
        f"#!{sys.executable}\nraise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def partially_failing_opencli(tmp_path: Path) -> Path:
    executable = tmp_path / "opencli"
    payload = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": "Build Java services.",
            "apply_url": None,
            "detail_error": None,
        }
    ]
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import sys",
                f"payload = {payload!r}",
                "if sys.argv[3] == 'second query':",
                "    raise SystemExit(75)",
                "print(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def challenge_then_success_opencli(tmp_path: Path) -> tuple[Path, Path, Path]:
    executable = tmp_path / "opencli-linkedin-challenge"
    attempts_path = tmp_path / "opencli-linkedin-attempts"
    events_path = tmp_path / "opencli-linkedin-events"
    probes_path = tmp_path / "opencli-linkedin-probes"
    payload = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": "Build Java services.",
            "apply_url": None,
            "detail_error": None,
        }
    ]
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"attempts = pathlib.Path({str(attempts_path)!r})",
            f"events = pathlib.Path({str(events_path)!r})",
            f"probes = pathlib.Path({str(probes_path)!r})",
            f"payload = {payload!r}",
            "args = sys.argv[1:]",
            "if args[:2] == ['browser', 'site:linkedin'] and args[2:] == ['close']:",
            "    with events.open('a') as log: log.write('close\\n')",
            "    raise SystemExit(0)",
            "if args[:3] == ['browser', 'site:linkedin', 'eval']:",
            "    with events.open('a') as log: log.write('eval\\n')",
            "    count = int(probes.read_text()) + 1 if probes.exists() else 1",
            "    probes.write_text(str(count))",
            "    status = 'challenge' if count == 1 else 'ok'",
            "    print(json.dumps({'status': status}))",
            "    raise SystemExit(0)",
            "if '--site-session' not in args or args[args.index('--site-session') + 1] != 'persistent':",
            "    raise SystemExit(78)",
            "with events.open('a') as log: log.write('search\\n')",
            "count = int(attempts.read_text()) + 1 if attempts.exists() else 1",
            "attempts.write_text(str(count))",
            "if count == 1:",
            "    raise SystemExit(77)",
            "print(json.dumps(payload))",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, attempts_path, events_path


def uncleared_challenge_opencli(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "opencli-linkedin-uncleared-challenge"
    events_path = tmp_path / "opencli-linkedin-events"
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"events = pathlib.Path({str(events_path)!r})",
            "args = sys.argv[1:]",
            "if args[:2] == ['browser', 'site:linkedin'] and args[2:] == ['close']:",
            "    with events.open('a') as log: log.write('close\\n')",
            "    raise SystemExit(0)",
            "if args[:3] == ['browser', 'site:linkedin', 'eval']:",
            "    with events.open('a') as log: log.write('eval\\n')",
            "    print(json.dumps({'status': 'challenge'}))",
            "    raise SystemExit(0)",
            "with events.open('a') as log: log.write(f'search:{args[2]}\\n')",
            "raise SystemExit(77)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, events_path


def partially_invalid_opencli(tmp_path: Path) -> Path:
    executable = tmp_path / "opencli"
    payload = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": "Build Java services.",
            "apply_url": None,
            "detail_error": None,
        }
    ]
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import sys",
                f"payload = {payload!r}",
                "if sys.argv[3] == 'second query':",
                "    print('not-json')",
                "else:",
                "    print(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def duplicate_opencli(tmp_path: Path) -> Path:
    executable = tmp_path / "opencli"
    incomplete = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": None,
            "apply_url": None,
            "detail_error": "fetch failed",
        }
    ]
    complete = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": "Complete Java and Spring Boot job description.",
            "apply_url": "https://jobs.example.com/apply/4423914728",
            "detail_error": None,
        }
    ]
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import sys",
            f"incomplete = {incomplete!r}",
            f"complete = {complete!r}",
            "print(json.dumps(incomplete if sys.argv[3] == 'first query' else complete))",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def company_size_opencli(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "opencli-company-size"
    calls_path = tmp_path / "opencli-company-size-calls"
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"calls = pathlib.Path({str(calls_path)!r})",
            "args = sys.argv[1:]",
            "with calls.open('a', encoding='utf-8') as output:",
            "    output.write(json.dumps(args) + '\\n')",
            "if args[:2] == ['linkedin', 'job-detail']:",
            "    print(json.dumps([{'company_url': 'https://www.linkedin.com/company/airbus-helicopters/life'}]))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'open':",
            "    print(json.dumps({'url': args[3]}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'eval':",
            "    print(json.dumps({'status': 'ok', 'reported_size': '10,001+ employees'}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'close':",
            "    print('Browser session tab lease released')",
            "else:",
            "    raise SystemExit(78)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, calls_path


def challenge_company_size_opencli(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "opencli-company-size-challenge"
    challenge_path = tmp_path / "opencli-company-size-challenge-seen"
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"challenge = pathlib.Path({str(challenge_path)!r})",
            "args = sys.argv[1:]",
            "if args[:2] == ['linkedin', 'job-detail']:",
            "    print(json.dumps([{'company_url': 'https://www.linkedin.com/company/airbus-helicopters/life'}]))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'open':",
            "    print(json.dumps({'url': args[3]}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'eval':",
            "    if not challenge.exists():",
            "        challenge.write_text('seen')",
            "        print(json.dumps({'status': 'challenge', 'reported_size': ''}))",
            "    else:",
            "        print(json.dumps({'status': 'ok', 'reported_size': '10,001+ employees'}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'close':",
            "    print('Browser session tab lease released')",
            "else:",
            "    raise SystemExit(78)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, challenge_path


def challenge_job_detail_opencli(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "opencli-job-detail-challenge"
    attempts_path = tmp_path / "opencli-job-detail-attempts"
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"attempts = pathlib.Path({str(attempts_path)!r})",
            "args = sys.argv[1:]",
            "if args[:2] == ['browser', 'site:linkedin'] and args[2:] == ['close']:",
            "    raise SystemExit(0)",
            "if args[:2] == ['linkedin', 'job-detail']:",
            "    if '--site-session' not in args or args[args.index('--site-session') + 1] != 'persistent':",
            "        raise SystemExit(78)",
            "    count = int(attempts.read_text()) + 1 if attempts.exists() else 1",
            "    attempts.write_text(str(count))",
            "    if count == 1:",
            "        raise SystemExit(77)",
            "    print(json.dumps([{'company_url': 'https://www.linkedin.com/company/airbus-helicopters/life'}]))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'open':",
            "    print(json.dumps({'url': args[3]}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'eval':",
            "    print(json.dumps({'status': 'ok', 'reported_size': '10,001+ employees'}))",
            "elif len(args) >= 3 and args[0] == 'browser' and args[2] == 'close':",
            "    print('Browser session tab lease released')",
            "else:",
            "    raise SystemExit(78)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, attempts_path


def test_discover_maps_opencli_search_details_into_source_occurrences(
    tmp_path: Path,
) -> None:
    executable = fake_opencli(
        tmp_path,
        [
            {
                "rank": 1,
                "title": "Java Backend Engineer",
                "company": "Example GmbH",
                "location": "Berlin, Germany (Hybrid)",
                "listed": "2026-08-03",
                "salary": "",
                "url": "https://www.linkedin.com/jobs/view/4423914728",
                "description": "Build Java and Spring Boot services.",
                "apply_url": "https://jobs.example.com/4423914728",
                "detail_error": None,
            }
        ],
    )
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    references = linkedin.discover()
    occurrence = linkedin.fetch_detail(references[0])

    assert len(references) == 1
    assert references[0].source is SourceKind.LINKEDIN
    assert references[0].source_instance == "default"
    assert references[0].external_id == "4423914728"
    assert references[0].listing_posted_at == date(2026, 8, 3)
    assert str(references[0].detail_url) == "https://www.linkedin.com/jobs/view/4423914728"
    assert occurrence.source is SourceKind.LINKEDIN
    assert occurrence.external_id == "4423914728"
    assert str(occurrence.url) == "https://www.linkedin.com/jobs/view/4423914728"
    assert occurrence.description == "Build Java and Spring Boot services."
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    source = occurrence.company_industry_source
    assert source is not None
    assert source.source_name == "linkedin"
    assert str(source.lookup_url) == "https://www.linkedin.com/jobs/view/4423914728"


def test_linkedin_company_about_page_returns_company_size(tmp_path: Path) -> None:
    from job_scan.sources.linkedin import lookup_company_size

    executable, calls_path = company_size_opencli(tmp_path)

    result = lookup_company_size(
        "4423914728",
        "Airbus Helicopters",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.reported_size == "10,001+ employees"
    assert result.minimum_employees == 10001
    assert result.maximum_employees is None
    assert result.source_name == "linkedin"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    company_open = next(call for call in calls if call[:3:2] == ["browser", "open"])
    assert [
        "browser",
        company_open[1],
        "open",
        "https://www.linkedin.com/company/airbus-helicopters/about/",
        "--window",
        "background",
    ] in calls


def test_linkedin_company_page_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan.sources.linkedin import lookup_company_size

    executable, challenge_path = challenge_company_size_opencli(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = lookup_company_size(
        "4423914728",
        "Airbus Helicopters",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.minimum_employees == 10001
    assert challenge_path.exists()


def test_linkedin_job_detail_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan.sources.linkedin import lookup_company_size

    executable, attempts_path = challenge_job_detail_opencli(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = lookup_company_size(
        "4423914728",
        "Airbus Helicopters",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.minimum_employees == 10001
    assert attempts_path.read_text() == "2"


def test_linkedin_company_page_script_reads_company_size() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.linkedin import _COMPANY_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<main><div>Company size</div><div>10,001+ employees</div></main>"
        )

        payload = page.evaluate(_COMPANY_PAGE_JS)

        assert payload == {"status": "ok", "reported_size": "10,001+ employees"}
        browser.close()


def test_discover_passes_the_configured_limit_to_opencli(tmp_path: Path) -> None:
    executable = fake_opencli(tmp_path, [], expected_limit="75")
    linkedin = LinkedinAdapter(
        config(linkedin_limit=75),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert linkedin.discover() == []


def test_search_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, attempts_path, events_path = challenge_then_success_opencli(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(linkedin)

    assert [occurrence.external_id for occurrence in result.occurrences] == ["4423914728"]
    assert result.completed_listing is True
    assert attempts_path.read_text() == "2"
    assert events_path.read_text().splitlines() == [
        "search",
        "eval",
        "eval",
        "search",
        "close",
    ]


def test_search_challenge_timeout_stops_before_next_query(tmp_path: Path) -> None:
    executable, events_path = uncleared_challenge_opencli(tmp_path)
    linkedin = LinkedinAdapter(
        config(search_terms=["first query", "second query"]),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0.001,
    )

    result = run_source(linkedin)

    assert result.occurrences == []
    assert [error.error_code for error in result.errors] == ["linkedin_challenge"]
    assert events_path.read_text().splitlines() == [
        "search:first query",
        "eval",
        "close",
    ]


@pytest.mark.parametrize(
    ("posted_within_days", "expected_date_posted"),
    [
        (0, "24h"),
        (1, "24h"),
        (3, "week"),
        (7, "week"),
        (14, "month"),
        (None, None),
    ],
)
def test_discover_filters_linkedin_before_applying_the_result_limit(
    tmp_path: Path,
    posted_within_days: int | None,
    expected_date_posted: str | None,
) -> None:
    executable = fake_opencli(
        tmp_path,
        [],
        expected_date_posted=expected_date_posted,
    )
    linkedin = LinkedinAdapter(
        config(posted_within_days=posted_within_days),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    assert linkedin.discover() == []


def test_one_failed_query_keeps_successful_linkedin_results(
    tmp_path: Path,
) -> None:
    linkedin = LinkedinAdapter(
        config(search_terms=["first query", "second query"]),
        opencli_executable=partially_failing_opencli(tmp_path),
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(linkedin)

    assert result.completed_listing is False
    assert [item.external_id for item in result.occurrences] == ["4423914728"]
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "opencli_timeout"


def test_one_invalid_query_keeps_successful_linkedin_results(
    tmp_path: Path,
) -> None:
    linkedin = LinkedinAdapter(
        config(search_terms=["first query", "second query"]),
        opencli_executable=partially_invalid_opencli(tmp_path),
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(linkedin)

    assert result.completed_listing is False
    assert [item.external_id for item in result.occurrences] == ["4423914728"]
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].error_code == "invalid_response"


def test_missing_opencli_detail_keeps_listing_as_incomplete(tmp_path: Path) -> None:
    executable = fake_opencli(
        tmp_path,
        [
            {
                "rank": 1,
                "title": "Java Backend Engineer",
                "company": "Example GmbH",
                "location": "Germany",
                "listed": "yesterday",
                "salary": "",
                "url": "https://www.linkedin.com/jobs/view/4423914728",
                "description": None,
                "apply_url": None,
                "detail_error": "fetch failed: job panel did not load",
            }
        ],
    )
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    reference = linkedin.discover()[0]
    occurrence = linkedin.fetch_detail(reference)

    assert reference.listing_posted_at is None
    assert occurrence.description == ""
    assert occurrence.detail_complete is False
    assert occurrence.fetch_error_code == "linkedin_detail_failed"
    assert str(occurrence.url) == "https://www.linkedin.com/jobs/view/4423914728"


def test_auth_exit_becomes_actionable_isolated_source_error(tmp_path: Path) -> None:
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=exiting_opencli(tmp_path, 77),
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(linkedin)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "linkedin_auth_required"
    assert result.errors[0].message == (
        "LinkedIn login is required in the connected Chrome profile."
    )


def test_linkedin_keeps_platform_url_while_parsing_company_application_url(
    tmp_path: Path,
) -> None:
    executable = fake_opencli(
        tmp_path,
        [
            {
                "rank": 1,
                "title": "Java Backend Engineer",
                "company": "Example GmbH",
                "location": "Germany",
                "listed": "2026-08-03",
                "salary": "",
                "url": "https://www.linkedin.com/jobs/view/4423914728",
                "description": "Build Java services.",
                "apply_url": (
                    "https://www.linkedin.com/safety/go/"
                    "?url=https%3A%2F%2Fjobs.example.com%2Fapply%2F4423914728"
                    "&urlhash=example"
                ),
                "detail_error": None,
            }
        ],
    )
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    reference = linkedin.discover()[0]
    occurrence = linkedin.fetch_detail(reference)

    assert str(reference.listing_application_url) == (
        "https://jobs.example.com/apply/4423914728"
    )
    assert str(occurrence.url) == "https://www.linkedin.com/jobs/view/4423914728"


def test_malformed_row_is_isolated_without_discarding_valid_rows(tmp_path: Path) -> None:
    executable = fake_opencli(
        tmp_path,
        [
            {
                "rank": 1,
                "company": "Broken GmbH",
                "location": "Germany",
                "url": "https://www.linkedin.com/jobs/view/1111111111",
            },
            {
                "rank": 2,
                "title": "Java Backend Engineer",
                "company": "Example GmbH",
                "location": "Germany",
                "listed": "2026-08-03",
                "salary": "",
                "url": "https://www.linkedin.com/jobs/view/4423914728",
                "description": "Build Java services.",
                "apply_url": None,
                "detail_error": None,
            },
        ],
    )
    linkedin = LinkedinAdapter(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(linkedin)

    assert result.completed_listing is True
    assert [item.external_id for item in result.occurrences] == ["4423914728"]
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].error_code == "invalid_response"
    assert result.errors[0].item_key == "linkedin:default:1111111111"
    assert result.discovered_source_job_keys == {
        "linkedin:default:1111111111",
        "linkedin:default:4423914728",
    }


def test_duplicate_search_result_keeps_the_more_complete_detail(tmp_path: Path) -> None:
    linkedin = LinkedinAdapter(
        config(search_terms=["first query", "second query"]),
        opencli_executable=duplicate_opencli(tmp_path),
        limit=2,
        timeout_seconds=5,
    )

    references = linkedin.discover()
    occurrence = linkedin.fetch_detail(references[0])

    assert len(references) == 1
    assert occurrence.detail_complete is True
    assert occurrence.description == "Complete Java and Spring Boot job description."
    assert str(occurrence.url) == "https://www.linkedin.com/jobs/view/4423914728"


def test_scheduled_scan_uses_persisted_opencli_and_node_runtime_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = [
        {
            "rank": 1,
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Germany",
            "listed": "2026-08-03",
            "salary": "",
            "url": "https://www.linkedin.com/jobs/view/4423914728",
            "description": "Build Java services.",
            "apply_url": None,
            "detail_error": None,
        }
    ]
    runtime_bin = tmp_path / "node-bin"
    runtime_bin.mkdir()
    node = runtime_bin / "node"
    node.write_text(
        f"#!{sys.executable}\nimport json\nprint(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    node.chmod(0o755)
    executable = tmp_path / "opencli"
    executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("JOB_SCAN_OPENCLI", str(executable))
    monkeypatch.setenv("PATH", str(runtime_bin))
    monkeypatch.setattr(linkedin_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(linkedin_module.sys, "executable", "/isolated/python")

    linkedin = LinkedinAdapter(config(), limit=2, timeout_seconds=5)

    assert linkedin.discover()[0].external_id == "4423914728"
