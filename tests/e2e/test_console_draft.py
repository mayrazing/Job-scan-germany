from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlparse

import pytest

from job_scan.ai_config import AiProviderView
from job_scan.ats_models import (
    AtsCheckBundle,
    AtsHistoryEntry,
    AtsJobAssessment,
    AtsJobResult,
    AtsResumeAssessment,
    AtsResumeFinding,
)
from job_scan.dashboard.render import render_console
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    CompanyIndustryEvidence,
    CompanySizeEvidence,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.resume_catalog import ResumeCatalogEntry

playwright = pytest.importorskip("playwright.sync_api")


DEEPSEEK_PROVIDER = AiProviderView(
    id="deepseek",
    display_name="DeepSeek",
    base_url="https://api.example.com/anthropic",
    model="deepseek-v4-flash",
    reasoning_effort="max",
    api_key_configured=True,
)

OPEN_ROUTER_PROVIDER = AiProviderView(
    id="open-router",
    display_name="Open Router",
    base_url="https://openrouter.example.com/anthropic",
    model="claude-sonnet-4",
    reasoning_effort="high",
    api_key_configured=True,
)

AI_PROVIDERS = [DEEPSEEK_PROVIDER, OPEN_ROUTER_PROVIDER]
RESUME = Path(__file__).parents[1] / "fixtures" / "resume" / "sample.docx"
NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


def ats_history_entry(run_id: str) -> AtsHistoryEntry:
    return AtsHistoryEntry(
        run_id=run_id,
        search_run_id="search-1",
        candidate_name="Ada Lovelace",
        resume_filename=f"{run_id}.pdf",
        finished_at=datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        readiness_score=88,
        job_count=2,
        failed_job_count=0,
    )


def ats_bundle(run_id: str, *, job_keys: tuple[str, ...]) -> AtsCheckBundle:
    return AtsCheckBundle(
        run_id=run_id,
        search_run_id="search-1",
        candidate_name="Ada Lovelace",
        resume_filename=f"{run_id}.pdf",
        started_at=datetime(2026, 8, 8, 12, 25, tzinfo=UTC),
        finished_at=datetime(2026, 8, 8, 12, 30, tzinfo=UTC),
        ai_runtime="claude-code",
        ai_model="sonnet",
        resume=AtsResumeAssessment(
            readiness_score=88,
            verdict="ready",
            title="Resume text is readable",
            summary="Core resume content was extracted successfully.",
            findings=[
                AtsResumeFinding(
                    label="Text extraction",
                    status="pass",
                    detail="All resume text was extracted.",
                )
            ],
        ),
        jobs=[
            AtsJobResult(
                job_key=job_key,
                title=f"Role {job_key}",
                company=f"Company {job_key}",
                location="Berlin",
                url=f"https://jobs.example/{job_key}",
                content_hash=f"sha256:{job_key}",
                assessment=AtsJobAssessment(
                    job_key=job_key,
                    match_score=80,
                    match_label="possible",
                    required_skills_score=82,
                    experience_score=79,
                    keyword_score=75,
                    matched=["Python"],
                    needs_attention=["Kubernetes"],
                    suggestions=["Add Kubernetes only if accurate."],
                ),
            )
            for job_key in job_keys
        ],
    )


ATS_HISTORY = [ats_history_entry("ats-1"), ats_history_entry("ats-2")]
SELECTED_ATS = ats_bundle("ats-1", job_keys=("job-1", "job-2"))


def task(task_id: str, kind: str, status: str, *, message: str | None = None) -> dict[str, str]:
    return {
        "task_id": task_id,
        "kind": kind,
        "label": f"Task <{task_id}>",
        "status": status,
        "message": message or status.title(),
    }


def ats_state(
    *,
    status: str,
    stage: str,
    tasks: list[dict[str, str]],
    progress_percent: float = 40,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "run_id": "ats-1",
        "search_run_id": "search-1",
        "status": status,
        "stage": stage,
        "message": f"ATS {stage} <safe>",
        "progress_percent": progress_percent,
        "tasks": tasks,
        "error": error,
    }


def respond_ats_start(route: object, posted: list[dict[str, object]]) -> None:
    posted.append(ats_form_data(route.request.post_data or ""))
    selected = posted[-1]["job_keys"]
    route.fulfill(
        status=202,
        content_type="application/json",
        body=json.dumps(
            ats_state(
                status="running",
                stage="resume",
                tasks=[
                    task("resume", "resume", "running"),
                    *[task(str(job_key), "job", "waiting") for job_key in selected],
                ],
            )
        ),
    )


def ats_form_data(body: str) -> dict[str, object]:
    values: dict[str, object] = {}
    marker = 'Content-Disposition: form-data; name="'
    for section in body.split(marker)[1:]:
        name, remainder = section.split('"', 1)
        value = remainder.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]
        values[name] = json.loads(value) if name == "job_keys" else value
    return values


def respond_next(route: object, states: list[dict[str, object]]) -> None:
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(states.pop(0)),
    )


def source_job(
    key: str,
    sources: tuple[SourceKind, ...],
    *,
    posted_at: date | None = None,
    score: int | None = None,
    german_requirement: Literal["required", "optional", "none", "uncertain"] | None = None,
    company_industry: CompanyIndustryEvidence | None = None,
    company_size: CompanySizeEvidence | None = None,
) -> JobRecord:
    effective_posted_at = posted_at or datetime.now(UTC).date()
    occurrences = [
        SourceOccurrence(
            source=source,
            source_instance="default",
            external_id=f"{key}-{source.value}",
            source_generation=1,
            url=f"https://jobs.example/{key}/{source.value}",
            company=f"Company {key}",
            title=f"Role {key}",
            location="Berlin",
            description=f"Description {key}",
            posted_at=effective_posted_at,
            content_hash=f"sha256:{key}-{source.value}",
            availability_status=AvailabilityStatus.ACTIVE,
            detail_complete=True,
        )
        for source in sources
    ]
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=occurrences,
        primary_source_occurrence_key=occurrences[0].source_occurrence_key,
        company=f"Company {key}",
        title=f"Role {key}",
        location="Berlin",
        url=f"https://jobs.example/{key}",
        description=f"Description {key}",
        posted_at=effective_posted_at,
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=MachineStatus.ELIGIBLE,
        score=score,
        ai_review=(
            AIReview(
                job_key=key,
                german_requirement=german_requirement,
                visa_sponsorship="not_mentioned",
                existing_work_authorization="not_mentioned",
                citizenship_requirement="none",
                security_clearance="none",
                staffing_agency="no",
                company_industry=None,
                company_industry_confidence="low",
                company_industry_evidence=[],
                score=score if score is not None else 80,
                reason=f"Review {key}",
                confidence="high",
            )
            if german_requirement is not None
            else None
        ),
        user_status_updated_at=NOW,
        company_industry=company_industry,
        company_size=company_size,
    )


def reported_company_industry(industry: str) -> CompanyIndustryEvidence:
    return CompanyIndustryEvidence(
        company_name="Fixture company",
        industry=industry,
        source_url="https://companies.example/profile",
        source_title="Company profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
    )


def reported_company_size(
    label: str,
    minimum: int,
    maximum: int | None,
) -> CompanySizeEvidence:
    return CompanySizeEvidence(
        company_name="Fixture company",
        band="unknown",
        reported_size=label,
        minimum_employees=minimum,
        maximum_employees=maximum,
        source_url="https://companies.example/profile",
        source_title="Company profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
    )


SOURCE_FILTER_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=42),
    jobs=[
        source_job(
            "linkedin-only",
            (SourceKind.LINKEDIN,),
            score=85,
            german_requirement="required",
            company_industry=reported_company_industry("Industrial Automation"),
            company_size=reported_company_size("501-1,000 employees", 501, 1000),
        ),
        source_job(
            "stepstone-only",
            (SourceKind.STEPSTONE,),
            score=75,
            german_requirement="none",
            company_industry=reported_company_industry("Software Development"),
            company_size=reported_company_size("251-1000 Mitarbeiter", 251, 1000),
        ),
        source_job(
            "glassdoor-only",
            (SourceKind.GLASSDOOR,),
            score=95,
            german_requirement="optional",
            company_size=reported_company_size("10,000+", 10000, None),
        ),
        source_job(
            "bosch-only",
            (SourceKind.BOSCH,),
            german_requirement="uncertain",
        ),
        source_job("dallmeier-only", (SourceKind.DALLMEIER,)),
        source_job("dhl-only", (SourceKind.DHL,)),
        source_job("rohde-schwarz-only", (SourceKind.SUCCESSFACTORS,)),
        source_job("siemens-only", (SourceKind.SIEMENS,)),
        source_job("telekom-only", (SourceKind.TELEKOM,)),
        source_job("thyssenkrupp-only", (SourceKind.THYSSENKRUPP,)),
        source_job(
            "shared",
            (SourceKind.LINKEDIN, SourceKind.STEPSTONE),
            score=80,
        ),
        source_job(
            "old-linkedin",
            (SourceKind.LINKEDIN,),
            posted_at=datetime.now(UTC).date() - timedelta(days=30),
            score=90,
        ),
    ],
)

CHANGED_SOURCE_FILTER_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=43),
    jobs=[
        source_job("fresh-linkedin", (SourceKind.LINKEDIN,)),
        source_job("fresh-stepstone", (SourceKind.STEPSTONE,)),
        source_job("fresh-glassdoor", (SourceKind.GLASSDOOR,)),
        source_job("fresh-dhl", (SourceKind.DHL,)),
        source_job("fresh-indeed", (SourceKind.INDEED,)),
    ],
)

GLOBAL_STATUS_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=44),
    jobs=[
        source_job(
            "global-saved",
            (SourceKind.LINKEDIN,),
            score=85,
            german_requirement="none",
        ).model_copy(update={"user_status": UserStatus.SAVED})
    ],
)

GLOBAL_ATS_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=45),
    jobs=[
        job.model_copy(update={"user_status": UserStatus.SAVED})
        for job in SOURCE_FILTER_SNAPSHOT.jobs
    ],
)


@pytest.fixture
def setup_page() -> Iterator[object]:
    """Serve the real console assets to one isolated headless browser page."""
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        ai_selection = {
            "ai_runtime": "claude-code",
            "claude": {
                "model": "sonnet",
                "effort": "medium",
                "thinking_enabled": True,
            },
            "locked": False,
        }

        def respond(route: object) -> None:
            request = route.request
            if request.url.endswith("/api/ai/config"):
                if request.method == "PUT":
                    update = request.post_data_json
                    ai_selection.update(
                        {
                            "ai_runtime": update["ai_runtime"],
                            "claude": update["claude"],
                        }
                    )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(ai_selection),
                )
                return
            if request.url.endswith("/api/ai/providers"):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        [provider.model_dump(mode="json") for provider in AI_PROVIDERS]
                    ),
                )
                return
            if request.url.endswith("/api/schedule"):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"installed": False, "local_time": None}),
                )
                return
            if request.url.endswith("/api/setup-and-scan/current"):
                route.fulfill(status=204, body="")
                return
            if request.url.endswith("/api/ats-runs/current"):
                route.fulfill(status=204, body="")
                return
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    (
                        CHANGED_SOURCE_FILTER_SNAPSHOT
                        if "source-set=changed" in request.url
                        else SOURCE_FILTER_SNAPSHOT
                    ),
                    global_snapshot=(
                        GLOBAL_STATUS_SNAPSHOT
                        if "global-status=1" in request.url
                        else GLOBAL_ATS_SNAPSHOT
                        if "ats-jobs=1" in request.url
                        else None
                    ),
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )

        page.route("**/*", respond)
        page.goto("http://draft.test/setup")
        page.wait_for_load_state("networkidle")
        yield page
        context.close()
        browser.close()


