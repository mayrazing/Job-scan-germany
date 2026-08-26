from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

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
    JobNote,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
    UserStatusHistoryEntry,
)

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
        resume_id="sha256:" + "a" * 64,
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
        resume_id="sha256:" + "a" * 64,
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
        "result_ids": ["ats-1"] if status == "complete" else [],
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

ATS_RESUME_ID = "sha256:" + "a" * 64

GLOBAL_STATUS_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=44),
    jobs=[
        source_job(
            "global-saved",
            (SourceKind.LINKEDIN,),
            score=85,
            german_requirement="none",
        ).model_copy(
            update={
                "user_status": UserStatus.SAVED,
                "application_resume_id": ATS_RESUME_ID,
                "application_resume_filename": "Ada CV.pdf",
            }
        )
    ],
)

GLOBAL_LIFECYCLE_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=45),
    jobs=[
        source_job(
            "global-interviewing",
            (SourceKind.LINKEDIN,),
        ).model_copy(
            update={
                "user_status": UserStatus.INTERVIEWING,
                "user_status_updated_at": datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
                "user_status_history": [
                    UserStatusHistoryEntry(
                        status=UserStatus.SAVED,
                        changed_at=datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
                    ),
                    UserStatusHistoryEntry(
                        status=UserStatus.APPLIED,
                        changed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
                    ),
                    UserStatusHistoryEntry(
                        status=UserStatus.INTERVIEWING,
                        changed_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
                    ),
                ],
            }
        )
    ],
)

GLOBAL_MANUAL_FACTS_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=46),
    jobs=[
        source_job(
            "global-manual",
            (SourceKind.LINKEDIN,),
            score=85,
            german_requirement="none",
        ).model_copy(
            update={
                "posted_at": None,
                "user_status": UserStatus.SAVED,
                "application_resume_id": ATS_RESUME_ID,
                "application_resume_filename": "Ada CV.pdf",
            }
        )
    ],
)

GLOBAL_ATS_SNAPSHOT = Snapshot(
    meta=StoreMeta(data_revision=45),
    jobs=[
        job.model_copy(
            update={
                "user_status": UserStatus.SAVED,
                "application_resume_id": ATS_RESUME_ID,
                "application_resume_filename": "Ada CV.pdf",
            }
        )
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
            "codex": {
                "model": "gpt-5.6-sol",
                "effort": "high",
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
                            "codex": update["codex"],
                        }
                    )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(ai_selection),
                )
                return
            if request.url.endswith("/api/ai/codex-models"):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        [
                            {
                                "id": "gpt-5.6-sol",
                                "name": "GPT-5.6-Sol",
                                "default_reasoning_effort": "low",
                                "supported_reasoning_efforts": [
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                    "max",
                                    "ultra",
                                ],
                            },
                            {
                                "id": "gpt-5.6-luna",
                                "name": "GPT-5.6-Luna",
                                "default_reasoning_effort": "medium",
                                "supported_reasoning_efforts": [
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                    "max",
                                ],
                            },
                        ]
                    ),
                )
                return
            if request.url.endswith("/api/ai/codex-auth"):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "authenticated": True,
                            "login": {
                                "state": "idle",
                                "verification_url": None,
                                "user_code": None,
                                "error": None,
                            },
                        }
                    ),
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
                        GLOBAL_LIFECYCLE_SNAPSHOT
                        if "lifecycle=1" in request.url
                        else GLOBAL_MANUAL_FACTS_SNAPSHOT
                        if "manual-facts=1" in request.url
                        else GLOBAL_STATUS_SNAPSHOT
                        if "global-status=1" in request.url
                        else GLOBAL_ATS_SNAPSHOT
                        if "ats-jobs=1" in request.url
                        else None
                    ),
                    ai_providers=AI_PROVIDERS,
                    ats_history=ATS_HISTORY,
                    selected_ats=SELECTED_ATS,
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


def open_job_details(card: object) -> object:
    card.locator("[data-job-preview-open-area]").click()
    dialog = card.locator("[data-job-detail-dialog]")
    dialog.wait_for(state="visible")
    return dialog


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
    ) == ["claude-code", "codex-cli", "api:deepseek", "api:open-router"]
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
            "codex": {
                "model": "gpt-5.6-sol",
                "effort": "high",
            },
        }
    ]


def test_ai_configuration_loads_codex_models_and_supported_efforts(
    setup_page: object,
) -> None:
    setup_page.locator("[data-open-ai-config]").click()
    setup_page.locator("#ai-runtime").select_option("codex-cli")
    setup_page.locator("#codex-model-feedback").get_by_text(
        "2 Codex CLI models available."
    ).wait_for()

    assert setup_page.evaluate(
        "Object.keys(document.querySelector('#codex-model').tomselect.options)"
    ) == ["gpt-5.6-sol", "gpt-5.6-luna"]

    setup_page.locator("#codex-effort").select_option("ultra")
    setup_page.evaluate(
        "document.querySelector('#codex-model').tomselect.setValue('gpt-5.6-luna')"
    )

    assert setup_page.locator("#codex-effort option").evaluate_all(
        "options => options.map(option => option.value)"
    ) == ["low", "medium", "high", "xhigh", "max"]
    assert setup_page.locator("#codex-effort").input_value() == "medium"


