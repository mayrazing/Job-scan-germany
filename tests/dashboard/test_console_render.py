from __future__ import annotations

import importlib.resources
from datetime import UTC, date, datetime

from bs4 import BeautifulSoup

from job_scan.ai_config import AiProviderView
from job_scan.ai_selection import AiRuntimeSelection, CodexRuntimeSelection
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsFailure,
    AtsHistoryEntry,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
)
from job_scan.config import ClaudeSettings, SchedulerSettings
from job_scan.dashboard.render import render_console, render_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    CompanySizeEvidence,
    JobRecord,
    MachineStatus,
    Snapshot,
    StoreMeta,
    UserStatus,
)
from job_scan.search_history import SearchHistoryEntry
from job_scan.setup_service import SetupAnswers


def review_job(key: str, machine: MachineStatus = MachineStatus.ELIGIBLE) -> JobRecord:
    now = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    return JobRecord(
        canonical_job_key=key,
        primary_source_occurrence_key=f"linkedin:{key}:req@1",
        company=f"Company {key}",
        title=f"Role {key}",
        location="Berlin",
        url=f"https://jobs.example/{key}",
        description=f"Description {key}",
        posted_at=date(2026, 8, 1),
        content_hash=f"sha256:{key}",
        first_seen=now,
        last_seen=now,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=machine,
        user_status_updated_at=now,
    )


def review_snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=42), jobs=list(jobs))


def ats_history_entry(run_id: str) -> AtsHistoryEntry:
    return AtsHistoryEntry(
        run_id=run_id,
        search_run_id="search-1",
        resume_id="sha256:" + "a" * 64,
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        finished_at=datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        readiness_score=88,
        job_count=2,
        failed_job_count=1,
    )


def ats_bundle(run_id: str) -> AtsCheckBundle:
    return AtsCheckBundle(
        run_id=run_id,
        search_run_id="search-1",
        resume_id="sha256:" + "a" * 64,
        candidate_name="Ada Lovelace",
        resume_filename="Ada CV.pdf",
        started_at=datetime(2026, 8, 8, 12, 25, tzinfo=UTC),
        finished_at=datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        ai_runtime="claude-code",
        ai_model="sonnet",
        resume=AtsResumeAssessment(
            readiness_score=88,
            verdict="needs_attention",
            title="Resume text is readable",
            summary="Core resume content was extracted successfully.",
            findings=[
                AtsResumeFinding(
                    label="Text extraction",
                    status="pass",
                    detail="All resume text was extracted.",
                ),
                AtsResumeFinding(
                    label="Section names",
                    status="warning",
                    detail="Use conventional section headings.",
                ),
            ],
        ),
        jobs=[
            AtsJobResult(
                job_key="job-1",
                title="Backend Engineer",
                company="Example GmbH",
                location="Berlin",
                url="https://jobs.example/job-1",
                content_hash="sha256:job-1",
                assessment=AtsJobAssessment(
                    job_key="job-1",
                    match_score=84,
                    match_label="strong",
                    required_skills_score=88,
                    experience_score=82,
                    keyword_score=76,
                    matched=["Python delivery experience"],
                    needs_attention=["Kubernetes is not named"],
                    suggestions=["Add Kubernetes only if accurate."],
                ),
            ),
            AtsJobResult(
                job_key="job-failed",
                title="Platform Engineer",
                company="Northstar Systems",
                location="Hamburg",
                url="https://jobs.example/job-failed",
                content_hash="sha256:job-failed",
                failure=AtsFailure(
                    category="timeout",
                    message="AI check timed out.",
                ),
            ),
        ],
    )