def open_ats_jobs_in_tracker(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?ats-jobs=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")


def test_ai_configuration_modal_saves_one_global_selection(setup_page: object) -> None:
    requests: list[dict[str, object]] = []

    def save_selection(route: object) -> None:
        if route.request.method != "PUT":
            route.fallback()
            return
        requests.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({**requests[-1], "locked": False}),
        )

    setup_page.route("**/api/ai/config", save_selection)
    setup_page.locator("[data-open-ai-config]").click()
    modal = setup_page.locator("#ai-config-modal")
    modal.wait_for(state="visible")
    assert setup_page.locator("#ai-runtime option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["claude-code", "api:deepseek", "api:open-router"]
    assert setup_page.locator("[data-activate-ai-provider]").count() == 0
    assert setup_page.locator("[data-edit-selected-api]").count() == 0

    setup_page.evaluate(
        "document.querySelector('#claude-model').tomselect.setValue('opus')"
    )
    setup_page.locator("#ai-runtime").select_option("api:open-router")

    assert setup_page.locator("#api-model-summary").text_content() == (
        "Open Router · claude-sonnet-4"
    )
    assert setup_page.locator("#ai-runtime").input_value() == "api:open-router"
    setup_page.locator("[data-save-ai-selection]").click()
    setup_page.locator("#ai-selection-feedback").get_by_text(
        "AI selection saved."
    ).wait_for()

    assert requests == [
        {
            "ai_runtime": "api:open-router",
            "claude": {
                "model": "opus",
                "effort": "medium",
                "thinking_enabled": True,
            },
        }
    ]


def test_ai_configuration_modal_is_read_only_while_ai_is_in_use(
    setup_page: object,
) -> None:
    setup_page.route(
        "**/api/ai/config",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ai_runtime": "api:deepseek",
                    "claude": {
                        "model": "opus",
                        "effort": "high",
                        "thinking_enabled": False,
                    },
                    "locked": True,
                }
            ),
        ),
    )
    setup_page.reload()

    open_button = setup_page.locator("[data-open-ai-config]")
    assert open_button.is_enabled()
    open_button.click()

    modal = setup_page.locator("#ai-config-modal")
    modal.wait_for(state="visible")
    assert modal.locator("#ai-config-lock-note").is_visible()
    assert modal.locator("#ai-runtime").is_disabled()
    assert modal.locator("[data-add-ai-provider]").is_disabled()
    assert modal.locator("[data-save-ai-selection]").is_disabled()


def test_job_tracker_ats_controls_share_height_and_edges(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="job-tracker"]').click()

    rectangles = setup_page.locator(
        "#new-run-button, #ats-resume, [data-open-ats]"
    ).evaluate_all(
        """controls => controls.map(control => {
          const rectangle = control.getBoundingClientRect();
          return {
            top: rectangle.top,
            bottom: rectangle.bottom,
            height: rectangle.height,
          };
        })"""
    )
    file_heights = setup_page.locator("#ats-resume").evaluate(
        """input => ({
          control: input.getBoundingClientRect().height,
          button: parseFloat(getComputedStyle(input, '::file-selector-button').height),
        })"""
    )

    for edge in ("top", "bottom", "height"):
        assert max(item[edge] for item in rectangles) == pytest.approx(
            min(item[edge] for item in rectangles), abs=0.1
        )
    assert file_heights["button"] == pytest.approx(file_heights["control"], abs=0.1)


def test_job_tracker_tab_shows_global_jobs_outside_review(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#review")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator('[data-nav-step="job-tracker"]').click()

    job_tracker = setup_page.locator("#job-tracker-view")
    job_tracker.wait_for(state="visible")

    assert setup_page.locator("#review-view").is_hidden()
    assert job_tracker.locator('[data-review-block="current"]').count() == 0
    assert job_tracker.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"]'
    ).is_visible()
    assert setup_page.locator("#review-actions").is_visible()
    assert setup_page.locator(
        '[data-nav-step="job-tracker"][aria-current="step"]'
    ).count() == 1
    assert setup_page.url == "http://draft.test/setup?global-status=1#job-tracker"

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")
    assert job_tracker.is_visible()


def test_ats_selection_is_available_only_in_job_tracker(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#review")
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator(
        '[data-review-block="current"] [data-ats-select-job]'
    ).count() == 0
    assert setup_page.locator("#new-run-button").is_visible()
    assert setup_page.locator("#ats-resume").is_hidden()
    assert setup_page.locator("[data-open-ats]").is_hidden()
    job_tracker_button = setup_page.locator(
        '#review-actions [data-review-only][data-back-to-job-tracker]'
    )
    assert job_tracker_button.text_content() == "Job Tracker"
    assert job_tracker_button.is_visible()

    job_tracker_button.click()

    assert setup_page.locator(
        '[data-review-block="global"] [data-ats-select-job]'
    ).count() == 1
    assert setup_page.locator("#new-run-button").is_visible()
    assert job_tracker_button.is_hidden()
    assert setup_page.locator("#ats-resume").is_visible()
    assert setup_page.locator("[data-open-ats]").is_visible()


def test_idle_ats_run_points_back_to_job_tracker(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="ats-run"]').click()

    assert setup_page.locator("#ats-run-message").text_content() == (
        "No ATS check is running. Start one from Job Tracker."
    )
    back = setup_page.locator("#ats-running [data-back-to-job-tracker]")
    assert back.get_attribute("href") == "#job-tracker"

    back.click()

    assert setup_page.locator("#job-tracker-view").is_visible()


def test_arbeitsagentur_switch_defaults_on_and_restores_disabled_draft(
    setup_page: object,
) -> None:
    switch = setup_page.locator("#arbeitsagentur-enabled")
    assert switch.is_checked()

    switch.uncheck()
    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    assert not setup_page.locator("#arbeitsagentur-enabled").is_checked()
    assert (
        setup_page.evaluate(
            "JSON.parse(localStorage.getItem('job-scan.setup-draft.v1')).arbeitsagentur_enabled"
        )
        is False
    )


def test_opencli_switch_disables_limit_and_restores_both_values(
    setup_page: object,
) -> None:
    switch = setup_page.locator("#linkedin-enabled")
    limit = setup_page.locator("#linkedin-limit")
    assert switch.is_checked()
    assert limit.is_enabled()

    limit.fill("37")
    switch.uncheck()

    assert limit.is_disabled()
    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    assert not setup_page.locator("#linkedin-enabled").is_checked()
    assert setup_page.locator("#linkedin-limit").is_disabled()
    assert setup_page.locator("#linkedin-limit").input_value() == "37"
    assert setup_page.evaluate(
        "JSON.parse(localStorage.getItem('job-scan.setup-draft.v1')).linkedin_enabled"
    ) is False


def test_bosch_switch_defaults_off_and_restores_selected_draft(
    setup_page: object,
) -> None:
    switch = setup_page.locator("#target-company-bosch")
    assert not switch.is_checked()

    switch.check()
    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#target-company-bosch").is_checked()
    assert setup_page.evaluate(
        "JSON.parse(localStorage.getItem('job-scan.setup-draft.v1')).target_companies"
    ) == ["bosch"]


def test_target_company_switches_restore_together(setup_page: object) -> None:
    bosch = setup_page.locator("#target-company-bosch")
    dallmeier = setup_page.locator("#target-company-dallmeier")
    dhl = setup_page.locator("#target-company-dhl")
    rohde_schwarz = setup_page.locator("#target-company-rohde-schwarz")
    siemens = setup_page.locator("#target-company-siemens")
    telekom = setup_page.locator("#target-company-telekom")
    thyssenkrupp = setup_page.locator("#target-company-thyssenkrupp")
    assert not bosch.is_checked()
    assert not dallmeier.is_checked()
    assert not dhl.is_checked()
    assert not rohde_schwarz.is_checked()
    assert not siemens.is_checked()
    assert not telekom.is_checked()
    assert not thyssenkrupp.is_checked()

    bosch.check()
    dallmeier.check()
    dhl.check()
    rohde_schwarz.check()
    siemens.check()
    telekom.check()
    thyssenkrupp.check()
    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#target-company-bosch").is_checked()
    assert setup_page.locator("#target-company-dallmeier").is_checked()
    assert setup_page.locator("#target-company-dhl").is_checked()
    assert setup_page.locator("#target-company-rohde-schwarz").is_checked()
    assert setup_page.locator("#target-company-siemens").is_checked()
    assert setup_page.locator("#target-company-telekom").is_checked()
    assert setup_page.locator("#target-company-thyssenkrupp").is_checked()
    assert setup_page.evaluate(
        "JSON.parse(localStorage.getItem('job-scan.setup-draft.v1')).target_companies"
    ) == [
        "bosch",
        "telekom",
        "rohde-schwarz",
        "siemens",
        "dhl",
        "thyssenkrupp",
        "dallmeier",
    ]


def test_disabled_opencli_switch_does_not_hide_existing_review_sources(
    setup_page: object,
) -> None:
    assert setup_page.locator("#source-filter").count() == 1

    setup_page.locator("#linkedin-enabled").uncheck()
    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == [
        "bosch",
        "dallmeier",
        "dhl",
        "glassdoor",
        "linkedin",
        "siemens",
        "stepstone",
        "successfactors",
        "telekom",
        "thyssenkrupp",
    ]

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == [
        "bosch",
        "dallmeier",
        "dhl",
        "glassdoor",
        "linkedin",
        "siemens",
        "stepstone",
        "successfactors",
        "telekom",
        "thyssenkrupp",
    ]
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="glassdoor-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="dallmeier-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="dhl-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="rohde-schwarz-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="siemens-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="telekom-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="thyssenkrupp-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="shared"]').is_visible()


def test_source_filter_labels_target_company_jobs(setup_page: object) -> None:
    options = setup_page.locator("#source-filter option")
    labels = {
        options.nth(index).get_attribute("value"): options.nth(index).text_content()
        for index in range(options.count())
    }

    assert labels["bosch"] == "Bosch"
    assert labels["dallmeier"] == "Dallmeier"
    assert labels["dhl"] == "DHL"
    assert labels["successfactors"] == "Rohde & Schwarz"
    assert labels["siemens"] == "Siemens"
    assert labels["telekom"] == "Deutsche Telekom"
    assert labels["thyssenkrupp"] == "thyssenkrupp"


def test_source_filter_uses_summary_control_and_checkbox_dropdown(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    wrapper = setup_page.locator(".source-filter")
    control = wrapper.locator(".ts-control")
    selected_tags = control.locator(":scope > .item")

    assert selected_tags.evaluate_all(
        "items => items.every(item => getComputedStyle(item).display === 'none')"
    )
    assert control.get_attribute("data-summary") == "10 sources selected"

    control.click()
    checkboxes = wrapper.locator('.ts-dropdown .option input[type="checkbox"]')
    assert checkboxes.count() == 10
    assert checkboxes.evaluate_all("items => items.every(item => item.checked)")


def test_source_filter_matches_any_checked_source(setup_page: object) -> None:
    assert setup_page.locator("#source-filter").count() == 1
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['linkedin'])"
    )

    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="shared"]').is_visible()

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.clear()"
    )
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="shared"]').is_hidden()


