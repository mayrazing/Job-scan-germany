from __future__ import annotations

from datetime import UTC, date, datetime

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
            _job("shortlisted", user=UserStatus.SHORTLISTED),
            _job("applied", user=UserStatus.APPLIED),
            _job(
                "rejected",
                user=UserStatus.REJECTED,
                availability=AvailabilityStatus.CLOSED,
            ),
            _job("ignored", user=UserStatus.IGNORED),
        )
    )

    assert list(dashboard.active_groups) == [
        PrimaryView.SHORTLISTED,
        PrimaryView.APPLIED,
        PrimaryView.REJECTED,
        PrimaryView.IGNORED,
    ]
    assert {
        view: [card.canonical_key for card in group.cards]
        for view, group in dashboard.active_groups.items()
    } == {
        PrimaryView.SHORTLISTED: ["shortlisted"],
        PrimaryView.APPLIED: ["applied"],
        PrimaryView.REJECTED: ["rejected"],
        PrimaryView.IGNORED: ["ignored"],
    }


def test_console_splits_current_search_and_global_statuses_into_two_blocks() -> None:
    current = _snapshot(
        _job("recommended"),
        _job("pending", machine=MachineStatus.PENDING),
        _job("excluded", machine=MachineStatus.EXCLUDED),
    )
    global_jobs = _snapshot(
        _job("shortlisted", user=UserStatus.SHORTLISTED),
        _job("applied", user=UserStatus.APPLIED),
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
    assert [
        item.get("data-review-group-tab")
        for item in current_block.select("[data-review-group-tab]")
    ] == ["recommended", "pending", "excluded"]
    assert [
        item.get("data-review-group-tab")
        for item in global_block.select("[data-review-group-tab]")
    ] == ["shortlisted", "applied", "rejected", "ignored"]
    assert current_block.select("[data-global-job-delete]") == []
    assert len(global_block.select("[data-global-job-delete]")) == 4
    assert global_block.select_one('[data-job-key="applied"]') is not None


def test_console_status_picker_has_no_new_option() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    picker = page.select_one('[data-job-key="recommended"] select[name="status"]')
    assert picker is not None
    assert [option.get("value") for option in picker.select("option")] == [
        "",
        "shortlisted",
        "applied",
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

    footer = page.select_one("#review-view footer")
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

    footer = page.select_one("#review-view footer")
    assert footer is not None
    controls = footer.select_one(".review-ats-controls")
    assert controls is not None
    assert footer.select_one("#ats-ai-choice") is None
    assert [child.name for child in controls.find_all(recursive=False)] == [
        "button",
        "label",
        "button",
    ]


def test_console_global_status_has_manual_job_url_dialog() -> None:
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


def test_standalone_review_has_manual_job_url_dialog() -> None:
    page = BeautifulSoup(
        render_dashboard(
            _snapshot(_job("recommended")),
            _snapshot(_job("shortlisted", user=UserStatus.SHORTLISTED)),
        ),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    assert global_block.select_one("[data-open-manual-job]") is not None
    assert global_block.select_one("#manual-job-dialog [data-manual-job-form]") is not None