def test_console_renders_setup_run_and_real_review_link() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    setup = page.select_one("#setup-form")
    assert setup is not None
    target_help = setup.select_one("#target-companies-config > .form-text")
    assert target_help is not None
    assert target_help.get_text(" ", strip=True) == (
        "Search selected companies directly. Locations shape official searches when "
        "supported; other company sources filter locations from job details."
    )
    assert {
        "resume",
        "search-terms",
        "locations",
        "posted-within-days",
        "target-company-bosch",
        "target-company-dallmeier",
        "target-company-dhl",
        "target-company-rohde-schwarz",
        "target-company-siemens",
        "target-company-telekom",
        "target-company-thyssenkrupp",
        "german-level",
        "indeed-de-limit",
        "linkedin-limit",
        "stepstone-de-limit",
        "glassdoor-de-limit",
        "simplify-de-limit",
        "minimum-company-size",
        "scan-time",
    } <= {field.get("id") for field in setup.select("input, select")}
    assert setup.select_one("#claude-batch-size") is None
    assert setup.select_one("#radius-km") is None
    assert setup.select_one("#remote-preference") is None
    assert setup.select_one("#candidate-name") is None
    target_company = setup.select_one("#target-company-bosch")
    assert target_company is not None
    assert not target_company.has_attr("checked")
    assert target_company.get("value") == "bosch"
    telekom = setup.select_one("#target-company-telekom")
    assert telekom is not None
    assert not telekom.has_attr("checked")
    assert telekom.get("value") == "telekom"
    rohde_schwarz = setup.select_one("#target-company-rohde-schwarz")
    assert rohde_schwarz is not None
    assert not rohde_schwarz.has_attr("checked")
    assert rohde_schwarz.get("value") == "rohde-schwarz"
    siemens = setup.select_one("#target-company-siemens")
    assert siemens is not None
    assert not siemens.has_attr("checked")
    assert siemens.get("value") == "siemens"
    dhl = setup.select_one("#target-company-dhl")
    assert dhl is not None
    assert not dhl.has_attr("checked")
    assert dhl.get("value") == "dhl"
    thyssenkrupp = setup.select_one("#target-company-thyssenkrupp")
    assert thyssenkrupp is not None
    assert not thyssenkrupp.has_attr("checked")
    assert thyssenkrupp.get("value") == "thyssenkrupp"
    dallmeier = setup.select_one("#target-company-dallmeier")
    assert dallmeier is not None
    assert not dallmeier.has_attr("checked")
    assert dallmeier.get("value") == "dallmeier"
    assert "Posting dates are unavailable" in dallmeier.parent.get_text(
        " ", strip=True
    )
    posting_window = setup.select_one("#posted-within-days")
    assert [option.get("value") for option in posting_window.select("option")] == [
        "0",
        "1",
        "3",
        "7",
        "14",
        "",
    ]
    assert posting_window.select_one("option[selected]").get("value") == "7"
    assert setup.select_one("#staffing-penalty") is None
    for field_id in (
        "linkedin-limit",
        "indeed-de-limit",
        "stepstone-de-limit",
        "glassdoor-de-limit",
        "simplify-de-limit",
    ):
        assert setup.select_one(f"#{field_id}").get("value") == "10"
    company_size = setup.select_one("#minimum-company-size")
    assert [option.get("value") for option in company_size.select("option")] == [
        "0",
        "50",
        "250",
        "1000",
        "10000",
    ]
    assert company_size.select_one("option[selected]").get("value") == "0"
    assert setup.select_one('[aria-label="About staffing agency penalty"]') is None
    assert page.select_one("#run-view[hidden]") is not None
    workflow_review_link = page.select_one('[data-nav-step="review"]')
    assert workflow_review_link is not None
    assert workflow_review_link.get("href") == "#review"
    assert page.select_one("#review-view[hidden]") is not None
    review_link = page.select_one("#review-link[hidden]")
    assert review_link is not None
    assert review_link.get("href") == "#review"
    assert page.select_one("#review-preview") is None


def test_console_renders_job_tracker_as_own_workflow_step_after_review() -> None:
    snapshot = review_snapshot(
        review_job("recommended", MachineStatus.ELIGIBLE),
        review_job("pending", MachineStatus.PENDING),
    )
    tracked = review_snapshot(
        review_job("saved").model_copy(update={"user_status": UserStatus.SAVED})
    )
    page = BeautifulSoup(
        render_console(
            snapshot,
            global_snapshot=tracked,
        ),
        "html.parser",
    )

    workflow = page.select_one("nav[aria-label='Workflow']")
    assert workflow is not None
    assert [link.get("data-nav-step") for link in workflow.select("[data-nav-step]")] == [
        "setup",
        "run",
        "review",
        "job-tracker",
        "ats-run",
        "ats",
    ]
    job_tracker_link = page.select_one(
        '[data-nav-step="job-tracker"][href="#job-tracker"]'
    )
    assert job_tracker_link is not None
    assert job_tracker_link.get_text(" ", strip=True) == "Job Tracker 4"
    assert page.select_one('[data-nav-step="ats-run"][href="#ats-run"]') is not None
    assert page.select_one('[data-nav-step="ats"][href="#ats-check"]') is not None
    review_view = page.select_one("#review-view[hidden]")
    job_tracker_view = page.select_one("#job-tracker-view[hidden]")
    assert review_view is not None
    assert job_tracker_view is not None
    assert review_view.select_one('[data-review-block="current"]') is not None
    assert review_view.select_one('[data-review-block="global"]') is None
    assert job_tracker_view.select_one('[data-review-block="global"]') is not None
    assert job_tracker_view.select_one("h2").get_text(strip=True) == "Job Tracker"
    assert page.select_one("#ats-running[hidden]") is not None
    assert page.select_one("#ats-check[hidden]") is not None
    assert "No ATS check is running" in page.select_one("#ats-running").get_text(
        " ", strip=True
    )
    start = page.select_one("[data-open-ats]")
    assert start is not None and start.has_attr("disabled")
    assert not start.has_attr("data-search-run-id")
    assert start.get_text(" ", strip=True) == "Check 0 selected jobs"
    assert review_view.select("[data-ats-select-job]") == []
    assert job_tracker_view.select("[data-ats-select-job]") == []
    assert "Step 1 of 6" in page.select_one("#setup").get_text(" ", strip=True)
    assert "Step 3 of 6" in review_view.get_text(" ", strip=True)
    assert "Step 4 of 6" in job_tracker_view.get_text(" ", strip=True)
    assert "Step 5 of 6" in page.select_one("#ats-running").get_text(" ", strip=True)
    assert "Step 6 of 6" in page.select_one("#ats-check").get_text(" ", strip=True)