def test_source_filter_restores_manual_selection_after_reload(setup_page: object) -> None:
    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['linkedin'])"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == ["linkedin"]
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()


def test_source_filter_defaults_to_all_when_available_sources_change(
    setup_page: object,
) -> None:
    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['linkedin'])"
    )

    setup_page.goto("http://draft.test/setup?source-set=changed#review")
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == ["dhl", "glassdoor", "indeed", "linkedin", "stepstone"]


def test_source_filter_restores_empty_selection_after_reload(setup_page: object) -> None:
    setup_page.evaluate("document.querySelector('#source-filter').tomselect.clear()")

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator('[data-nav-step="review"]').click()

    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == []
    assert setup_page.locator(".review-groups [data-sources]:visible").count() == 0


def test_review_filters_do_not_hide_global_status_jobs(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#review")
    setup_page.wait_for_load_state("networkidle")
    global_card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"]'
    )
    global_count = setup_page.locator(
        '[data-review-block="global"] [data-review-group-count="saved"]'
    )

    setup_page.evaluate("document.querySelector('#source-filter').tomselect.clear()")

    assert setup_page.locator(
        '[data-review-block="current"] .review-groups [data-sources]:visible'
    ).count() == 0
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    assert global_card.is_visible()
    assert global_count.text_content() == "1"


def test_review_group_counts_follow_active_filters(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")
    nav_count = setup_page.locator(
        '[data-review-block="current"] [data-review-group-count="recommended"]'
    )

    assert nav_count.text_content() == "11"

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['stepstone'])"
    )

    assert nav_count.text_content() == "2"


def test_minimum_score_filter_includes_boundary_and_updates_counts(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")
    score_filter = setup_page.locator("#review-minimum-score")

    assert score_filter.input_value() == ""
    score_filter.select_option("80")

    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="shared"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_hidden()
    assert setup_page.locator(
        '[data-review-block="current"] [data-review-group-count="recommended"]'
    ).text_content() == "3"


def test_source_filter_does_not_overlap_posted_filter_at_narrow_width(
    setup_page: object,
) -> None:
    setup_page.set_viewport_size({"width": 1053, "height": 900})
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.evaluate(
        """
        const control = document.querySelector('#source-filter').tomselect;
        control.clear();
        control.clearOptions();
        control.addOption({ value: 'smartrecruiters', text: 'smartrecruiters' });
        control.addItem('smartrecruiters');
        """
    )

    source_control = setup_page.locator(".source-filter .ts-wrapper").bounding_box()
    posted_filter = setup_page.locator(".posted-within-filter").bounding_box()

    assert source_control is not None
    assert posted_filter is not None
    root_rem = setup_page.evaluate(
        "parseFloat(getComputedStyle(document.documentElement).fontSize)"
    )
    gap = posted_filter["x"] - (source_control["x"] + source_control["width"])
    assert gap == pytest.approx(root_rem, abs=0.5)


@pytest.mark.parametrize("viewport_width", [1053, 1440])
def test_review_filter_controls_keep_fixed_width(
    setup_page: object,
    viewport_width: int,
) -> None:
    setup_page.set_viewport_size({"width": viewport_width, "height": 900})
    setup_page.locator('[data-nav-step="review"]').click()

    source = setup_page.locator(".source-filter .ts-wrapper").bounding_box()
    posted = setup_page.locator("#review-posted-within-days").bounding_box()
    company = setup_page.locator("#review-company-size").bounding_box()

    assert source is not None
    assert posted is not None
    assert company is not None
    root_rem = setup_page.evaluate(
        "parseFloat(getComputedStyle(document.documentElement).fontSize)"
    )
    expected_width = 16 * root_rem
    assert all(
        box["width"] == pytest.approx(expected_width, abs=0.5)
        for box in (source, posted, company)
    )


def test_posted_within_filter_combines_with_checked_sources(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    posted_within = setup_page.locator("#review-posted-within-days")
    assert posted_within.input_value() == "7"
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="old-linkedin"]').is_hidden()

    posted_within.select_option("")
    assert setup_page.locator('article[data-job-key="old-linkedin"]').is_visible()

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['stepstone'])"
    )
    assert setup_page.locator('article[data-job-key="old-linkedin"]').is_hidden()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_visible()


def test_company_size_filter_uses_reported_ranges(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    company_size = setup_page.locator("#review-company-size")
    assert company_size.input_value() == "0"

    company_size.select_option("1000")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="glassdoor-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="shared"]').is_hidden()

    company_size.select_option("10000")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="glassdoor-only"]').is_visible()

    company_size.select_option("0")
    assert setup_page.locator('article[data-job-key="shared"]').is_visible()


def test_company_industry_filter_includes_unknown_and_combines_with_sources(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")

    company_industry = setup_page.locator("#review-company-industry")
    assert company_industry.locator("option").all_text_contents() == [
        "Any industry",
        "Industrial Automation",
        "Software Development",
        "Unknown",
    ]

    company_industry.select_option(label="Industrial Automation")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_hidden()

    company_industry.select_option(label="Unknown")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_visible()

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['stepstone'])"
    )
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="shared"]').is_visible()


def test_language_requirement_filter_uses_explicit_german_requirement(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    language = setup_page.locator("#review-language-requirement")

    assert language.locator("option").all_text_contents() == [
        "Any requirement",
        "German required",
        "No German requirement",
    ]
    assert language.input_value() == "not-required"
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_visible()

    language.select_option("required")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_hidden()

    language.select_option("not-required")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="stepstone-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="glassdoor-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_hidden()
    assert setup_page.locator('article[data-job-key="dhl-only"]').is_hidden()

    language.select_option("")
    assert setup_page.locator('article[data-job-key="linkedin-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="bosch-only"]').is_visible()
    assert setup_page.locator('article[data-job-key="dhl-only"]').is_visible()


def test_language_requirement_filter_combines_with_sources_and_updates_counts(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    nav_count = setup_page.locator(
        '[data-review-block="current"] [data-review-group-count="recommended"]'
    )

    setup_page.locator("#review-language-requirement").select_option("required")
    assert nav_count.text_content() == "1"

    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['stepstone'])"
    )
    assert nav_count.text_content() == "0"


def test_company_size_help_opens_on_click(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")
    card = setup_page.locator('article[data-job-key="bosch-only"]')

    card.locator("[data-company-size-help]").click()

    tooltip = setup_page.locator(".tooltip.show .tooltip-inner")
    assert tooltip.text_content() == (
        "Use AI to search the web for this company's employee count."
    )


def test_company_size_search_reports_lookup_failure_on_the_selected_card(
    setup_page: object,
) -> None:
    requested: list[str] = []

    def reject_lookup(route: object) -> None:
        requested.append(route.request.url)
        route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps(
                {"detail": "AI could not verify this company's employee count."}
            ),
        )

    setup_page.route("**/api/jobs/bosch-only/company-size", reject_lookup)
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")
    card = setup_page.locator('article[data-job-key="bosch-only"]')

    card.locator("[data-company-size-search]").click()

    error = card.locator(".company-size-search-error")
    error.wait_for(state="visible")
    assert error.text_content() == "AI could not verify this company's employee count."
    assert error.is_visible()
    assert card.locator("[data-company-size-search]").is_enabled()
    assert card.locator("[data-company-size-search]").text_content() == "AI Search"
    assert requested == ["http://draft.test/api/jobs/bosch-only/company-size"]


def test_global_company_size_search_uses_the_global_job_endpoint(
    setup_page: object,
) -> None:
    requested: list[str] = []

    def return_company_size(route: object) -> None:
        requested.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    setup_page.route("**/company-size", return_company_size)
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    global_card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"]'
    )

    global_card.locator("[data-company-size-search]").click()
    setup_page.wait_for_load_state("networkidle")

    assert requested == [
        "http://draft.test/api/global-jobs/global-saved/company-size"
    ]


def test_global_job_delete_confirms_and_uses_the_global_endpoint(
    setup_page: object,
) -> None:
    confirmation_messages: list[str] = []
    setup_page.route(
        "**/api/global-jobs/global-saved",
        lambda route: route.fulfill(status=204, body=""),
    )
    setup_page.on(
        "dialog",
        lambda dialog: (confirmation_messages.append(dialog.message), dialog.accept()),
    )
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    delete_button = setup_page.locator(
        '[data-review-block="global"] '
        'article[data-job-key="global-saved"] [data-global-job-delete]'
    )

    with setup_page.expect_request(
        "**/api/global-jobs/global-saved"
    ) as request_info:
        delete_button.click()

    assert request_info.value.method == "DELETE"
    assert confirmation_messages == [
        "Permanently delete this job and its Job Tracker history?"
    ]


def test_status_change_updates_review_without_page_navigation(
    setup_page: object,
) -> None:
    updated_global = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={"user_status": UserStatus.INTERVIEWING}
            )
        ],
    )
    status_saved = False

    def respond_after_status_change(route: object) -> None:
        nonlocal status_saved
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved/status"):
            status_saved = True
            route.fulfill(status=204, body="")
            return
        if status_saved and request.method == "GET" and "/setup" in request.url:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    SOURCE_FILTER_SNAPSHOT,
                    global_snapshot=updated_global,
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_after_status_change)
    setup_page.goto("http://draft.test/setup?global-status=1#review")
    setup_page.wait_for_load_state("networkidle")
    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['linkedin'])"
    )
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    global_review = setup_page.locator('[data-review-block="global"]')
    status_form = global_review.locator(
        'article[data-job-key="global-saved"] [data-job-action="status"]'
    )

    status_form.locator('select[name="status"]').select_option("interviewing")
    status_form.locator('button[type="submit"]').click()

    interviewing_card = global_review.locator(
        '#interviewing article[data-job-key="global-saved"]'
    )
    interviewing_card.wait_for(state="attached")
    assert navigations == []
    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == ["linkedin"]
    assert global_review.locator(
        '[data-review-group-tab="saved"]'
    ).get_attribute("aria-current") == "page"
    assert global_review.locator(
        '[data-review-group-count="saved"]'
    ).text_content() == "0"
    assert global_review.locator(
        '[data-review-group-count="interviewing"]'
    ).text_content() == "1"
    global_review.locator('[data-review-group-tab="interviewing"]').click()
    interviewing_card.locator("[data-ats-select-job]").check()
    assert setup_page.locator("[data-open-ats]").text_content() == (
        "Check 1 selected jobs"
    )


def test_global_status_request_includes_the_selected_resume(
    setup_page: object,
) -> None:
    resume_id = "sha256:" + "a" * 64
    resume = ResumeCatalogEntry(
        resume_id=resume_id,
        profile_hash="sha256:" + "b" * 64,
        candidate_name="Backend CV",
        filename="backend.pdf",
        created_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
    )
    posted: list[dict[str, object]] = []

    def serve_resume_tracker(route: object) -> None:
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved/status"):
            posted.append(request.post_data_json)
            route.fulfill(status=204, body="")
            return
        if "resume-status=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=GLOBAL_STATUS_SNAPSHOT,
                resume_catalog=[resume],
                selected_resume_id=resume_id,
            ),
        )

    setup_page.route("**/*", serve_resume_tracker)
    setup_page.goto("http://draft.test/setup?resume-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    form = setup_page.locator(
        '[data-review-block="global"] '
        '[data-job-key="global-saved"] [data-job-action="status"]'
    )

    form.locator('select[name="status"]').select_option("applied")
    form.locator('button[type="submit"]').click()
    setup_page.wait_for_timeout(100)

    assert posted == [{"status": "applied", "resume_id": resume_id}]


