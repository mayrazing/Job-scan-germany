from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import CompanySizeSource, SourceKind
from job_scan.sources import run_source


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


def adapter_type():
    from job_scan.sources.indeed import IndeedDeAdapter

    return IndeedDeAdapter


def test_search_url_supports_three_day_posting_window() -> None:
    from job_scan.sources.indeed import _search_url

    assert "fromage=3" in _search_url(
        config(posted_within_days=3),
        "Java Backend Engineer",
        "Deutschland",
        0,
    )


def search_url(
    query: str = "Java Backend Engineer",
    location: str = "Deutschland",
    *,
    start: int = 0,
) -> str:
    url = (
        "https://de.indeed.com/jobs"
        f"?q={query.replace(' ', '+')}&l={location.replace(' ', '+')}"
        "&fromage=7&sort=date"
    )
    return f"{url}&start={start}" if start else url


def detail_url(job_id: str) -> str:
    return f"https://de.indeed.com/viewjob?jk={job_id}"


def navigation_url(job_id: str) -> str:
    return f"https://de.indeed.com/rc/clk?jk={job_id}&from=serp&vjs=3"


def sponsored_navigation_url() -> str:
    return "https://de.indeed.com/pagead/clk?ad=fixture&vjs=3"


def search_row(job_id: str, *, title: str = "Java Backend Engineer") -> dict[str, str]:
    return {
        "id": job_id,
        "title": title,
        "company": "Example GmbH",
        "location": "Berlin",
        "url": detail_url(job_id),
        "navigation_url": navigation_url(job_id),
    }


def detail_payload(
    *,
    description: str = "Build Java and Spring Boot services.",
    company_url: str = "",
) -> dict[str, object]:
    return {
        "status": "ok",
        "job": {
            "title": "Java Backend Engineer",
            "company": "Example GmbH",
            "location": "Berlin",
            "description": description,
            "company_url": company_url,
        },
    }


def fake_opencli(
    tmp_path: Path,
    pages: dict[str, object],
    *,
    require_single_session: bool = False,
    reject_direct_viewjob: bool = False,
) -> tuple[Path, Path]:
    executable = tmp_path / "opencli"
    state_path = tmp_path / "opencli-active-url"
    starts_path = tmp_path / "opencli-session-starts"
    responses_path = tmp_path / "opencli-responses.json"
    responses_path.write_text(json.dumps(pages), encoding="utf-8")
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"state = pathlib.Path({str(state_path)!r})",
            f"starts = pathlib.Path({str(starts_path)!r})",
            f"responses = pathlib.Path({str(responses_path)!r})",
            f"require_single_session = {require_single_session!r}",
            f"reject_direct_viewjob = {reject_direct_viewjob!r}",
            "args = sys.argv[1:]",
            "if len(args) < 3 or args[0] != 'browser':",
            "    raise SystemExit(78)",
            "command = args[2]",
            "if command == 'open':",
            "    if args[4:] != ['--window', 'background']:",
            "        raise SystemExit(78)",
            "    if reject_direct_viewjob and '/viewjob?' in args[3]:",
            "        raise SystemExit(77)",
            "    if not state.exists():",
            "        count = int(starts.read_text()) + 1 if starts.exists() else 1",
            "        starts.write_text(str(count))",
            "        if require_single_session and count > 1:",
            "            raise SystemExit(77)",
            "    state.write_text(args[3], encoding='utf-8')",
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
            "elif command == 'click':",
            "    if not state.exists() or len(args) != 4:",
            "        raise SystemExit(78)",
            "    marker = 'a[data-jk=\"'",
            "    if not args[3].startswith(marker) or not args[3].endswith('\"]'):",
            "        raise SystemExit(78)",
            "    job_id = args[3][len(marker):-2]",
            "    state.write_text(",
            "        f'https://de.indeed.com/viewjob?jk={job_id}', encoding='utf-8'",
            "    )",
            "    print(json.dumps({'clicked': True, 'matches_n': 1}))",
            "elif command == 'wait':",
            "    if not state.exists() or args[3:] != ['time', '2']:",
            "        raise SystemExit(78)",
            "    print('Waited 2s')",
            "elif command == 'close':",
            "    state.unlink(missing_ok=True)",
            "    print('Browser session tab lease released')",
            "else:",
            "    raise SystemExit(78)",
        ]
    )
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)
    return executable, state_path


