from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import CompanySizeSource, SourceKind
from job_scan.sources import ExplicitlyClosed, run_source

JOB_ID = "6921622b-85e7-4281-9339-1cfde1d0e877"


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Backend Engineer"],
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


def search_row(job_id: str = JOB_ID) -> dict[str, object]:
    return {
        "id": job_id,
        "title": "Backend Engineer",
        "company_name": "Example GmbH",
        "company_size": "51-200",
        "locations": ["Berlin", "Remote"],
        "start_date": 1_785_801_600,
    }


def detail_payload(job_id: str = JOB_ID) -> dict[str, object]:
    return {
        "status": "ok",
        "job": {
            "id": job_id,
            "job_id": "example-backend-engineer",
            "title": "Backend Engineer",
            "description": "<p>Build reliable services.</p>",
            "responsibilities": ["Own production APIs."],
            "requirements": ["Strong Python skills."],
            "desirable": ["Go experience."],
            "additional_requirements": ["Located in Germany."],
            "start_date": 1_785_801_600,
            "active": True,
            "visible": True,
            "archive": False,
            "locations": ["Berlin"],
            "job": {"company": {"name": "Example GmbH"}},
        },
    }


def fake_opencli(
    tmp_path: Path,
    responses: list[object],
) -> tuple[Path, Path, Path]:
    executable = tmp_path / "opencli"
    state_path = tmp_path / "opencli-active"
    responses_path = tmp_path / "opencli-responses.json"
    eval_log_path = tmp_path / "opencli-eval.jsonl"
    responses_path.write_text(json.dumps(responses), encoding="utf-8")
    script = "\n".join(
        [
            f"#!{sys.executable}",
            "import json",
            "import pathlib",
            "import sys",
            f"state = pathlib.Path({str(state_path)!r})",
            f"responses = pathlib.Path({str(responses_path)!r})",
            f"eval_log = pathlib.Path({str(eval_log_path)!r})",
            "args = sys.argv[1:]",
            "if len(args) < 3 or args[0] != 'browser':",
            "    raise SystemExit(78)",
            "command = args[2]",
            "if command == 'open':",
            "    if args[4:] != ['--window', 'background']:",
            "        raise SystemExit(78)",
            "    state.write_text(args[3], encoding='utf-8')",
            "    print(json.dumps({'url': args[3], 'page': 'fake'}))",
            "elif command == 'eval':",
            "    if not state.exists():",
            "        raise SystemExit(78)",
            "    with eval_log.open('a', encoding='utf-8') as log:",
            "        log.write(json.dumps(args[3]) + '\\n')",
            "    queued = json.loads(responses.read_text(encoding='utf-8'))",
            "    if not queued:",
            "        raise SystemExit(66)",
            "    payload = queued.pop(0)",
            "    responses.write_text(json.dumps(queued), encoding='utf-8')",
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
    return executable, state_path, eval_log_path


def adapter_type():
    from job_scan.sources.simplify import SimplifyDeAdapter

    return SimplifyDeAdapter


def test_discover_deduplicates_simplify_jobs_across_queries(tmp_path: Path) -> None:
    executable, state_path, eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
        ],
    )
    adapter = adapter_type()(
        config(search_terms=["Backend Engineer", "Python Engineer"], simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == [JOB_ID]
    assert references[0].source is SourceKind.SIMPLIFY
    assert references[0].listing_location == "Berlin, Remote"
    assert references[0].listing_posted_at == date(2026, 8, 4)
    scripts = [json.loads(line) for line in eval_log_path.read_text().splitlines()]
    assert "Backend Engineer" in scripts[0]
    assert "Python Engineer" in scripts[1]
    assert "Berlin" in scripts[0]
    assert not state_path.exists()


def test_search_waits_for_manual_challenge_clearance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "challenge", "rows": []},
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
        ],
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == [JOB_ID]
    assert not state_path.exists()


def test_uncleared_search_challenge_becomes_browser_error(tmp_path: Path) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [{"status": "challenge", "rows": []}],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "simplify_challenge"
    assert not state_path.exists()


