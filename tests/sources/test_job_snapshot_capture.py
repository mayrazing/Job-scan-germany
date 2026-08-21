from __future__ import annotations

from datetime import date

import pytest
from pydantic import HttpUrl

from job_scan.domain import AvailabilityStatus, SourceKind, SourceOccurrence
from job_scan.sources import job_snapshot_capture as capture_module
from job_scan.sources.job_snapshot_capture import browser_snapshot_script


def test_browser_snapshot_script_waits_for_dynamic_job_content() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    script = browser_snapshot_script(
        """
        const title = document.querySelector("h1");
        if (!title) {
          return {status: "unavailable", error_code: "structure_mismatch"};
        }
        return {status: "ok", html: title.textContent};
        """
    )

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<main></main><script>"
            "setTimeout(() => { document.querySelector('main').innerHTML = "
            "'<h1>Loaded job</h1>'; }, 100);"
            "</script>"
        )

        assert page.evaluate(script) == {"status": "ok", "html": "Loaded job"}
        browser.close()


def test_browser_snapshot_script_removes_active_legacy_css() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    script = browser_snapshot_script(
        """
        return buildJobSnapshot({
          snapshotKey: "test:default:1",
          title: "Test job",
          sourceLabel: "Test",
          accent: "#000",
          roots: [document.querySelector("main")],
        });
        """
    )

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<style>.old { behavior: url(active.htc); width: expression(alert(1)); "
            "color: red; }</style><main><p class='old'>Job information</p></main>"
        )

        html = page.evaluate(script)["html"]

        assert "behavior:" not in html
        assert "expression(" not in html
        assert "color: red" in html
        browser.close()


@pytest.mark.parametrize(
    ("source", "source_instance", "external_id", "url", "source_name"),
    [
        (
            SourceKind.ARBEITSAGENTUR,
            "default",
            "10000-1234567890-S",
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S",
            "arbeitsagentur",
        ),
        (
            SourceKind.BOSCH,
            "bosch",
            "REF300001A",
            "https://jobs.bosch.com/job/REF300001A",
            "Bosch",
        ),
        (
            SourceKind.DALLMEIER,
            "dallmeier",
            "java-developer-w-m-d",
            "https://www.dallmeier.com/career/java-developer-w-m-d",
            "Dallmeier",
        ),
        (
            SourceKind.DHL,
            "dhl",
            "DPDHGLOBALAV361651ENAMEREXTERNAL",
            "https://careers.dhl.com/global/en/job/DPDHGLOBALAV361651ENAMEREXTERNAL",
            "dhl",
        ),
        (
            SourceKind.GLASSDOOR,
            "de",
            "1010232175081",
            "https://www.glassdoor.de/job-listing/example-JV_IC2622109_KO0,7_KE8,15.htm?jl=1010232175081",
            "glassdoor",
        ),
        (
            SourceKind.INDEED,
            "de",
            "8c683c2df48291d7",
            "https://de.indeed.com/viewjob?jk=8c683c2df48291d7",
            "indeed",
        ),
        (
            SourceKind.LINKEDIN,
            "default",
            "4454519520",
            "https://www.linkedin.com/jobs/view/4454519520",
            "linkedin",
        ),
        (
            SourceKind.SIEMENS,
            "siemens",
            "513387",
            "https://jobs.siemens.com/careers/job/5631561234513387",
            "Siemens",
        ),
        (
            SourceKind.SIMPLIFY,
            "de",
            "4189a132-d02f-4d3a-90ab-df09f5743198",
            "https://simplify.jobs/jobs?jobId=4189a132-d02f-4d3a-90ab-df09f5743198",
            "simplify",
        ),
        (
            SourceKind.SMARTRECRUITERS,
            "boschgroup",
            "744000143108870",
            "https://jobs.smartrecruiters.com/BoschGroup/744000143108870",
            "smartrecruiters",
        ),
        (
            SourceKind.STEPSTONE,
            "de",
            "13889830",
            "https://www.stepstone.de/stellenangebote--role--13889830-inline.html",
            "stepstone",
        ),
        (
            SourceKind.SUCCESSFACTORS,
            "rohdeschwarz",
            "707",
            "https://career.rohde-schwarz.com/jobs/707",
            "Rohde & Schwarz",
        ),
        (
            SourceKind.TELEKOM,
            "telekom",
            "907522",
            "https://www.telekom.com/en/careers/job/907522",
            "Deutsche Telekom",
        ),
        (
            SourceKind.THYSSENKRUPP,
            "thyssenkrupp",
            "967315",
            "https://jobs.thyssenkrupp.com/job/967315",
            "thyssenkrupp",
        ),
    ],
)
def test_single_job_capture_dispatches_every_automatic_source(
    monkeypatch: pytest.MonkeyPatch,
    source: SourceKind,
    source_instance: str,
    external_id: str,
    url: str,
    source_name: str,
) -> None:
    capture = getattr(capture_module, "capture_source_job_snapshot_html", None)
    assert capture is not None
    requests: list[dict[str, object]] = []

    def browser_capture(**request: object) -> str:
        requests.append(request)
        return "captured"

    monkeypatch.setattr(capture_module, "capture_browser_snapshot", browser_capture)
    occurrence = SourceOccurrence(
        source=source,
        source_instance=source_instance,
        external_id=external_id,
        source_generation=1,
        url=HttpUrl(url),
        company="Example GmbH",
        title="Backend Engineer",
        location="Berlin",
        description="Build services.",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:job",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )

    assert capture(occurrence) == "captured"
    assert len(requests) == 1
    assert requests[0]["url"] == url
    assert requests[0]["source_name"] == source_name
    assert isinstance(requests[0]["script"], str)
    assert requests[0]["script"]


def test_single_job_capture_does_not_open_manual_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = getattr(capture_module, "capture_source_job_snapshot_html", None)
    assert capture is not None

    def unexpected_capture(**_request: object) -> str:
        raise AssertionError("manual job page was opened")

    monkeypatch.setattr(capture_module, "capture_browser_snapshot", unexpected_capture)
    occurrence = SourceOccurrence(
        source=SourceKind.MANUAL,
        source_instance="careers.example",
        external_id="manual-1",
        source_generation=1,
        url=HttpUrl("https://careers.example/jobs/1"),
        company="Example GmbH",
        title="Backend Engineer",
        location="Berlin",
        description="Build services.",
        posted_at=date(2026, 8, 1),
        content_hash="sha256:manual",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )

    assert capture(occurrence) is None