def test_discover_and_fetch_detail_map_indeed_de_into_source_occurrence(
    tmp_path: Path,
) -> None:
    job_id = "8c683c2df48291d7"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): detail_payload(),
        },
        require_single_session=True,
        reject_direct_viewjob=True,
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    references = adapter.discover()
    occurrence = adapter.fetch_detail(references[0])

    assert len(references) == 1
    assert references[0].source is SourceKind.INDEED
    assert references[0].source_instance == "de"
    assert references[0].external_id == job_id
    assert str(references[0].detail_url) == detail_url(job_id)
    assert occurrence.source is SourceKind.INDEED
    assert occurrence.source_instance == "de"
    assert occurrence.external_id == job_id
    assert occurrence.description == "Build Java and Spring Boot services."
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    assert not state_path.exists()


def test_new_indeed_job_carries_transient_snapshot_html(tmp_path: Path) -> None:
    job_id = "8c683c2df48291d7"
    snapshot_html = (
        '<!doctype html><html data-job-scan-snapshot="indeed:de:8c683c2df48291d7">'
        "<body>Java Backend Engineer</body></html>"
    )
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): [
                detail_payload(),
                {"status": "ok", "html": snapshot_html},
            ],
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
        capture_snapshot=lambda _reference: True,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None
    assert not state_path.exists()


def test_indeed_snapshot_failure_does_not_discard_the_job(tmp_path: Path) -> None:
    job_id = "8c683c2df48291d7"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): [
                detail_payload(),
                {"status": "unavailable", "error_code": "structure_mismatch"},
            ],
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
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


def test_detail_retains_indeed_company_about_page_for_company_size(
    tmp_path: Path,
) -> None:
    job_id = "8c683c2df48291d7"
    company_url = "https://de.indeed.com/cmp/Allianz"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): detail_payload(company_url=company_url),
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

    source = occurrence.company_size_source
    assert source is not None
    assert source.source_name == "indeed"
    assert str(source.lookup_url) == "https://de.indeed.com/cmp/Allianz/about"
    assert str(source.public_url) == "https://de.indeed.com/cmp/Allianz/about"
    industry_source = occurrence.company_industry_source
    assert industry_source is not None
    assert industry_source.source_name == "indeed"
    assert str(industry_source.lookup_url) == "https://de.indeed.com/cmp/Allianz/about"
    assert not state_path.exists()


def test_indeed_company_about_page_returns_mitarbeiter_range(tmp_path: Path) -> None:
    from job_scan.sources.indeed import lookup_company_size

    about_url = "https://de.indeed.com/cmp/Allianz/about"
    executable, state_path = fake_opencli(
        tmp_path,
        {about_url: {"status": "ok", "reported_size": "5.001 bis 10.000"}},
    )
    source = CompanySizeSource(
        source_name="indeed",
        lookup_url=about_url,
        public_url=about_url,
        source_title="Indeed company profile",
    )

    result = lookup_company_size(
        source,
        "Allianz",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.reported_size == "5.001 bis 10.000"
    assert result.minimum_employees == 5001
    assert result.maximum_employees == 10000
    assert result.source_name == "indeed"
    assert not state_path.exists()


def test_company_page_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from job_scan.sources.indeed import lookup_company_size

    about_url = "https://de.indeed.com/cmp/Allianz/about"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            about_url: [
                {"status": "challenge", "reported_size": ""},
                {"status": "ok", "reported_size": "5.001 bis 10.000"},
            ]
        },
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    source = CompanySizeSource(
        source_name="indeed",
        lookup_url=about_url,
        public_url=about_url,
        source_title="Indeed company profile",
    )

    result = lookup_company_size(
        source,
        "Allianz",
        datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    assert result is not None
    assert result.minimum_employees == 5001
    assert not state_path.exists()


def test_each_result_uses_its_own_indeed_navigation_url_for_detail(
    tmp_path: Path,
) -> None:
    first_id = "1111111111111111"
    second_id = "2222222222222222"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {
                "status": "ok",
                "rows": [search_row(first_id), search_row(second_id)],
            },
            navigation_url(first_id): detail_payload(description="First detail."),
            navigation_url(second_id): detail_payload(description="Second detail."),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.description for item in result.occurrences] == [
        "First detail.",
        "Second detail.",
    ]
    assert result.errors == []
    assert not state_path.exists()


def test_sponsored_indeed_result_uses_same_origin_pagead_navigation(
    tmp_path: Path,
) -> None:
    job_id = "3333333333333333"
    row = search_row(job_id)
    row["navigation_url"] = sponsored_navigation_url()
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [row]},
            sponsored_navigation_url(): detail_payload(description="Sponsored detail."),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.description for item in result.occurrences] == ["Sponsored detail."]
    assert result.errors == []
    assert not state_path.exists()


