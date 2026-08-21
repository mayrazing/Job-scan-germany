from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import CompanySizeSource, SourceKind
from job_scan.sources import run_source


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Software Engineer"],
        "locations": ["Berlin"],
        "german_level": "A2",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(local_time="08:00"),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter_type():
    from job_scan.sources.stepstone import StepstoneDeAdapter

    return StepstoneDeAdapter


def test_search_url_supports_three_day_posting_window() -> None:
    from job_scan.sources.stepstone import _search_url

    assert "ag=age_3" in _search_url(
        config(posted_within_days=3),
        "Software Engineer",
        "Berlin",
        1,
    )


def search_url(
    query: str = "Software Engineer",
    location: str = "Berlin",
    *,
    page: int = 1,
) -> str:
    url = (
        "https://www.stepstone.de/jobs/"
        f"{query.casefold().replace(' ', '-')}/in-{location.casefold().replace(' ', '-')}"
        "?ag=age_7&sort=2"
    )
    return f"{url}&page={page}" if page > 1 else url


def detail_url(job_id: str) -> str:
    return (
        "https://www.stepstone.de/stellenangebote--Software-Engineer-"
        f"Berlin-Example-GmbH--{job_id}-inline.html"
    )


def company_url() -> str:
    return "https://www.stepstone.de/cmp/de/example-gmbh-12345/jobs"


def search_row(job_id: str) -> dict[str, str]:
    return {
        "id": job_id,
        "title": "Software Engineer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": detail_url(job_id),
        "company_url": company_url(),
    }


def search_payload(
    rows: list[dict[str, str]],
    *,
    card_count: int | None = None,
) -> dict[str, object]:
    return {
        "status": "ok",
        "rows": rows,
        "card_count": len(rows) if card_count is None else card_count,
    }


def search_preloaded_state_html(items: list[tuple[str, str]]) -> str:
    preloaded_state = {
        "app-unifiedResultlist": {
            "searchResults": {
                "items": [{"id": int(job_id), "section": section} for job_id, section in items],
            },
        },
    }
    return f"<script>window.__PRELOADED_STATE__ = {json.dumps(preloaded_state)};</script>"


def search_card_html(job_id: str) -> str:
    return f"""
    <article id="job-item-{job_id}" data-testid="job-item">
      <a data-at="company-logo" href="{company_url()}">Example</a>
      <a data-testid="job-item-title" href="{detail_url(job_id)}">
        Software Engineer
      </a>
      <span data-at="job-item-company-name">Example GmbH</span>
      <span data-at="job-item-location">Berlin</span>
    </article>
    """


def search_page_html(items: list[tuple[str, str]]) -> str:
    return search_preloaded_state_html(items) + "".join(
        search_card_html(job_id) for job_id, _section in items
    )


def detail_payload(
    job_id: str,
    *,
    description: str | None = None,
    organization_url: str | None = None,
) -> dict[str, object]:
    return {
        "status": "ok",
        "job": {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Software Engineer",
            "url": detail_url(job_id),
            "datePosted": "2026-08-04T07:04:22.767Z",
            "validThrough": "2026-09-04T07:04:22.767Z",
            "hiringOrganization": {
                "@type": "Organization",
                "name": "Example GmbH",
                "url": organization_url or company_url(),
            },
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressCountry": "DE",
                    "addressLocality": "Berlin",
                },
            },
            "description": description
            or "<h2>Über uns</h2><p>Build Java and Spring Boot services.</p>",
        },
    }


