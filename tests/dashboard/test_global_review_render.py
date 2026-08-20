from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.dashboard import view_model
from job_scan.dashboard.render import render_console, render_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    PrimaryView,
    Snapshot,
    StoreMeta,
    UserStatus,
)
from job_scan.resume_catalog import ResumeCatalogEntry

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _job(
    key: str,
    *,
    machine: MachineStatus = MachineStatus.ELIGIBLE,
    user: UserStatus = UserStatus.NEW,
    availability: AvailabilityStatus = AvailabilityStatus.ACTIVE,
) -> JobRecord:
    return JobRecord(
        canonical_job_key=key,
        primary_source_occurrence_key=f"linkedin:test:{key}@1",
        company=f"Company {key}",
        title=f"Role {key}",
        location="Berlin",
        url=HttpUrl(f"https://jobs.example/{key}"),
        description=f"Complete description for {key}",
        posted_at=date(2026, 8, 18),
        content_hash=f"sha256:{key}",
        first_seen=NOW,
        last_seen=NOW,
        availability_status=availability,
        machine_status=machine,
        user_status=user,
        user_status_updated_at=NOW,
        score=80,
    )


def _snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=7, generated_at=NOW), jobs=list(jobs))


def test_global_dashboard_contains_only_manual_status_groups_and_keeps_closed_jobs() -> None:
    assert hasattr(view_model, "build_global_dashboard")
    dashboard = view_model.build_global_dashboard(
        _snapshot(
            _job("new", user=UserStatus.NEW),
            _job("saved", user=UserStatus.SAVED),
            _job("applied", user=UserStatus.APPLIED),
            _job("interviewing", user=UserStatus.INTERVIEWING),
            _job("offer", user=UserStatus.OFFER),
            _job("withdrawn", user=UserStatus.WITHDRAWN),
            _job(
                "rejected",
                user=UserStatus.REJECTED,
                availability=AvailabilityStatus.CLOSED,
            ),
            _job("ignored", user=UserStatus.IGNORED),
        )
    )

    assert list(dashboard.active_groups) == [
        PrimaryView.SAVED,
        PrimaryView.APPLIED,
        PrimaryView.INTERVIEWING,
        PrimaryView.OFFER,
        PrimaryView.WITHDRAWN,
        PrimaryView.REJECTED,
        PrimaryView.IGNORED,
    ]
    assert {
        view: [card.canonical_key for card in group.cards]
        for view, group in dashboard.active_groups.items()
    } == {
        PrimaryView.SAVED: ["saved"],
        PrimaryView.APPLIED: ["applied"],
        PrimaryView.INTERVIEWING: ["interviewing"],
        PrimaryView.OFFER: ["offer"],
        PrimaryView.WITHDRAWN: ["withdrawn"],
        PrimaryView.REJECTED: ["rejected"],
        PrimaryView.IGNORED: ["ignored"],
    }


def test_console_splits_current_search_and_tracked_jobs_into_two_views() -> None:
    current = _snapshot(
        _job("recommended"),
        _job("pending", machine=MachineStatus.PENDING),
        _job("excluded", machine=MachineStatus.EXCLUDED),
    )
    global_jobs = _snapshot(
        _job("saved", user=UserStatus.SAVED),
        _job("applied", user=UserStatus.APPLIED),
        _job("interviewing", user=UserStatus.INTERVIEWING),
        _job("offer", user=UserStatus.OFFER),
        _job("withdrawn", user=UserStatus.WITHDRAWN),
        _job("rejected", user=UserStatus.REJECTED),
        _job("ignored", user=UserStatus.IGNORED),
    )

    page = BeautifulSoup(
        render_console(
            current,
            global_snapshot=global_jobs,
            ats_default_resume_filename="Current CV.pdf",
        ),
        "html.parser",
    )

    current_block = page.select_one('[data-review-block="current"]')
    global_block = page.select_one('[data-review-block="global"]')
    assert current_block is not None
    assert global_block is not None
    assert current_block.find_parent(id="review-view") is not None
    assert global_block.find_parent(id="job-tracker-view") is not None
    assert [
        item.get("data-review-group-tab")
        for item in current_block.select("[data-review-group-tab]")
    ] == ["recommended", "pending", "excluded"]
    assert [
        item.get("data-review-group-tab")
        for item in global_block.select("[data-review-group-tab]")
    ] == [
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ]
    assert current_block.select("[data-global-job-delete]") == []
    assert len(global_block.select("[data-global-job-delete]")) == 7
    assert global_block.select_one('[data-job-key="applied"]') is not None


