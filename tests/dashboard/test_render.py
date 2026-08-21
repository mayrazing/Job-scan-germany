from __future__ import annotations

import importlib.resources
import re
from datetime import UTC, date, datetime

import pytest
from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.dashboard.render import render_dashboard
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    CompanyIndustryEvidence,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.job_snapshot import JobSnapshotReference

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
PAYLOAD = "<script>alert(1)</script>"


def _occurrence(key: str) -> SourceOccurrence:
    return SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id=key,
        source_generation=1,
        url=HttpUrl(f"https://jobs.example/{key}"),
        company="Acme",
        title=PAYLOAD,
        location="Berlin",
        description="Build services",
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        availability_status=AvailabilityStatus.ACTIVE,
    )


def _job(
    key: str,
    availability: AvailabilityStatus = AvailabilityStatus.ACTIVE,
) -> JobRecord:
    occurrence = _occurrence(key)
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company="Acme",
        title=PAYLOAD,
        location="Berlin",
        url=HttpUrl(f"https://jobs.example/{key}"),
        description="Build services",
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=availability,
        machine_status=MachineStatus.ELIGIBLE,
        user_status_updated_at=NOW,
        score=91,
        reason=PAYLOAD,
        labels=["Remote"],
        ai_review=AIReview(
            job_key=key,
            german_requirement="optional",
            visa_sponsorship="not_mentioned",
            existing_work_authorization="not_mentioned",
            citizenship_requirement="none",
            security_clearance="none",
            staffing_agency="no",
            eligibility_evidence=[PAYLOAD],
            company_industry=None,
            company_industry_confidence="low",
            company_industry_evidence=[],
            score=91,
            reason=PAYLOAD,
            confidence="high",
        ),
    )


def test_render_contains_review_groups_without_availability_history() -> None:
    restored = _job("restored")
    restored.machine_status = MachineStatus.EXCLUDED
    restored.manual_override = "show"
    closed_applied = _job("closed-applied", AvailabilityStatus.CLOSED)
    closed_applied.user_status = UserStatus.APPLIED
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=42),
        jobs=[
            restored,
            _job("stale", AvailabilityStatus.STALE),
            closed_applied,
        ],
    )

    html = render_dashboard(snapshot, snapshot)
    soup = BeautifulSoup(html, "html.parser")

    assert soup.select_one('meta[name="job-scan-revision"][content="42"]') is not None
    for group_id in (
        "recommended",
        "saved",
        "pending",
        "excluded",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ):
        assert soup.select_one(f"section#{group_id}.job-group") is not None
    assert soup.select_one("#history") is None
    assert soup.select("[data-history-filter], [data-history-kind]") == []
    assert soup.select_one('#applied [data-job-key="closed-applied"]') is not None
    assert soup.select_one('[data-job-key="stale"]') is None
    assert "linkedin" in soup.get_text(" ", strip=True)
    assert "Evidence" in soup.get_text(" ", strip=True)
    assert "Restored" in soup.get_text(" ", strip=True)


def test_render_distinguishes_pending_ai_review_failures_from_source_failures() -> None:
    review_failure = _job("review-failure")
    review_failure.machine_status = MachineStatus.PENDING
    review_failure.last_error = "schema_validation"
    source_failure = _job("source-failure")
    source_failure.machine_status = MachineStatus.PENDING_SOURCE
    source_failure.last_error = "invalid_response"

    page = BeautifulSoup(
        render_dashboard(
            Snapshot(
                meta=StoreMeta(data_revision=42),
                jobs=[
                    review_failure,
                    source_failure,
                ],
            )
        ),
        "html.parser",
    )

    review_error = page.select_one('[data-job-key="review-failure"] .review-error')
    assert review_error is not None
    assert "AI review failed. Error: schema validation." in review_error.get_text(
        " ", strip=True
    )
    assert page.select_one('[data-job-key="source-failure"] .review-error') is None
    source_error = page.select_one('[data-job-key="source-failure"] .source-error')
    assert source_error is not None
    assert source_error.get_text(" ", strip=True) == (
        "Source detail unavailable. AI review was not run. Error: invalid response."
    )