def test_console_uses_top_workflow_navigation_without_a_duplicate_step_rail() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    console_card = page.select_one("article.console-card")
    assert console_card is not None
    assert console_card.select_one('aside[aria-label="Scan progress"]') is None
    assert console_card.select_one(":scope > .console-body") is not None


def test_console_uses_job_tracker_cards_without_separate_ats_selectors() -> None:
    snapshot = review_snapshot(
        review_job("recommended", MachineStatus.ELIGIBLE),
        review_job("pending", MachineStatus.PENDING),
    )
    tracked = review_snapshot(
        review_job("saved").model_copy(update={"user_status": UserStatus.SAVED}),
        review_job("applied").model_copy(update={"user_status": UserStatus.APPLIED}),
    )
    page = BeautifulSoup(
        render_console(
            snapshot,
            global_snapshot=tracked,
        ),
        "html.parser",
    )

    current_block = page.select_one('[data-review-block="current"]')
    global_block = page.select_one('[data-review-block="global"]')
    assert current_block is not None
    assert global_block is not None
    assert page.select("[data-ats-select-group]") == []
    assert page.select(".review-groups > details.job-group") == []
    assert len(page.select(".review-groups > section.job-group")) == 10
    assert page.select(".review-groups > .job-group > summary") == []
    assert current_block.select("[data-ats-select-job]") == []
    assert global_block.select("[data-ats-select-job]") == []
    assert global_block.select_one("#saved .card-body > .ats-job-selector") is None


def test_job_without_a_saved_resume_keeps_no_resume_text_without_checkbox() -> None:
    tracked = review_job("saved").model_copy(
        update={"user_status": UserStatus.SAVED}
    )
    page = BeautifulSoup(
        render_console(global_snapshot=review_snapshot(tracked)),
        "html.parser",
    )

    card = page.select_one('[data-job-key="saved"]')
    assert card is not None
    assert card.select_one("[data-ats-select-job]") is None
    assert card.select_one("[data-ats-resume-missing]").get_text(
        " ", strip=True
    ) == "No resume"


def test_job_tracker_shows_each_resume_with_download_and_replace() -> None:
    resume_a = "sha256:" + "a" * 64
    tracked = review_snapshot(
        review_job("known").model_copy(
            update={
                "user_status": UserStatus.APPLIED,
                "application_resume_id": resume_a,
                "application_resume_filename": "backend.pdf",
            }
        ),
        review_job("unknown").model_copy(
            update={"user_status": UserStatus.REJECTED}
        ),
    )
    page = BeautifulSoup(
        render_console(global_snapshot=tracked),
        "html.parser",
    )

    known = page.select_one('[data-review-block="global"] [data-job-key="known"]')
    unknown = page.select_one(
        '[data-review-block="global"] [data-job-key="unknown"]'
    )
    assert known is not None
    assert unknown is not None
    known_resume = known.select_one("[data-job-resume-name]")
    unknown_resume = unknown.select_one("[data-job-resume-name]")
    assert known_resume is not None
    assert unknown_resume is not None
    assert known_resume.get_text(" ", strip=True) == "backend.pdf"
    assert unknown_resume.get_text(" ", strip=True) == "Unknown"
    assert known.select_one("[data-job-resume-download]") is not None
    assert unknown.select_one("[data-job-resume-download]") is None
    assert known.select_one("[data-job-resume-replace]") is not None
    assert unknown.select_one("[data-job-resume-replace]") is not None
    assert known.select_one('[data-job-action="application-resume"]') is None
    assert "Use saved resume" not in known.get_text(" ", strip=True)


def test_console_omits_obsolete_review_group_collapse_control() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select_one("[data-collapse-review-groups]") is None


