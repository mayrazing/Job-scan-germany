from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from bs4 import BeautifulSoup
from pydantic import HttpUrl

from job_scan.dashboard import view_model
from job_scan.dashboard.render import render_console, render_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    JobNote,
    JobRecord,
    MachineStatus,
    PrimaryView,
    ReevaluationNotice,
    SalaryPeriod,
    SalaryValue,
    Snapshot,
    StoreMeta,
    TrackerGroup,
    UserStatus,
    UserStatusHistoryEntry,
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


def test_configured_groups_drive_global_sidebar_status_picker_and_lifecycle_names() -> None:
    custom_status = "group-phone-screen"
    job = _job("custom", user=UserStatus.SAVED).model_copy(
        update={
            "user_status": custom_status,
            "user_status_history": [
                UserStatusHistoryEntry(status=UserStatus.SAVED, changed_at=NOW),
                UserStatusHistoryEntry(
                    status=custom_status,
                    changed_at=NOW + timedelta(days=1),
                ),
            ],
        }
    )
    snapshot = Snapshot(
        meta=StoreMeta(
            data_revision=7,
            generated_at=NOW,
            tracker_groups=[
                TrackerGroup(id="saved", name="Inbox"),
                TrackerGroup(id=custom_status, name="Phone screen"),
            ],
        ),
        jobs=[job],
    )

    dashboard = view_model.build_global_dashboard(snapshot)
    html = render_console(global_snapshot=snapshot)
    soup = BeautifulSoup(html, "html.parser")
    global_block = soup.select_one('[data-review-block="global"]')
    assert global_block is not None
    card = global_block.select_one('[data-job-key="custom"]')
    assert card is not None

    assert [group.title for group in dashboard.active_groups.values()] == [
        "Inbox",
        "Phone screen",
    ]
    assert [group.id for group in dashboard.active_groups.values()] == [
        "saved",
        custom_status,
    ]
    assert [label.get_text(strip=True) for label in global_block.select(".review-group-label")] == [
        "Inbox",
        "Phone screen",
    ]
    assert [option.get_text(strip=True) for option in card.select('select[name="status"] option')] == [
        "Inbox",
        "Phone screen",
    ] * 2
    assert card.select_one(".job-preview-status small").get_text(strip=True) == "Phone screen"
    assert "User status: Phone screen" in card.select_one(
        ".job-detail-status"
    ).get_text(" ", strip=True)
    assert [
        step.get_text(" ", strip=True)
        for step in card.select("[data-lifecycle-step] strong")
    ] == ["Inbox", "Phone screen"]
    assert soup.select_one(
        "#manual-job-dialog [data-submit-manual-job]"
    ).get_text(strip=True) == (
        "Import to Inbox"
    )


def test_job_tracker_renders_group_settings_control_and_dialogs() -> None:
    html = render_console(global_snapshot=_snapshot(_job("saved", user=UserStatus.SAVED)))
    soup = BeautifulSoup(html, "html.parser")

    opener = soup.select_one("[data-open-tracker-groups]")
    dialog = soup.select_one("[data-tracker-group-dialog]")
    delete_dialog = soup.select_one("[data-tracker-group-delete-dialog]")
    rows = soup.select("[data-tracker-group-row]")
    saved_row = soup.select_one('[data-tracker-group-row][data-group-id="saved"]')

    assert opener is not None
    assert opener.get("aria-controls") == "tracker-group-dialog"
    assert dialog is not None
    assert delete_dialog is not None
    assert len(rows) == 7
    assert saved_row is not None
    assert saved_row.select_one("[data-delete-tracker-group]").has_attr("disabled")
    assert soup.select_one(".tracker-source-control #global-source-filter") is not None
    assert soup.select_one(".tracker-source-control [data-open-tracker-groups]") is not None


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
        render_console(
            _snapshot(_job("recommended")),
            global_snapshot=_snapshot(tracked),
        ),
        "html.parser",
    )

    added = page.select_one('[data-job-key="applied"] [data-job-preview-added]')
    assert added is not None
    assert added.get_text(" ", strip=True) == "Added: 2026-08-16"
    assert added.select_one("time").get("datetime") == (
        NOW - timedelta(days=2)
    ).isoformat()
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
    date_inputs = lifecycle.select("[data-lifecycle-date-input]")
    assert [item.get("value") for item in date_inputs] == [
        "2026-08-16",
        "2026-08-18",
        "2026-08-16",
        "2026-08-18",
    ]
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