def test_indeed_navigation_rejects_unrelated_same_origin_path(
    tmp_path: Path,
) -> None:
    job_id = "4444444444444444"
    row = search_row(job_id)
    row["navigation_url"] = f"https://de.indeed.com/account/settings?jk={job_id}"
    executable, state_path = fake_opencli(
        tmp_path,
        {search_url(): {"status": "ok", "rows": [row]}},
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "contract"
    assert result.errors[0].item_key == f"indeed:de:{job_id}"
    assert not state_path.exists()


def test_discover_paginates_until_the_configured_indeed_de_limit(
    tmp_path: Path,
) -> None:
    first_page = [search_row(f"{index:016x}") for index in range(10)]
    second_page = [search_row(f"{index:016x}") for index in range(10, 15)]
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": first_page},
            search_url(start=10): {"status": "ok", "rows": second_page},
        },
    )
    adapter = adapter_type()(
        config(indeed_de_limit=12),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [f"{index:016x}" for index in range(12)]
    assert not state_path.exists()


def test_indeed_limit_applies_to_each_search_term_and_location(
    tmp_path: Path,
) -> None:
    combinations = [
        ("Java", "Berlin", "0000000000000001"),
        ("Java", "Hamburg", "0000000000000002"),
        ("Python", "Berlin", "0000000000000003"),
        ("Python", "Hamburg", "0000000000000004"),
    ]
    pages: dict[str, object] = {}
    for query, location, job_id in combinations:
        pages[search_url(query, location)] = {
            "status": "ok",
            "rows": [search_row(job_id)],
        }
        pages[navigation_url(job_id)] = detail_payload()
    executable, state_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(
            search_terms=["Java", "Python"],
            locations=["Berlin", "Hamburg"],
            indeed_de_limit=1,
        ),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == [
        "0000000000000001",
        "0000000000000002",
        "0000000000000003",
        "0000000000000004",
    ]
    assert not state_path.exists()


def test_pagination_recovers_valid_duplicate_after_malformed_row_and_keeps_filling_limit(
    tmp_path: Path,
) -> None:
    first_page = [search_row(f"{index:016x}") for index in range(10)]
    first_page[0].pop("title")
    second_page = [search_row(f"{index:016x}") for index in range(11)]
    third_page = [search_row(f"{index:016x}") for index in range(11, 13)]
    pages: dict[str, object] = {
        search_url(): {"status": "ok", "rows": first_page},
        search_url(start=10): {"status": "ok", "rows": second_page},
        search_url(start=20): {"status": "ok", "rows": third_page},
    }
    pages.update({navigation_url(f"{index:016x}"): detail_payload() for index in range(12)})
    executable, state_path = fake_opencli(tmp_path, pages)
    adapter = adapter_type()(
        config(indeed_de_limit=12),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [item.external_id for item in references] == [f"{index:016x}" for index in range(12)]
    assert adapter.drain_discovery_errors() == []
    assert not state_path.exists()


def test_search_waits_for_job_cards_when_result_count_renders_first() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <main>
              <h1>Java Backend Engineer Jobs in Berlin</h1>
              <div data-testid="searchCount">200 jobs</div>
            </main>
            """
        )
        page.evaluate(
            """
            setTimeout(() => {
                  document.body.insertAdjacentHTML('beforeend', `
                    <article class="job_seen_beacon">
                      <a data-jk="8c683c2df48291d7"
                         href="https://de.indeed.com/rc/clk?jk=8c683c2df48291d7&from=serp&vjs=3">
                        Java Backend Engineer
                      </a>
                  <span data-testid="company-name">Example GmbH</span>
                  <span data-testid="text-location">Berlin</span>
                </article>
              `);
            }, 100)
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "rows": [search_row("8c683c2df48291d7")],
        }
        browser.close()


def test_search_reports_challenge_without_waiting_for_results() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<title>Just a moment...</title><main>Verify you are human</main>")
        page.evaluate(
            """() => {
              window.challengeTimeoutCalls = 0;
              window.setTimeout = (callback) => {
                window.challengeTimeoutCalls += 1;
                callback();
              };
            }"""
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "challenge", "rows": []}
        assert page.evaluate("window.challengeTimeoutCalls") == 0
        browser.close()


def test_search_ignores_recommendation_cards_when_heading_says_no_jobs() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <h1>
              Es wurden keine Jobs für die Suche Distributed Systems Engineer
              zqxjvnonexistent Jobs in Berlin gefunden.
            </h1>
            <article class="job_seen_beacon">
              <a data-jk="8c683c2df48291d7"
                 href="https://de.indeed.com/rc/clk?jk=8c683c2df48291d7&from=serp&vjs=3">
                Java Backend Engineer
              </a>
              <span data-testid="company-name">Example GmbH</span>
              <span data-testid="text-location">Berlin</span>
            </article>
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "rows": []}
        browser.close()


