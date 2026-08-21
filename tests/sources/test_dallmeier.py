from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.base import JobReference, ListingFilteredOut
from job_scan.sources.dallmeier import DallmeierAdapter

ORIGIN = "https://www.dallmeier.com"
CAREERS_URL = f"{ORIGIN}/about-us/careers"
JAVA_PATH = "/about-us/careers/java-developer-w/m/d-backend"
HARDWARE_PATH = "/about-us/careers/entwicklungsingenieur-hardware-w/m/d"


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Java Backend", "Software Engineer"],
        "locations": ["Munich"],
        "posted_within_days": 3,
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter(
    tmp_path: Path,
    *,
    app_config: AppConfig | None = None,
) -> DallmeierAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return DallmeierAdapter(app_config or config(), client)


def careers_html() -> str:
    return f"""
    <html><body><main>
      <section class="acontent">
        <h2>Career Opportunities</h2>
        <div class="container"><div class="row content">
          <div class="col-lg-9">
            <div class="frame frame-type-textpic"><div class="ce-bodytext">
              <p><strong>Software Development</strong></p>
              <ul>
                <li><a href="{JAVA_PATH}">Softwareentwickler Java Backend</a></li>
                <li><a href="{HARDWARE_PATH}">Entwicklungsingenieur Hardware</a></li>
                <li><a href="{JAVA_PATH}">Duplicate Java link</a></li>
                <li><a href="/about-us/careers/online-application">Apply online</a></li>
                <li>
                  <a href="/about-us/careers/auszubildende/r-fachinformatiker-w/m/d">
                    Fachinformatiker Ausbildung
                  </a>
                </li>
              </ul>
            </div></div>
          </div>
          <div class="col-lg-3">
            <div class="ce-bodytext">
              <a href="/about-us/careers/online-application">Online Application</a>
            </div>
          </div>
        </div></div>
      </section>
      <h2>Apprenticeship</h2>
      <a href="/about-us/careers/auszubildende/r-fachinformatiker-w/m/d">
        Fachinformatiker Ausbildung
      </a>
    </main></body></html>
    """


def detail_html(
    *,
    canonical_path: str = JAVA_PATH,
    location: str = "München + Stuttgart – Dallmeier Systems",
) -> str:
    return f"""
    <html><head>
      <link rel="canonical" href="{ORIGIN}{canonical_path}">
    </head><body><main>
      <div class="page-header"><h1>(Senior) Java Backend Developer (w/m/d)</h1></div>
      <div class="frame frame-type-header">
        <header><h2>Job Description</h2></header>
      </div>
      <div class="frame frame-type-textpic">
        <header><h4>Standort: {location}</h4></header>
        <div class="ce-bodytext">
          <p>Build backend services.</p>
          <ul><li>Operate Kubernetes clusters.</li></ul>
        </div>
      </div>
      <h2>Contact</h2>
      <div class="ce-bodytext">Generic footer contact.</div>
    </main></body></html>
    """


def source_reference() -> JobReference:
    return JobReference(
        source=SourceKind.DALLMEIER,
        source_instance="dallmeier",
        external_id="java-developer-w/m/d-backend",
        detail_url=f"{ORIGIN}{JAVA_PATH}",
        platform_url=f"{ORIGIN}{JAVA_PATH}",
        listing_title="Softwareentwickler Java Backend",
        listing_company="Dallmeier",
        listing_location="",
        listing_posted_at=None,
    )


@respx.mock
def test_discover_reads_each_standard_job_once_without_upstream_search_filters(
    tmp_path: Path,
) -> None:
    careers_route = respx.get(CAREERS_URL).mock(
        return_value=httpx.Response(200, text=careers_html())
    )
    source = adapter(tmp_path)

    references = source.discover()

    assert [reference.external_id for reference in references] == [
        "java-developer-w/m/d-backend",
        "entwicklungsingenieur-hardware-w/m/d",
    ]
    assert references[0].source is SourceKind.DALLMEIER
    assert references[0].source_instance == "dallmeier"
    assert str(references[0].detail_url) == f"{ORIGIN}{JAVA_PATH}"
    assert references[0].listing_title == "Softwareentwickler Java Backend"
    assert references[0].listing_company == "Dallmeier"
    assert references[0].listing_location == ""
    assert references[0].listing_posted_at is None
    assert careers_route.call_count == 1


@respx.mock
def test_discover_rejects_a_page_without_the_authoritative_job_section(
    tmp_path: Path,
) -> None:
    respx.get(CAREERS_URL).mock(
        return_value=httpx.Response(200, text="<html><body>Careers</body></html>")
    )

    with pytest.raises(InvalidResponse, match="Career Opportunities"):
        adapter(tmp_path).discover()


@respx.mock
def test_discover_does_not_fall_through_to_a_later_contact_section(
    tmp_path: Path,
) -> None:
    html = """
    <html><body><main>
      <section class="acontent">
        <h2>Career Opportunities</h2>
        <div class="container"><div class="row content">
          <div class="col-lg-9"><p>Broken listing structure</p></div>
          <div class="col-lg-3">
            <div class="ce-bodytext">
              <a href="/about-us/careers/online-application">Online Application</a>
            </div>
          </div>
        </div></div>
      </section>
    </main></body></html>
    """
    respx.get(CAREERS_URL).mock(return_value=httpx.Response(200, text=html))

    with pytest.raises(InvalidResponse, match="ordinary job list"):
        adapter(tmp_path).discover()