def test_console_renders_review_groups_as_draggable_navigation_tabs() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    workspace = page.select_one('[data-review-block="current"] .review-workspace')
    tabs = workspace.select(".review-group-nav [data-review-group-tab]")
    panels = workspace.select(".review-groups > section.job-group")

    assert workspace is not None
    assert [tab.get("data-review-group-tab") for tab in tabs] == [
        "recommended",
        "pending",
        "excluded",
    ]
    assert all(tab.get("draggable") == "true" for tab in tabs)
    assert all(tab.get("aria-keyshortcuts") == "Alt+ArrowUp Alt+ArrowDown" for tab in tabs)
    assert all(tab.select_one("[data-review-group-drag-handle]") for tab in tabs)
    assert page.select_one(".review-group-announcement[aria-live='polite']") is not None
    assert tabs[0].get("aria-current") == "page"
    assert [panel.get("id") for panel in panels if not panel.has_attr("hidden")] == ["recommended"]
    assert page.select("summary[data-review-group-drag-source]") == []


def test_standalone_dashboard_does_not_render_console_only_ats_selectors() -> None:
    page = BeautifulSoup(
        render_dashboard(review_snapshot(review_job("recommended"))),
        "html.parser",
    )

    assert page.select("[data-ats-select-job]") == []
    assert page.select("[data-ats-select-group]") == []


def test_ats_history_is_collapsed_and_selected_bundle_is_rendered() -> None:
    entry = ats_history_entry("ats-1")
    bundle = ats_bundle("ats-1")
    page = BeautifulSoup(
        render_console(ats_history=[entry], selected_ats=bundle),
        "html.parser",
    )

    history = page.select_one("details#ats-history")
    assert history is not None and not history.has_attr("open")
    assert history.select_one('[data-ats-history-id="ats-1"].is-selected') is not None
    assert history.select_one(
        '[data-ats-history-view][href="/setup?ats_run_id=ats-1#ats-check"]'
    )
    assert history.select_one("[data-ats-history-delete]")
    assert page.select_one("#resume-readiness") is not None
    assert page.select_one('[data-ats-report="job-1"]') is not None
    assert (
        "do not guarantee screening results"
        in page.select_one("#ats-check").get_text(" ", strip=True)
    )
    ats_result_text = page.select_one("#ats-check").get_text(" ", strip=True)
    assert bundle.ai_runtime not in ats_result_text
    assert bundle.ai_model not in ats_result_text

    failed_option = page.select_one('[data-ats-job="job-failed"]')
    failed_report = page.select_one('[data-ats-report="job-failed"]')
    assert failed_option is not None
    assert failed_option.get_text(" ", strip=True).endswith("Check failed")
    assert "%" not in failed_option.get_text(" ", strip=True)
    assert failed_report is not None
    assert "Check failed" in failed_report.get_text(" ", strip=True)
    assert "AI check timed out." in failed_report.get_text(" ", strip=True)
    assert failed_report.select("progress") == []
    assert "%" not in failed_report.get_text(" ", strip=True)


def test_ats_history_time_falls_back_to_check_finish_time() -> None:
    entry = ats_history_entry("ats-1")
    bundle = ats_bundle("ats-1")
    page = BeautifulSoup(
        render_console(ats_history=[entry], selected_ats=bundle),
        "html.parser",
    )

    history = page.select_one("details#ats-history")
    assert history is not None
    time = history.select_one('[data-ats-history-id="ats-1"] time[data-local-datetime]')
    assert time is not None
    assert time["datetime"] == "2026-08-08T12:30:00+00:00"
    assert "2026-08-08 12:30" in time.get_text()
    context = page.select_one("#ats-history-context")
    assert context is not None
    assert "2026-08-08 12:30" in context.get_text()


def test_console_renders_search_history_with_resume_view_and_delete_actions() -> None:
    entry = SearchHistoryEntry(
        run_id="run-42",
        candidate_name="Ada Lovelace",
        finished_at=datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
        resume_filename="Ada CV.pdf",
        job_count=18,
        recommended_count=4,
    )

    page = BeautifulSoup(
        render_console(scan_history=[entry], selected_run_id="run-42"),
        "html.parser",
    )

    history = page.select_one("#scan-history")
    assert history is not None
    row = history.select_one('[data-scan-history-id="run-42"]')
    assert row is not None
    assert "Ada Lovelace" in row.get_text(" ", strip=True)
    assert "18 jobs" in row.get_text(" ", strip=True)
    assert "4 recommended" in row.get_text(" ", strip=True)
    finished_at = row.select_one("time[data-local-datetime]")
    assert finished_at is not None
    assert finished_at.get("datetime") == "2026-08-07T12:30:00+00:00"
    assert row.select_one('[data-scan-download][href="/api/scan-history/run-42/resume"]')
    assert row.select_one('[data-scan-view][href="/setup?run_id=run-42#review"]')
    assert row.select_one("[data-scan-delete]")
    assert page.body.get("data-review-run-id") == "run-42"