def test_application_resume_correction_posts_without_page_navigation(
    setup_page: object,
) -> None:
    resume_a = "sha256:" + "a" * 64
    resume_b = "sha256:" + "b" * 64
    resumes = [
        ResumeCatalogEntry(
            resume_id=resume_a,
            profile_hash="sha256:" + "c" * 64,
            candidate_name="Backend CV",
            filename="backend.pdf",
            created_at=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
        ),
        ResumeCatalogEntry(
            resume_id=resume_b,
            profile_hash="sha256:" + "d" * 64,
            candidate_name="Platform CV",
            filename="platform.pdf",
            created_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        ),
    ]
    tracked = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={
                    "user_status": UserStatus.APPLIED,
                    "application_resume_id": resume_a,
                }
            )
        ],
    )
    posted: list[dict[str, object]] = []

    def serve_resume_correction(route: object) -> None:
        request = route.request
        if request.url.endswith(
            "/api/global-jobs/global-saved/application-resume"
        ):
            posted.append(request.post_data_json)
            route.fulfill(status=204, body="")
            return
        if "resume-correction=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=tracked,
                resume_catalog=resumes,
                selected_resume_id=resume_a,
            ),
        )

    setup_page.route("**/*", serve_resume_correction)
    setup_page.goto("http://draft.test/setup?resume-correction=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="applied"]'
    ).click()
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    form = setup_page.locator(
        '[data-review-block="global"] '
        '[data-job-key="global-saved"] '
        '[data-job-action="application-resume"]'
    )

    form.locator('select[name="resume_id"]').select_option(resume_b)
    form.locator('button[type="submit"]').click()
    setup_page.wait_for_timeout(100)

    assert posted == [{"resume_id": resume_b}]
    assert navigations == []


def test_global_resume_selection_updates_only_global_review(
    setup_page: object,
) -> None:
    first_resume_id = "sha256:" + "a" * 64
    second_resume_id = "sha256:" + "b" * 64
    resumes = [
        ResumeCatalogEntry(
            resume_id=first_resume_id,
            profile_hash="sha256:" + "c" * 64,
            candidate_name="Backend CV",
            filename="backend.pdf",
            created_at=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
        ),
        ResumeCatalogEntry(
            resume_id=second_resume_id,
            profile_hash="sha256:" + "d" * 64,
            candidate_name="Platform CV",
            filename="platform.pdf",
            created_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        ),
    ]
    second_global_snapshot = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            source_job(
                "global-applied",
                (SourceKind.STEPSTONE,),
                score=90,
                german_requirement="none",
            ).model_copy(update={"user_status": UserStatus.APPLIED})
        ],
    )

    def respond_to_resume_selection(route: object) -> None:
        request = route.request
        if request.method == "GET" and "/setup" in request.url:
            selected_resume_id = parse_qs(urlparse(request.url).query).get(
                "resume_id", [first_resume_id]
            )[0]
            selected_second = selected_resume_id == second_resume_id
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    SOURCE_FILTER_SNAPSHOT,
                    global_snapshot=(
                        second_global_snapshot
                        if selected_second
                        else GLOBAL_STATUS_SNAPSHOT
                    ),
                    resume_catalog=resumes,
                    selected_resume_id=selected_resume_id,
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_to_resume_selection)
    setup_page.goto(
        f"http://draft.test/setup?resume_id={quote(first_resume_id)}#review"
    )
    setup_page.wait_for_load_state("networkidle")
    setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.setValue(['linkedin'])"
    )
    current_review = setup_page.locator('[data-review-block="current"]')
    global_review = setup_page.locator('[data-review-block="global"]')
    current_review.locator('[data-review-group-tab="excluded"]').click()
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    global_review.locator('[data-review-group-tab="applied"]').click()
    setup_page.locator("body").evaluate(
        "body => { body.dataset.resumeLocalMarker = 'preserved'; }"
    )

    resume_select = global_review.locator("[data-global-resume-select]")
    resume_select.select_option(second_resume_id)

    global_review.locator(
        'article[data-job-key="global-applied"]'
    ).wait_for(state="visible")
    assert global_review.locator(
        "[data-global-resume-select]"
    ).input_value() == second_resume_id
    assert setup_page.locator("body").get_attribute(
        "data-resume-local-marker"
    ) == "preserved"
    assert setup_page.evaluate(
        "document.querySelector('#source-filter').tomselect.items"
    ) == ["linkedin"]
    assert current_review.locator(
        '[data-review-group-tab="excluded"]'
    ).get_attribute("aria-current") == "page"
    assert global_review.locator(
        '[data-review-group-tab="applied"]'
    ).get_attribute("aria-current") == "page"
    assert global_review.locator(
        'article[data-job-key="global-saved"]'
    ).count() == 0
    assert global_review.locator(
        '[data-review-group-count="saved"]'
    ).text_content() == "0"
    assert global_review.locator(
        '[data-review-group-count="applied"]'
    ).text_content() == "1"
    assert setup_page.locator("body").get_attribute(
        "data-selected-resume-id"
    ) == second_resume_id
    assert parse_qs(urlparse(setup_page.url).query)["resume_id"] == [
        second_resume_id
    ]
    assert urlparse(setup_page.url).fragment == "applied"
    assert setup_page.locator("#job-tracker-view").is_visible()


def test_status_change_exposes_ats_selection_only_after_job_enters_tracker(
    setup_page: object,
) -> None:
    current_job = source_job(
        "current-status-local",
        (SourceKind.LINKEDIN,),
        german_requirement="none",
    )
    saved_job = current_job.model_copy(
        update={"user_status": UserStatus.SAVED}
    )
    status_saved = False

    def respond_after_status_change(route: object) -> None:
        nonlocal status_saved
        request = route.request
        if request.url.endswith("/api/jobs/current-status-local/status"):
            status_saved = True
            route.fulfill(status=204, body="")
            return
        if request.method == "GET" and "/setup" in request.url:
            visible_job = saved_job if status_saved else current_job
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    Snapshot(
                        meta=StoreMeta(data_revision=51 if status_saved else 50),
                        jobs=[visible_job],
                    ),
                    global_snapshot=Snapshot(
                        meta=StoreMeta(data_revision=51),
                        jobs=[saved_job] if status_saved else [],
                    ),
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_after_status_change)
    setup_page.goto("http://draft.test/setup?status-block-local=1#review")
    setup_page.wait_for_load_state("networkidle")
    current_card = setup_page.locator(
        '[data-review-block="current"] '
        'article[data-job-key="current-status-local"]'
    )
    assert current_card.locator("[data-ats-select-job]").count() == 0
    status_form = current_card.locator('[data-job-action="status"]')
    status_form.locator('select[name="status"]').select_option("saved")

    status_form.locator('button[type="submit"]').click()

    global_card = setup_page.locator(
        '[data-review-block="global"] '
        '#saved article[data-job-key="current-status-local"]'
    )
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    global_card.wait_for(state="visible")
    assert current_card.count() == 0
    selector = global_card.locator("[data-ats-select-job]")
    assert not selector.is_checked()
    selector.check()
    assert setup_page.locator("[data-open-ats]").text_content() == (
        "Check 1 selected jobs"
    )


def test_restore_updates_only_the_affected_review_card(
    setup_page: object,
) -> None:
    excluded = source_job(
        "excluded-local",
        (SourceKind.LINKEDIN,),
        german_requirement="none",
    ).model_copy(
        update={
            "machine_status": MachineStatus.EXCLUDED,
            "last_successful_review_profile_hash": "sha256:profile",
        }
    )
    restored = excluded.model_copy(update={"manual_override": "show"})
    restore_saved = False

    def respond_to_restore(route: object) -> None:
        nonlocal restore_saved
        request = route.request
        if request.url.endswith("/api/jobs/excluded-local/restore"):
            restore_saved = True
            route.fulfill(status=204, body="")
            return
        if request.method == "GET" and "/setup" in request.url:
            snapshot = Snapshot(
                meta=StoreMeta(data_revision=46 if restore_saved else 45),
                jobs=[restored if restore_saved else excluded],
            )
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    snapshot,
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_to_restore)
    setup_page.goto("http://draft.test/setup?restore-local=1#review")
    setup_page.wait_for_load_state("networkidle")
    current_review = setup_page.locator('[data-review-block="current"]')
    current_review.locator('[data-review-group-tab="excluded"]').click()
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))

    current_review.locator(
        '#excluded article[data-job-key="excluded-local"] '
        '[data-job-action="restore"] button'
    ).click()

    restored_card = current_review.locator(
        '#recommended article[data-job-key="excluded-local"]'
    )
    restored_card.wait_for(state="attached")
    assert navigations == []
    assert restored_card.locator(".restored-label").text_content() == "Restored"
    assert restored_card.locator('[data-job-action="restore"]').count() == 0
    assert current_review.locator(
        '[data-review-group-count="recommended"]'
    ).text_content() == "1"
    assert current_review.locator(
        '[data-review-group-count="excluded"]'
    ).text_content() == "0"


def test_company_size_search_updates_only_the_affected_review_card(
    setup_page: object,
) -> None:
    job = source_job(
        "company-size-local",
        (SourceKind.LINKEDIN,),
        german_requirement="none",
    )
    company_size = reported_company_size("501-1,000 employees", 501, 1000)
    refreshed_job = job.model_copy(update={"company_size": company_size})
    company_size_saved = False

    def respond_to_company_size(route: object) -> None:
        nonlocal company_size_saved
        request = route.request
        if request.url.endswith("/api/jobs/company-size-local/company-size"):
            company_size_saved = True
            route.fulfill(
                status=200,
                content_type="application/json",
                body=company_size.model_dump_json(),
            )
            return
        if request.method == "GET" and "/setup" in request.url:
            snapshot = Snapshot(
                meta=StoreMeta(data_revision=48 if company_size_saved else 47),
                jobs=[refreshed_job if company_size_saved else job],
            )
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    snapshot,
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_to_company_size)
    setup_page.goto("http://draft.test/setup?company-size-local=1#review")
    setup_page.wait_for_load_state("networkidle")
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    card = setup_page.locator(
        'article[data-job-key="company-size-local"]'
    ).first

    card.locator("[data-company-size-search]").click()

    refreshed_card = setup_page.locator(
        'article[data-job-key="company-size-local"]'
    ).first
    refreshed_card.locator(".company-size a").wait_for(state="attached")
    assert navigations == []
    assert "501-1,000 employees" in refreshed_card.locator(
        ".company-size"
    ).text_content()
    assert refreshed_card.get_attribute("data-company-size-minimum") == "501"
    assert refreshed_card.get_attribute("data-company-size-maximum") == "1000"
    setup_page.evaluate("document.querySelector('#source-filter').tomselect.clear()")
    assert refreshed_card.is_hidden()


def test_global_job_delete_updates_review_without_page_navigation(
    setup_page: object,
) -> None:
    job_deleted = False

    def respond_to_delete(route: object) -> None:
        nonlocal job_deleted
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved"):
            job_deleted = True
            route.fulfill(status=204, body="")
            return
        if job_deleted and request.method == "GET" and "/setup" in request.url:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    SOURCE_FILTER_SNAPSHOT,
                    global_snapshot=Snapshot(
                        meta=StoreMeta(data_revision=49),
                        jobs=[],
                    ),
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
                    ats_source_run_id="search-1",
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_to_delete)
    setup_page.on("dialog", lambda dialog: dialog.accept())
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    global_review = setup_page.locator('[data-review-block="global"]')

    global_review.locator(
        'article[data-job-key="global-saved"] [data-global-job-delete]'
    ).click()

    card = global_review.locator('article[data-job-key="global-saved"]')
    card.wait_for(state="detached")
    assert navigations == []
    assert global_review.locator(
        '[data-review-group-count="saved"]'
    ).text_content() == "0"


