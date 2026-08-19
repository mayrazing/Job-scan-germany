from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from importlib.util import find_spec
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import CompanySizeSource, SourceKind
from job_scan.sources import run_source

ORIGIN = "https://www.glassdoor.de"
LOCATION_URL = f"{ORIGIN}/Job/index.htm"


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
    from job_scan.sources.glassdoor import GlassdoorDeAdapter

    return GlassdoorDeAdapter


def test_search_url_supports_three_day_posting_window() -> None:
    from job_scan.sources.glassdoor import _LocationTarget, _search_url

    assert "fromAge=3" in _search_url(
        config(posted_within_days=3),
        "Software Engineer",
        _LocationTarget(location_id=2_622_109, location_type="C", name="Berlin"),
    )


def location_payload(name: str = "Berlin", location_id: int = 2_622_109) -> dict[str, object]:
    return {
        "status": "ok",
        "locations": [
            {
                "locationId": location_id,
                "locationType": "C",
                "locationName": name,
                "country2LetterIso": "DE",
            }
        ],
    }


def search_url(
    query: str = "Software Engineer",
    location: str = "Berlin",
    *,
    location_id: int = 2_622_109,
) -> str:
    location_slug = location.casefold().replace(" ", "-")
    query_slug = query.casefold().replace(" ", "-")
    query_start = len(location_slug) + 1
    path = (
        f"{ORIGIN}/Job/{location_slug}-{query_slug}-jobs-SRCH_IL.0,"
        f"{len(location_slug)}_IC{location_id}_KO{query_start},"
        f"{query_start + len(query_slug)}.htm"
    )
    return f"{path}?fromAge=7&sortBy=date_desc"


def canonical_search_url(*, page: int = 1) -> str:
    suffix = "" if page == 1 else f"_IP{page}"
    return (
        f"{ORIGIN}/Job/berlin-software-engineer-jobs-"
        f"SRCH_IL.0,6_IC2622109_KO7,24{suffix}.htm?fromAge=7&sortBy=date_desc"
    )


def detail_url(job_id: str) -> str:
    return (
        f"{ORIGIN}/job-listing/software-engineer-example-gmbh-"
        f"JV_IC2622109_KO0,17_KE18,30.htm?jl={job_id}"
    )


def company_url() -> str:
    return f"{ORIGIN}/%C3%9Cberblick/Arbeit-bei-Example-GmbH-EI_IE12345.11,23.htm"


def search_row(job_id: str) -> dict[str, str]:
    return {
        "id": job_id,
        "title": "Software Engineer",
        "company": "Example GmbH",
        "location": "Berlin",
        "url": detail_url(job_id),
    }


def search_payload(rows: list[object], *, page: int = 1) -> dict[str, object]:
    return {"status": "ok", "page_url": canonical_search_url(page=page), "rows": rows}


def detail_payload(job_id: str, *, description: str | None = None) -> dict[str, object]:
    return {
        "status": "ok",
        "page_url": detail_url(job_id),
        "job": {
            "@context": "https://schema.org/",
            "@type": "JobPosting",
            "title": "Software Engineer",
            "datePosted": "2026-08-04T07:04:22",
            "hiringOrganization": {
                "@type": "Organization",
                "name": "Example GmbH",
                "sameAs": company_url(),
            },
            "jobLocation": {
                "@type": "Place",
                "address": {
                    "@type": "PostalAddress",
                    "addressCountry": {"@type": "Country", "name": "Germany"},
                    "addressLocality": "Berlin",
                },
            },
            "description": description
            or "<h2>About us</h2><p>Build Java and Spring Boot services.</p>",
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


def test_glassdoor_source_module_exists() -> None:
    assert find_spec("job_scan.sources.glassdoor") is not None


def test_discover_resolves_german_location_and_paginates_to_limit(tmp_path: Path) -> None:
    first_page = [search_row(str(101_000_000_0000 + index)) for index in range(30)]
    second_page = [search_row(str(101_000_000_0030 + index)) for index in range(5)]
    pages: dict[str, object] = {
        LOCATION_URL: location_payload(),
        search_url(): search_payload(first_page),
        canonical_search_url(page=2): search_payload(second_page, page=2),
    }
    for row in first_page + second_page:
        pages[row["url"]] = detail_payload(row["id"])
    executable, state_path, opened_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=35,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [
        str(101_000_000_0000 + index) for index in range(35)
    ]
    opened_urls = opened_path.read_text(encoding="utf-8").splitlines()
    assert opened_urls[:3] == [LOCATION_URL, search_url(), canonical_search_url(page=2)]
    assert not state_path.exists()


def test_location_selection_accepts_known_english_german_city_alias() -> None:
    from job_scan.sources.glassdoor import _select_location

    target = _select_location(
        [
            {
                "locationId": 2_622_109,
                "locationType": "C",
                "locationName": "Berlin",
                "country2LetterIso": "DE",
            },
            {
                "locationId": 2_613_959,
                "locationType": "C",
                "locationName": "München",
                "country2LetterIso": "DE",
            },
        ],
        "Munich",
    )

    assert target is not None
    assert target.name == "München"
    assert target.location_id == 2_613_959


def test_discover_skips_location_when_only_other_german_city_is_resolved(
    tmp_path: Path,
) -> None:
    wrong_job_id = "1010138743368"
    executable, state_path, opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(name="Berlin"),
            search_url(): search_payload([search_row(wrong_job_id)]),
            detail_url(wrong_job_id): detail_payload(wrong_job_id),
        },
    )
    adapter = adapter_type()(
        config(locations=["Nowherezzzz"]),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert result.errors == []
    assert result.completed_listing is True
    assert opened_path.read_text(encoding="utf-8").splitlines() == [LOCATION_URL]
    assert not state_path.exists()


def test_discover_searches_supported_locations_and_skips_unsupported_ones(
    tmp_path: Path,
) -> None:
    job_id = "1010138743368"
    executable, state_path, opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(name="Berlin"),
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): detail_payload(job_id),
        },
    )
    adapter = adapter_type()(
        config(locations=["Berlin", "Nowherezzzz"]),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.external_id for item in result.occurrences] == [job_id]
    opened_urls = opened_path.read_text(encoding="utf-8").splitlines()
    assert opened_urls.count(search_url()) == 1
    assert not state_path.exists()