def test_console_renders_search_history_as_collapsed_collapsible_region() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    history = page.select_one("details#scan-history")
    assert history is not None
    assert not history.has_attr("open")
    assert history.select_one("summary.scan-history-heading") is not None
    assert history.select_one(":scope > .scan-history-list") is not None


def test_console_places_resume_suggestions_below_search_terms_without_target_lanes() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    search_terms = page.select_one("#search-terms").find_parent(class_="field-wide")

    assert search_terms.select_one("#search-term-suggestions") is not None
    assert page.select_one("#target-lanes") is None
    assert page.select_one("#target-lane-suggestions") is None
    assert page.select_one("#resume-suggestion-status") is not None


def test_console_starts_with_empty_search_terms() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select("#search-terms option[selected]") == []


def test_console_hides_search_term_instructions_before_ai_suggestions() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    instructions = page.select_one("#search-term-suggestion-help")

    assert instructions is not None
    assert instructions.has_attr("hidden")
    assert instructions.get_text(strip=True) == (
        "Choose suggestions or type your own. Press Enter to add."
    )


def test_console_explains_what_resume_analysis_does() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    explanation = page.select_one("#analyze-resume-help")

    assert explanation is not None
    assert explanation.get_text(strip=True) == (
        "Click this button to let AI analyze your resume. "
        "Job title suggestions will appear under Search terms below."
    )


def test_console_groups_browser_source_settings_under_opencli() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    opencli = page.select_one("fieldset#opencli-config")

    assert opencli is not None
    assert opencli.select_one("legend").get_text(strip=True) == "OpenCLI"
    assert opencli.select_one("#linkedin-limit") is not None
    assert opencli.select_one("#indeed-de-limit") is not None
    assert opencli.select_one("#stepstone-de-limit") is not None
    assert opencli.select_one("#glassdoor-de-limit") is not None
    assert opencli.select_one("#simplify-de-limit") is not None
    assert opencli.select_one("#linkedin-limit").get("min") == "1"
    assert opencli.select_one("#indeed-de-limit").get("min") == "1"
    assert opencli.select_one("#stepstone-de-limit").get("min") == "1"
    assert opencli.select_one("#glassdoor-de-limit").get("min") == "1"
    assert opencli.select_one("#simplify-de-limit").get("min") == "1"
    assert page.select_one("#advanced-settings #linkedin-limit") is None
    assert page.select_one("#advanced-settings #indeed-de-limit") is None
    assert page.select_one("#advanced-settings #stepstone-de-limit") is None
    assert page.select_one("#advanced-settings #glassdoor-de-limit") is None
    assert page.select_one("#advanced-settings #simplify-de-limit") is None
    glassdoor_control = opencli.select_one("#glassdoor-de-limit").find_parent(
        class_="opencli-source-control"
    )
    assert glassdoor_control is not None
    assert "Glassdoor DE" in glassdoor_control.get_text(" ", strip=True)
    assert "Jobs per search term and location. Maximum 100." in opencli.get_text(
        " ", strip=True
    )
    stepstone_control = opencli.select_one("#stepstone-de-limit").find_parent(
        class_="opencli-source-control"
    )
    assert stepstone_control is not None
    assert "StepStone" in stepstone_control.get_text(" ", strip=True)
    simplify_control = opencli.select_one("#simplify-de-limit").find_parent(
        class_="opencli-source-control"
    )
    assert simplify_control is not None
    assert "Simplify DE" in simplify_control.get_text(" ", strip=True)


def test_console_renders_one_switch_per_opencli_source_and_disables_its_limit() -> None:
    setup = SetupAnswers.model_validate(
        {
            "search_terms": ["backend"],
            "locations": ["Berlin"],
            "german_level": "A2",
            "linkedin_enabled": False,
            "claude": ClaudeSettings(model="sonnet", effort="medium"),
            "scheduler": SchedulerSettings(),
        }
    )
    page = BeautifulSoup(render_console(setup_answers=setup), "html.parser")

    for source in ("linkedin", "indeed-de", "stepstone-de", "glassdoor-de", "simplify-de"):
        switch = page.select_one(f"#{source}-enabled")
        limit = page.select_one(f"#{source}-limit")
        assert switch is not None
        assert switch.get("type") == "checkbox"
        assert switch.get("role") == "switch"
        assert switch.get("aria-controls") == f"{source}-limit"
        assert limit is not None
    assert not page.select_one("#linkedin-enabled").has_attr("checked")
    assert page.select_one("#linkedin-limit").has_attr("disabled")
    assert page.select_one("#indeed-de-enabled").has_attr("checked")
    assert not page.select_one("#indeed-de-limit").has_attr("disabled")