def test_global_job_delete_cancel_keeps_the_card(setup_page: object) -> None:
    requested: list[str] = []
    setup_page.route(
        "**/api/global-jobs/global-saved",
        lambda route: requested.append(route.request.url),
    )
    setup_page.on("dialog", lambda dialog: dialog.dismiss())
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] '
        'article[data-job-key="global-saved"]'
    )

    card.locator("[data-global-job-delete]").click()

    assert requested == []
    assert card.is_visible()
    assert card.locator("[data-global-job-delete]").is_enabled()


def test_manual_job_dialog_submits_url_and_refreshes_review(
    setup_page: object,
) -> None:
    posted: list[dict[str, object]] = []
    import_id = "manual-import-1"

    def import_job(route: object) -> None:
        posted.append(json.loads(route.request.post_data))
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "import_id": import_id,
                    "status": "running",
                    "step": "queued",
                    "message": "Manual import started.",
                    "progress_percent": 2,
                    "job_key": None,
                    "result_status": None,
                    "resume_id": "sha256:" + "a" * 64,
                    "error": None,
                }
            ),
        )

    def poll_import(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "import_id": import_id,
                    "status": "complete",
                    "step": "complete",
                    "message": "Manual import complete.",
                    "progress_percent": 100,
                    "job_key": "manual-42",
                    "result_status": "saved",
                    "resume_id": "sha256:" + "a" * 64,
                    "error": None,
                }
            ),
        )

    setup_page.route("**/api/global-jobs/import", import_job)
    setup_page.route(f"**/api/manual-job-imports/{import_id}", poll_import)
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    resume_id = "sha256:" + "a" * 64
    setup_page.evaluate(
        "resumeId => { document.body.dataset.selectedResumeId = resumeId; }",
        resume_id,
    )
    dialog = setup_page.locator("#manual-job-dialog")

    setup_page.locator("[data-open-manual-job]").click()

    dialog.wait_for(state="visible")
    dialog.locator("#manual-job-url").fill(
        "https://careers.example/jobs/42"
    )
    dialog.locator("[data-submit-manual-job]").click()
    dialog.wait_for(state="hidden")

    assert posted == [
        {"url": "https://careers.example/jobs/42", "resume_id": resume_id}
    ]


def test_manual_job_dialog_uploads_a_new_resume(setup_page: object) -> None:
    posted: list[tuple[str, str]] = []
    import_id = "manual-import-2"

    def import_job(route: object) -> None:
        posted.append(
            (
                route.request.headers.get("content-type", ""),
                route.request.post_data or "",
            )
        )
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "import_id": import_id,
                    "status": "running",
                    "step": "queued",
                    "message": "Manual import started.",
                    "progress_percent": 2,
                    "job_key": None,
                    "result_status": None,
                    "resume_id": "sha256:" + "b" * 64,
                    "error": None,
                }
            ),
        )

    def poll_import(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "import_id": import_id,
                    "status": "complete",
                    "step": "complete",
                    "message": "Manual import complete.",
                    "progress_percent": 100,
                    "job_key": "manual-42",
                    "result_status": "saved",
                    "resume_id": "sha256:" + "b" * 64,
                    "error": None,
                }
            ),
        )

    setup_page.route("**/api/global-jobs/import-with-resume", import_job)
    setup_page.route(f"**/api/manual-job-imports/{import_id}", poll_import)
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    setup_page.locator("[data-open-manual-job]").click()
    dialog = setup_page.locator("#manual-job-dialog")
    dialog.locator("#manual-job-url").fill("https://careers.example/jobs/42")
    dialog.locator("#manual-job-resume").set_input_files(
        {
            "name": "backend.pdf",
            "mimeType": "application/pdf",
            "buffer": b"PDF resume",
        }
    )

    dialog.locator("[data-submit-manual-job]").click()
    dialog.wait_for(state="hidden")
    setup_page.wait_for_load_state("networkidle")

    assert len(posted) == 1
    assert posted[0][0].startswith("multipart/form-data;")
    assert 'name="url"' in posted[0][1]
    assert "https://careers.example/jobs/42" in posted[0][1]
    assert "backend.pdf" in posted[0][1]
    assert urlparse(setup_page.url).fragment == "job-tracker"
    assert setup_page.locator("#job-tracker-view").is_visible()


def test_manual_job_dialog_keeps_url_and_shows_import_failure(
    setup_page: object,
) -> None:
    setup_page.route(
        "**/api/global-jobs/import",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps(
                {"detail": "This page does not contain one complete job."}
            ),
        ),
    )
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    setup_page.locator("[data-open-manual-job]").click()
    dialog = setup_page.locator("#manual-job-dialog")
    url_input = dialog.locator("#manual-job-url")
    submit = dialog.locator("[data-submit-manual-job]")
    url_input.fill("https://careers.example/jobs/empty")

    submit.click()

    error = dialog.locator("[data-manual-job-error]")
    error.wait_for(state="visible")
    assert error.text_content() == "This page does not contain one complete job."
    assert dialog.is_visible()
    assert url_input.input_value() == "https://careers.example/jobs/empty"
    assert submit.is_enabled()
    assert submit.text_content() == "Import to Saved"


def test_setup_draft_restores_regular_fields_after_reload(setup_page: object) -> None:
    setup_page.locator("#linkedin-limit").fill("63")
    setup_page.locator("#indeed-de-limit").fill("37")
    setup_page.locator("#stepstone-de-limit").fill("41")
    setup_page.locator("#glassdoor-de-limit").fill("43")
    setup_page.locator("#simplify-de-limit").fill("47")
    setup_page.locator("#minimum-company-size").select_option("1000")
    setup_page.locator("#posted-within-days").select_option("14")
    setup_page.locator("#claude-batch-size").fill("17")
    setup_page.locator("#scan-time").fill("08:45")
    setup_page.evaluate(
        "const control = document.querySelector('#search-terms').tomselect;"
        "control.addOption({value: 'Custom Role', text: 'Custom Role'});"
        "control.setValue(['Backend Engineer', 'Custom Role'])"
    )
    setup_page.evaluate(
        "document.querySelector('#locations').tomselect.setValue(['Munich']);"
        "document.querySelector('#german-level').tomselect.setValue('B1')"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#linkedin-limit").input_value() == "63"
    assert setup_page.locator("#indeed-de-limit").input_value() == "37"
    assert setup_page.locator("#stepstone-de-limit").input_value() == "41"
    assert setup_page.locator("#glassdoor-de-limit").input_value() == "43"
    assert setup_page.locator("#simplify-de-limit").input_value() == "47"
    assert setup_page.locator("#minimum-company-size").input_value() == "1000"
    assert setup_page.locator("#posted-within-days").input_value() == "14"
    assert setup_page.locator("#claude-batch-size").input_value() == "17"
    assert setup_page.locator("#scan-time").input_value() == "08:45"
    assert setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.items"
    ) == ["Backend Engineer", "Custom Role"]
    assert setup_page.evaluate(
        "document.querySelector('#locations').tomselect.items"
    ) == ["Munich"]
    assert setup_page.evaluate(
        "document.querySelector('#german-level').tomselect.getValue()"
    ) == "B1"


def test_setup_draft_does_not_store_api_key(setup_page: object) -> None:
    setup_page.locator("#scan-time").fill("08:45")
    setup_page.locator("[data-open-ai-config]").click()
    setup_page.locator("[data-add-ai-provider]").click()
    setup_page.locator("#ai-provider-api-key").fill("secret-must-not-persist")

    serialized = setup_page.evaluate(
        "window.localStorage.getItem('job-scan.setup-draft.v1')"
    )

    assert serialized is not None
    assert "secret-must-not-persist" not in serialized


def test_resume_suggestions_only_run_after_analyze_click_and_keep_existing_setup(
    setup_page: object,
) -> None:
    suggestion_requests: list[str] = []

    def respond_suggestions(route: object) -> None:
        suggestion_requests.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "search_terms": ["Java Backend Engineer", "Platform Engineer"],
                }
            ),
        )

    setup_page.route(
        "**/api/resume-suggestions",
        respond_suggestions,
    )

    analyze_button = setup_page.get_by_role("button", name="Analyze resume with AI")
    search_term_help = setup_page.locator("#search-term-suggestion-help")
    assert analyze_button.is_disabled()
    assert search_term_help.is_hidden()
    assert setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.items"
    ) == []

    setup_page.locator("#resume").set_input_files(str(RESUME))
    assert suggestion_requests == []

    assert analyze_button.is_enabled()
    analyze_button.click()

    search_suggestion = setup_page.get_by_role("button", name="Java Backend Engineer")
    search_suggestion.wait_for()
    assert suggestion_requests == ["POST"]
    assert search_term_help.is_visible()
    search_suggestion.click()

    assert search_suggestion.is_disabled()

    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.removeItem('Java Backend Engineer')"
    )
    assert search_suggestion.is_enabled()

    search_suggestion.click()

    assert setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.items"
    ) == ["Java Backend Engineer"]
    assert setup_page.evaluate(
        "'target_lanes' in JSON.parse(localStorage.getItem('job-scan.setup-draft.v1'))"
    ) is False


def test_deleting_latest_history_keeps_global_setup_draft(setup_page: object) -> None:
    setup_page.evaluate(
        "localStorage.setItem('job-scan.setup-draft.v1', JSON.stringify({sentinel: 'keep'}))"
    )
    setup_page.evaluate(
        "document.querySelector('#scan-history').insertAdjacentHTML("
        "'beforeend', "
        "'<div data-scan-history-id=\"run-latest\">' +"
        "'<span class=\"ai-provider-title\"><strong>Candidate</strong></span>' +"
        "'<button type=\"button\" data-scan-delete>Delete</button>' +"
        "'</div>')"
    )

    setup_page.route(
        "**/api/scan-history/run-latest",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"deleted_latest": True}),
        ),
    )
    setup_page.on("dialog", lambda dialog: dialog.accept())

    setup_page.evaluate("document.querySelector('[data-scan-delete]').click()")
    setup_page.wait_for_url("**/setup#setup")

    assert setup_page.evaluate(
        "JSON.parse(localStorage.getItem('job-scan.setup-draft.v1')).sentinel"
    ) == "keep"