def test_selecting_logged_out_codex_opens_device_login_dialog(
    setup_page: object,
) -> None:
    login = {
        "state": "idle",
        "verification_url": None,
        "user_code": None,
        "error": None,
    }
    authenticated = False

    def auth_status(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"authenticated": authenticated, "login": login}
            ),
        )

    def start_login(route: object) -> None:
        login.update(
            {
                "state": "pending",
                "verification_url": "https://auth.openai.com/codex/device",
                "user_code": "TEST-9YWCE",
                "error": None,
            }
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(login))

    setup_page.route("**/api/ai/codex-auth", auth_status)
    setup_page.route("**/api/ai/codex-login", start_login)
    setup_page.locator("[data-open-ai-config]").click()
    setup_page.locator("#ai-runtime").select_option("codex-cli")

    dialog = setup_page.locator("#codex-login-dialog")
    dialog.wait_for(state="visible")
    assert dialog.locator("[data-codex-login-code]").text_content() == "TEST-9YWCE"
    assert dialog.locator("[data-open-codex-login]").get_attribute("data-url") == (
        "https://auth.openai.com/codex/device"
    )

    authenticated = True
    login.update(
        {
            "state": "succeeded",
            "verification_url": None,
            "user_code": None,
        }
    )
    dialog.wait_for(state="hidden")
    setup_page.locator("#codex-model-feedback").get_by_text(
        "2 Codex CLI models available."
    ).wait_for()


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
        "#new-run-button, [data-open-ats]"
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
    for edge in ("top", "bottom", "height"):
        assert max(item[edge] for item in rectangles) == pytest.approx(
            min(item[edge] for item in rectangles), abs=0.1
        )


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
    assert setup_page.locator("#ats-resume").count() == 0
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
    assert setup_page.locator("#ats-resume").count() == 0
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


def test_job_tracker_source_filter_filters_only_global_jobs(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")

    assert setup_page.locator("#global-source-filter").count() == 1
    assert setup_page.evaluate(
        "document.querySelector('#global-source-filter').tomselect.items"
    ) == ["linkedin"]

    global_card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"]'
    )
    global_count = setup_page.locator(
        '[data-review-block="global"] [data-review-group-count="saved"]'
    )
    assert global_card.is_visible()
    assert global_count.text_content() == "1"

    setup_page.evaluate(
        "document.querySelector('#global-source-filter').tomselect.clear()"
    )

    assert global_card.is_hidden()
    assert global_count.text_content() == "0"
    assert setup_page.locator(
        '[data-review-block="current"] .review-groups [data-sources]'
    ).count() > 0

    setup_page.evaluate(
        "document.querySelector('#global-source-filter').tomselect.setValue(['linkedin'])"
    )
    assert global_card.is_visible()


def test_job_tracker_url_filter_matches_normalized_and_partial_urls(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    global_card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"]'
    )
    url_filter = setup_page.locator("#global-url-filter")

    url_filter.fill(
        "HTTPS://JOBS.EXAMPLE/global-saved/?utm_source=tracker#application"
    )
    assert global_card.is_visible()

    url_filter.fill("global-saved")
    assert global_card.is_visible()

    url_filter.fill("different-job")
    assert global_card.is_hidden()

    url_filter.fill("")
    assert global_card.is_visible()


def test_job_tracker_selects_a_new_manual_source_after_cards_refresh(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")

    setup_page.evaluate(
        """() => {
          const original = document.querySelector(
            '[data-review-block="global"] article[data-job-key="global-saved"]',
          );
          const manual = original.cloneNode(true);
          manual.dataset.jobKey = "manual-saved";
          manual.dataset.sources = "manual";
          manual.dataset.jobUrl = "https://careers.example/jobs/manual-saved";
          original.parentElement.append(manual);
          document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
        }"""
    )

    assert setup_page.evaluate(
        "document.querySelector('#global-source-filter').tomselect.items"
    ) == ["linkedin", "manual"]
    assert setup_page.locator('[data-job-key="manual-saved"]').is_visible()


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

    dialog = open_job_details(card)
    dialog.locator("[data-company-size-help]").click()

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

    dialog = open_job_details(card)
    dialog.locator("[data-company-size-search]").click()

    error = dialog.locator(".company-size-search-error")
    error.wait_for(state="visible")
    assert error.text_content() == "AI could not verify this company's employee count."
    assert error.is_visible()
    assert dialog.locator("[data-company-size-search]").is_enabled()
    assert dialog.locator("[data-company-size-search]").text_content() == "AI Search"
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

    open_job_details(global_card).locator("[data-company-size-search]").click()
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
    global_card = setup_page.locator(
        '[data-review-block="global"] '
        'article[data-job-key="global-saved"]'
    )
    delete_button = open_job_details(global_card).locator("[data-global-job-delete]")

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
        'article[data-job-key="global-saved"] .job-preview-status-form'
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


def test_lifecycle_date_change_checks_the_current_adjacent_nodes(
    setup_page: object,
) -> None:
    posted: list[dict[str, str]] = []
    alerts: list[str] = []

    def save_lifecycle_date(route: object) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(status=204, body="")

    setup_page.route(
        "**/api/global-jobs/global-interviewing/lifecycle/*/date",
        save_lifecycle_date,
    )
    setup_page.on(
        "dialog",
        lambda dialog: (alerts.append(dialog.message), dialog.accept()),
    )
    setup_page.goto("http://draft.test/setup?lifecycle=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="interviewing"]'
    ).click()
    card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-interviewing"]'
    )
    dialog = open_job_details(card)
    middle = dialog.locator(
        '[data-lifecycle-date-input][data-lifecycle-event-index="1"]'
    ).first

    middle.fill("2026-08-05")
    setup_page.wait_for_timeout(100)

    assert middle.input_value() == "2026-08-08"
    assert posted == []

    middle.fill("2026-08-11")
    setup_page.wait_for_timeout(100)

    assert middle.input_value() == "2026-08-08"
    assert posted == []

    middle.fill("2026-08-09")
    setup_page.wait_for_timeout(100)

    last = dialog.locator(
        '[data-lifecycle-date-input][data-lifecycle-event-index="2"]'
    ).first
    last.fill("2026-08-08")
    setup_page.wait_for_timeout(100)

    assert last.input_value() == "2026-08-10"
    assert posted == [{"changed_on": "2026-08-09"}]
    assert len(alerts) == 3