def test_search_challenge_timeout_stops_before_next_query(tmp_path: Path) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "challenge", "rows": []},
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
        ],
    )
    adapter = adapter_type()(
        config(search_terms=["first query", "second query"]),
        opencli_executable=executable,
        timeout_seconds=5,
        challenge_wait_seconds=0,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert [error.error_code for error in result.errors] == ["simplify_challenge"]
    assert not state_path.exists()


def test_fetch_detail_returns_full_description_and_simplify_platform_url(
    tmp_path: Path,
) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
        ],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.source is SourceKind.SIMPLIFY
    assert occurrence.external_id == JOB_ID
    assert str(occurrence.url) == f"https://simplify.jobs/jobs?jobId={JOB_ID}"
    assert occurrence.description == (
        "Build reliable services.\n"
        "Responsibilities\nOwn production APIs.\n"
        "Requirements\nStrong Python skills.\n"
        "Desirable\nGo experience.\n"
        "Additional requirements\nLocated in Germany."
    )
    assert occurrence.detail_complete is True
    source = occurrence.company_size_source
    assert source is not None
    assert source.source_name == "simplify"
    assert source.reported_size == "51-200"
    assert not state_path.exists()


def test_new_simplify_job_carries_transient_snapshot_html(tmp_path: Path) -> None:
    snapshot_html = (
        f'<html data-job-scan-snapshot="simplify:de:{JOB_ID}">'
        "<body>Backend Engineer</body></html>"
    )
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
            {"status": "ok", "html": snapshot_html},
        ],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        capture_snapshot=lambda _reference: True,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None
    assert not state_path.exists()


def test_simplify_snapshot_failure_does_not_discard_the_job(tmp_path: Path) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
            {"status": "unavailable", "error_code": "structure_mismatch"},
        ],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
        capture_snapshot=lambda _reference: True,
    )

    result = run_source(adapter)

    assert len(result.occurrences) == 1
    assert result.occurrences[0].description.startswith("Build reliable services.")
    assert result.occurrences[0].job_snapshot_html is None
    assert result.occurrences[0].job_snapshot_error_code == "snapshot_capture_failed"
    assert not state_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("active", False), ("visible", False), ("archive", True)],
)
def test_closed_simplify_detail_is_reported_as_explicitly_closed(
    tmp_path: Path,
    field: str,
    value: bool,
) -> None:
    closed = detail_payload()
    closed["job"][field] = value  # type: ignore[index]
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [{"status": "ok", "rows": [search_row()]}, closed],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.occurrences == []
    assert result.errors == []
    assert result.explicitly_closed_source_job_keys == {f"simplify:de:{JOB_ID}"}
    assert not state_path.exists()


def test_changed_simplify_search_contract_is_isolated_as_browser_error(
    tmp_path: Path,
) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [{"status": "missing_search_contract", "rows": []}],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert result.completed_listing is False
    assert result.occurrences == []
    assert len(result.errors) == 1
    assert result.errors[0].category == "browser"
    assert result.errors[0].error_code == "simplify_search_contract"
    assert not state_path.exists()


def test_detail_failure_keeps_simplify_listing_company_size_on_partial(
    tmp_path: Path,
) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            {"status": "request_failed"},
        ],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert len(result.occurrences) == 1
    occurrence = result.occurrences[0]
    assert occurrence.detail_complete is False
    assert occurrence.company_size_source is not None
    assert occurrence.company_size_source.reported_size == "51-200"
    assert not state_path.exists()


def test_oversized_simplify_company_size_is_ignored_without_dropping_job(
    tmp_path: Path,
) -> None:
    row = search_row()
    row["company_size"] = "x" * 101
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [{"status": "ok", "rows": [row]}, detail_payload()],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    result = run_source(adapter)

    assert [item.external_id for item in result.occurrences] == [JOB_ID]
    assert result.occurrences[0].company_size_source is None
    assert result.completed_listing is True
    assert not state_path.exists()