def test_job_tracker_card_shows_actual_status_lifecycle_and_expandable_history() -> None:
    tracked_data = _job("applied", user=UserStatus.APPLIED).model_dump(mode="json")
    tracked_data["user_status_history"] = [
        {
            "status": "saved",
            "changed_at": (NOW - timedelta(days=2)).isoformat(),
        },
        {
            "status": "applied",
            "changed_at": NOW.isoformat(),
        },
    ]
    tracked = JobRecord.model_validate(tracked_data)

    page = BeautifulSoup(
        render_dashboard(_snapshot(_job("recommended")), _snapshot(tracked)),
        "html.parser",
    )

    lifecycle = page.select_one('[data-job-key="applied"] [data-job-lifecycle]')
    assert lifecycle is not None
    assert [
        step.get_text(" ", strip=True)
        for step in lifecycle.select("[data-lifecycle-step]")
    ] == ["Saved 2026-08-16", "Applied 2026-08-18"]
    assert [
        step.get("data-state") for step in lifecycle.select("[data-lifecycle-step]")
    ] == ["complete", "current"]
    assert lifecycle.select_one('[aria-current="step"] strong').get_text(strip=True) == (
        "Applied"
    )
    history = lifecycle.select_one("details[data-lifecycle-history]")
    assert history is not None
    assert history.select_one("summary").get_text(" ", strip=True) == (
        "Lifecycle details"
    )
    assert len(history.select("[data-lifecycle-event]")) == 2
    lifecycle_text = lifecycle.get_text(" ", strip=True)
    assert "Found" not in lifecycle_text
    assert "Outcome" not in lifecycle_text


def test_console_status_picker_contains_every_global_job_status() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    picker = page.select_one('[data-job-key="recommended"] select[name="status"]')
    assert picker is not None
    assert [option.get("value") for option in picker.select("option")] == [
        "",
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ]
    assert picker.select_one('option[value=""][disabled][selected]') is not None


def test_console_places_optional_ats_resume_upload_next_to_start_button() -> None:
    page = BeautifulSoup(
        render_console(
            _snapshot(_job("recommended")),
            ats_default_resume_filename="Current CV.pdf",
        ),
        "html.parser",
    )

    footer = page.select_one("footer#review-actions")
    assert footer is not None
    upload = footer.select_one(
        'input#ats-resume[name="resume"][type="file"][accept=".pdf,.docx"]'
    )
    assert upload is not None
    assert not upload.has_attr("required")
    assert "Default: Current CV.pdf" in footer.get_text(" ", strip=True)
    assert footer.select_one("[data-open-ats]") is not None


def test_console_places_ats_resume_between_new_run_and_start_button() -> None:
    page = BeautifulSoup(
        render_console(
            _snapshot(_job("recommended")),
            ats_default_resume_filename="Current CV.pdf",
        ),
        "html.parser",
    )

    footer = page.select_one("footer#review-actions")
    assert footer is not None
    controls = footer.select_one(".review-ats-controls")
    assert controls is not None
    assert footer.select_one("#ats-ai-choice") is None
    assert [child.name for child in controls.find_all(recursive=False)] == [
        "button",
        "label",
        "button",
    ]


def test_console_job_tracker_has_manual_job_url_dialog() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    button = global_block.select_one(
        'button[data-open-manual-job][aria-controls="manual-job-dialog"]'
    )
    assert button is not None
    assert button.get_text(" ", strip=True) == "Add job from URL"
    dialog = global_block.select_one('dialog#manual-job-dialog[aria-labelledby]')
    assert dialog is not None
    form = dialog.select_one('form[data-manual-job-form][method="dialog"]')
    assert form is not None
    assert form.get("aria-busy") == "false"
    url_input = form.select_one(
        'input#manual-job-url[name="url"][type="url"][required]'
    )
    assert url_input is not None
    assert form.select_one('[data-manual-job-error][role="alert"]') is not None


def test_console_job_tracker_lists_resumes_and_accepts_a_new_upload() -> None:
    resume_id = "sha256:" + "a" * 64
    resume = ResumeCatalogEntry(
        resume_id=resume_id,
        profile_hash="sha256:" + "b" * 64,
        candidate_name="Backend CV",
        filename="backend.pdf",
        created_at=datetime(2026, 8, 19, 10, 0, tzinfo=UTC),
    )

    page = BeautifulSoup(
        render_console(
            _snapshot(_job("recommended")),
            resume_catalog=[resume],
            selected_resume_id=resume_id,
        ),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    selector = global_block.select_one("select[data-global-resume-select]")
    assert selector is not None
    selected = selector.select_one(
        f'option[data-global-resume-id="{resume_id}"][selected]'
    )
    assert selected is not None
    assert "Backend CV" in selected.get_text(" ", strip=True)
    assert "backend.pdf" in selected.get_text(" ", strip=True)
    assert selected.get("data-review-url").endswith("#job-tracker")
    upload = global_block.select_one(
        'input#manual-job-resume[name="resume"][type="file"][accept=".pdf,.docx"]'
    )
    assert upload is not None
    assert page.body.get("data-selected-resume-id") == resume_id


def test_standalone_review_has_manual_job_url_dialog() -> None:
    page = BeautifulSoup(
        render_dashboard(
            _snapshot(_job("recommended")),
            _snapshot(_job("saved", user=UserStatus.SAVED)),
        ),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    assert global_block.get("id") == "review"
    assert global_block.select_one("[data-open-manual-job]") is not None
    assert global_block.select_one("#manual-job-dialog [data-manual-job-form]") is not None
    assert global_block.select_one("[data-submit-manual-job]").get_text(
        " ", strip=True
    ) == "Import to Saved"