def test_unknown_facts_save_without_a_page_refresh(setup_page: object) -> None:
    posted: list[dict[str, object]] = []

    def save_fact(route: object) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(status=204, body="")

    setup_page.route(
        "**/api/global-jobs/global-manual/facts",
        save_fact,
    )
    setup_page.goto("http://draft.test/setup?manual-facts=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-manual"]'
    )
    detail = open_job_details(card)

    for field_name, value in (
        ("posted_at", "2026-08-12"),
        ("company_size", "4200"),
        ("company_industry", "Logistics"),
    ):
        detail.locator(f'[data-manual-fact-open="{field_name}"]').click()
        editor = detail.locator(
            f'[data-manual-fact-dialog][data-manual-fact-field="{field_name}"]'
        )
        editor.locator('input[name="value"]').fill(value)
        editor.locator("[data-manual-fact-save]").click()
        editor.wait_for(state="hidden")
        assert detail.is_visible()

    assert setup_page.url == "http://draft.test/setup?manual-facts=1#job-tracker"
    assert card.locator(".job-preview-posted").text_content() == (
        "Posted: 2026-08-12"
    )
    assert card.get_attribute("data-posted-at") == "2026-08-12"
    assert card.get_attribute("data-company-size-minimum") == "4200"
    assert card.get_attribute("data-company-size-maximum") == "4200"
    assert card.get_attribute("data-company-industry") == "Logistics"
    company_size = detail.locator('[data-manual-fact="company_size"]')
    assert company_size.locator("[data-manual-fact-value]").text_content() == (
        "4,200 employees"
    )
    assert company_size.locator("[data-manual-fact-provenance]").text_content() == (
        " · Manually added"
    )
    company_industry = detail.locator('[data-manual-fact="company_industry"]')
    assert company_industry.locator("[data-manual-fact-value]").text_content() == (
        "Logistics"
    )
    assert company_industry.locator(
        "[data-manual-fact-provenance]"
    ).text_content() == " · Manually added"
    assert posted == [
        {"posted_at": "2026-08-12"},
        {"company_size": 4200},
        {"company_industry": "Logistics"},
    ]


def test_lifecycle_date_opens_picker_on_first_click(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?lifecycle=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="interviewing"]'
    ).click()
    card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-interviewing"]'
    )
    dialog = open_job_details(card)
    date_input = dialog.locator("[data-lifecycle-date-input]").first
    input_box = date_input.bounding_box()

    assert input_box is not None
    setup_page.mouse.click(
        input_box["x"] + (input_box["width"] / 2),
        input_box["y"] + (input_box["height"] / 2),
    )
    setup_page.keyboard.press("Escape")

    assert dialog.is_visible()


def test_saved_lifecycle_date_updates_job_tracker_added_date(
    setup_page: object,
) -> None:
    posted: list[dict[str, str]] = []

    def save_lifecycle_date(route: object) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(status=204, body="")

    setup_page.route(
        "**/api/global-jobs/global-interviewing/lifecycle/0/date",
        save_lifecycle_date,
    )
    setup_page.goto("http://draft.test/setup?lifecycle=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="interviewing"]'
    ).click()
    card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-interviewing"]'
    )
    added = card.locator("[data-job-preview-added]")

    assert added.text_content() == "Added: 2026-08-06"

    dialog = open_job_details(card)
    dialog.locator(
        '[data-lifecycle-date-input][data-lifecycle-event-index="0"]'
    ).first.fill("2026-08-07")
    setup_page.wait_for_timeout(100)

    assert added.text_content() == "Added: 2026-08-07"
    assert posted == [{"changed_on": "2026-08-07"}]