def fake_opencli(tmp_path: Path, pages: dict[str, object]) -> tuple[Path, Path, Path]:
    executable = tmp_path / "opencli"
    state_path = tmp_path / "opencli-active-url"
    opened_path = tmp_path / "opencli-opened-urls"
    responses_path = tmp_path / "opencli-responses.json"
    responses_path.write_text(json.dumps(pages), encoding="utf-8")
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"state = pathlib.Path({str(state_path)!r})",
            f"opened = pathlib.Path({str(opened_path)!r})",
            f"responses = pathlib.Path({str(responses_path)!r})",
            "args = sys.argv[1:]",
            "if len(args) < 3 or args[0] != 'browser':",
            "    raise SystemExit(78)",
            "command = args[2]",
            "if command == 'open':",
            "    if args[4:] != ['--window', 'background']:",
            "        raise SystemExit(78)",
            "    state.write_text(args[3], encoding='utf-8')",
            "    with opened.open('a', encoding='utf-8') as log:",
            "        log.write(args[3] + '\\n')",
            "    print(json.dumps({'url': args[3], 'page': 'fake'}))",
            "elif command == 'eval':",
            "    if not state.exists():",
            "        raise SystemExit(78)",
            "    url = state.read_text(encoding='utf-8')",
            "    pages = json.loads(responses.read_text(encoding='utf-8'))",
            "    if url not in pages:",
            "        raise SystemExit(66)",
            "    payload = pages[url]",
            "    if isinstance(payload, list):",
            "        if not payload:",
            "            raise SystemExit(66)",
            "        selected = payload.pop(0)",
            "        responses.write_text(json.dumps(pages), encoding='utf-8')",
            "        payload = selected",
            "    print(json.dumps(payload))",
            "elif command == 'close':",
            "    state.unlink(missing_ok=True)",
            "    print('Browser session tab lease released')",
            "else:",
            "    raise SystemExit(78)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, state_path, opened_path


def test_discover_paginates_to_page_two_until_stepstone_limit(tmp_path: Path) -> None:
    first_page = [search_row(str(14_000_000 + index)) for index in range(25)]
    second_page = [search_row(str(14_000_025 + index)) for index in range(10)]
    pages: dict[str, object] = {
        search_url(): search_payload(first_page),
        search_url(page=2): search_payload(second_page),
    }
    for row in first_page + second_page:
        pages[row["url"]] = detail_payload(row["id"])
    executable, state_path, opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(stepstone_de_limit=30),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [
        str(14_000_000 + index) for index in range(30)
    ]
    opened_urls = opened_path.read_text(encoding="utf-8").splitlines()
    assert opened_urls[:2] == [search_url(), search_url(page=2)]
    assert search_url(page=3) not in opened_urls
    assert not state_path.exists()


def test_pagination_uses_unfiltered_card_count_after_semantic_rows_are_removed(
    tmp_path: Path,
) -> None:
    first_page = [search_row(str(14_000_000 + index)) for index in range(20)]
    second_page = [search_row(str(14_000_020 + index)) for index in range(2)]
    pages: dict[str, object] = {
        search_url(): search_payload(first_page, card_count=25),
        search_url(page=2): search_payload(second_page),
    }
    for row in first_page + second_page:
        pages[row["url"]] = detail_payload(row["id"])
    executable, state_path, opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(stepstone_de_limit=22),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [
        str(14_000_000 + index) for index in range(22)
    ]
    assert opened_path.read_text(encoding="utf-8").splitlines()[:2] == [
        search_url(),
        search_url(page=2),
    ]
    assert not state_path.exists()


def test_pagination_keeps_first_page_results_when_following_page_is_empty(
    tmp_path: Path,
) -> None:
    first_row = search_row("14000000")
    pages: dict[str, object] = {
        search_url(): search_payload([first_row], card_count=25),
        search_url(page=2): search_payload([]),
        first_row["url"]: detail_payload(first_row["id"]),
    }
    executable, state_path, opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(stepstone_de_limit=2),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == ["14000000"]
    assert opened_path.read_text(encoding="utf-8").splitlines()[:2] == [
        search_url(),
        search_url(page=2),
    ]
    assert adapter.completed_listing is True
    assert not state_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "rows": []},
        {"status": "ok", "rows": [], "card_count": True},
        {"status": "ok", "rows": [], "card_count": -1},
        {"status": "ok", "rows": [search_row("14358591")], "card_count": 0},
    ],
)
def test_search_rejects_invalid_unfiltered_card_count(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): payload,
            detail_url("14358591"): detail_payload("14358591"),
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].error_code == "invalid_response"
    assert not state_path.exists()