def test_console_groups_arbeitsagentur_switch_under_api() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    api = page.select_one("fieldset#api-config")
    switch = api.select_one("#arbeitsagentur-enabled") if api is not None else None

    assert api is not None
    assert api.select_one("legend").get_text(strip=True) == "API"
    assert switch is not None
    assert switch.get("type") == "checkbox"
    assert switch.get("role") == "switch"
    assert switch.has_attr("checked")


def test_console_summary_has_empty_targets_for_real_scan_counts() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select_one("#found-count").get_text(strip=True) == "0"
    assert page.select_one("#reviewed-count").get_text(strip=True) == "0"
    assert page.select_one("#eligible-count").get_text(strip=True) == "0"
    assert page.select_one("#warning-count").get_text(strip=True) == "0"


def test_console_embeds_the_real_review_queue_structure() -> None:
    page = BeautifulSoup(render_console(), "html.parser")
    review = page.select_one("#review-view")
    job_tracker = page.select_one("#job-tracker-view")

    assert review is not None
    assert job_tracker is not None
    for group_id in (
        "recommended",
        "pending",
        "excluded",
    ):
        group = review.select_one(f"section#{group_id}.job-group")
        assert group is not None
        assert group.select_one(":scope > summary") is None
        assert group.select_one(":scope > .card-grid") is not None
    for group_id in (
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ):
        group = job_tracker.select_one(f"section#{group_id}.job-group")
        assert group is not None
        assert group.select_one(":scope > summary") is None
        assert group.select_one(":scope > .card-grid") is not None
    assert review.select_one("#history") is None
    assert review.select("[data-history-filter], [data-history-kind]") == []

    company_size_filter = review.select_one("#review-company-size")
    assert company_size_filter is not None
    assert [option.get("value") for option in company_size_filter.select("option")] == [
        "0",
        "50",
        "250",
        "1000",
        "10000",
    ]

    company_industry_filter = review.select_one("#review-company-industry")
    assert company_industry_filter is not None
    assert [
        option.get_text(strip=True)
        for option in company_industry_filter.select("option")
    ] == ["Any industry"]


def test_packaged_console_javascript_recognizes_every_review_group_hash() -> None:
    javascript = (
        importlib.resources.files("job_scan.dashboard")
        .joinpath("static", "console.js")
        .read_text(encoding="utf-8")
    )

    for group_hash in (
        "#saved",
        "#applied",
        "#interviewing",
        "#offer",
        "#withdrawn",
        "#rejected",
        "#ignored",
    ):
        assert f'"{group_hash}"' in javascript
    assert '"#shortlisted"' not in javascript


def test_console_is_self_contained_for_local_server_use() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select_one("style") is not None
    assert page.select_one("script") is not None
    assert not page.select("link[rel='stylesheet']")
    assert not page.select("script[src]")


def test_console_places_each_step_description_beside_its_title() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    title_ids = (
        "setup-title",
        "run-title",
        "review-title",
        "job-tracker-title",
        "ats-running-title",
        "ats-title",
    )
    for title_id in title_ids:
        title = page.select_one(f"#{title_id}")
        assert title is not None
        title_line = title.parent
        assert "section-title-line" in title_line.get("class", [])
        assert title_line.select_one(":scope > p") is not None


def test_console_renders_collapsible_background_task_menu_in_workflow_bar() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    toolbar = page.select_one(
        ".console-sticky-header > header.page-header.workflow-toolbar"
    )
    assert toolbar is not None
    assert toolbar.select_one(".console-brand > h1").get_text(strip=True) == "Job scan"
    assert toolbar.select_one(".console-brand > .eyebrow") is None
    assert toolbar.select_one("nav.flow-nav") is not None
    assert (
        toolbar.select_one(".console-header-actions > [data-open-ai-config]")
        is not None
    )
    assert toolbar.select_one(".console-state.visually-hidden #header-status") is not None
    toggle = toolbar.select_one(
        '[data-background-task-toggle][aria-controls="background-task-panel"]'
    )
    assert toggle is not None
    assert toggle.get("aria-expanded") == "false"
    panel = toolbar.select_one("#background-task-panel[hidden]")
    assert panel is not None
    assert panel.select_one("[data-background-task-list]") is not None
    assert panel.select_one("[data-background-task-empty]") is not None