def test_lifecycle_node_delete_requires_confirmation_and_preserves_saved(
    setup_page: object,
) -> None:
    deleted: list[str] = []
    alerts: list[str] = []
    lifecycle_deleted = False
    lifecycle_job = GLOBAL_LIFECYCLE_SNAPSHOT.jobs[0]
    refreshed_lifecycle = Snapshot(
        meta=StoreMeta(data_revision=46),
        jobs=[
            lifecycle_job.model_copy(
                update={
                    "user_status_history": [
                        lifecycle_job.user_status_history[0],
                        lifecycle_job.user_status_history[2],
                    ]
                }
            )
        ],
    )

    def serve_lifecycle_page(route: object) -> None:
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=(
                    refreshed_lifecycle
                    if lifecycle_deleted
                    else GLOBAL_LIFECYCLE_SNAPSHOT
                ),
                ai_providers=AI_PROVIDERS,
                ats_history=ATS_HISTORY,
                selected_ats=SELECTED_ATS,
            ),
        )

    def delete_lifecycle_node(route: object) -> None:
        nonlocal lifecycle_deleted
        deleted.append(route.request.url)
        lifecycle_deleted = True
        route.fulfill(status=204, body="")

    setup_page.route("**/setup?lifecycle=1", serve_lifecycle_page)
    setup_page.route(
        "**/api/global-jobs/global-interviewing/lifecycle/1",
        delete_lifecycle_node,
    )
    setup_page.on(
        "dialog",
        lambda dialog: (alerts.append(dialog.message), dialog.accept()),
    )
    setup_page.goto("http://draft.test/setup?lifecycle=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="interviewing"]'
    ).click()
    card = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-interviewing"]'
    )
    job_dialog = open_job_details(card)
    saved = job_dialog.locator(
        '[data-lifecycle-step][data-lifecycle-event-index="0"] strong'
    )
    applied = job_dialog.locator(
        '[data-lifecycle-step][data-lifecycle-event-index="1"] strong'
    )
    delete_dialog = card.locator("[data-lifecycle-delete-dialog]")

    saved.dblclick()

    assert alerts == [
        "Saved is the lifecycle starting point and cannot be deleted."
    ]
    assert not delete_dialog.is_visible()

    applied.dblclick()
    assert delete_dialog.is_visible()
    assert delete_dialog.locator("[data-lifecycle-delete-status]").text_content() == (
        "Applied"
    )
    delete_dialog.locator("[data-cancel-lifecycle-delete]").click()

    assert not delete_dialog.is_visible()
    assert deleted == []

    applied.dblclick()
    delete_dialog.locator("[data-confirm-lifecycle-delete]").click()
    job_dialog.locator(
        '[data-lifecycle-step][data-lifecycle-status="applied"]'
    ).wait_for(state="detached")

    assert deleted == [
        "http://draft.test/api/global-jobs/global-interviewing/lifecycle/1"
    ]
    assert job_dialog.is_visible()
    assert not delete_dialog.is_visible()
    assert job_dialog.locator("[data-lifecycle-step]").evaluate_all(
        "steps => steps.map(step => step.dataset.lifecycleStatus)"
    ) == ["saved", "interviewing"]

    job_dialog.locator(
        '[data-lifecycle-step][data-lifecycle-status="interviewing"] strong'
    ).dblclick()

    assert delete_dialog.is_visible()
    assert delete_dialog.locator("[data-confirm-lifecycle-delete]").is_enabled()


def test_job_tracker_card_drag_to_group_updates_status(
    setup_page: object,
) -> None:
    updated_global = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={"user_status": UserStatus.APPLIED}
            )
        ],
    )
    posted: list[dict[str, object]] = []

    def respond_after_card_drop(route: object) -> None:
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved/status"):
            posted.append(request.post_data_json)
            route.fulfill(status=204, body="")
            return
        if posted and request.method == "GET" and "/setup" in request.url:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=render_console(
                    SOURCE_FILTER_SNAPSHOT,
                    global_snapshot=updated_global,
                ),
            )
            return
        route.fallback()

    setup_page.route("**/*", respond_after_card_drop)
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    assert setup_page.locator(
        '[data-review-block="current"] [data-job-drag-source]'
    ).count() == 0
    global_review = setup_page.locator('[data-review-block="global"]')
    card = global_review.locator(
        '#saved article[data-job-key="global-saved"]'
    )
    target = global_review.locator('[data-review-group-tab="applied"]')

    assert card.get_attribute("draggable") == "true"
    card.drag_to(target)

    applied_card = global_review.locator(
        '#applied article[data-job-key="global-saved"]'
    )
    applied_card.wait_for(state="attached")
    assert posted == [{"status": "applied"}]
    assert global_review.locator(
        '[data-review-group-count="saved"]'
    ).text_content() == "0"
    assert global_review.locator(
        '[data-review-group-count="applied"]'
    ).text_content() == "1"
    assert global_review.locator(
        '[data-review-group-tab="saved"]'
    ).get_attribute("aria-current") == "page"

    target.click()
    applied_card.drag_to(target)
    setup_page.wait_for_timeout(100)
    assert posted == [{"status": "applied"}]


def test_global_status_request_does_not_replace_the_job_resume(
    setup_page: object,
) -> None:
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
            ),
        )

    setup_page.route("**/*", serve_resume_tracker)
    setup_page.goto("http://draft.test/setup?resume-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    form = setup_page.locator(
        '[data-review-block="global"] '
        '[data-job-key="global-saved"] .job-preview-status-form'
    )

    form.locator('select[name="status"]').select_option("applied")
    form.locator('button[type="submit"]').click()
    setup_page.wait_for_timeout(100)

    assert posted == [{"status": "applied"}]


def test_job_tracker_omits_saved_resume_picker(
    setup_page: object,
) -> None:
    resume_a = "sha256:" + "a" * 64
    tracked = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={
                    "user_status": UserStatus.APPLIED,
                    "application_resume_id": resume_a,
                    "application_resume_filename": "backend.pdf",
                }
            )
        ],
    )
    def serve_resume_picker_page(route: object) -> None:
        request = route.request
        if "resume-correction=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=tracked,
            ),
        )

    setup_page.route("**/*", serve_resume_picker_page)
    setup_page.goto("http://draft.test/setup?resume-correction=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator(
        '[data-review-block="global"] [data-review-group-tab="applied"]'
    ).click()
    card = setup_page.locator(
        '[data-review-block="global"] '
        '[data-job-key="global-saved"]'
    )
    detail = open_job_details(card)

    assert detail.locator('[data-job-action="application-resume"]').count() == 0
    assert detail.get_by_text("Use saved resume").count() == 0
    assert detail.get_by_text("Change resume").count() == 0