def test_duplicate_simplify_row_can_add_missing_company_size(tmp_path: Path) -> None:
    first = search_row()
    first.pop("company_size")
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [first]},
            {"status": "ok", "rows": [search_row()]},
            detail_payload(),
        ],
    )
    adapter = adapter_type()(
        config(search_terms=["Backend Engineer", "Python Engineer"], simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]
    occurrence = adapter.fetch_detail(reference)

    assert occurrence.company_size_source is not None
    assert occurrence.company_size_source.reported_size == "51-200"
    assert not state_path.exists()


def test_simplify_410_detail_preserves_http_410_closure_reason(tmp_path: Path) -> None:
    executable, state_path, _eval_log_path = fake_opencli(
        tmp_path,
        [
            {"status": "ok", "rows": [search_row()]},
            {"status": "closed", "reason": "http_410"},
        ],
    )
    adapter = adapter_type()(
        config(simplify_de_limit=1),
        opencli_executable=executable,
        timeout_seconds=5,
    )

    reference = adapter.discover()[0]

    with pytest.raises(ExplicitlyClosed) as error:
        adapter.fetch_detail(reference)
    assert error.value.reason == "http_410"
    assert not state_path.exists()


def test_simplify_listing_size_becomes_native_company_size_evidence() -> None:
    from job_scan.sources.simplify import lookup_company_size

    source = CompanySizeSource(
        source_name="simplify",
        lookup_url=f"https://api.simplify.jobs/v2/job-posting/:id/{JOB_ID}/company",
        public_url=f"https://simplify.jobs/jobs?jobId={JOB_ID}",
        source_title="Simplify job posting",
        reported_size="51-200",
    )

    result = lookup_company_size(
        source,
        "Example GmbH",
        datetime(2026, 8, 7, 9, 0, tzinfo=UTC),
    )

    assert result is not None
    assert result.reported_size == "51-200"
    assert result.minimum_employees == 51
    assert result.maximum_employees == 200
    assert result.source_name == "simplify"


def test_search_script_waits_for_delayed_public_search_contract() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.simplify import _search_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<main>Simplify jobs</main>")
        page.evaluate(
            """() => {
              window.contractChecks = 0;
              Object.defineProperty(performance, "getEntriesByType", {
                value: () => {
                  window.contractChecks += 1;
                  if (window.contractChecks < 3) return [];
                  return [{
                    name: "https://js-ha.simplify.jobs/collections/jobs/documents/search?x-typesense-api-key=scoped-test-key"
                  }];
                },
              });
              window.fetch = async (url) => {
                window.lastFetchUrl = String(url);
                return {
                  ok: true,
                  json: async () => ({hits: [{
                    document: {id: "job-1"},
                    text_match_info: {num_tokens_dropped: 0},
                  }]}),
                };
              };
            }"""
        )

        payload = page.evaluate(
            _search_script(
                config(posted_within_days=None),
                "Backend Engineer",
                "Berlin",
                1,
            )
        )

        assert payload == {"status": "ok", "rows": [{"id": "job-1"}]}
        assert page.evaluate("window.contractChecks") == 3
        request = urlsplit(page.evaluate("window.lastFetchUrl"))
        parameters = parse_qs(request.query)
        assert request.netloc == "js-ha.simplify.jobs"
        assert parameters["q"] == ["Backend Engineer"]
        assert parameters["per_page"] == ["1"]
        assert parameters["x-typesense-api-key"] == ["scoped-test-key"]
        assert parameters["filter_by"] == ["countries:=[`Germany`] && locations:`Berlin`"]
        assert "scoped-test-key" not in json.dumps(payload)
        browser.close()


def test_snapshot_page_script_keeps_only_simplify_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.simplify import _snapshot_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <style>.title-block {{ color: rgb(124, 58, 237); }}</style>
            <div data-testid="details-view" id="details-card-{JOB_ID}">
              <div>
                <div class="left-column">
                  <div><button>Full-Time</button></div>
                  <div class="title-block"><h1>Backend Engineer</h1><h2>Platform</h2></div>
                  <div><h2>Example GmbH</h2><span>51-200 employees</span></div>
                  <p>Developer tools</p>
                  <div></div>
                  <div>Berlin, Germany Remote Senior</div>
                  <div></div>
                  <div><h3>Required Skills</h3><span>Python</span></div>
                  <div></div>
                  <button>Get referrals → Referral advertisement</button>
                  <div>History</div>
                </div>
                <div class="right-column">
                  <div><h1>Add a resume to see your match</h1></div>
                  <div><h3>Get referred to Example GmbH</h3></div>
                  <div><h3>Job Description</h3><p>Build reliable services.</p></div>
                  <div><h3>Job Responsibilities</h3><p>Own production APIs.</p></div>
                  <div><h3>Job Requirements</h3><p>Strong Python skills.</p></div>
                  <div><h3>Simplify's Take</h3><p>Platform commentary.</p></div>
                  <div><h3>Benefits</h3><p>Remote work.</p></div>
                  <div><h3>Growth &amp; Insights and Company News</h3></div>
                </div>
              </div>
            </div>
            """
        )
        payload = page.evaluate(_snapshot_script(JOB_ID))

        assert payload["status"] == "ok"
        html = payload["html"]
        assert f'data-job-scan-snapshot="simplify:de:{JOB_ID}"' in html
        assert "Backend Engineer" in html
        assert "Example GmbH" in html
        assert "Berlin, Germany" in html
        assert "Build reliable services." in html
        assert "Own production APIs." in html
        assert "Strong Python skills." in html
        assert "Remote work." in html
        assert "rgb(124, 58, 237)" in html
        assert "Referral advertisement" not in html
        assert "Add a resume to see your match" not in html
        assert "Platform commentary." not in html
        assert "Company News" not in html
        assert ">History<" not in html
        browser.close()


def test_search_script_reports_visible_browser_challenge() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.simplify import _search_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<title>Just a moment...</title><main>Verify you are human</main>")
        page.evaluate(
            """() => {
              Object.defineProperty(performance, "getEntriesByType", {
                value: () => [{
                  name: "https://js-ha.simplify.jobs/collections/jobs/documents/search?x-typesense-api-key=scoped-test-key"
                }],
              });
              window.fetch = async () => ({
                ok: true,
                json: async () => ({hits: [{document: {id: "job-1"}}]}),
              });
            }"""
        )

        payload = page.evaluate(
            _search_script(
                config(posted_within_days=None),
                "Backend Engineer",
                "Berlin",
                1,
            )
        )

        assert payload == {"status": "challenge", "rows": []}
        browser.close()


def test_detail_script_reports_visible_browser_challenge() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.simplify import _detail_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<title>Access Denied</title><main>Verify you are human</main>")
        page.evaluate(
            """() => {
              window.fetch = async () => ({
                ok: true,
                json: async () => ({id: "job-1"}),
              });
            }"""
        )

        payload = page.evaluate(_detail_script(JOB_ID))

        assert payload == {"status": "challenge"}
        browser.close()


def test_search_script_keeps_results_that_dropped_query_tokens() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.simplify import _search_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<main>Simplify jobs</main>")
        page.evaluate(
            """() => {
              Object.defineProperty(performance, "getEntriesByType", {
                value: () => [{
                  name: "https://js-ha.simplify.jobs/collections/jobs/documents/search?x-typesense-api-key=scoped-test-key"
                }],
              });
              window.fetch = async () => ({
                ok: true,
                json: async () => ({hits: [
                  {
                    document: {id: "full-match"},
                    text_match_info: {num_tokens_dropped: 0},
                  },
                  {
                    document: {id: "loose-match"},
                    text_match_info: {num_tokens_dropped: 2},
                  },
                ]}),
              });
            }"""
        )

        payload = page.evaluate(
            _search_script(
                config(posted_within_days=None),
                "Distributed Systems Engineer",
                "",
                10,
            )
        )

        assert payload == {
            "status": "ok",
            "rows": [{"id": "full-match"}, {"id": "loose-match"}],
        }
        browser.close()