def test_run_page_polls_and_renders_real_backend_stages(setup_page: object) -> None:
    states = [
        {
            "run_id": "web-run-1",
            "status": "running",
            "stage": "sources",
            "message": "Searching configured job sources...",
            "result": None,
            "error": None,
        },
        {
            "run_id": "web-run-1",
            "status": "running",
            "stage": "review",
            "message": "Reviewing complete job descriptions...",
            "result": None,
            "error": None,
        },
        {
            "run_id": "web-run-1",
            "status": "running",
            "stage": "company_size",
            "message": "Checking company sizes: 2/4 companies...",
            "progress_percent": 97,
            "company_size_progress": {
                "completed_companies": 2,
                "total_companies": 4,
            },
            "result": None,
            "error": None,
        },
        {
            "run_id": "web-run-1",
            "status": "running",
            "stage": "publish",
            "message": "Publishing review queue...",
            "result": None,
            "error": None,
        },
        {
            "run_id": "web-run-1",
            "status": "complete",
            "stage": "publish",
            "message": "Review queue published.",
            "result": {
                "summary": {
                    "occurrence_count": 42,
                    "reviewed_count": 8,
                    "eligible_count": 5,
                    "source_error_count": 1,
                },
                "schedule": {"installed": False, "local_time": None},
            },
            "error": None,
        },
    ]

    posted_bodies: list[str] = []

    def respond_run(route: object) -> None:
        if route.request.method == "POST":
            posted_bodies.append(route.request.post_data or "")
            body = {
                "run_id": "web-run-1",
                "status": "running",
                "stage": "profile",
                "message": "Building candidate profile...",
                "result": None,
                "error": None,
            }
            route.fulfill(status=202, content_type="application/json", body=json.dumps(body))
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(states.pop(0)),
        )

    setup_page.route("**/api/setup-and-scan", respond_run)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_run)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )
    setup_page.locator("[data-open-ai-config]").click()
    setup_page.locator("#ai-config-modal").wait_for(state="visible")
    setup_page.locator("#claude-thinking-enabled").uncheck()
    setup_page.locator("#ai-config-modal [data-bs-dismiss='modal']").last.click()
    setup_page.locator("#ai-config-modal").wait_for(state="hidden")
    setup_page.locator("#target-company-bosch").check()
    setup_page.locator("#target-company-dallmeier").check()
    setup_page.locator("#target-company-dhl").check()
    setup_page.locator("#target-company-rohde-schwarz").check()
    setup_page.locator("#target-company-siemens").check()
    setup_page.locator("#target-company-telekom").check()
    setup_page.locator("#target-company-thyssenkrupp").check()

    setup_page.get_by_role("button", name="Save and run scan").click()

    setup_page.locator("#run-percent").wait_for(state="visible")
    setup_page.get_by_text("100%", exact=True).wait_for()
    assert setup_page.locator('[data-run-item="profile"] small').text_content() == (
        "Profile ready"
    )
    assert setup_page.locator('[data-run-item="sources"] small').text_content() == (
        "42 jobs found"
    )
    assert setup_page.locator('[data-run-item="review"] small').text_content() == (
        "8 jobs reviewed"
    )
    assert setup_page.locator('[data-run-item="company_size"] small').text_content() == (
        "Company sizes checked"
    )
    assert setup_page.locator('[data-run-item="publish"] small').text_content() == (
        "5 eligible jobs"
    )
    assert '"thinking_enabled":false' in posted_bodies[0]
    assert (
        '"target_companies":["bosch","telekom","rohde-schwarz","siemens","dhl","thyssenkrupp","dallmeier"]'
        in posted_bodies[0]
    )
    assert states == []


def test_run_navigation_opens_idle_state(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="run"]').click()

    assert setup_page.url == "http://draft.test/setup#run"
    assert setup_page.locator("#run-view").is_visible()
    assert setup_page.locator("#run-percent").text_content() == "Idle"
    assert setup_page.locator("#run-message").text_content() == (
        "No scan is running. Start one from Setup."
    )


def test_active_run_restores_progress_and_disables_setup_submit(
    setup_page: object,
) -> None:
    running = {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "sources",
        "message": "Searching configured job sources...",
        "progress_percent": 64,
        "review_progress": None,
        "result": None,
        "error": None,
    }

    def respond_running(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(running),
        )

    setup_page.route("**/api/setup-and-scan/current", respond_running)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_running)
    setup_page.reload()

    submit = setup_page.get_by_role("button", name="Scan already running")
    submit.wait_for()
    assert submit.is_disabled()

    setup_page.locator('[data-nav-step="run"]').click()

    assert setup_page.locator("#run-view").is_visible()
    assert setup_page.locator("#run-percent").text_content() == "64%"
    assert setup_page.locator("#run-message").text_content() == (
        "Searching configured job sources..."
    )


def test_active_run_keeps_submitted_ai_name_after_setup_runtime_changes(
    setup_page: object,
) -> None:
    running = {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "company_size",
        "message": "Checking company sizes...",
        "progress_percent": 95,
        "ai_runtime": "api:deepseek",
        "review_progress": None,
        "result": None,
        "error": None,
    }

    def respond_running(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(running),
        )

    def fail_provider_list(route: object) -> None:
        route.fulfill(status=503, body="")

    setup_page.route("**/api/ai/providers", fail_provider_list)
    setup_page.route("**/api/setup-and-scan/current", respond_running)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_running)
    setup_page.reload()
    setup_page.get_by_role("button", name="Scan already running").wait_for()

    setup_page.locator("[data-open-ai-config]").click()
    setup_page.locator("#ai-config-modal").wait_for(state="visible")
    setup_page.locator("#ai-runtime").select_option("claude-code")
    setup_page.locator("#ai-config-modal [data-bs-dismiss='modal']").last.click()
    setup_page.locator("#ai-config-modal").wait_for(state="hidden")
    setup_page.locator('[data-nav-step="run"]').click()

    assert setup_page.locator('[data-run-item="review"] strong').text_content() == (
        "DeepSeek API review"
    )


def test_completed_run_open_review_desk_shows_fresh_review(setup_page: object) -> None:
    complete = {
        "run_id": "web-run-1",
        "status": "complete",
        "stage": "publish",
        "message": "Review queue published.",
        "result": {
            "summary": {
                "occurrence_count": 42,
                "reviewed_count": 8,
                "eligible_count": 5,
                "source_error_count": 0,
            },
            "schedule": {"installed": False, "local_time": None},
        },
        "error": None,
    }

    def respond_run(route: object) -> None:
        if route.request.method == "POST":
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "run_id": "web-run-1",
                        "status": "running",
                        "stage": "profile",
                        "message": "Building candidate profile...",
                        "result": None,
                        "error": None,
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(complete),
        )

    setup_page.route("**/api/setup-and-scan", respond_run)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_run)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )
    setup_page.get_by_role("button", name="Save and run scan").click()
    review_link = setup_page.get_by_role("link", name="Open review desk")
    review_link.wait_for(state="visible")

    review_link.click()

    setup_page.locator("#review-view").wait_for(state="visible", timeout=2_000)
    assert setup_page.url == "http://draft.test/setup#review"


def test_run_page_renders_review_batch_percent_and_counts(setup_page: object) -> None:
    review_state = {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "review",
        "message": "Reviewing complete job descriptions: 1/2 batches, 10/20 jobs...",
        "progress_percent": 85,
        "review_progress": {
            "completed_batches": 1,
            "total_batches": 2,
            "completed_jobs": 10,
            "total_jobs": 20,
        },
        "result": None,
        "error": None,
    }

    def respond_run(route: object) -> None:
        if route.request.method == "POST":
            body = {
                "run_id": "web-run-1",
                "status": "running",
                "stage": "profile",
                "message": "Building candidate profile...",
                "progress_percent": 10,
                "review_progress": None,
                "result": None,
                "error": None,
            }
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(body),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(review_state),
        )

    setup_page.route("**/api/setup-and-scan", respond_run)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_run)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )

    setup_page.get_by_role("button", name="Save and run scan").click()

    setup_page.locator("#run-percent", has_text="85%").wait_for(timeout=2_000)
    assert setup_page.locator("#run-message").text_content() == review_state["message"]
    assert setup_page.locator('[data-run-item="review"] small').text_content() == (
        "1/2 batches · 10/20 jobs"
    )


def test_run_page_renders_source_percent_and_counts(setup_page: object) -> None:
    source_state = {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "sources",
        "message": "Searching job sources: 2/4 sources, 17 jobs found, 1 warning...",
        "progress_percent": 55,
        "source_progress": {
            "completed_sources": 2,
            "total_sources": 4,
            "found_jobs": 17,
            "warning_count": 1,
        },
        "result": None,
        "error": None,
    }

    def respond_run(route: object) -> None:
        if route.request.method == "POST":
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "run_id": "web-run-1",
                        "status": "running",
                        "stage": "profile",
                        "message": "Building candidate profile...",
                        "progress_percent": 10,
                        "result": None,
                        "error": None,
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(source_state),
        )

    setup_page.route("**/api/setup-and-scan", respond_run)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_run)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )

    setup_page.get_by_role("button", name="Save and run scan").click()

    setup_page.locator("#run-percent", has_text="55%").wait_for(timeout=2_000)
    assert setup_page.locator("#run-message").text_content() == source_state["message"]
    assert setup_page.locator('[data-run-item="sources"] small').text_content() == (
        "2/4 sources · 17 jobs found · 1 warning"
    )


def test_run_page_renders_company_size_percent_and_counts(setup_page: object) -> None:
    company_size_state = {
        "run_id": "web-run-1",
        "status": "running",
        "stage": "company_size",
        "message": "Checking company sizes: 2/4 companies...",
        "progress_percent": 97,
        "review_progress": None,
        "company_size_progress": {
            "completed_companies": 2,
            "total_companies": 4,
        },
        "result": None,
        "error": None,
    }

    def respond_run(route: object) -> None:
        if route.request.method == "POST":
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "run_id": "web-run-1",
                        "status": "running",
                        "stage": "profile",
                        "message": "Building candidate profile...",
                        "progress_percent": 10,
                        "review_progress": None,
                        "company_size_progress": None,
                        "result": None,
                        "error": None,
                    }
                ),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(company_size_state),
        )

    setup_page.route("**/api/setup-and-scan", respond_run)
    setup_page.route("**/api/setup-and-scan/web-run-1", respond_run)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )

    setup_page.get_by_role("button", name="Save and run scan").click()

    setup_page.locator("#run-percent", has_text="97%").wait_for(timeout=2_000)
    assert setup_page.locator("#run-message").text_content() == company_size_state[
        "message"
    ]
    assert setup_page.locator('[data-run-item="company_size"] small').text_content() == (
        "2/4 companies"
    )


def test_run_page_reports_service_disconnect_instead_of_running_forever(
    setup_page: object,
) -> None:
    def disconnect(route: object) -> None:
        if route.request.method == "POST":
            body = {
                "run_id": "web-run-1",
                "status": "running",
                "stage": "profile",
                "message": "Building candidate profile...",
                "result": None,
                "error": None,
            }
            route.fulfill(status=202, content_type="application/json", body=json.dumps(body))
            return
        route.abort("connectionfailed")

    setup_page.route("**/api/setup-and-scan", disconnect)
    setup_page.route("**/api/setup-and-scan/web-run-1", disconnect)
    setup_page.locator("#resume").set_input_files(str(RESUME))
    setup_page.evaluate(
        "document.querySelector('#search-terms').tomselect.setValue(['Backend Engineer'])"
    )

    setup_page.get_by_role("button", name="Save and run scan").click()

    setup_page.locator("#run-percent", has_text="Failed").wait_for()
    assert setup_page.locator("#run-message").text_content() == (
        "Connection to scan service lost. Restart the service and try again."
    )