def test_fetch_detail_returns_complete_glassdoor_job_posting(tmp_path: Path) -> None:
    job_id = "1010138743368"
    description = "<h2>About us</h2><p>Build cloud products.</p>"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(),
            search_url(): search_payload([search_row(job_id)]),
            detail_url(job_id): detail_payload(job_id, description=description),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert reference.source is SourceKind.GLASSDOOR
    assert occurrence.source is SourceKind.GLASSDOOR
    assert occurrence.external_id == job_id
    assert occurrence.posted_at == date(2026, 8, 4)
    assert occurrence.description == "About us\nBuild cloud products."
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    assert occurrence.company_size_source is not None
    assert occurrence.company_size_source.source_name == "glassdoor"
    assert str(occurrence.company_size_source.lookup_url) == company_url()
    industry_source = occurrence.company_industry_source
    assert industry_source is not None
    assert industry_source.source_name == "glassdoor"
    assert str(industry_source.lookup_url) == company_url()
    assert not state_path.exists()


def test_search_challenge_is_isolated_as_glassdoor_browser_error(tmp_path: Path) -> None:
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(),
            search_url(): {"status": "challenge", "page_url": search_url(), "rows": []},
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "glassdoor_challenge"
    assert not state_path.exists()


def test_search_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "1010138743368"
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(),
            search_url(): [
                {"status": "challenge", "page_url": search_url(), "rows": []},
                search_payload([search_row(job_id)]),
            ],
            detail_url(job_id): detail_payload(job_id),
        },
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
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
        {
            LOCATION_URL: location_payload(),
            search_url(query=first_query): {
                "status": "challenge",
                "page_url": search_url(query=first_query),
                "rows": [],
            },
        },
    )
    adapter = adapter_type()(
        config(search_terms=[first_query, "second query"]),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert [error.error_code for error in result.errors] == ["glassdoor_challenge"]
    assert not state_path.exists()


def test_malformed_glassdoor_row_does_not_hide_valid_job(tmp_path: Path) -> None:
    broken_id = "1010138743368"
    valid_id = "1010138743369"
    broken = search_row(broken_id)
    broken.pop("title")
    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            LOCATION_URL: location_payload(),
            search_url(): search_payload([broken, search_row(valid_id)]),
            detail_url(valid_id): detail_payload(valid_id),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.external_id for item in result.occurrences] == [valid_id]
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].item_key == f"glassdoor:de:{broken_id}"
    assert not state_path.exists()