def test_fetch_detail_returns_one_complete_stepstone_job_posting(tmp_path: Path) -> None:
    job_id = "14358591"
    description = (
        "<h2>Über uns</h2><p>We build cloud products.</p>"
        "<h2>Deine Aufgaben</h2><p>Own production services.</p>"
    )
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): detail_payload(job_id, description=description),
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert reference.source is SourceKind.STEPSTONE
    assert reference.source_instance == "de"
    assert occurrence.source is SourceKind.STEPSTONE
    assert occurrence.external_id == job_id
    assert occurrence.posted_at == date(2026, 8, 4)
    assert occurrence.description == (
        "Über uns\nWe build cloud products.\nDeine Aufgaben\nOwn production services."
    )
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    source = occurrence.company_size_source
    assert source is not None
    assert source.source_name == "stepstone"
    assert str(source.lookup_url) == company_url()
    industry_source = occurrence.company_industry_source
    assert industry_source is not None
    assert industry_source.source_name == "stepstone"
    assert str(industry_source.lookup_url) == company_url()
    assert not state_path.exists()


def test_new_stepstone_job_carries_transient_snapshot_html(tmp_path: Path) -> None:
    job_id = "14358591"
    snapshot_html = (
        '<!doctype html><html data-job-scan-snapshot="stepstone:de:14358591">'
        "<body>Software Engineer</body></html>"
    )
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): [
                detail_payload(job_id),
                {"status": "ok", "html": snapshot_html},
            ],
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        capture_snapshot=lambda _reference: True,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None
    assert not state_path.exists()


def test_stepstone_snapshot_failure_does_not_discard_the_job(tmp_path: Path) -> None:
    job_id = "14358591"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): [
                detail_payload(job_id),
                {"status": "unavailable", "error_code": "structure_mismatch"},
            ],
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        capture_snapshot=lambda _reference: True,
    )

    result = run_source(adapter)

    assert len(result.occurrences) == 1
    assert result.occurrences[0].detail_complete is True
    assert result.occurrences[0].job_snapshot_html is None
    assert result.occurrences[0].job_snapshot_error_code == "snapshot_capture_failed"
    assert result.errors == []
    assert not state_path.exists()


def test_detail_falls_back_to_listing_stepstone_company_page(
    tmp_path: Path,
) -> None:
    job_id = "14358591"
    executable, _state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): detail_payload(
                job_id,
                organization_url="https://www.example.com/careers",
            ),
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert reference.listing_company_industry_source is not None
    assert str(reference.listing_company_industry_source.lookup_url) == company_url()
    assert occurrence.company_size_source is not None
    assert str(occurrence.company_size_source.lookup_url) == company_url()


def test_stepstone_limit_applies_to_each_search_term_and_location(tmp_path: Path) -> None:
    combinations = [
        ("Java", "Berlin", "14350001"),
        ("Java", "Hamburg", "14350002"),
        ("Python", "Berlin", "14350003"),
        ("Python", "Hamburg", "14350004"),
    ]
    pages: dict[str, object] = {}
    for query, location, job_id in combinations:
        pages[search_url(query, location)] = search_payload([search_row(job_id)])
        pages[detail_url(job_id)] = detail_payload(job_id)
    executable, state_path, _opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(
            search_terms=["Java", "Python"],
            locations=["Berlin", "Hamburg"],
            stepstone_de_limit=1,
        ),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [
        "14350001",
        "14350002",
        "14350003",
        "14350004",
    ]
    assert not state_path.exists()


def test_pagination_skips_duplicate_page_and_keeps_filling_stepstone_limit(
    tmp_path: Path,
) -> None:
    first_page = [search_row(str(14_000_000 + index)) for index in range(25)]
    duplicate_page = [search_row(str(14_000_000 + index)) for index in range(25)]
    final_row = search_row("14000025")
    pages: dict[str, object] = {
        search_url(): search_payload(first_page),
        search_url(page=2): search_payload(duplicate_page),
        search_url(page=3): search_payload([final_row]),
    }
    for row in [*first_page, final_row]:
        pages[row["url"]] = detail_payload(row["id"])
    executable, state_path, opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(stepstone_de_limit=26),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [
        str(14_000_000 + index) for index in range(26)
    ]
    assert search_url(page=3) in opened_path.read_text(encoding="utf-8").splitlines()
    assert not state_path.exists()


def test_search_challenge_is_isolated_as_stepstone_browser_error(tmp_path: Path) -> None:
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {search_url(): {"status": "challenge", "rows": []}},
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "stepstone_challenge"
    assert not state_path.exists()