def test_console_places_ats_start_without_a_shared_resume_upload() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    footer = page.select_one("footer#review-actions")
    assert footer is not None
    assert footer.select_one("#ats-resume") is None
    assert footer.select_one("[data-open-ats]") is not None


def test_console_places_ats_start_after_job_tracker_navigation() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    footer = page.select_one("footer#review-actions")
    assert footer is not None
    controls = footer.select_one(".review-ats-controls")
    assert controls is not None
    assert footer.select_one("#ats-ai-choice") is None
    assert [child.name for child in controls.find_all(recursive=False)] == [
        "button",
        "a",
        "button",
    ]


def test_console_job_tracker_has_manual_job_url_dialog() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    filters = global_block.select_one(".review-filters.job-tracker-filters")
    assert filters is not None
    button = global_block.select_one(
        'button[data-open-manual-job][aria-controls="manual-job-dialog"]'
    )
    assert button is not None
    assert button.parent == filters
    assert button.get_text(" ", strip=True) == "Add job from URL"
    assert "All searches" not in global_block.get_text(" ", strip=True)
    assert "Tracked jobs" not in global_block.get_text(" ", strip=True)
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


def test_console_job_tracker_has_url_filter_and_card_url() -> None:
    tracked = _snapshot(_job("saved", user=UserStatus.SAVED))
    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    url_filter = global_block.select_one(
        'input#global-url-filter[name="global-url-filter"][type="search"]'
    )
    assert url_filter is not None
    assert url_filter.get("placeholder") == "Paste job URL"
    card = global_block.select_one('[data-job-key="saved"]')
    assert card is not None
    assert card.get("data-job-url") == "https://jobs.example/saved"


def test_console_job_tracker_omits_resume_block_and_requires_a_new_upload() -> None:
    page = BeautifulSoup(
        render_console(_snapshot(_job("recommended"))),
        "html.parser",
    )

    global_block = page.select_one('[data-review-block="global"]')
    assert global_block is not None
    assert global_block.select_one(".global-resume-section") is None
    assert global_block.select_one("[data-global-resume-select]") is None
    upload = global_block.select_one(
        'input#manual-job-resume[name="resume"][type="file"]'
        '[accept=".pdf,.docx"][required]'
    )
    assert upload is not None
    assert upload.find_parent("label").select_one("span").get_text(
        " ", strip=True
    ) == "New resume"
    assert upload.find_next_sibling("small") is None
    assert page.body.get("data-selected-resume-id") is None


def test_standalone_dashboard_omits_job_tracker() -> None:
    page = BeautifulSoup(render_dashboard(_snapshot(_job("recommended"))), "html.parser")

    assert page.select_one('[data-review-block="global"]') is None
    assert page.select_one("[data-open-manual-job]") is None
    assert page.select_one('[data-review-block="current"]') is not None


def test_job_tracker_saved_job_shows_download_and_resume_replacement() -> None:
    resume_id = "sha256:" + "a" * 64
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "application_resume_id": resume_id,
                "application_resume_filename": "backend.pdf",
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")

    card = page.select_one('[data-review-block="global"] [data-job-key="saved"]')
    assert card is not None
    resume_summary = card.select_one("[data-job-resume]")
    assert resume_summary is not None
    resume_name = resume_summary.select_one("[data-job-resume-name]")
    assert resume_name is not None
    assert resume_name.get_text(" ", strip=True) == "backend.pdf"
    download = card.select_one('[data-job-resume-download][href="/api/global-jobs/saved/resume"]')
    assert download is not None
    assert download.get_text(" ", strip=True) == "Download"
    upload = card.select_one(
        'form[data-job-action="resume"] input[name="resume"][type="file"][required][hidden]'
    )
    assert upload is not None
    replace = card.select_one("[data-job-resume-replace]")
    assert replace is not None
    assert replace.get_text(" ", strip=True) == "Replace"
    reevaluate = card.select_one(
        'form[data-job-action="re-evaluate"] '
        'button[data-job-resume-reevaluate][type="submit"]'
    )
    assert reevaluate is not None
    assert reevaluate.get_text(" ", strip=True) == "Re-evaluate"
    assert card.select_one("[data-job-reevaluate-progress][role=status]") is not None
    assert "Use saved resume" not in card.get_text(" ", strip=True)