def test_render_shows_snapshot_links_and_automatic_source_triggers() -> None:
    saved = _job("saved-snapshot")
    saved.source_occurrences[0].job_snapshot = JobSnapshotReference(
        snapshot_id=f"sha256:{'a' * 64}",
        captured_at=NOW,
    )
    unavailable = _job("unavailable-snapshot")
    unavailable.source_occurrences[0].job_snapshot_error_code = (
        "snapshot_capture_failed"
    )
    old = _job("old-without-snapshot")
    manual = _job("manual-without-snapshot")
    manual.source_occurrences[0].source = SourceKind.MANUAL
    manual.source_occurrences[0].source_instance = "careers.example"
    manual.source_occurrences[0].external_id = "manual-1"
    manual.primary_source_occurrence_key = (
        manual.source_occurrences[0].source_occurrence_key
    )

    page = BeautifulSoup(
        render_dashboard(
            Snapshot(
                meta=StoreMeta(data_revision=42),
                jobs=[saved, unavailable, old, manual],
            )
        ),
        "html.parser",
    )

    saved_link = page.select_one(
        '[data-job-key="saved-snapshot"] [data-job-snapshot]'
    )
    assert saved_link is not None
    assert saved_link.get_text(" ", strip=True) == "Job snapshot"
    assert saved_link.get("href") == f"/api/job-snapshots/sha256:{'a' * 64}"
    assert saved_link.get("target") == "_blank"
    regenerate = page.select_one(
        '[data-job-key="saved-snapshot"] [data-job-snapshot-regenerate]'
    )
    assert regenerate is not None
    assert regenerate.get_text(" ", strip=True) == "Regenerate snapshot"
    assert (
        page.select_one(
            '[data-job-key="saved-snapshot"] [data-job-snapshot-trigger]'
        )
        is None
    )
    assert page.select_one(
        '[data-job-key="unavailable-snapshot"] [data-job-snapshot-unavailable]'
    ).get_text(" ", strip=True) == "Snapshot unavailable"
    retry = page.select_one(
        '[data-job-key="unavailable-snapshot"] [data-job-snapshot-trigger]'
    )
    assert retry is not None
    assert retry.get_text(" ", strip=True) == "Retry snapshot"
    generate = page.select_one(
        '[data-job-key="old-without-snapshot"] [data-job-snapshot-trigger]'
    )
    assert generate is not None
    assert generate.get_text(" ", strip=True) == "Generate snapshot"
    assert page.select_one(
        '[data-job-key="saved-snapshot"] [data-job-snapshot-trigger]'
    ) is None
    manual_generate = page.select_one(
        '[data-job-key="manual-without-snapshot"] [data-job-snapshot-trigger]'
    )
    assert manual_generate is not None
    assert manual_generate.get_text(" ", strip=True) == "Generate snapshot"