def test_search_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "14358591"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): [
                {"status": "challenge", "rows": []},
                search_payload([search_row(job_id)]),
            ],
            detail_url(job_id): detail_payload(job_id),
        },
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [occurrence.external_id for occurrence in result.occurrences] == [job_id]
    assert result.completed_listing is True
    assert not state_path.exists()


def test_search_challenge_timeout_stops_before_next_query(tmp_path: Path) -> None:
    first_query = "first query"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {search_url(query=first_query): {"status": "challenge", "rows": []}},
    )
    adapter = adapter_type()(
        config(search_terms=[first_query, "second query"]),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert [error.error_code for error in result.errors] == ["stepstone_challenge"]
    assert not state_path.exists()


def test_detail_challenge_keeps_stepstone_listing_as_partial(tmp_path: Path) -> None:
    job_id = "14358591"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): {"status": "challenge"},
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert len(result.occurrences) == 1
    assert result.occurrences[0].detail_complete is False
    assert result.occurrences[0].fetch_error_code == "stepstone_challenge"
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert not state_path.exists()


def test_closed_stepstone_detail_is_reported_as_explicitly_closed(tmp_path: Path) -> None:
    job_id = "14358591"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): {"status": "closed"},
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert result.errors == []
    assert result.explicitly_closed_source_job_keys == {f"stepstone:de:{job_id}"}
    assert not state_path.exists()


def test_malformed_stepstone_row_does_not_hide_valid_job(tmp_path: Path) -> None:
    broken_id = "14350001"
    valid_id = "14350002"
    broken = search_row(broken_id)
    broken.pop("title")
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            search_url(): search_payload([broken, search_row(valid_id)]),
            detail_url(valid_id): detail_payload(valid_id),
        },
    )
    adapter = adapter_type()(
        config(stepstone_de_limit=2),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.external_id for item in result.occurrences] == [valid_id]
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].item_key == f"stepstone:de:{broken_id}"
    assert not state_path.exists()


def test_stepstone_company_page_returns_native_employee_range(tmp_path: Path) -> None:
    from job_scan.sources.stepstone import lookup_company_size

    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {company_url(): {"status": "ok", "reported_size": "51-250 Mitarbeiter"}},
    )
    source = CompanySizeSource(
        source_name="stepstone",
        lookup_url=company_url(),
        public_url=company_url(),
        source_title="StepStone company profile",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.reported_size == "51-250 Mitarbeiter"
    assert result.minimum_employees == 51
    assert result.maximum_employees == 250
    assert result.source_name == "stepstone"
    assert not state_path.exists()


def test_stepstone_company_page_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan.sources.stepstone import lookup_company_size

    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            company_url(): [
                {"status": "challenge", "reported_size": ""},
                {"status": "ok", "reported_size": "51-250 Mitarbeiter"},
            ]
        },
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    source = CompanySizeSource(
        source_name="stepstone",
        lookup_url=company_url(),
        public_url=company_url(),
        source_title="StepStone company profile",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.minimum_employees == 51
    assert not state_path.exists()


def test_stepstone_company_page_without_size_returns_none(tmp_path: Path) -> None:
    from job_scan.sources.stepstone import lookup_company_size

    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {company_url(): {"status": "ok", "reported_size": ""}},
    )
    source = CompanySizeSource(
        source_name="stepstone",
        lookup_url=company_url(),
        public_url=company_url(),
        source_title="StepStone company profile",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is None
    assert not state_path.exists()


def test_search_page_script_reads_stable_stepstone_job_card_fields() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(search_page_html([("14358591", "main")]))

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "rows": [search_row("14358591")],
            "card_count": 1,
        }
        browser.close()


def test_search_page_script_omits_semantic_fallback_cards() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(search_page_html([("14358591", "semantic")]))

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "rows": [], "card_count": 1}
        browser.close()


def test_search_page_script_keeps_main_card_when_semantic_card_is_present() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SEARCH_PAGE_JS

    main_id = "14358591"
    semantic_id = "14358592"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(search_page_html([(main_id, "main"), (semantic_id, "semantic")]))

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "rows": [search_row(main_id)],
            "card_count": 2,
        }
        browser.close()


def test_search_page_script_accepts_preloaded_empty_page_with_heading() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<h1>11 Treffer für Java Backend Developer</h1>" + search_page_html([]))

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "rows": [], "card_count": 0}
        browser.close()