def test_job_tracker_marks_the_score_stale_after_resume_replacement() -> None:
    current_resume_id = "sha256:" + "b" * 64
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "application_resume_id": current_resume_id,
                "application_resume_filename": "updated.pdf",
                "last_evaluated_resume_id": "sha256:" + "a" * 64,
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")

    progress = page.select_one(
        '[data-job-key="saved"] [data-job-reevaluate-progress]'
    )
    assert progress is not None
    assert not progress.has_attr("hidden")
    assert progress.get_text(" ", strip=True) == (
        "Current resume has not been evaluated. Re-evaluate to update this score."
    )


def test_job_tracker_renders_persisted_reevaluation_result_state() -> None:
    finished_at = NOW + timedelta(minutes=8)
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "reevaluation_notice": ReevaluationNotice(
                    status="succeeded",
                    finished_at=finished_at,
                )
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")

    card = page.select_one('[data-review-block="global"] [data-job-key="saved"]')
    assert card is not None
    assert card.get("data-reevaluation-status") == "succeeded"
    assert card.get("data-reevaluation-finished-at") == finished_at.isoformat()
    assert card.get("aria-label") == (
        "View details for Role saved at Company saved. "
        "Re-evaluation succeeded; open to acknowledge"
    )
    assert page.select_one(
        '[data-review-block="global"] [data-review-group-notice-count="saved"]'
    ) is not None


def test_job_tracker_renders_dated_notes_with_add_edit_and_delete_controls() -> None:
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "notes": [
                    JobNote(
                        id=UUID("11111111-1111-4111-8111-111111111111"),
                        content="Follow up with recruiter.",
                        created_at=NOW,
                    )
                ]
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")

    card = page.select_one('[data-review-block="global"] [data-job-key="saved"]')
    assert card is not None
    notes = card.select_one("[data-job-notes]")
    assert notes is not None
    assert notes.select_one("[data-job-note-add]").get_text(strip=True) == "+"
    item = notes.select_one(
        '[data-job-note][data-note-id="11111111-1111-4111-8111-111111111111"]'
    )
    assert item is not None
    assert item.select_one("[data-job-note-content]").get_text(strip=True) == (
        "Follow up with recruiter."
    )
    assert item.select_one("[data-job-note-date]").get_text(strip=True) == "2026-08-18"
    assert item.select_one("[data-job-note-edit]") is not None
    assert item.select_one("[data-job-note-delete]") is not None
    assert card.select_one("[data-job-note-dialog]") is not None
    assert card.select_one("[data-job-note-delete-dialog]") is not None


def test_job_tracker_renders_editable_expected_and_offer_salaries() -> None:
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "expected_salary": SalaryValue(
                    amount="5,500 EUR",
                    period=SalaryPeriod.MONTH,
                ),
                "offer_salary": SalaryValue(
                    amount="70,000 EUR",
                    period=SalaryPeriod.YEAR,
                ),
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")
    card = page.select_one('[data-review-block="global"] [data-job-key="saved"]')
    assert card is not None
    preview = card.select_one("[data-job-preview-salaries]")
    assert preview is not None
    assert [
        item.get_text(" ", strip=True) for item in preview.select(":scope > div")
    ] == [
        "Expected salary: 5,500 EUR /month",
        "Offer salary: 70,000 EUR /year",
    ]
    form = card.select_one('form[data-job-action="salary"]')
    assert form is not None
    assert form.select_one('input[name="expected_salary"]').get("value") == "5,500 EUR"
    assert form.select_one(
        'select[name="expected_salary_period"] option[selected]'
    ).get("value") == "month"
    assert form.select_one('input[name="offer_salary"]').get("value") == "70,000 EUR"
    assert form.select_one(
        'select[name="offer_salary_period"] option[selected]'
    ).get("value") == "year"