def test_glassdoor_company_page_returns_native_employee_range(tmp_path: Path) -> None:
    from job_scan.sources.glassdoor import lookup_company_size

    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {company_url(): {"status": "ok", "reported_size": "10000+ Mitarbeiter"}},
    )
    source = CompanySizeSource(
        source_name="glassdoor",
        lookup_url=company_url(),
        public_url=company_url(),
        source_title="Glassdoor company profile",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.reported_size == "10000+ Mitarbeiter"
    assert result.minimum_employees == 10000
    assert result.maximum_employees is None
    assert result.source_name == "glassdoor"
    assert not state_path.exists()


def test_glassdoor_company_page_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan.sources.glassdoor import lookup_company_size

    executable, state_path, _opened_path = fake_opencli(
        tmp_path,
        {
            company_url(): [
                {"status": "challenge", "reported_size": ""},
                {"status": "ok", "reported_size": "10000+ Mitarbeiter"},
            ]
        },
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    source = CompanySizeSource(
        source_name="glassdoor",
        lookup_url=company_url(),
        public_url=company_url(),
        source_title="Glassdoor company profile",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.minimum_employees == 10000
    assert not state_path.exists()


def test_search_page_script_reads_stable_glassdoor_job_card_fields() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _SEARCH_PAGE_JS

    job_id = "1010138743368"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <h1>200 Jobs für Software Engineer in Berlin</h1>
            <ul aria-label="Jobs List">
              <li data-test="jobListing">
                <div id="job-employer-{job_id}"><span>Example GmbH</span></div>
                <a data-test="job-title" id="job-title-{job_id}"
                   href="{detail_url(job_id)}">Software Engineer</a>
                <div data-test="emp-location">Berlin</div>
              </li>
            </ul>
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "page_url": "about:blank",
            "rows": [search_row(job_id)],
        }
        browser.close()


def test_search_page_script_keeps_results_when_aside_mentions_zero_jobs() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _SEARCH_PAGE_JS

    job_id = "1010138743368"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <h1>200 Jobs für Software Engineer in Berlin</h1>
            <aside>Example GmbH currently has 0 Jobs</aside>
            <ul aria-label="Jobs List">
              <li data-test="jobListing">
                <div id="job-employer-{job_id}"><span>Example GmbH</span></div>
                <a data-test="job-title" id="job-title-{job_id}"
                   href="{detail_url(job_id)}">Software Engineer</a>
                <div data-test="emp-location">Berlin</div>
              </li>
            </ul>
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "page_url": "about:blank",
            "rows": [search_row(job_id)],
        }
        browser.close()


def test_search_page_script_ignores_similar_jobs_when_search_has_zero_matches() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _SEARCH_PAGE_JS

    job_id = "1010138743368"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <h1>0 Jobs für Distributed Systems Engineer zqxjvnonexistent</h1>
            <h2>Ähnliche Jobs</h2>
            <ul aria-label="Jobs List">
              <li data-test="jobListing">
                <div id="job-employer-{job_id}"><span>Example GmbH</span></div>
                <a data-test="job-title" id="job-title-{job_id}"
                   href="{detail_url(job_id)}">Software Engineer</a>
                <div data-test="emp-location">Berlin</div>
              </li>
            </ul>
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "page_url": "about:blank", "rows": []}
        browser.close()


def test_search_page_script_waits_for_late_zero_jobs_heading() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _SEARCH_PAGE_JS

    job_id = "1010138743368"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <h2>Ähnliche Jobs</h2>
            <ul aria-label="Jobs List">
              <li data-test="jobListing">
                <div id="job-employer-{job_id}"><span>Example GmbH</span></div>
                <a data-test="job-title" id="job-title-{job_id}"
                   href="{detail_url(job_id)}">Software Engineer</a>
                <div data-test="emp-location">Berlin</div>
              </li>
            </ul>
            """
        )
        page.evaluate(
            """
            setTimeout(() => {
              document.body.insertAdjacentHTML(
                "afterbegin",
                "<h1>0 Jobs für Distributed Systems Engineer zqxjvnonexistent</h1>"
              );
            }, 100);
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "page_url": "about:blank", "rows": []}
        browser.close()


def test_detail_page_script_reads_glassdoor_job_posting_json_ld() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _DETAIL_PAGE_JS

    job = detail_payload("1010138743368")["job"]
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<script type="application/ld+json">not-json</script>'
            '<script type="application/ld+json">'
            f"{json.dumps(job)}"
            "</script>"
        )

        payload = page.evaluate(_DETAIL_PAGE_JS)

        assert payload == {"status": "ok", "page_url": "about:blank", "job": job}
        browser.close()


def test_detail_page_script_keeps_posting_when_description_says_not_found() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _DETAIL_PAGE_JS

    job = detail_payload(
        "1010138743368",
        description="<p>The phrase not found can occur in ordinary job copy.</p>",
    )["job"]
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<main>The phrase not found can occur in ordinary job copy.</main>"
            '<script type="application/ld+json">'
            f"{json.dumps(job)}"
            "</script>"
        )

        payload = page.evaluate(_DETAIL_PAGE_JS)

        assert payload == {"status": "ok", "page_url": "about:blank", "job": job}
        browser.close()


def test_company_page_script_normalizes_mehr_als_employee_count() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.glassdoor import _COMPANY_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<main>Mehr als 10.000 Mitarbeiter</main>")

        payload = page.evaluate(_COMPANY_PAGE_JS)

        assert payload == {"status": "ok", "reported_size": "10000+ Mitarbeiter"}
        browser.close()