def test_console_renders_real_ai_configuration_controls() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    open_button = page.select_one(
        '.page-header [data-open-ai-config][data-bs-target="#ai-config-modal"]'
    )
    modal = page.select_one(
        'div.modal#ai-config-modal[aria-labelledby="ai-config-modal-title"]'
    )
    assert open_button is not None
    assert modal is not None
    ai_config = modal.select_one("#ai-config")
    assert ai_config is not None
    assert ai_config.select_one("[data-add-ai-provider]") is not None
    assert ai_config.select_one("#ai-provider-editor[hidden]") is not None
    assert ai_config.select_one("[data-discover-ai-models]") is not None
    assert ai_config.select_one("[data-save-ai-provider]") is not None
    runtime = modal.select_one("#ai-runtime")
    assert runtime is not None
    assert [option.get("value") for option in runtime.select("option")] == [
        "claude-code",
        "codex-cli",
    ]
    assert modal.select_one("[data-save-ai-selection]") is not None
    assert page.select_one("#setup-form > #ai-config") is None
    assert page.select_one("#advanced-settings #ai-runtime") is None
    assert page.select_one("#advanced-settings #claude-batch-size") is None


def test_console_renders_codex_cli_model_and_effort_controls() -> None:
    page = BeautifulSoup(
        render_console(
            ai_selection=AiRuntimeSelection(
                ai_runtime="codex-cli",
                codex=CodexRuntimeSelection(
                    model="gpt-5.6-sol",
                    effort="high",
                ),
            )
        ),
        "html.parser",
    )

    runtime = page.select_one("#ai-runtime option[value='codex-cli'][selected]")
    settings = page.select_one("#codex-cli-settings")
    assert runtime is not None
    assert runtime.get_text(" ", strip=True) == "Codex CLI · gpt-5.6-sol"
    assert settings is not None and not settings.has_attr("hidden")
    assert settings.select_one("#codex-model option[selected]").get("value") == (
        "gpt-5.6-sol"
    )
    assert len(settings.select("#codex-model option")) == 1
    assert [option.get("value") for option in settings.select("#codex-effort option")] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert settings.select_one("#codex-effort option[selected]").get("value") == "high"


def test_console_renders_codex_device_login_dialog() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    dialog = page.select_one("#codex-login-dialog")
    assert dialog is not None
    assert dialog.select_one("[data-codex-login-code]") is not None
    assert dialog.select_one("[data-open-codex-login]") is not None
    assert dialog.select_one("[data-cancel-codex-login]") is not None


def test_console_places_enabled_thinking_switch_below_claude_model() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    model = page.select_one("#claude-model")
    switch = page.select_one("#claude-thinking-enabled")
    model_settings = page.select_one("#claude-code-settings .claude-model-settings")

    assert model is not None
    assert switch is not None
    assert model_settings is not None
    assert switch.get("type") == "checkbox"
    assert switch.get("role") == "switch"
    assert switch.has_attr("checked")
    assert model_settings.select_one("#claude-model") is model
    assert model_settings.select_one("#claude-thinking-enabled") is switch


def test_console_opens_advanced_settings_by_default() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    toggle = page.select_one("#advanced-settings .accordion-button")
    body = page.select_one("#advanced-settings-body")

    assert toggle is not None
    assert "collapsed" not in toggle.get("class", [])
    assert toggle.get("aria-expanded") == "true"
    assert body is not None
    assert "show" in body.get("class", [])


def test_console_uses_one_global_ai_selection_without_an_ats_picker() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select_one("#ai-config-modal #ai-runtime") is not None
    assert page.select_one("#ats-ai-choice") is None
    review = page.select_one("#review-view")
    assert review is not None
    assert "ATS AI" not in review.get_text(" ", strip=True)


def test_console_lists_all_saved_api_providers_as_runtimes_without_activation() -> None:
    html = render_console(
        ai_providers=[
            AiProviderView(
                id="deepseek",
                display_name="DeepSeek",
                base_url="https://api.example.com/anthropic",
                model="deepseek-chat",
                reasoning_effort="low",
                api_key_configured=True,
            ),
            AiProviderView(
                id="open-router",
                display_name="Open Router",
                base_url="https://openrouter.example.com/anthropic",
                model="claude-sonnet-4",
                reasoning_effort="high",
                api_key_configured=True,
            ),
        ]
    )
    page = BeautifulSoup(html, "html.parser")

    provider = page.select_one("[data-ai-provider='deepseek']")
    assert provider is not None
    assert "DeepSeek" in provider.get_text(" ", strip=True)
    assert "Active" not in provider.get_text(" ", strip=True)
    assert page.select_one("[data-activate-ai-provider]") is None
    assert provider.select_one("[data-edit-ai-provider]") is not None
    assert provider.select_one("[data-delete-ai-provider]") is not None
    assert page.select_one("#ai-provider-api-key").get("value") is None
    options = {
        option.get("value"): option.get_text(" ", strip=True)
        for option in page.select("#ai-runtime option")
    }
    assert options == {
        "claude-code": "Claude Code CLI · sonnet",
        "codex-cli": "Codex CLI · gpt-5.6-sol",
        "api:deepseek": "DeepSeek API · deepseek-chat",
        "api:open-router": "Open Router API · claude-sonnet-4",
    }
    assert page.select_one("[data-edit-selected-api]") is None