def test_ats_start_polls_common_then_parallel_job_states(setup_page: object) -> None:
    states = [
        ats_state(
            status="running",
            stage="resume",
            tasks=[
                task("resume", "resume", "running"),
                task("linkedin-only", "job", "waiting"),
                task("stepstone-only", "job", "waiting"),
            ],
        ),
        ats_state(
            status="running",
            stage="jobs",
            progress_percent=70,
            tasks=[
                task("resume", "resume", "complete"),
                task("linkedin-only", "job", "running"),
                task("stepstone-only", "job", "running"),
            ],
        ),
        ats_state(
            status="complete",
            stage="archive",
            progress_percent=100,
            tasks=[
                task("resume", "resume", "complete"),
                task("linkedin-only", "job", "complete"),
                task("stepstone-only", "job", "complete"),
            ],
        ),
    ]
    posted: list[dict[str, object]] = []
    setup_page.route(
        "**/api/ats-runs",
        lambda route: respond_ats_start(route, posted),
    )
    setup_page.route(
        "**/api/ats-runs/ats-1",
        lambda route: respond_next(route, states),
    )

    open_ats_jobs_in_tracker(setup_page)
    start = setup_page.locator("[data-open-ats]")
    assert start.is_disabled()
    setup_page.locator(
        '[data-ats-select-job][value="linkedin-only"]'
    ).check()
    setup_page.locator(
        '[data-ats-select-job][value="stepstone-only"]'
    ).check()
    assert start.text_content() == "Check 2 selected jobs"
    assert "linkedin-only" not in (
        setup_page.evaluate(
            "window.localStorage.getItem('job-scan.setup-draft.v1')"
        )
        or ""
    )
    start.click()

    setup_page.locator(
        '[data-ats-task="linkedin-only"][data-state="active"]'
    ).wait_for(timeout=3_000)
    assert setup_page.locator("#ats-run-progress").get_attribute(
        "aria-valuenow"
    ) == "70"
    setup_page.locator(
        '#ats-results-link[href="/setup?ats_run_id=ats-1#ats-check"]'
    ).wait_for(timeout=3_000)

    assert posted == [
        {
            "search_run_id": "search-1",
            "job_keys": ["linkedin-only", "stepstone-only"],
        }
    ]
    assert setup_page.locator(
        '[data-ats-task="resume"]'
    ).get_attribute("data-state") == "complete"
    assert setup_page.locator(
        '[data-ats-task="linkedin-only"]'
    ).get_attribute("data-state") == "complete"
    assert setup_page.locator(
        '[data-ats-task="stepstone-only"]'
    ).get_attribute("data-state") == "complete"
    assert setup_page.locator("#ats-run-percent").text_content() == "100%"
    assert setup_page.locator("#ats-run-badge").text_content() == "Complete"
    assert setup_page.locator("[data-ats-task-list] img").count() == 0
    assert states == []


def test_ats_job_checkbox_uses_a_visible_unchecked_border(setup_page: object) -> None:
    open_ats_jobs_in_tracker(setup_page)
    job_checkbox = setup_page.locator(
        '[data-review-block="global"] #saved [data-ats-select-job]'
    ).first

    assert job_checkbox.evaluate(
        "control => getComputedStyle(control).borderTopColor"
    ) == "rgb(81, 97, 90)"


def test_review_cards_have_a_framed_non_collapsible_area(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    current_review = setup_page.locator('[data-review-block="current"]')

    assert current_review.locator("[data-collapse-review-groups]").count() == 0
    assert current_review.locator(".review-groups > .job-group > summary").count() == 0
    assert current_review.locator(".review-groups > details.job-group").count() == 0
    assert current_review.locator(".review-groups").evaluate(
        "node => getComputedStyle(node).borderTopWidth"
    ) == "1px"


def test_review_jobs_scroll_without_moving_the_page(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    setup_page.locator("#review-language-requirement").select_option("")
    groups = setup_page.locator('[data-review-block="current"] .review-groups')
    groups.evaluate("node => node.scrollIntoView({block: 'start'})")
    setup_page.wait_for_timeout(100)
    groups.hover()
    setup_page.wait_for_timeout(100)
    page_scroll_before = setup_page.evaluate("window.scrollY")

    setup_page.mouse.wheel(0, 900)
    setup_page.wait_for_timeout(100)

    assert groups.evaluate("node => node.scrollTop") > 0
    assert setup_page.evaluate("window.scrollY") == page_scroll_before


def test_review_group_tabs_switch_the_visible_panel(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    current_review = setup_page.locator('[data-review-block="current"]')
    tabs = current_review.locator("[data-review-group-tab]")
    assert tabs.count() == 3
    assert (
        current_review.locator(".review-groups > section.job-group:visible").count()
        == 1
    )
    assert current_review.locator("#recommended:not([hidden])").count() == 1
    assert (
        current_review.locator(
            '[data-review-group-tab="recommended"][aria-current="page"]'
        ).count()
        == 1
    )

    current_review.locator('[data-review-group-tab="excluded"]').click()

    assert current_review.locator("#recommended").is_hidden()
    assert current_review.locator("#excluded").is_visible()
    assert current_review.locator(
        '[data-review-group-tab="excluded"][aria-current="page"]'
    ).count() == 1
    assert setup_page.evaluate("window.location.hash") == "#excluded"


def test_review_group_drag_reorders_navigation_and_panels(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    current_review = setup_page.locator('[data-review-block="current"]')
    assert current_review.locator("[data-review-group-drag-handle]").count() == 3

    target_tab = current_review.locator('[data-review-group-tab="recommended"]')
    target_bounds = target_tab.bounding_box()
    assert target_bounds is not None
    source_tab = current_review.locator('[data-review-group-tab="pending"]')
    assert source_tab.get_attribute("draggable") == "true"
    source_tab.drag_to(
        target_tab,
        target_position={"x": 12, "y": 1},
    )

    assert current_review.locator("[data-review-group-tab]").evaluate_all(
        "items => items.map(item => item.dataset.reviewGroupTab)"
    )[:3] == [
        "pending",
        "recommended",
        "excluded",
    ]
    assert current_review.locator(
        ".review-groups > section.job-group"
    ).evaluate_all("items => items.map(item => item.id)")[:3] == [
        "pending",
        "recommended",
        "excluded",
    ]


def test_review_group_keyboard_reorders_navigation_and_panels(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    global_review = setup_page.locator('[data-review-block="global"]')
    applied = global_review.locator('[data-review-group-tab="applied"]')
    applied.focus()

    setup_page.keyboard.press("Alt+ArrowUp")

    assert global_review.locator("[data-review-group-tab]").evaluate_all(
        "items => items.map(item => item.dataset.reviewGroupTab)"
    )[:3] == ["applied", "saved", "interviewing"]
    assert global_review.locator(
        ".review-groups > section.job-group"
    ).evaluate_all("items => items.map(item => item.id)")[:3] == [
        "applied",
        "saved",
        "interviewing",
    ]
    assert "position 1 of 7" in global_review.locator(
        ".review-group-announcement"
    ).text_content()


def test_review_group_touch_drag_reorders_navigation_and_panels(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    current_review = setup_page.locator('[data-review-block="current"]')
    source = current_review.locator(
        '[data-review-group-tab="pending"] [data-review-group-drag-handle]'
    )
    target = current_review.locator('[data-review-group-tab="recommended"]')
    source_bounds = source.bounding_box()
    target_bounds = target.bounding_box()
    assert source_bounds is not None
    assert target_bounds is not None
    pointer = {
        "pointerId": 7,
        "pointerType": "touch",
        "isPrimary": True,
        "bubbles": True,
        "cancelable": True,
        "buttons": 1,
        "clientX": source_bounds["x"] + source_bounds["width"] / 2,
        "clientY": source_bounds["y"] + source_bounds["height"] / 2,
    }
    source.dispatch_event("pointerdown", pointer)
    target.dispatch_event(
        "pointermove",
        {
            "pointerId": 7,
            "pointerType": "touch",
            "isPrimary": True,
            "bubbles": True,
            "cancelable": True,
            "buttons": 1,
            "clientX": target_bounds["x"] + 12,
            "clientY": target_bounds["y"] + 1,
        },
    )
    setup_page.evaluate(
        """window.dispatchEvent(new PointerEvent('pointerup', {
          pointerId: 7,
          pointerType: 'touch',
          isPrimary: true,
          bubbles: true,
        }))"""
    )

    assert current_review.locator("[data-review-group-tab]").evaluate_all(
        "items => items.map(item => item.dataset.reviewGroupTab)"
    ) == ["pending", "recommended", "excluded"]
    assert current_review.locator(
        ".review-groups > section.job-group"
    ).evaluate_all("items => items.map(item => item.id)") == [
        "pending",
        "recommended",
        "excluded",
    ]


def test_review_group_selection_shows_the_selected_panel(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    excluded = setup_page.locator("#excluded")

    assert excluded.is_hidden()
    setup_page.locator('[data-review-group-tab="excluded"]').click()

    assert excluded.is_visible()
    assert excluded.locator("summary").count() == 0


def test_review_group_order_restores_after_reload(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    current_review = setup_page.locator('[data-review-block="current"]')
    assert current_review.locator("[data-review-group-drag-handle]").count() == 3
    current_review.locator('[data-review-group-tab="pending"]').drag_to(
        current_review.locator('[data-review-group-tab="recommended"]'),
        target_position={"x": 12, "y": 1},
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    current_review = setup_page.locator('[data-review-block="current"]')
    assert current_review.locator(
        ".review-groups > section.job-group"
    ).evaluate_all(
        "items => items.map(item => item.id)"
    ) == [
        "pending",
        "recommended",
        "excluded",
    ]
    assert current_review.locator("[data-review-group-tab]").evaluate_all(
        "items => items.map(item => item.dataset.reviewGroupTab)"
    ) == [
        "pending",
        "recommended",
        "excluded",
    ]


def test_review_group_order_ignores_unknown_and_appends_missing_groups(
    setup_page: object,
) -> None:
    setup_page.evaluate(
        "localStorage.setItem('job-scan.review-group-order.v1', "
        "JSON.stringify(['pending', 'unknown', 'pending']))"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    current_review = setup_page.locator('[data-review-block="current"]')
    assert current_review.locator(
        ".review-groups > section.job-group"
    ).evaluate_all("items => items.map(item => item.id)") == [
        "pending",
        "recommended",
        "excluded",
    ]


def test_global_review_group_order_migrates_legacy_shortlisted_id(
    setup_page: object,
) -> None:
    setup_page.evaluate(
        "localStorage.setItem('job-scan.global-review-group-order.v1', "
        "JSON.stringify(['shortlisted', 'applied', 'rejected', 'ignored']))"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    group_ids = setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab]'
    ).evaluate_all("items => items.map(item => item.dataset.reviewGroupTab)")
    stored_order = setup_page.evaluate(
        "JSON.parse(localStorage.getItem('job-scan.global-review-group-order.v1'))"
    )
    assert group_ids[:2] == ["saved", "applied"]
    assert "shortlisted" not in group_ids
    assert set(group_ids) == {
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    }
    assert stored_order == group_ids


def test_legacy_group_order_survives_storage_migration_write_failure(
    setup_page: object,
) -> None:
    setup_page.evaluate(
        "localStorage.setItem('job-scan.global-review-group-order.v1', "
        "JSON.stringify(['applied', 'shortlisted']))"
    )
    setup_page.add_init_script(
        "Storage.prototype.setItem = () => { throw new Error('storage disabled'); };"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    group_ids = setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab]'
    ).evaluate_all("items => items.map(item => item.dataset.reviewGroupTab)")
    assert group_ids[:2] == ["applied", "saved"]


def test_review_group_order_discards_saved_history_entry(setup_page: object) -> None:
    setup_page.evaluate(
        "localStorage.setItem('job-scan.review-group-order.v1', "
        "JSON.stringify(['history', 'pending']))"
    )

    setup_page.reload()
    setup_page.wait_for_load_state("networkidle")

    current_review = setup_page.locator('[data-review-block="current"]')
    group_ids = current_review.locator("[data-review-group-tab]").evaluate_all(
        "items => items.map(item => item.dataset.reviewGroupTab)"
    )
    assert "history" not in group_ids
    assert group_ids[0] == "pending"
    assert current_review.locator("#history").count() == 0


def test_review_group_deep_links_select_the_matching_panel(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup#pending")
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#pending").is_visible()
    assert setup_page.locator('[data-review-group-tab="pending"][aria-current="page"]').count() == 1

    setup_page.goto("http://draft.test/setup#history-stale")
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#recommended").is_visible()
    assert setup_page.locator(
        '[data-review-group-tab="recommended"][aria-current="page"]'
    ).count() == 1


def test_ats_failed_state_maps_error_and_skipped_without_polling_again(
    setup_page: object,
) -> None:
    terminal = ats_state(
        status="failed",
        stage="resume",
        progress_percent=20,
        error="Resume <b>could not be read</b>.",
        tasks=[
            task("resume", "resume", "failed"),
            task("linkedin-only", "job", "skipped"),
            task("stepstone-only", "job", "skipped"),
        ],
    )
    posted: list[dict[str, object]] = []
    polls: list[str] = []
    setup_page.route(
        "**/api/ats-runs",
        lambda route: respond_ats_start(route, posted),
    )

    def respond_failed(route: object) -> None:
        polls.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(terminal),
        )

    setup_page.route("**/api/ats-runs/ats-1", respond_failed)
    open_ats_jobs_in_tracker(setup_page)
    setup_page.locator(
        '[data-ats-select-job][value="linkedin-only"]'
    ).check()
    setup_page.locator(
        '[data-ats-select-job][value="stepstone-only"]'
    ).check()
    setup_page.locator("[data-open-ats]").click()

    assert setup_page.locator(
        '[data-ats-task="resume"]'
    ).get_attribute("data-state") == "active"
    setup_page.locator(
        '[data-ats-task="resume"][data-state="error"]'
    ).wait_for(timeout=2_000)
    assert setup_page.locator(
        '[data-ats-task="linkedin-only"]'
    ).get_attribute("data-state") == "skipped"
    assert setup_page.locator("#ats-run-message").text_content() == (
        "Resume <b>could not be read</b>."
    )
    assert setup_page.locator("#ats-run-message b").count() == 0
    assert setup_page.locator("#ats-results-link").is_hidden()
    setup_page.wait_for_timeout(700)
    assert polls == ["GET"]


def test_ats_start_failure_keeps_selection_for_retry(setup_page: object) -> None:
    requests: list[dict[str, object]] = []

    def respond_start(route: object) -> None:
        requests.append(ats_form_data(route.request.post_data or ""))
        if len(requests) == 1:
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"detail": "Selected job is no longer recommended."}),
            )
            return
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                ats_state(
                    status="complete",
                    stage="archive",
                    progress_percent=100,
                    tasks=[
                        task("resume", "resume", "complete"),
                        task("linkedin-only", "job", "complete"),
                    ],
                )
            ),
        )

    setup_page.route("**/api/ats-runs", respond_start)
    setup_page.on("dialog", lambda dialog: dialog.accept())
    open_ats_jobs_in_tracker(setup_page)
    selected = setup_page.locator(
        '[data-ats-select-job][value="linkedin-only"]'
    )
    selected.check()
    start = setup_page.locator("[data-open-ats]")

    start.click()
    setup_page.wait_for_timeout(100)
    assert selected.is_checked()
    assert start.is_enabled()
    assert start.text_content() == "Check 1 selected jobs"

    start.click()
    setup_page.locator("#ats-results-link").wait_for(state="visible")
    assert len(requests) == 2


def test_ats_result_switches_jobs_and_deletes_selected_history(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?ats_run_id=ats-1#ats-check")
    setup_page.locator('[data-ats-job="job-2"]').click()

    assert setup_page.locator('[data-ats-job="job-1"]').get_attribute(
        "aria-pressed"
    ) == "false"
    assert setup_page.locator('[data-ats-job="job-2"]').get_attribute(
        "aria-pressed"
    ) == "true"
    assert setup_page.locator('[data-ats-report="job-1"]').is_hidden()
    assert setup_page.locator('[data-ats-report="job-2"]').is_visible()

    setup_page.route(
        "**/api/ats-history/ats-1",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"deleted":true}',
        ),
    )
    setup_page.on("dialog", lambda dialog: dialog.accept())
    setup_page.locator("#ats-history summary").click()
    setup_page.locator(
        '[data-ats-history-id="ats-1"] [data-ats-history-delete]'
    ).click()
    setup_page.wait_for_url("**/setup#ats-check")


def test_ats_delete_nonselected_history_removes_only_that_row(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup#ats-check")
    setup_page.route(
        "**/api/ats-history/ats-2",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"deleted":true}',
        ),
    )
    setup_page.on("dialog", lambda dialog: dialog.accept())
    setup_page.locator("#ats-history summary").click()

    setup_page.locator(
        '[data-ats-history-id="ats-2"] [data-ats-history-delete]'
    ).click()

    setup_page.locator('[data-ats-history-id="ats-2"]').wait_for(state="detached")
    assert setup_page.url == "http://draft.test/setup#ats-check"
    assert setup_page.locator('[data-ats-history-id="ats-1"]').is_visible()


def test_ats_run_nav_shows_idle_and_ats_check_nav_uses_saved_results(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="ats-run"]').click()

    assert setup_page.url == "http://draft.test/setup#ats-run"
    assert setup_page.locator("#ats-running").is_visible()
    assert setup_page.locator("#ats-run-badge").text_content() == "Idle"
    assert setup_page.locator("#ats-run-percent").text_content() == "Idle"
    assert setup_page.locator("#ats-run-message").text_content() == (
        "No ATS check is running. Start one from Job Tracker."
    )

    setup_page.locator('[data-nav-step="ats"]').click()

    assert setup_page.url == "http://draft.test/setup#ats-check"
    assert setup_page.locator("#ats-check").is_visible()


@pytest.mark.parametrize("has_saved_result", [False, True], ids=["no-saved", "saved"])
def test_ats_top_nav_opens_just_completed_result(
    setup_page: object,
    has_saved_result: bool,
) -> None:
    setup_page.route(
        "**/setup*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=GLOBAL_ATS_SNAPSHOT,
                ai_providers=AI_PROVIDERS,
                ats_history=ATS_HISTORY if has_saved_result else [],
                selected_ats=SELECTED_ATS if has_saved_result else None,
                ats_source_run_id="search-1",
            ),
        ),
    )
    setup_page.goto("http://draft.test/setup?fresh=1#job-tracker")

    completed = ats_state(
        status="complete",
        stage="archive",
        progress_percent=100,
        tasks=[
            task("resume", "resume", "complete"),
            task("linkedin-only", "job", "complete"),
        ],
    )
    completed["run_id"] = "ats/new result"
    setup_page.route(
        "**/api/ats-runs",
        lambda route: route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(completed),
        ),
    )
    setup_page.locator(
        '[data-ats-select-job][value="linkedin-only"]'
    ).check()
    setup_page.locator("[data-open-ats]").click()
    setup_page.locator(
        '#ats-results-link[href="/setup?ats_run_id=ats%2Fnew%20result#ats-check"]'
    ).wait_for()

    setup_page.locator('[data-nav-step="ats"]').click()

    setup_page.wait_for_url(
        "**/setup?ats_run_id=ats%2Fnew%20result#ats-check",
        timeout=2_000,
    )