def test_render_shows_company_industry_method_source_and_jd_evidence() -> None:
    inferred = _job("inferred")
    inferred.company_industry = CompanyIndustryEvidence(
        company_name="Acme",
        industry="Industrial Automation",
        source_url=inferred.url,
        source_title="AI inference from complete job description",
        checked_at=NOW,
        confidence="medium",
        lookup_method="ai",
        source_name="ai",
        evidence=["Build services"],
    )
    native = _job("native")
    native.company_industry = CompanyIndustryEvidence(
        company_name="Acme",
        industry="Software Development",
        source_url="https://example.test/companies/acme",
        source_title="LinkedIn company profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )

    soup = BeautifulSoup(
        render_dashboard(
            Snapshot(
                meta=StoreMeta(data_revision=42),
                jobs=[inferred, native],
            )
        ),
        "html.parser",
    )

    inferred_card = soup.select_one('article[data-job-key="inferred"]')
    assert inferred_card is not None
    inferred_text = " ".join(inferred_card.get_text(" ", strip=True).split())
    assert "Company industry Industrial Automation · AI from JD" in inferred_text
    assert "medium confidence" in inferred_text
    assert "Company industry evidence Build services" in inferred_text

    native_card = soup.select_one('article[data-job-key="native"]')
    assert native_card is not None
    native_link = native_card.select_one(
        '.company-industry a[href="https://example.test/companies/acme"]'
    )
    assert native_link is not None
    assert native_link.get_text(strip=True) == "LinkedIn company profile"
    assert "Source: linkedin" in native_card.get_text(" ", strip=True)


def test_render_exposes_all_user_statuses_and_only_actionable_restore() -> None:
    eligible = _job("eligible")
    excluded = _job("excluded")
    excluded.machine_status = MachineStatus.EXCLUDED
    excluded.last_successful_review_profile_hash = "sha256:profile"
    restored = _job("restored")
    restored.machine_status = MachineStatus.EXCLUDED
    restored.manual_override = "show"

    html = render_dashboard(
        Snapshot(
            meta=StoreMeta(data_revision=42),
            jobs=[eligible, excluded, restored],
        )
    )
    soup = BeautifulSoup(html, "html.parser")

    assert not soup.find("strong", string="View:")
    assert len(soup.find_all("strong", string="User status:")) == 3
    expected_statuses = {
        "",
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    }
    cards = {
        card["data-job-key"]: card for card in soup.select("article[data-job-key]")
    }
    assert set(cards) == {"eligible", "excluded", "restored"}
    for key, card in cards.items():
        status_form = card.select_one('form[data-job-action="status"]')
        assert status_form is not None
        assert status_form.get("data-job-key") == key
        assert {
            option.get("value")
            for option in status_form.select('select[name="status"] option')
        } == expected_statuses
        selected = status_form.select_one('select[name="status"] option[selected]')
        assert selected is not None
        assert selected.get("value") == ""
        assert selected.has_attr("disabled")

    restore_forms = soup.select('form[data-job-action="restore"]')
    assert len(restore_forms) == 1
    assert restore_forms[0].get("data-job-key") == "excluded"


def test_packaged_dashboard_javascript_uses_review_api_request_contract() -> None:
    javascript = (
        importlib.resources.files("job_scan.dashboard")
        .joinpath("static", "dashboard.js")
        .read_text(encoding="utf-8")
    )

    assert "encodeURIComponent(rawJobKey)" in javascript
    assert 'method: "POST"' in javascript
    assert 'credentials: "same-origin"' in javascript
    assert '"Content-Type": "application/json"' in javascript
    assert "const payload = { status }" in javascript
    assert "payload.resume_id = selectedResumeId" in javascript
    assert "JSON.stringify(payload)" in javascript
    assert 'if (action === "restore" || action === "snapshot")' in javascript
    assert "return options" in javascript
    assert "response.ok" in javascript
    assert "await refreshReviewJob(rawJobKey)" in javascript
    assert 'submitButton.textContent = "Import to Saved"' in javascript
    assert "Import to Shortlisted" not in javascript


@pytest.mark.parametrize(
    ("run_id", "status_scope", "expected_path"),
    [
        ("", "current", "/api/jobs/canonical%2F42/snapshot"),
        (
            "search-1",
            "current",
            "/api/scan-history/search-1/jobs/canonical%2F42/snapshot",
        ),
        ("", "global", "/api/global-jobs/canonical%2F42/snapshot"),
    ],
)
def test_snapshot_button_posts_to_the_visible_card_scope(
    run_id: str,
    status_scope: str,
    expected_path: str,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    javascript = (
        importlib.resources.files("job_scan.dashboard")
        .joinpath("static", "dashboard.js")
        .read_text(encoding="utf-8")
    )
    initial_card = f"""
    <article class="job-card" data-job-key="canonical/42" data-status-scope="{status_scope}">
      <form data-job-action="snapshot" data-job-key="canonical/42">
        <button type="submit">Generate snapshot</button>
      </form>
    </article>"""
    refreshed_card = f"""
    <article class="job-card" data-job-key="canonical/42" data-status-scope="{status_scope}">
      <a href="/api/job-snapshots/sha256:test" data-job-snapshot>Job snapshot</a>
    </article>"""

    def page_html(card: str) -> str:
        return f"""<!doctype html>
<html><body data-review-run-id="{run_id}">
  <div data-review-block="current">
    <div class="review-groups">
      <section class="job-group" id="recommended"><div class="card-grid">{card}</div></section>
    </div>
  </div>
</body></html>"""

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        request_details: list[tuple[str, str | None, str | None]] = []
        captured = False

        def serve_page(route: object) -> None:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=page_html(refreshed_card if captured else initial_card),
            )

        def capture_snapshot(route: object, request: object) -> None:
            nonlocal captured
            request_details.append(
                (request.method, request.headers.get("cookie"), request.post_data)
            )
            captured = True
            route.fulfill(status=204, body="")

        page.route(
            "http://job-scan.test/",
            serve_page,
        )
        page.route("**/snapshot", capture_snapshot)
        page.context.add_cookies(
            [
                {
                    "name": "job_scan_session",
                    "value": "test",
                    "url": "http://job-scan.test/",
                }
            ]
        )
        page.goto("http://job-scan.test/")
        page.add_script_tag(content=javascript)

        with page.expect_request(
            lambda request: request.url.endswith(expected_path),
            timeout=1_000,
        ):
            page.locator("[data-job-action='snapshot'] button").click()

        page.locator("[data-job-snapshot]").wait_for(timeout=1_000)
        assert request_details == [("POST", "job_scan_session=test", None)]
        assert page.locator("[data-job-action='snapshot']").count() == 0
        assert page.locator("[data-job-snapshot]").inner_text() == "Job snapshot"

        browser.close()


def test_snapshot_button_restores_after_a_failed_request() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    javascript = (
        importlib.resources.files("job_scan.dashboard")
        .joinpath("static", "dashboard.js")
        .read_text(encoding="utf-8")
    )
    html = """<!doctype html><html><body>
      <article data-status-scope="current">
        <form data-job-action="snapshot" data-job-key="canonical-42">
          <button type="submit">Generate snapshot</button>
        </form>
      </article>
    </body></html>"""

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "http://job-scan.test/",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=html,
            ),
        )
        methods: list[str] = []

        def reject_snapshot(route: object, request: object) -> None:
            methods.append(request.method)
            route.fulfill(
                status=409,
                content_type="application/json",
                body='{"detail":"A scan is running."}',
            )

        page.route("**/snapshot", reject_snapshot)
        alerts: list[str] = []

        def dismiss_alert(dialog: object) -> None:
            alerts.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss_alert)
        page.goto("http://job-scan.test/")
        page.add_script_tag(content=javascript)
        button = page.locator("[data-job-action='snapshot'] button")
        button.click()
        page.wait_for_function(
            "() => document.querySelector('[data-job-action=\"snapshot\"] button').disabled === false"
        )

        assert methods == ["POST"]
        assert alerts == ["A scan is running."]
        assert button.inner_text() == "Generate snapshot"
        assert button.is_enabled()

        browser.close()


def test_render_escapes_raw_text_once_and_hardens_external_links() -> None:
    html = render_dashboard(Snapshot(meta=StoreMeta(data_revision=42), jobs=[_job("safe")]))
    soup = BeautifulSoup(html, "html.parser")

    assert PAYLOAD not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&amp;lt;script&amp;gt;" not in html
    assert soup.find("script", string="alert(1)") is None
    link = soup.select_one('a[href="https://jobs.example/safe"]')
    assert link is not None
    assert link.get("target") == "_blank"
    assert link.get("rel") == ["noopener", "noreferrer"]


def test_render_has_no_remote_styles_scripts_fonts_or_images() -> None:
    html = render_dashboard(Snapshot(meta=StoreMeta(data_revision=42), jobs=[_job("local")]))
    soup = BeautifulSoup(html, "html.parser")

    assert not soup.select("link[href], script[src], img[src], source[src]")
    styles = "\n".join(tag.get_text() for tag in soup.find_all("style"))
    assert (
        re.search(
            r"(?:url|@import)\s*\(?'?[\"']?https?://",
            styles,
            re.IGNORECASE,
        )
        is None
    )
    assert "@font-face" not in styles
