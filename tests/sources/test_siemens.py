from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed, run_source
from job_scan.sources.base import JobReference
from job_scan.sources.siemens import SiemensAdapter

ORIGIN = "https://jobs.siemens.com"
SEARCH_PREFIX = f"{ORIGIN}/en_US/externaljobs/SearchJobs"
DETAIL_URL = f"{ORIGIN}/en_US/externaljobs/JobDetail/513387"
SEARCH_PATTERN = re.compile(re.escape(SEARCH_PREFIX) + r"(?:/.*)?")


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Backend Java Developer"],
        "locations": ["Berlin"],
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter(tmp_path: Path, *, app_config: AppConfig | None = None) -> SiemensAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return SiemensAdapter(app_config or config(), client)


def listing_html(
    job_id: str,
    title: str,
    *,
    location: str = "Berlin, Berlin, Germany",
) -> str:
    detail_url = f"{ORIGIN}/en_US/externaljobs/JobDetail/{job_id}"
    return f"""
    <article class="article article--result 1">
      <div class="article__header__text">
        <h3 class="article__header__text__title">
          <a href="{detail_url}">{html.escape(title)}</a>
        </h3>
        <div class="article__header__text__subtitle">
          <span class="list-item-location">{html.escape(location)}</span>
          <span class="separator">•</span>
          <span class="list-item-jobId">Job ID: {html.escape(job_id)}</span>
          <span class="separator">•</span>
          <span class="list-item-family">Research &amp; Development</span>
        </div>
      </div>
    </article>
    """


def search_html(
    listings: list[tuple[str, str, str]],
    *,
    next_url: str | None = None,
) -> str:
    cards = "".join(
        listing_html(job_id, title, location=location) for job_id, title, location in listings
    )
    next_link = (
        ""
        if next_url is None
        else (
            '<a aria-label="Go to Next Page, Number 2" '
            f'href="{html.escape(next_url)}">Next &gt;&gt;</a>'
        )
    )
    return f"<html><body><main>{cards}<nav>{next_link}</nav></main></body></html>"


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


@respx.mock
def test_discover_queries_each_unique_term_and_accepts_every_official_result(
    tmp_path: Path,
) -> None:
    next_url = (
        f"{SEARCH_PREFIX}/Backend%20Java%20Developer/"
        "?42386=%5B812132%5D&42386_format=17546&42387=%5B812528%5D"
        "&42387_format=17547&listFilterMode=1"
        "&folderRecordsPerPage=6&folderOffset=6"
    )
    route = respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(
                200,
                text=search_html(
                    [
                        (
                            "513387",
                            "Software Developer Authentication & Authorization (w/m/d)",
                            "Berlin, Berlin, Germany",
                        ),
                        ("500001", "Backend Engineer", "Hamburg, Hamburg, Germany"),
                    ],
                    next_url=next_url,
                ),
            ),
            httpx.Response(
                200,
                text=search_html(
                    [
                        (
                            "513387",
                            "Software Developer Authentication & Authorization (w/m/d)",
                            "Berlin, Berlin, Germany",
                        ),
                        ("511119", "Platform Engineer", "Multiple Locations"),
                    ]
                ),
            ),
        ]
    )
    siemens = adapter(
        tmp_path,
        app_config=config(
            search_terms=[
                "Backend Java Developer",
                "backend java developer",
                " ",
            ],
            locations=["Berlin", " berlin "],
        ),
    )

    references = siemens.discover()

    assert [reference.external_id for reference in references] == [
        "513387",
        "500001",
        "511119",
    ]
    assert references[0].source is SourceKind.SIEMENS
    assert references[0].source_instance == "siemens"
    assert references[0].listing_company == "Siemens"
    assert references[0].listing_location == "Berlin, Berlin, Germany"
    assert str(references[0].detail_url) == DETAIL_URL
    assert route.call_count == 2
    assert route.calls[0].request.url.path.endswith("/SearchJobs/Backend Java Developer")
    assert _query(route.calls[0].request) == {
        "42386": ["[812132]"],
        "42386_format": ["17546"],
        "42387": ["[812528]"],
        "42387_format": ["17547"],
        "folderRecordsPerPage": ["6"],
        "listFilterMode": ["1"],
    }
    assert _query(route.calls[1].request)["folderOffset"] == ["6"]


@respx.mock
def test_discover_searches_all_german_jobs_when_setup_has_no_locations(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html([("513387", "Software Developer", "Forchheim, Bayern, Germany")]),
        )
    )
    siemens = adapter(tmp_path, app_config=config(locations=[]))

    references = siemens.discover()

    assert [reference.external_id for reference in references] == ["513387"]
    assert route.calls[0].request.url.path.endswith("/SearchJobs/Backend Java Developer")