def test_search_waits_for_delayed_no_jobs_heading_before_accepting_cards() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <article class="job_seen_beacon">
              <a data-jk="8c683c2df48291d7"
                 href="https://de.indeed.com/rc/clk?jk=8c683c2df48291d7&from=serp&vjs=3">
                Java Backend Engineer
              </a>
              <span data-testid="company-name">Example GmbH</span>
              <span data-testid="text-location">Berlin</span>
            </article>
            """
        )
        page.evaluate(
            """
            setTimeout(() => {
              document.body.insertAdjacentHTML(
                'afterbegin',
                '<h1>Es wurden keine Jobs für die Suche Distributed Systems '
                  + 'Engineer zqxjvnonexistent Jobs in Berlin gefunden.</h1>'
              );
            }, 100);
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {"status": "ok", "rows": []}
        browser.close()


def test_search_keeps_real_card_when_unrelated_region_says_zero_jobs() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SEARCH_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <h1>Java Backend Engineer Jobs in Berlin</h1>
            <article class="job_seen_beacon">
              <a data-jk="8c683c2df48291d7"
                 href="https://de.indeed.com/rc/clk?jk=8c683c2df48291d7&from=serp&vjs=3">
                Java Backend Engineer
              </a>
              <span data-testid="company-name">Example GmbH</span>
              <span data-testid="text-location">Berlin</span>
            </article>
            <aside>0 Jobs saved</aside>
            """
        )

        payload = page.evaluate(_SEARCH_PAGE_JS)

        assert payload == {
            "status": "ok",
            "rows": [search_row("8c683c2df48291d7")],
        }
        browser.close()


def test_detail_waits_for_the_matching_job_heading() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _detail_page_js

    expected_title = "Java Backend Engineer"
    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<h1 data-testid="jobsearch-JobInfoHeader-title">Previous job</h1>')
        page.evaluate(
            """
            setTimeout(() => {
              document.body.innerHTML = `
                <h1 data-testid="jobsearch-JobInfoHeader-title">
                  Java Backend Engineer - job post
                </h1>
                <div data-testid="inlineHeader-companyName">Example GmbH</div>
                <div data-testid="inlineHeader-companyLocation">Berlin</div>
                <div id="jobDescriptionText">Build Java services.</div>
              `;
            }, 100)
            """
        )

        payload = page.evaluate(_detail_page_js(expected_title))

        assert payload == {
            "status": "ok",
            "job": {
                "title": expected_title,
                "company": "Example GmbH",
                "location": "Berlin",
                "description": "Build Java services.",
                "company_url": "",
            },
        }
        browser.close()


def test_snapshot_page_script_keeps_only_indeed_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _SNAPSHOT_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <style>.jobsearch-InfoHeaderContainer { color: rgb(37, 87, 167); }</style>
            <main data-jk="8c683c2df48291d7">
              <nav>Indeed account and search</nav>
              <div class="jobsearch-InfoHeaderContainer">
                <h1 data-testid="jobsearch-JobInfoHeader-title">Java Backend Engineer</h1>
                <div data-testid="inlineHeader-companyName">Example GmbH</div>
                <button>Jetzt bewerben</button>
              </div>
              <div id="jobDetailsSection">Anstellungsart Festanstellung</div>
              <div id="jobLocationSectionWrapper">Arbeitsort Berlin</div>
              <div id="benefits">Leistungen Weiterbildung</div>
              <div id="jobDescriptionTitle">Vollständige Stellenbeschreibung</div>
              <div id="jobDescriptionText">
                <h2>Ihr Aufgabenbereich</h2><p>Build Java services.</p>
              </div>
              <aside>Ähnliche Jobs und Werbung</aside>
              <img src="https://ads.example/tracker.png">
            </main>
            """
        )
        payload = page.evaluate(_SNAPSHOT_PAGE_JS)

        assert payload["status"] == "ok"
        snapshot = payload["html"]
        assert 'data-job-scan-snapshot="indeed:de:8c683c2df48291d7"' in snapshot
        assert "Java Backend Engineer" in snapshot
        assert "Anstellungsart Festanstellung" in snapshot
        assert "Arbeitsort Berlin" in snapshot
        assert "Leistungen Weiterbildung" in snapshot
        assert "Build Java services." in snapshot
        assert "rgb(37, 87, 167)" in snapshot
        assert "Indeed account and search" not in snapshot
        assert "Ähnliche Jobs und Werbung" not in snapshot
        assert "Jetzt bewerben" not in snapshot
        assert "https://ads.example" not in snapshot
        assert "<script" not in snapshot
        browser.close()