def test_job_tracker_resume_upload_posts_without_page_navigation(
    setup_page: object,
) -> None:
    resume_id = "sha256:" + "a" * 64
    tracked = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={
                    "application_resume_id": resume_id,
                    "application_resume_filename": "backend.pdf",
                }
            )
        ],
    )
    refreshed = Snapshot(
        meta=StoreMeta(data_revision=46),
        jobs=[
            tracked.jobs[0].model_copy(
                update={"application_resume_filename": "updated.pdf"}
            )
        ],
    )
    visible_snapshot = [tracked]
    posted: list[tuple[str, str]] = []

    def serve_resume_upload(route: object) -> None:
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved/resume"):
            posted.append(
                (
                    request.headers.get("content-type", ""),
                    request.post_data or "",
                )
            )
            visible_snapshot[0] = refreshed
            route.fulfill(status=204, body="")
            return
        if "resume-upload=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=visible_snapshot[0],
            ),
        )

    setup_page.route("**/*", serve_resume_upload)
    setup_page.goto("http://draft.test/setup?resume-upload=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    card = setup_page.locator(
        '[data-review-block="global"] [data-job-key="global-saved"]'
    )
    ats_checkbox = card.locator("[data-ats-select-job]")
    ats_checkbox.check()
    detail = open_job_details(card)
    form = detail.locator('[data-job-action="resume"]')

    with setup_page.expect_file_chooser() as chooser_info:
        form.locator("[data-job-resume-replace]").click()
    chooser_info.value.set_files(
        {
            "name": "updated.pdf",
            "mimeType": "application/pdf",
            "buffer": b"UPDATED RESUME",
        }
    )
    setup_page.wait_for_timeout(100)

    assert len(posted) == 1
    assert posted[0][0].startswith("multipart/form-data;")
    assert "updated.pdf" in posted[0][1]
    assert navigations == []
    assert detail.is_visible()
    assert detail.locator("[data-job-resume-name]").inner_text() == "updated.pdf"
    assert ats_checkbox.is_checked()


def test_job_tracker_notes_add_edit_and_delete_without_closing_details(
    setup_page: object,
) -> None:
    first_note_id = UUID("11111111-1111-4111-8111-111111111111")
    second_note_id = UUID("22222222-2222-4222-8222-222222222222")
    tracked = Snapshot(
        meta=StoreMeta(data_revision=45),
        jobs=[
            GLOBAL_STATUS_SNAPSHOT.jobs[0].model_copy(
                update={
                    "notes": [
                        JobNote(
                            id=first_note_id,
                            content="Initial note",
                            created_at=NOW,
                        )
                    ]
                }
            )
        ],
    )
    visible_snapshot = [tracked]
    requests: list[tuple[str, str]] = []

    def serve_notes(route: object) -> None:
        request = route.request
        path = urlparse(request.url).path
        notes_path = "/api/global-jobs/global-saved/notes"
        if path == notes_path or path.startswith(f"{notes_path}/"):
            requests.append((request.method, path))
            job = visible_snapshot[0].jobs[0].model_copy(deep=True)
            if request.method == "POST":
                job.notes.append(
                    JobNote(
                        id=second_note_id,
                        content=request.post_data_json["content"],
                        created_at=NOW + timedelta(days=1),
                    )
                )
            elif request.method == "PUT":
                note_id = UUID(path.rsplit("/", 1)[-1])
                note = next(item for item in job.notes if item.id == note_id)
                note.content = request.post_data_json["content"]
            elif request.method == "DELETE":
                note_id = UUID(path.rsplit("/", 1)[-1])
                job.notes = [item for item in job.notes if item.id != note_id]
            visible_snapshot[0] = Snapshot(
                meta=StoreMeta(
                    data_revision=visible_snapshot[0].meta.data_revision + 1
                ),
                jobs=[job],
            )
            route.fulfill(status=204, body="")
            return
        if "notes-edit=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=visible_snapshot[0],
            ),
        )

    setup_page.route("**/*", serve_notes)
    setup_page.goto("http://draft.test/setup?notes-edit=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] [data-job-key="global-saved"]'
    )
    detail = open_job_details(card)

    detail.locator("[data-job-note-add]").click()
    editor = card.locator("[data-job-note-dialog]")
    assert editor.is_visible()
    editor.locator("[data-job-note-input]").fill("Second note")
    editor.locator("[data-job-note-save]").click()
    detail.get_by_text("Second note", exact=True).wait_for()
    assert detail.is_visible()
    assert detail.locator(
        f'[data-job-note][data-note-id="{second_note_id}"] [data-job-note-date]'
    ).inner_text() == "2026-08-07"

    first_note = detail.locator(
        f'[data-job-note][data-note-id="{first_note_id}"]'
    )
    first_note.locator("[data-job-note-edit]").click()
    editor.locator("[data-job-note-input]").fill("Edited initial note")
    editor.locator("[data-job-note-save]").click()
    detail.get_by_text("Edited initial note", exact=True).wait_for()
    assert detail.is_visible()
    assert first_note.locator("[data-job-note-date]").inner_text() == "2026-08-06"

    second_note = detail.locator(
        f'[data-job-note][data-note-id="{second_note_id}"]'
    )
    second_note.locator("[data-job-note-delete]").click()
    delete_dialog = card.locator("[data-job-note-delete-dialog]")
    assert delete_dialog.is_visible()
    delete_dialog.locator("[data-job-note-delete-confirm]").click()
    second_note.wait_for(state="detached")

    assert detail.is_visible()
    assert detail.locator("[data-job-note]").count() == 1
    assert requests == [
        ("POST", "/api/global-jobs/global-saved/notes"),
        ("PUT", f"/api/global-jobs/global-saved/notes/{first_note_id}"),
        ("DELETE", f"/api/global-jobs/global-saved/notes/{second_note_id}"),
    ]