@respx.mock
def test_discover_ignores_unmapped_city_when_another_city_is_supported(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    ("513387", "Software Developer", "Berlin, Berlin, Germany"),
                    ("513388", "Unsupported city job", "Nowherezzzz, Berlin, Germany"),
                ]
            ),
        )
    )
    siemens = adapter(
        tmp_path,
        app_config=config(locations=["Berlin", "Nowherezzzz"]),
    )

    references = siemens.discover()

    assert [reference.external_id for reference in references] == ["513387", "513388"]
    assert _query(route.calls[0].request)["42387"] == ["[812528]"]


@respx.mock
def test_discover_skips_search_when_no_configured_city_is_supported(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(200, text=search_html([]))
    )
    siemens = adapter(
        tmp_path,
        app_config=config(locations=["Nowherezzzz"]),
    )

    assert siemens.discover() == []
    assert route.call_count == 0


@respx.mock
def test_discover_accepts_location_returned_by_official_city_query(
    tmp_path: Path,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [("513387", "Software Developer", "Frankfurt am Main, Hessen, Germany")]
            ),
        )
    )
    siemens = adapter(
        tmp_path,
        app_config=config(locations=["Berlin"]),
    )

    references = siemens.discover()

    assert [reference.external_id for reference in references] == ["513387"]


def detail_html(
    *,
    job_id: str = "513387",
    company: str | None = "Siemens Healthineers AG",
    locations: tuple[str, ...] = (
        "Forchheim -  - Germany",
        "Berlin - Berlin - Germany",
        "Kiel - Schleswig-Holstein - Germany",
    ),
) -> str:
    location_items = "".join(f"<li>{html.escape(location)}</li>" for location in locations)
    return f"""
    <html><body><main>
      <section>
        <div class="section__header__text">
          <h3 class="section__header__text__title">
            Software Developer Authentication &amp; Authorization (w/m/d)
          </h3>
        </div>
        <article class="article article--details regular-fields--cols-2Z">
          <div class="article__content" id="section0__content">
            <div class="article__content__view__field">
              <div class="article__content__view__field__label">Job ID</div>
              <div class="article__content__view__field__value">{job_id}</div>
            </div>
            <div class="article__content__view__field">
              <div class="article__content__view__field__label">Posted since</div>
              <div class="article__content__view__field__value">15-Jul-2026</div>
            </div>
            {f'''
            <div class="article__content__view__field">
              <div class="article__content__view__field__label">Company</div>
              <div class="article__content__view__field__value">
                {html.escape(company)}
              </div>
            </div>
            ''' if company is not None else ''}
            <div class="article__content__view__field tf_locations">
              <div class="article__content__view__field__label">Location(s)</div>
              <div class="article__content__view__field__value">
                <ul class="list--locations">
                  {location_items}
                </ul>
              </div>
            </div>
          </div>
        </article>
        <article class="article article--details">
          <div class="article__content" id="section1__content">
            <div class="article__content__view__field__value">
              <p>Build secure backend services.</p>
              <ul><li>Develop OAuth2 and OIDC integrations.</li></ul>
            </div>
          </div>
        </article>
      </section>
    </main></body></html>
    """


def reference() -> JobReference:
    return JobReference(
        source=SourceKind.SIEMENS,
        source_instance="siemens",
        external_id="513387",
        detail_url=DETAIL_URL,
        platform_url=DETAIL_URL,
        listing_title="Software Developer Authentication & Authorization (w/m/d)",
        listing_company="Siemens",
        listing_location="Multiple Locations",
    )


@respx.mock
def test_fetch_detail_reads_the_official_fields_and_description(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    siemens = adapter(tmp_path)

    occurrence = siemens.fetch_detail(reference())

    assert occurrence.source is SourceKind.SIEMENS
    assert occurrence.source_instance == "siemens"
    assert occurrence.external_id == "513387"
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.company == "Siemens Healthineers AG"
    assert occurrence.title == ("Software Developer Authentication & Authorization (w/m/d)")
    assert occurrence.location == (
        "Forchheim, Germany; Berlin, Berlin, Germany; Kiel, Schleswig-Holstein, Germany"
    )
    assert occurrence.description == (
        "Build secure backend services. Develop OAuth2 and OIDC integrations."
    )
    assert occurrence.posted_at == date(2026, 7, 15)
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_fetch_detail_falls_back_to_listing_company_when_detail_omits_company(
    tmp_path: Path,
) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html(company=None))
    )
    siemens = adapter(tmp_path)

    occurrence = siemens.fetch_detail(reference())

    assert occurrence.company == "Siemens"
    assert occurrence.posted_at == date(2026, 7, 15)
    assert occurrence.description == (
        "Build secure backend services. Develop OAuth2 and OIDC integrations."
    )
    assert occurrence.detail_complete is True