def test_search_page_script_waits_for_card_when_preloaded_items_are_not_empty() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SEARCH_PAGE_JS

    job_id = "14358591"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<h1>11 Treffer für Java Backend Developer</h1>"
            + search_preloaded_state_html([(job_id, "main")])
        )
        page.evaluate(
            f"""
            setTimeout(() => {{
              document.body.insertAdjacentHTML(
                "beforeend",
                {json.dumps(search_card_html(job_id))}
              );
            }}, 100);
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "rows": [search_row(job_id)],
            "card_count": 1,
        }
        browser.close()


def test_detail_page_script_reads_job_posting_from_graph_after_malformed_script() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _DETAIL_PAGE_JS

    job = detail_payload("14358591")["job"]
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<script type="application/ld+json">not-json</script>'
            '<script type="application/ld+json">'
            f"{json.dumps({'@context': 'https://schema.org', '@graph': [job]})}"
            "</script>"
        )

        payload = page.evaluate(_DETAIL_PAGE_JS)

        assert payload == {"status": "ok", "job": job}
        browser.close()


@pytest.mark.parametrize(
    ("html", "expected_status"),
    [
        ("<title>Access Denied</title>", "challenge"),
        ("<main>Dieser Job ist nicht mehr verfügbar</main>", "closed"),
    ],
)
def test_detail_page_script_reports_terminal_page_state(
    html: str,
    expected_status: str,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _DETAIL_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        payload = page.evaluate(_DETAIL_PAGE_JS)

        assert payload == {"status": expected_status}
        browser.close()


def test_snapshot_page_script_keeps_only_stepstone_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _SNAPSHOT_PAGE_JS

    html = f"""
    <style>
      article {{ color: rgb(12, 37, 119); padding: 12px; }}
      h1 {{ font-size: 32px; }}
    </style>
    <article><h1>Senior Software Engineer Java</h1>
      <p>IDnow GmbH</p><button>Jetzt bewerben</button>
      <div data-at="header-company-logo-img"><img src="https://ads.example/logo.png"></div>
    </article>
    <article><h2>Passt dieser Job zu mir?</h2><p>Konto-Werbung</p></article>
    <article data-at="rebranded-version"><h2>Introduction</h2><p>About IDnow.</p></article>
    <article data-at="rebranded-version"><h2>Key Responsibilities</h2><p>Build Java services.</p></article>
    <article data-at="rebranded-version"><p>Your profile</p><h2>Preferred Experience</h2><p>Spring Boot.</p></article>
    <article data-at="rebranded-version"><h2>Perks &amp; Benefits</h2><p>Remote work.</p></article>
    <article data-at="rebranded-version"><h2>Gehalt</h2><p>Competitive salary.</p></article>
    <article><h2>Ähnliche Jobs</h2><p>Unrelated recommendation.</p></article>
    <script type="application/ld+json">{json.dumps(detail_payload('14358591')['job'])}</script>
    """
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html)

        payload = page.evaluate(_SNAPSHOT_PAGE_JS)

        assert payload["status"] == "ok"
        snapshot = payload["html"]
        assert 'data-job-scan-snapshot="stepstone:de:14358591"' in snapshot
        assert "Senior Software Engineer Java" in snapshot
        assert "Build Java services." in snapshot
        assert "Spring Boot." in snapshot
        assert "Remote work." in snapshot
        assert "Competitive salary." in snapshot
        assert "rgb(12, 37, 119)" in snapshot
        assert "Passt dieser Job zu mir?" not in snapshot
        assert "Konto-Werbung" not in snapshot
        assert "Unrelated recommendation." not in snapshot
        assert "Jetzt bewerben" not in snapshot
        assert "header-company-logo-img" not in snapshot
        assert "https://ads.example" not in snapshot
        assert "<script" not in snapshot
        browser.close()


def test_company_page_script_reads_stepstone_mitarbeiter_range() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.stepstone import _COMPANY_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<main>Energie- und Wasserversorgung • 51-250 Mitarbeiter</main>")

        payload = page.evaluate(_COMPANY_PAGE_JS)

        assert payload == {"status": "ok", "reported_size": "51-250 Mitarbeiter"}
        browser.close()