def test_job_tracker_salary_values_post_without_page_navigation(
    setup_page: object,
) -> None:
    posted: list[dict[str, object]] = []

    def serve_salary_update(route: object) -> None:
        request = route.request
        if request.url.endswith("/api/global-jobs/global-saved/salary"):
            posted.append(request.post_data_json)
            route.fulfill(status=204, body="")
            return
        if "salary-edit=1" not in request.url:
            route.fallback()
            return
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=GLOBAL_STATUS_SNAPSHOT,
            ),
        )

    setup_page.route("**/*", serve_salary_update)
    setup_page.goto("http://draft.test/setup?salary-edit=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    navigations: list[str] = []
    setup_page.on("framenavigated", lambda frame: navigations.append(frame.url))
    card = setup_page.locator(
        '[data-review-block="global"] [data-job-key="global-saved"]'
    )
    form = open_job_details(card).locator('[data-job-action="salary"]')

    form.locator('input[name="expected_salary"]').fill("5,500 EUR")
    form.locator('select[name="expected_salary_period"]').select_option("month")
    form.locator('input[name="offer_salary"]').fill("70,000 EUR")
    form.locator('select[name="offer_salary_period"]').select_option("year")
    form.locator('button[type="submit"]').click()
    setup_page.wait_for_timeout(100)

    assert posted == [
        {
            "expected_salary": "5,500 EUR",
            "expected_salary_period": "month",
            "offer_salary": "70,000 EUR",
            "offer_salary_period": "year",
        }
    ]
    assert navigations == []


def test_job_detail_save_buttons_show_saving_animation(setup_page: object) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] [data-job-key="global-saved"]'
    )
    detail = open_job_details(card)
    setup_page.evaluate("window.fetch = () => new Promise(() => {});")

    for action in ("status", "salary"):
        button = detail.locator(
            f'[data-job-action="{action}"] button[type="submit"]'
        )
        button.click()
        assert button.is_disabled()
        assert button.get_attribute("class") == "is-saving"
        assert button.get_text() == "Saving..."
        assert button.evaluate(
            "button => getComputedStyle(button, '::before').animationName"
        ) == "job-save-spin"


def test_job_tracker_omits_global_resume_selection(setup_page: object) -> None:
    setup_page.locator('[data-nav-step="job-tracker"]').click()

    global_review = setup_page.locator('[data-review-block="global"]')
    assert global_review.locator("[data-global-resume-select]").count() == 0
    assert global_review.locator(".global-resume-section").count() == 0


def test_status_change_requires_resume_upload_before_ats_selection(
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
    status_form = current_card.locator(".job-preview-status-form")
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
    assert selector.is_disabled()
    assert global_card.get_by_text("No resume").count() == 1


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

    excluded_card = current_review.locator(
        '#excluded article[data-job-key="excluded-local"]'
    )
    open_job_details(excluded_card).locator(
        '[data-job-action="restore"] button'
    ).click()

    restored_card = current_review.locator(
        '#recommended article[data-job-key="excluded-local"]'
    )
    restored_card.wait_for(state="attached")
    assert navigations == []
    assert restored_card.locator(".job-preview-restored").text_content() == "Restored"
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

    open_job_details(card).locator("[data-company-size-search]").click()

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

    global_card = global_review.locator('article[data-job-key="global-saved"]')
    open_job_details(global_card).locator("[data-global-job-delete]").click()

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

    dialog = open_job_details(card)
    dialog.locator("[data-global-job-delete]").click()

    assert requested == []
    assert card.is_visible()
    assert dialog.locator("[data-global-job-delete]").is_enabled()


def test_manual_job_dialog_requires_a_new_resume(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="job-tracker"]').click()
    dialog = setup_page.locator("#manual-job-dialog")

    setup_page.locator("[data-open-manual-job]").click()

    dialog.wait_for(state="visible")
    dialog.locator("#manual-job-url").fill(
        "https://careers.example/jobs/42"
    )
    dialog.locator("[data-submit-manual-job]").click()

    assert dialog.is_visible()
    assert dialog.locator("#manual-job-resume").evaluate(
        "input => input.validity.valueMissing"
    )


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


def test_manual_job_import_shows_company_size_check_then_updates_automatically(
    setup_page: object,
) -> None:
    import_id = "manual-import-company-size"
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    completed_snapshot = GLOBAL_STATUS_SNAPSHOT.model_copy(deep=True)
    completed_snapshot.jobs[0].company_size = reported_company_size(
        "501-1,000 employees",
        501,
        1000,
    )
    refresh_count = 0

    def start_import(route: object) -> None:
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
                    "resume_id": None,
                    "error": None,
                }
            ),
        )

    def complete_import(route: object) -> None:
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
                    "job_key": "global-saved",
                    "result_status": "saved",
                    "resume_id": ATS_RESUME_ID,
                    "error": None,
                }
            ),
        )

    def refresh_tracker(route: object) -> None:
        nonlocal refresh_count
        refresh_count += 1
        snapshot = (
            GLOBAL_STATUS_SNAPSHOT
            if refresh_count == 1
            else completed_snapshot
        )
        route.fulfill(
            status=200,
            content_type="text/html",
            body=render_console(
                SOURCE_FILTER_SNAPSHOT,
                global_snapshot=snapshot,
                ai_providers=AI_PROVIDERS,
                ats_history=ATS_HISTORY,
                selected_ats=SELECTED_ATS,
            ),
        )

    setup_page.route("**/api/global-jobs/import-with-resume", start_import)
    setup_page.route(f"**/api/manual-job-imports/{import_id}", complete_import)
    setup_page.route("**/setup?global-status=1", refresh_tracker)
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
    company_size = setup_page.locator(
        '[data-review-block="global"] article[data-job-key="global-saved"] '
        '.company-size [data-manual-fact-value]'
    )

    assert company_size.text_content().strip() == "Checking..."
    playwright.expect(company_size).to_have_text("501-1,000 employees")
    assert refresh_count >= 2