def test_job_tracker_does_not_render_a_shared_ats_resume_picker() -> None:
    page = BeautifulSoup(render_console(), "html.parser")

    assert page.select_one("#ats-resume") is None
    assert page.select_one("[data-ats-default-resume]") is None
    assert page.select_one("[data-open-ats]") is not None


def test_review_card_shows_company_size_source_and_checked_date() -> None:
    current = review_job("sized")
    current.company_size = CompanySizeEvidence(
        company_name=current.company,
        band="1000-9999",
        employee_count=4200,
        source_url="https://company.example/annual-report",
        source_title="Annual report",
        checked_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        confidence="high",
    )

    page = BeautifulSoup(render_console(review_snapshot(current)), "html.parser")
    fact = page.select_one('[data-job-key="sized"] .company-size')

    assert fact is not None
    assert "1,000-9,999" in fact.get_text(" ", strip=True)
    assert "2026-08-04" in fact.get_text(" ", strip=True)
    assert fact.select_one("a").get("href") == "https://company.example/annual-report"


def test_review_card_offers_ai_company_size_search_with_click_help() -> None:
    current = review_job("unknown-size")

    page = BeautifulSoup(render_console(review_snapshot(current)), "html.parser")
    fact = page.select_one('[data-job-key="unknown-size"] .company-size')

    assert fact is not None
    assert "Unknown" in fact.get_text(" ", strip=True)
    search = fact.select_one("[data-company-size-search]")
    assert search is not None
    assert search.get_text(" ", strip=True) == "AI Search"
    help_button = fact.select_one("[data-company-size-help]")
    assert help_button is not None
    assert help_button.get_text(strip=True) == "?"
    assert help_button.get("data-bs-trigger") == "click"
    assert help_button.get("data-bs-title") == (
        "Use AI to search the web for this company's employee count."
    )


def test_only_pending_source_card_explains_that_ai_review_was_not_run() -> None:
    source_failure = review_job("source-failure", machine=MachineStatus.PENDING_SOURCE)
    source_failure.last_error = "invalid_response"
    review_failure = review_job("review-failure", machine=MachineStatus.PENDING)
    review_failure.last_error = "schema_validation"

    page = BeautifulSoup(
        render_console(review_snapshot(source_failure, review_failure)),
        "html.parser",
    )
    source_error = page.select_one('[data-job-key="source-failure"] .source-error')

    assert source_error is not None
    assert source_error.get_text(" ", strip=True) == (
        "Source detail unavailable. AI review was not run. Error: invalid response."
    )
    assert page.select_one('[data-job-key="review-failure"] .source-error') is None


def test_pending_source_card_still_explains_failure_without_an_error_code() -> None:
    source_failure = review_job("legacy-source-failure", machine=MachineStatus.PENDING_SOURCE)

    page = BeautifulSoup(
        render_console(review_snapshot(source_failure)),
        "html.parser",
    )
    source_error = page.select_one(
        '[data-job-key="legacy-source-failure"] .source-error'
    )

    assert source_error is not None
    assert source_error.get_text(" ", strip=True) == (
        "Source detail unavailable. AI review was not run."
    )


def test_review_card_prefers_the_source_reported_company_size_range() -> None:
    current = review_job("source-sized")
    current.company_size = CompanySizeEvidence(
        company_name=current.company,
        band="unknown",
        reported_size="5.001 bis 10.000",
        minimum_employees=5001,
        maximum_employees=10000,
        source_url="https://de.indeed.com/cmp/example/about",
        source_title="Indeed company profile",
        checked_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
        confidence="high",
        lookup_method="native",
        source_name="indeed",
    )

    page = BeautifulSoup(render_console(review_snapshot(current)), "html.parser")
    fact = page.select_one('[data-job-key="source-sized"] .company-size')
    card = page.select_one('[data-job-key="source-sized"]')

    assert fact is not None
    assert "5.001 bis 10.000" in fact.get_text(" ", strip=True)
    assert card.get("data-company-size-minimum") == "5001"
    assert card.get("data-company-size-maximum") == "10000"


def test_company_only_exclusion_does_not_offer_unusable_restore_action() -> None:
    current = review_job("small", machine=MachineStatus.EXCLUDED)
    current.ai_review = None
    current.exclusion_reasons = ["company_too_small"]

    page = BeautifulSoup(render_console(review_snapshot(current)), "html.parser")

    assert page.select_one('[data-job-key="small"] [data-job-action="restore"]') is None