def test_review_and_job_tracker_previews_show_posted_date() -> None:
    current = _snapshot(_job("recommended"))
    tracked = _snapshot(_job("saved", user=UserStatus.SAVED))

    page = BeautifulSoup(
        render_console(current, global_snapshot=tracked),
        "html.parser",
    )

    tracker_posted = page.select_one(
        '[data-review-block="global"] [data-job-key="saved"] .job-preview-posted'
    )
    review_posted = page.select_one(
        '[data-review-block="current"] [data-job-key="recommended"] .job-preview-posted'
    )
    assert tracker_posted is not None
    assert review_posted is not None
    for posted in (tracker_posted, review_posted):
        assert posted.get_text(" ", strip=True) == "Posted: 2026-08-18"
        assert posted.select_one('time[datetime="2026-08-18"]') is not None


def test_job_tracker_preview_labels_missing_posted_date_as_unknown() -> None:
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(update={"posted_at": None})
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")
    posted = page.select_one(
        '[data-review-block="global"] [data-job-key="saved"] .job-preview-posted'
    )
    assert posted is not None
    assert posted.get_text(" ", strip=True) == "Posted: Unknown"
    assert posted.select_one("time") is None


def test_only_job_tracker_unknown_facts_render_clickable_text_editors() -> None:
    current = _snapshot(
        _job("recommended").model_copy(update={"posted_at": None})
    )
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(update={"posted_at": None})
    )

    page = BeautifulSoup(
        render_console(current, global_snapshot=tracked),
        "html.parser",
    )
    tracker = page.select_one(
        '[data-review-block="global"] [data-job-key="saved"]'
    )
    review = page.select_one(
        '[data-review-block="current"] [data-job-key="recommended"]'
    )
    assert tracker is not None
    assert review is not None
    for field_name, input_type in (
        ("posted_at", "date"),
        ("company_size", "number"),
        ("company_industry", "text"),
    ):
        trigger = tracker.select_one(
            f'[data-manual-fact-open="{field_name}"]'
        )
        editor = tracker.select_one(
            f'[data-manual-fact-dialog][data-manual-fact-field="{field_name}"]'
        )
        assert trigger is not None
        assert trigger.name == "button"
        assert trigger.get_text(" ", strip=True) == "Unknown"
        assert editor is not None
        assert editor.select_one(f'input[name="value"][type="{input_type}"]')
        assert editor.select_one('[data-manual-fact-save]') is not None
        assert editor.select_one('[data-manual-fact-cancel]') is not None
    assert review.select_one("[data-manual-fact-open]") is None


def test_job_tracker_displays_manually_added_facts() -> None:
    tracked = _snapshot(
        _job("saved", user=UserStatus.SAVED).model_copy(
            update={
                "posted_at": None,
                "manual_posted_at": date(2026, 8, 12),
                "manual_company_size": 4200,
                "manual_company_industry": "Logistics",
            }
        )
    )

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")
    card = page.select_one('[data-review-block="global"] [data-job-key="saved"]')
    assert card is not None
    assert card.get("data-posted-at") == "2026-08-12"
    assert card.get("data-company-size-minimum") == "4200"
    assert card.get("data-company-size-maximum") == "4200"
    assert card.get("data-company-industry") == "Logistics"
    assert card.select_one(".job-preview-posted").get_text(" ", strip=True) == (
        "Posted: 2026-08-12"
    )
    assert card.select_one('[data-manual-fact="posted_at"]').get_text(
        " ", strip=True
    ) == "2026-08-12"
    assert card.select_one('[data-manual-fact="company_size"]').get_text(
        " ", strip=True
    ) == "4,200 employees · Manually added"
    assert card.select_one('[data-manual-fact="company_industry"]').get_text(
        " ", strip=True
    ) == "Logistics · Manually added"


def test_job_tracker_preview_omits_unset_salaries() -> None:
    expected_only = _job("expected", user=UserStatus.SAVED).model_copy(
        update={
            "expected_salary": SalaryValue(
                amount="5,500 EUR",
                period=SalaryPeriod.MONTH,
            )
        }
    )
    tracked = _snapshot(expected_only, _job("empty", user=UserStatus.SAVED))

    page = BeautifulSoup(render_console(global_snapshot=tracked), "html.parser")
    expected_preview = page.select_one(
        '[data-job-key="expected"] [data-job-preview-salaries]'
    )
    assert expected_preview is not None
    assert expected_preview.get_text(" ", strip=True) == (
        "Expected salary: 5,500 EUR /month"
    )
    assert page.select_one('[data-job-key="empty"] [data-job-preview-salaries]') is None