def test_ats_check_nav_without_saved_result_opens_empty_results(
    setup_page: object,
) -> None:
    setup_page.route(
        "**/setup*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                ai_providers=AI_PROVIDERS,
                ats_source_run_id="search-1",
            ),
        ),
    )
    setup_page.goto("http://draft.test/setup?empty=1#ats-run")
    assert setup_page.locator("#ats-running").is_visible()

    setup_page.locator('[data-nav-step="ats"]').click()

    assert setup_page.url == "http://draft.test/setup?empty=1#ats-check"
    assert setup_page.locator("#ats-check").is_visible()
    assert setup_page.locator("#ats-result-scope").text_content() == "No saved checks"


def test_ats_run_restores_current_progress_after_page_load(setup_page: object) -> None:
    current = ats_state(
        status="running",
        stage="jobs",
        progress_percent=50,
        tasks=[
            task("resume", "resume", "complete"),
            task("linkedin-only", "job", "running"),
        ],
    )
    setup_page.route(
        "**/api/ats-runs/current",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(current),
        ),
    )
    setup_page.route(
        "**/api/ats-runs/ats-1",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(current),
        ),
    )

    setup_page.goto("http://draft.test/setup?current=1#ats-run")

    setup_page.locator('[data-ats-task="linkedin-only"][data-state="active"]').wait_for()
    assert setup_page.locator("#ats-running").is_visible()
    assert setup_page.locator("#ats-run-badge").text_content() == "Running"
    assert setup_page.locator("#ats-run-percent").text_content() == "50%"


def test_delayed_idle_lookup_cannot_overwrite_a_new_ats_run(setup_page: object) -> None:
    pending_current: list[object] = []
    running = ats_state(
        status="running",
        stage="resume",
        progress_percent=10,
        tasks=[task("resume", "resume", "running")],
    )
    setup_page.route(
        "**/api/ats-runs",
        lambda route: route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(running),
        ),
    )
    setup_page.route(
        "**/api/ats-runs/ats-1",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(running),
        ),
    )
    setup_page.route(
        "**/api/ats-runs/current",
        lambda route: pending_current.append(route),
    )
    setup_page.goto(
        "http://draft.test/setup?ats-jobs=1&delayed-current=1#job-tracker"
    )
    setup_page.locator(
        '[data-ats-select-job][value="linkedin-only"]'
    ).check()

    setup_page.locator("[data-open-ats]").click()
    setup_page.locator('[data-ats-task="resume"][data-state="active"]').wait_for()
    assert len(pending_current) == 1
    pending_current[0].fulfill(status=204, body="")
    setup_page.wait_for_timeout(100)

    assert setup_page.locator("#ats-run-badge").text_content() == "Running"
    assert setup_page.locator("#ats-run-percent").text_content() == "10%"
    assert setup_page.locator('[data-ats-task="resume"]').count() == 1


def test_reopening_active_ats_run_does_not_duplicate_polling(setup_page: object) -> None:
    running = ats_state(
        status="running",
        stage="jobs",
        progress_percent=50,
        tasks=[
            task("resume", "resume", "complete"),
            task("linkedin-only", "job", "running"),
        ],
    )
    current_requests: list[str] = []
    poll_requests: list[str] = []

    def respond_current(route: object) -> None:
        current_requests.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(running),
        )

    def respond_poll(route: object) -> None:
        poll_requests.append(route.request.method)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(running),
        )

    setup_page.route("**/api/ats-runs/current", respond_current)
    setup_page.route("**/api/ats-runs/ats-1", respond_poll)
    setup_page.goto("http://draft.test/setup?single-poll=1#ats-run")
    setup_page.locator('[data-ats-task="linkedin-only"][data-state="active"]').wait_for()

    setup_page.locator('[data-nav-step="ats-run"]').click()
    setup_page.locator('[data-nav-step="ats-run"]').click()
    setup_page.wait_for_timeout(650)

    assert current_requests == ["GET"]
    assert poll_requests == ["GET"]