def test_company_page_script_reads_german_mitarbeiter_range() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.indeed import _COMPANY_PAGE_JS

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<main><div>Mitarbeiter</div><div>5.001 bis 10.000</div></main>")

        payload = page.evaluate(_COMPANY_PAGE_JS)

        assert payload == {"status": "ok", "reported_size": "5.001 bis 10.000"}
        browser.close()


def test_challenge_becomes_an_isolated_indeed_browser_error(tmp_path: Path) -> None:
    executable, state_path = fake_opencli(
        tmp_path,
        {search_url(): {"status": "challenge", "rows": []}},
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "indeed_challenge"
    assert not state_path.exists()


def test_search_challenge_timeout_stops_before_next_query(tmp_path: Path) -> None:
    first_query = "first query"
    executable, state_path = fake_opencli(
        tmp_path,
        {search_url(query=first_query): {"status": "challenge", "rows": []}},
    )
    adapter = adapter_type()(
        config(search_terms=[first_query, "second query"]),
        opencli_executable=executable,
        limit=1,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert [error.error_code for error in result.errors] == ["indeed_challenge"]
    assert not state_path.exists()


def test_search_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "9999999999999999"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): [
                {"status": "challenge", "rows": []},
                {"status": "ok", "rows": [search_row(job_id)]},
            ],
            navigation_url(job_id): detail_payload(),
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


def test_detail_challenge_is_isolated_and_releases_browser_session(
    tmp_path: Path,
) -> None:
    job_id = "9999999999999999"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): {"status": "challenge"},
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert len(result.occurrences) == 1
    assert result.occurrences[0].external_id == job_id
    assert result.occurrences[0].detail_complete is False
    assert result.occurrences[0].fetch_error_code == "indeed_challenge"
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "indeed_challenge"
    assert not state_path.exists()


def test_detail_challenge_timeout_stops_before_next_detail(tmp_path: Path) -> None:
    first_id = "1111111111111111"
    second_id = "2222222222222222"
    executable, state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {
                "status": "ok",
                "rows": [search_row(first_id), search_row(second_id)],
            },
            navigation_url(first_id): {"status": "challenge"},
            navigation_url(second_id): detail_payload(),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert [occurrence.external_id for occurrence in result.occurrences] == [first_id]
    assert [error.error_code for error in result.errors] == ["indeed_challenge"]
    assert not state_path.exists()


def test_malformed_search_row_is_isolated_without_losing_valid_indeed_job(
    tmp_path: Path,
) -> None:
    broken_id = "1111111111111111"
    valid_id = "2222222222222222"
    executable, _state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {
                "status": "ok",
                "rows": [
                    {
                        "id": broken_id,
                        "company": "Broken GmbH",
                        "location": "Berlin",
                        "url": detail_url(broken_id),
                    },
                    search_row(valid_id),
                ],
            },
            navigation_url(valid_id): detail_payload(),
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
    assert result.errors[0].item_key == f"indeed:de:{broken_id}"
    assert result.discovered_source_job_keys == {
        f"indeed:de:{broken_id}",
        f"indeed:de:{valid_id}",
    }


def test_missing_indeed_description_keeps_listing_as_incomplete(tmp_path: Path) -> None:
    job_id = "3333333333333333"
    executable, _state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): detail_payload(description=""),
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.description == ""
    assert occurrence.detail_complete is False
    assert occurrence.fetch_error_code == "missing_full_description"


def test_closed_indeed_detail_is_reported_as_explicitly_closed(tmp_path: Path) -> None:
    job_id = "4444444444444444"
    executable, _state_path = fake_opencli(
        tmp_path,
        {
            search_url(): {"status": "ok", "rows": [search_row(job_id)]},
            navigation_url(job_id): {"status": "closed"},
        },
    )
    adapter = adapter_type()(
        config(),
        opencli_executable=executable,
        limit=2,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert result.errors == []
    assert result.explicitly_closed_source_job_keys == {f"indeed:de:{job_id}"}