def test_manual_job_dialog_keeps_url_and_shows_import_failure(
    setup_page: object,
) -> None:
    setup_page.route(
        "**/api/global-jobs/import-with-resume",
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
    dialog.locator("#manual-job-resume").set_input_files(
        {
            "name": "backend.pdf",
            "mimeType": "application/pdf",
            "buffer": b"PDF resume",
        }
    )

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


def test_review_job_list_row_has_fixed_sections_and_opens_full_details(
    setup_page: object,
) -> None:
    setup_page.locator('[data-nav-step="review"]').click()
    card = setup_page.locator(
        '[data-review-block="current"] .job-card:visible'
    ).first

    rectangle = card.bounding_box()
    grid_rectangle = card.locator("xpath=..").bounding_box()
    assert rectangle is not None
    assert grid_rectangle is not None
    assert abs(rectangle["width"] - grid_rectangle["width"]) <= 1
    assert rectangle["width"] > rectangle["height"] * 3
    assert rectangle["height"] <= 180

    regions = card.locator(
        ":scope > .job-preview-status, "
        ":scope > .job-preview-body > .job-preview-summary, "
        ":scope > .job-preview-body > .job-preview-status-form, "
        ":scope > .job-preview-body > .job-preview-footer"
    ).evaluate_all(
        "regions => regions.map(region => { const box = region.getBoundingClientRect(); "
        "return { top: box.top, right: box.right, bottom: box.bottom, left: box.left }; })"
    )
    assert len(regions) == 4
    status, summary, status_form, footer = regions
    assert status["right"] <= summary["left"]
    assert summary["right"] <= status_form["left"]
    assert status_form["bottom"] <= footer["top"]

    card.locator("[data-job-preview-open-area]").click()
    dialog = card.locator("[data-job-detail-dialog]")
    assert dialog.is_visible()
    assert dialog.locator(".job-detail-status > span").count() == 4
    assert dialog.locator(".facts").is_visible()
    assert dialog.locator(".evidence").is_visible()
    assert dialog.locator('[data-job-action="status"]').is_visible()
    dialog.locator("[data-close-job-detail]").click()

    card.locator('.job-preview-status-form select[name="status"]').click()
    assert dialog.is_hidden()


def test_job_tracker_list_row_keeps_ats_outside_full_action_dialog(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] .job-card:visible'
    ).first

    assert card.locator(
        ":scope > .job-preview-body > .job-preview-footer [data-ats-select-job]"
    ).count() == 1
    card.locator("[data-job-preview-open-area]").click()
    dialog = card.locator("[data-job-detail-dialog]")
    assert dialog.is_visible()
    assert dialog.locator("[data-company-size-search]").is_visible()
    assert dialog.locator("[data-global-job-delete]").is_visible()
    assert dialog.locator("[data-ats-select-job]").count() == 0