@respx.mock
def test_run_source_accepts_detail_location_returned_by_official_city_query(
    tmp_path: Path,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    (
                        "513387",
                        "Software Developer Authentication & Authorization (w/m/d)",
                        "Multiple Locations",
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(locations=("Hamburg - Hamburg - Germany",)),
        )
    )
    siemens = adapter(tmp_path, app_config=config(locations=["Berlin"]))

    result = run_source(siemens)

    assert [occurrence.external_id for occurrence in result.occurrences] == ["513387"]
    assert result.occurrences[0].location == "Hamburg, Hamburg, Germany"
    assert result.discovered_source_job_keys == {"siemens:siemens:513387"}
    assert result.errors == []


@respx.mock
def test_fetch_detail_rejects_a_page_for_another_job(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html(job_id="999999")))
    siemens = adapter(tmp_path)

    with pytest.raises(InvalidResponse, match="Job ID did not match"):
        siemens.fetch_detail(reference())


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(404, "http_404"), (410, "http_410")],
)
@respx.mock
def test_fetch_detail_marks_explicit_http_closure_statuses_closed(
    tmp_path: Path,
    status_code: int,
    reason: str,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(status_code))
    siemens = adapter(tmp_path)

    with pytest.raises(ExplicitlyClosed) as error:
        siemens.fetch_detail(reference())

    assert error.value.source_job_key == "siemens:siemens:513387"
    assert error.value.reason == reason


@respx.mock
def test_fetch_detail_marks_official_error_redirect_closed(tmp_path: Path) -> None:
    detail_route = respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "/Error"})
    )
    error_route = respx.get(f"{ORIGIN}/Error").mock(return_value=httpx.Response(404))
    siemens = adapter(tmp_path)

    with pytest.raises(ExplicitlyClosed) as error:
        siemens.fetch_detail(reference())

    assert error.value.source_job_key == "siemens:siemens:513387"
    assert error.value.reason == "http_404"
    assert detail_route.call_count == 1
    assert error_route.call_count == 1


@respx.mock
def test_new_siemens_job_carries_transient_snapshot_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    snapshot_html = (
        '<html data-job-scan-snapshot="siemens:siemens:513387">'
        "<body>Senior Software Engineer</body></html>"
    )
    monkeypatch.setattr(
        "job_scan.sources.siemens.capture_browser_snapshot",
        lambda **_arguments: snapshot_html,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = SiemensAdapter(
        config(),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(reference())

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None


@respx.mock
def test_siemens_snapshot_failure_does_not_discard_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    monkeypatch.setattr(
        "job_scan.sources.siemens.capture_browser_snapshot",
        lambda **_arguments: None,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = SiemensAdapter(
        config(),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(reference())

    assert occurrence.description
    assert occurrence.job_snapshot_html is None
    assert occurrence.job_snapshot_error_code == "snapshot_capture_failed"


def test_snapshot_page_script_keeps_only_siemens_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.siemens import _snapshot_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <style>.js_views { color: rgb(0, 94, 184); }</style>
            <main>
              <section class="js_views">
                <header><h3>Senior Software Engineer</h3></header>
                <article><dl><dt>Job ID</dt><dd>513387</dd><dt>Company</dt><dd>Siemens AG</dd></dl></article>
                <article><h4>What are my responsibilities?</h4><p>Build industrial software.</p>
                  <h4>What do I need to qualify?</h4><p>Java experience.</p></article>
              </section>
              <article class="article--actions"><button>Apply now</button></article>
              <section id="widgetRelatedJobs"><h2>Similar jobs</h2><p>Recommendation.</p></section>
            </main>
            """
        )

        payload = page.evaluate(_snapshot_script("513387"))

        assert payload["status"] == "ok"
        html = payload["html"]
        assert 'data-job-scan-snapshot="siemens:siemens:513387"' in html
        assert "Senior Software Engineer" in html
        assert "Build industrial software." in html
        assert "Java experience." in html
        assert "rgb(0, 94, 184)" in html
        assert "Apply now" not in html
        assert "Recommendation." not in html
        assert "http://" not in html
        assert "https://" not in html
        browser.close()