@respx.mock
def test_fetch_detail_reads_complete_job_and_matches_english_city_alias(
    tmp_path: Path,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(return_value=httpx.Response(200, text=detail_html()))
    source = adapter(tmp_path, app_config=config(locations=["Munich"]))

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.source is SourceKind.DALLMEIER
    assert occurrence.source_instance == "dallmeier"
    assert occurrence.external_id == "java-developer-w/m/d-backend"
    assert str(occurrence.url) == f"{ORIGIN}{JAVA_PATH}"
    assert occurrence.company == "Dallmeier Systems GmbH"
    assert occurrence.title == "(Senior) Java Backend Developer (w/m/d)"
    assert occurrence.location == "München, Germany; Stuttgart, Germany"
    assert occurrence.description == ("Build backend services. Operate Kubernetes clusters.")
    assert occurrence.posted_at is None
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_fetch_detail_filters_out_a_job_outside_all_setup_cities(
    tmp_path: Path,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(return_value=httpx.Response(200, text=detail_html()))
    source = adapter(tmp_path, app_config=config(locations=["Berlin", "Hamburg"]))

    with pytest.raises(ListingFilteredOut):
        source.fetch_detail(source_reference())


@pytest.mark.parametrize(
    ("raw_location", "expected_location"),
    [
        ("München | Vertragsart: Vollzeit", "München, Germany"),
        (
            "Aschheim bei München | Beginn: ab sofort, unbefristet",
            "Aschheim bei München, Germany",
        ),
    ],
)
@respx.mock
def test_fetch_detail_excludes_contract_metadata_from_standort(
    tmp_path: Path,
    raw_location: str,
    expected_location: str,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(
        return_value=httpx.Response(
            200,
            text=detail_html(location=raw_location),
        )
    )

    occurrence = adapter(
        tmp_path,
        app_config=config(locations=[]),
    ).fetch_detail(source_reference())

    assert occurrence.location == expected_location


@respx.mock
def test_fetch_detail_treats_aschheim_near_munich_as_a_munich_location(
    tmp_path: Path,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(
        return_value=httpx.Response(
            200,
            text=detail_html(location="Aschheim bei München | Beginn: ab sofort, unbefristet"),
        )
    )

    occurrence = adapter(
        tmp_path,
        app_config=config(locations=["Munich"]),
    ).fetch_detail(source_reference())

    assert occurrence.location == "Aschheim bei München, Germany"


@respx.mock
def test_fetch_detail_rejects_a_page_for_another_job(tmp_path: Path) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(
        return_value=httpx.Response(
            200,
            text=detail_html(canonical_path=HARDWARE_PATH),
        )
    )

    with pytest.raises(InvalidResponse, match="did not match"):
        adapter(tmp_path, app_config=config(locations=[])).fetch_detail(source_reference())


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
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(return_value=httpx.Response(status_code))

    with pytest.raises(ExplicitlyClosed) as error:
        adapter(tmp_path).fetch_detail(source_reference())

    assert error.value.source_job_key == ("dallmeier:dallmeier:java-developer-w/m/d-backend")
    assert error.value.reason == reason


@respx.mock
def test_new_dallmeier_job_carries_transient_snapshot_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(
        return_value=httpx.Response(200, text=detail_html())
    )
    snapshot_html = (
        '<html data-job-scan-snapshot="dallmeier:dallmeier:java-developer-w/m/d-backend">'
        "<body>Java Backend Developer</body></html>"
    )
    monkeypatch.setattr(
        "job_scan.sources.dallmeier.capture_browser_snapshot",
        lambda **_arguments: snapshot_html,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = DallmeierAdapter(
        config(locations=[]),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None


@respx.mock
def test_dallmeier_snapshot_failure_does_not_discard_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(f"{ORIGIN}{JAVA_PATH}").mock(
        return_value=httpx.Response(200, text=detail_html())
    )
    monkeypatch.setattr(
        "job_scan.sources.dallmeier.capture_browser_snapshot",
        lambda **_arguments: None,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = DallmeierAdapter(
        config(locations=[]),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.description
    assert occurrence.job_snapshot_html is None
    assert occurrence.job_snapshot_error_code == "snapshot_capture_failed"


def test_snapshot_page_script_keeps_only_dallmeier_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.dallmeier import _snapshot_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            f"""
            <head><link rel="canonical" href="{ORIGIN}{JAVA_PATH}">
              <style>.page-header {{ color: rgb(14, 73, 100); }}</style></head>
            <main>
              <div class="page-header"><h1>(Senior) Java Backend Developer</h1></div>
              <section id="acontent1860" class="acontent">
                <h2>Job Description</h2><div class="container"><div class="row content">
                  <div class="col-lg-9"><h3>Standort: München</h3>
                    <p>Build backend services.</p><h3>Your profile</h3><p>Java experience.</p></div>
                  <div class="col-lg-3"><h2>Contact</h2><p>Generic footer contact.</p></div>
                </div></div>
              </section>
              <section><h2>More careers</h2><p>Unrelated jobs.</p></section>
            </main>
            """
        )

        payload = page.evaluate(_snapshot_script("java-developer-w/m/d-backend"))

        assert payload["status"] == "ok"
        html = payload["html"]
        assert (
            'data-job-scan-snapshot="dallmeier:dallmeier:java-developer-w/m/d-backend"'
            in html
        )
        assert "(Senior) Java Backend Developer" in html
        assert "Build backend services." in html
        assert "Java experience." in html
        assert "rgb(14, 73, 100)" in html
        assert "Generic footer contact." not in html
        assert "Unrelated jobs." not in html
        assert "http://" not in html
        assert "https://" not in html
        browser.close()