def test_job_list_row_truncates_very_long_title_without_clipping_score(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup?global-status=1#job-tracker")
    setup_page.wait_for_load_state("networkidle")
    card = setup_page.locator(
        '[data-review-block="global"] .job-card:visible'
    ).first

    layout = card.locator(".job-preview-header").evaluate(
        """header => {
          document.documentElement.style.fontSize = "100%";
          const title = header.querySelector("h3");
          const score = header.querySelector(".score");
          title.textContent =
            "Softwareentwickler Online-Banking-Backend and Distributed Payments " +
            "Platform Reliability Engineer (m/w/d)";
          const headerBox = header.getBoundingClientRect();
          const scoreBox = score.getBoundingClientRect();
          const headerStyle = getComputedStyle(header);
          const titleStyle = getComputedStyle(title);
          const unclampedTitle = title.cloneNode(true);
          unclampedTitle.style.position = "absolute";
          unclampedTitle.style.visibility = "hidden";
          unclampedTitle.style.display = "block";
          unclampedTitle.style.width = `${title.clientWidth}px`;
          unclampedTitle.style.setProperty("-webkit-line-clamp", "unset");
          header.append(unclampedTitle);
          const unclampedHeight = unclampedTitle.getBoundingClientRect().height;
          unclampedTitle.remove();
          return {
            titleIsTruncated:
              unclampedHeight > title.getBoundingClientRect().height + 0.5,
            titleWidth: title.clientWidth,
            titleHeight: title.getBoundingClientRect().height,
            unclampedHeight,
            titleOverflow: titleStyle.textOverflow,
            scoreRight: scoreBox.right,
            contentRight: headerBox.right - parseFloat(headerStyle.paddingRight),
          };
        }"""
    )

    assert layout["titleIsTruncated"] is True, (
        layout["titleWidth"],
        layout["titleHeight"],
        layout["unclampedHeight"],
    )
    assert layout["titleOverflow"] == "ellipsis"
    assert layout["scoreRight"] <= layout["contentRight"] + 0.5


def test_job_list_row_keeps_reason_above_labels_without_clipping(
    setup_page: object,
) -> None:
    setup_page.goto("http://draft.test/setup#review")
    setup_page.wait_for_load_state("networkidle")
    setup_page.locator("#review-language-requirement").select_option("")
    card = setup_page.locator(
        '[data-review-block="current"] article.job-card:visible'
    ).first

    layout = card.locator(".job-preview-summary").evaluate(
        """summary => {
          document.documentElement.style.fontSize = "100%";
          let message = summary.querySelector(".job-preview-message");
          if (!message) {
            message = document.createElement("p");
            message.className = "job-preview-message";
            summary.append(message);
          }
          message.textContent =
            "Very strong core match. The JD's must-haves map almost one-to-one " +
            "onto demonstrated professional work: 5+ years backend development " +
            "with Spring Boot and RESTful APIs.";
          let labels = summary.querySelector(".labels");
          if (!labels) {
            labels = document.createElement("ul");
            labels.className = "labels";
            labels.innerHTML =
              "<li>Visa details to verify</li>" +
              "<li>Work authorization to verify</li>";
            summary.append(labels);
          }
          summary.insertBefore(message, labels);
          const summaryBox = summary.getBoundingClientRect();
          const labelsBox = labels.getBoundingClientRect();
          const messageStyle = getComputedStyle(message);
          return {
            messageClientHeight: message.clientHeight,
            messageScrollHeight: message.scrollHeight,
            messageWhiteSpace: messageStyle.whiteSpace,
            messageTextOverflow: messageStyle.textOverflow,
            labelsClientHeight: labels.clientHeight,
            labelsScrollHeight: labels.scrollHeight,
            labelsInsideSummary: labelsBox.bottom <= summaryBox.bottom + 0.5,
          };
        }"""
    )

    assert layout["messageClientHeight"] == layout["messageScrollHeight"]
    assert layout["messageWhiteSpace"] == "nowrap"
    assert layout["messageTextOverflow"] == "ellipsis"
    assert layout["labelsClientHeight"] == layout["labelsScrollHeight"]
    assert layout["labelsInsideSummary"] is True


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


@pytest.mark.parametrize(
    ("page_url", "groups_selector"),
    [
        (
            "http://draft.test/setup#review",
            '[data-review-block="current"] .review-groups',
        ),
        (
            "http://draft.test/setup?ats-jobs=1#job-tracker",
            '[data-review-block="global"] .review-groups',
        ),
    ],
)
def test_job_cards_chain_scroll_to_the_page_at_both_boundaries(
    setup_page: object,
    page_url: str,
    groups_selector: str,
) -> None:
    setup_page.goto(page_url)
    setup_page.wait_for_load_state("networkidle")
    language_filter = setup_page.locator("#review-language-requirement")
    if language_filter.is_visible():
        language_filter.select_option("")
    setup_page.evaluate(
        """() => {
          document.documentElement.style.scrollBehavior = "auto";
          const spacer = document.createElement("div");
          spacer.style.height = "1000px";
          document.body.append(spacer);
        }"""
    )
    groups = setup_page.locator(groups_selector)
    assert groups.evaluate("node => node.scrollHeight > node.clientHeight") is True
    assert groups.evaluate(
        "node => getComputedStyle(node).overscrollBehaviorY"
    ) == "auto"

    groups.evaluate(
        """node => {
          const top = node.getBoundingClientRect().top + window.scrollY;
          window.scrollTo(0, Math.max(1, top - 100));
          node.scrollTop = 0;
        }"""
    )
    setup_page.wait_for_timeout(100)
    groups_box = groups.bounding_box()
    assert groups_box is not None
    setup_page.mouse.move(groups_box["x"] + 10, groups_box["y"] + 10)
    page_scroll_before = setup_page.evaluate("window.scrollY")
    setup_page.mouse.wheel(0, -900)
    setup_page.wait_for_timeout(100)

    assert groups.evaluate("node => node.scrollTop") == 0
    assert setup_page.evaluate("window.scrollY") < page_scroll_before

    groups.evaluate(
        """node => {
          const top = node.getBoundingClientRect().top + window.scrollY;
          window.scrollTo(0, Math.max(1, top - 100));
          node.scrollTop = node.scrollHeight;
        }"""
    )
    setup_page.wait_for_timeout(100)
    groups_box = groups.bounding_box()
    assert groups_box is not None
    setup_page.mouse.move(groups_box["x"] + 10, groups_box["y"] + 10)
    page_scroll_before = setup_page.evaluate("window.scrollY")
    setup_page.mouse.wheel(0, 900)
    setup_page.wait_for_timeout(100)

    assert groups.evaluate(
        "node => node.scrollTop + node.clientHeight >= node.scrollHeight"
    ) is True
    assert setup_page.evaluate("window.scrollY") > page_scroll_before


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
    completed["result_ids"] = ["ats/new result"]
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
