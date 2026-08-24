from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

MOCK_PAGE = Path(__file__).parents[1] / "prototypes" / "web-app" / "index.html"


def test_mock_uses_ui5_for_standard_interface_controls() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    stylesheet_sources = [link.get("href", "") for link in page.select("link[href]")]
    script_sources = [script.get("src", "") for script in page.select("script[src]")]
    assert not any("bootstrap" in source for source in stylesheet_sources + script_sources)
    assert not any("tom-select" in source for source in stylesheet_sources + script_sources)
    assert "ui5-bundle.js" in script_sources

    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        rendered = browser.new_page()
        external_requests: list[str] = []
        rendered.on(
            "request",
            lambda request: external_requests.append(request.url)
            if request.url.startswith(("http://", "https://"))
            else None,
        )
        rendered.goto(MOCK_PAGE.as_uri())
        rendered.wait_for_function("customElements.get('ui5-button') !== undefined")

        expected_controls = {
            "ui5-button",
            "ui5-card",
            "ui5-checkbox",
            "ui5-combobox",
            "ui5-dialog",
            "ui5-file-uploader",
            "ui5-input",
            "ui5-multi-combobox",
            "ui5-panel",
            "ui5-progress-indicator",
            "ui5-select",
            "ui5-step-input",
            "ui5-time-picker",
        }
        assert expected_controls <= set(
            rendered.locator("[data-ui5-adapted]").evaluate_all(
                "elements => elements.map(element => element.localName)"
            )
        )
        assert rendered.evaluate(
            "document.querySelectorAll("
            "\"button, input:not([type='hidden']), select, textarea, dialog, details\""
            ").length"
        ) == 0
        assert external_requests == []
        assert rendered.locator("#ai-provider-base-url").evaluate(
            "control => control.type"
        ) == "URL"
        rendered.locator("[data-edit-ai-provider]").click()
        rendered.locator("#ai-provider-base-url").evaluate(
            "control => { control.value = 'not a url'; }"
        )
        rendered.locator("[data-save-ai-provider]").click()
        assert rendered.locator("#ai-editor-feedback").inner_text() == (
            "Enter a valid Base URL."
        )
        assert rendered.locator("#ai-provider-editor").get_attribute("hidden") is None
        browser.close()


def test_mock_covers_setup_run_and_review_flow() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    setup = page.select_one("#setup-form")
    assert setup is not None
    assert {
        "resume",
        "search-terms",
        "locations",
        "german-level",
        "linkedin-limit",
        "claude-model",
        "claude-effort",
        "claude-batch-size",
        "scan-time",
    } <= {field.get("id") for field in setup.select("input, select")}
    assert setup.select_one("#radius-km") is None
    assert setup.select_one("#remote-preference") is None
    assert setup.select_one("#target-lanes") is None
    assert setup.select_one("#staffing-penalty") is None

    assert setup.select_one("#company-list") is None
    assert setup.select_one("[data-add-company]") is None
    assert setup.select_one("#target-companies") is None
    assert page.select_one("#company-template") is None
    assert page.select_one("#run-view[hidden]") is not None
    assert page.select_one("#review-link[hidden]") is not None
    assert page.select_one("script[src='mock.js']") is not None
    assert page.select_one("link[href='mock.css']") is not None


def test_mock_adds_ats_check_as_the_fourth_workflow_step() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    workflow = page.select_one("nav[aria-label='Workflow']")
    assert workflow is not None
    assert [link.get("data-nav-step") for link in workflow.select("[data-nav-step]")] == [
        "setup",
        "run",
        "review",
        "ats",
    ]
    assert workflow.select_one('[data-nav-step="ats"][href="#ats-running"]') is not None
    assert page.select_one("#ats-running[hidden]") is not None
    assert page.select_one("#ats-check[hidden]") is not None
    start = page.select_one("#review-preview [data-open-ats]")
    assert start is not None and start.name == "button"
    assert start.has_attr("disabled")


def test_mock_review_matches_the_current_review_workspace() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")
    review = page.select_one("#review-preview")

    assert review is not None
    assert review.select_one("#review-title").get_text(strip=True) == "Review queue"
    assert review.select_one("details#scan-history .scan-history-list") is not None

    source_filter = review.select_one("#source-filter")
    posted_filter = review.select_one("#review-posted-within-days")
    company_filter = review.select_one("#review-company-size")
    assert source_filter is not None and source_filter.has_attr("multiple")
    assert [option.get("value") for option in posted_filter.select("option")] == [
        "0",
        "1",
        "3",
        "7",
        "14",
        "",
    ]
    assert [option.get("value") for option in company_filter.select("option")] == [
        "0",
        "50",
        "250",
        "1000",
        "10000",
    ]

    expected_groups = {
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
    }
    assert {
        link.get("href", "").removeprefix("#")
        for link in review.select(".review-group-nav a")
    } == expected_groups
    assert all(review.select_one(f"section#{group_id}") is not None for group_id in expected_groups)

    workspace = review.select_one(".review-workspace")
    assert workspace is not None
    group_tabs = workspace.select(".review-group-nav [data-review-group-tab]")
    assert [tab.get("data-review-group-tab") for tab in group_tabs] == [
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
    ]
    assert all(tab.has_attr("draggable") for tab in group_tabs)
    assert all(tab.select_one(".review-group-label") is not None for tab in group_tabs)
    assert group_tabs[0].get("aria-current") == "page"
    assert not review.select_one("#recommended").has_attr("hidden")
    assert all(
        review.select_one(f"#{group_id}").has_attr("hidden")
        for group_id in sorted(expected_groups - {"recommended"})
    )

    card = review.select_one(".review-groups .job-card:not([data-job-preview-card])")
    assert card is not None
    assert card.has_attr("data-sources")
    assert card.has_attr("data-posted-at")
    assert card.has_attr("data-company-size-minimum")
    assert card.select_one(".status-rail") is not None
    assert card.select_one(".facts .company-size") is not None
    assert card.select_one("details.evidence") is not None
    assert card.select_one('[data-job-action="status"] select[name="status"]') is not None

    assert review.select_one("#history") is None
    assert review.select("[data-history-filter], [data-history-kind]") == []

    tracker_statuses = [
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    ]
    assert all(
        [
            option.get("value")
            for option in select.select("option")
            if option.get("value")
        ]
        == tracker_statuses
        for select in review.select('[data-job-action="status"] select[name="status"]')
    )


def test_mock_job_preview_uses_full_width_list_row() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(MOCK_PAGE.as_uri())
        page.evaluate("document.querySelector('#review-preview').hidden = false")

        preview = page.locator("#recommended [data-job-preview-card]")
        grid = page.locator("#recommended .card-grid")
        assert preview.count() == 1
        box = preview.bounding_box()
        grid_box = grid.bounding_box()
        assert box is not None
        assert grid_box is not None
        assert abs(box["width"] - grid_box["width"]) <= 1
        assert box["width"] > box["height"] * 3
        assert box["height"] <= 180

        browser.close()


def test_mock_job_preview_card_opens_details_without_hijacking_status_control() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(MOCK_PAGE.as_uri())
        page.evaluate("document.querySelector('#review-preview').hidden = false")

        preview = page.locator("#recommended [data-job-preview-card]")
        dialog = page.locator("#job-detail-dialog")
        preview.locator("[data-job-preview-open-area]").click()
        assert dialog.evaluate("element => element.open") is True
        assert dialog.locator("h3").inner_text() == "Senior Backend Engineer"
        dialog.locator("[data-close-job-detail]").click()

        preview.locator('ui5-select[name="status"]').click()
        assert dialog.evaluate("element => element.open") is False

        browser.close()


def test_mock_applied_job_card_shows_lifecycle_summary_and_history() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    lifecycle = page.select_one("#applied .job-card [data-job-lifecycle]")
    assert lifecycle is not None
    assert [
        step.get_text(" ", strip=True)
        for step in lifecycle.select("[data-lifecycle-step]")
    ] == [
        "Saved 08-10",
        "Applied 08-12",
    ]
    assert [step.get("data-state") for step in lifecycle.select("[data-lifecycle-step]")] == [
        "complete",
        "current",
    ]

    history = lifecycle.select_one("details[data-lifecycle-history]")
    assert history is not None
    assert history.select_one("summary").get_text(" ", strip=True) == "Lifecycle details"
    assert [
        time.get("datetime") for time in history.select("[data-lifecycle-event] time")
    ] == [
        "2026-08-10T18:25:00Z",
        "2026-08-12T08:15:00Z",
    ]


def test_mock_review_and_ats_controls_update_the_visible_workspace() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MOCK_PAGE.as_uri())
        page.evaluate("document.querySelector('#review-preview').hidden = false")

        assert page.locator("#recommended .job-card:not([hidden])").count() == 3
        page.locator('[data-review-group-tab="excluded"]').click()
        assert page.locator("#recommended").get_attribute("hidden") is not None
        assert page.locator("#excluded").get_attribute("hidden") is None
        page.locator('[data-review-group-tab="recommended"]').click()

        group_tabs = page.locator("[data-review-group-tab]")
        assert group_tabs.evaluate_all(
            "tabs => tabs.map(tab => tab.dataset.reviewGroupTab)"
        )[:3] == ["recommended", "saved", "pending"]
        page.drag_and_drop(
            '[data-review-group-tab="pending"]',
            '[data-review-group-tab="recommended"]',
        )
        assert group_tabs.evaluate_all(
            "tabs => tabs.map(tab => tab.dataset.reviewGroupTab)"
        )[:3] == ["pending", "recommended", "saved"]

        page.locator("#review-company-size").evaluate(
            "(control, value) => { control.value = value; control.dispatchEvent(new Event('change', { bubbles: true })); }",
            "1000",
        )
        assert page.locator("#recommended .job-card:not([hidden])").count() == 2

        status_form = page.locator('#recommended [data-job-action="status"]').first
        status_form.locator('[name="status"]').evaluate(
            "(control, value) => { control.value = value; control.dispatchEvent(new Event('change', { bubbles: true })); }",
            "saved",
        )
        status_form.locator("ui5-button").click()
        status_card = status_form.locator("xpath=ancestor::ui5-card[1]")
        assert "saved" in status_card.locator("[data-user-status]").inner_text()

        page.locator("#review-company-size").evaluate(
            "(control, value) => { control.value = value; control.dispatchEvent(new Event('change', { bubbles: true })); }",
            "0",
        )
        page.locator('[data-review-group-tab="excluded"]').click()
        restore_form = page.locator('#excluded [data-job-action="restore"]')
        restore_form.locator("ui5-button").click()
        excluded_card = page.locator("#excluded .job-card")
        assert "eligible" in excluded_card.locator("[data-machine-status]").inner_text()
        assert restore_form.count() == 0

        page.locator('[data-review-group-tab="recommended"]').click()
        selectors = page.locator("#recommended [data-ats-select-job]")
        selectors.nth(0).click()
        selectors.nth(2).click()
        start_ats = page.locator("[data-open-ats]")
        assert start_ats.inner_text() == "Check 2 selected jobs"
        assert start_ats.is_enabled()
        start_ats.click()

        tasks = page.locator("#ats-running [data-ats-task]")
        assert tasks.count() == 3
        assert page.locator('#ats-running [data-ats-task="resume"]').count() == 1
        assert page.locator('#ats-running [data-ats-task-kind="job"]').all_inner_texts() == [
            "Senior Backend Engineer\nWaiting",
            "Python Developer\nWaiting",
        ]

        browser.close()


def test_mock_job_lifecycle_details_expand_on_click() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(MOCK_PAGE.as_uri())
        page.evaluate("document.querySelector('#review-preview').hidden = false")
        page.locator('[data-review-group-tab="applied"]').click()

        history = page.locator("#applied [data-lifecycle-history]")
        assert history.evaluate("element => element.collapsed") is True
        history.click(position={"x": 20, "y": 20})
        assert history.evaluate("element => element.collapsed") is False
        assert history.locator("[data-lifecycle-event]").count() == 2

        browser.close()


def test_mock_review_group_labels_do_not_overlap_counts_on_mobile() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(MOCK_PAGE.as_uri())
        page.evaluate("document.querySelector('#review-preview').hidden = false")

        for tab in page.locator("[data-review-group-tab]").all():
            label_box = tab.locator(".review-group-label").bounding_box()
            count_box = tab.locator(":scope > span:last-child").bounding_box()
            assert label_box is not None and count_box is not None
            assert label_box["x"] + label_box["width"] < count_box["x"]

        browser.close()


def test_ats_running_tracks_resume_then_independent_job_checks() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    running = page.select_one("#ats-running")
    assert running is not None
    progress = running.select_one("#ats-run-progress")
    assert progress is not None
    assert progress.get("aria-valuemin") == "0"
    assert progress.get("aria-valuemax") == "100"
    assert [item.get("data-ats-task") for item in running.select("[data-ats-task]")] == [
        "resume",
    ]
    assert running.select('[data-ats-task-kind="job"]') == []
    assert running.select_one("#ats-results-link[hidden][href='#ats-check']") is not None


def test_only_recommended_mock_jobs_can_be_selected_for_ats() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    selectors = page.select("#recommended [data-ats-select-job]")
    assert len(selectors) == 3
    assert page.select("#pending [data-ats-select-job]") == []
    start = page.select_one("[data-open-ats]")
    assert start.has_attr("disabled")
    assert start.get_text(" ", strip=True) == "Check 0 selected jobs"


def test_ats_check_separates_resume_readiness_from_job_matches() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    ats = page.select_one("#ats-check")
    assert ats is not None
    assert ats.select_one("#resume-readiness") is not None
    job_buttons = ats.select("[data-ats-job]")
    reports = ats.select("[data-ats-report]")
    assert len(job_buttons) >= 2
    assert {button.get("data-ats-job") for button in job_buttons} == {
        report.get("data-ats-report") for report in reports
    }
    assert len(ats.select("[data-ats-report]:not([hidden])")) == 1


def test_ats_check_history_is_collapsed_with_view_and_delete_actions() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    ats = page.select_one("#ats-check")
    history = ats.select_one("details#ats-history")
    assert history is not None
    assert not history.has_attr("open")
    rows = history.select("[data-ats-history-id]")
    assert len(rows) == 2
    assert len(history.select("[data-ats-history-id].is-selected")) == 1
    for row in rows:
        assert row.select_one("[data-ats-history-view]") is not None
        assert row.select_one("[data-ats-history-delete]") is not None
    assert ats.select_one("#ats-history-context") is not None


def test_empty_locations_means_germany_wide() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    locations = page.select_one("#locations")
    assert locations is not None
    assert "Germany-wide" not in {option.get_text(strip=True) for option in locations.select("option")}
    assert page.select_one("#locations-help").get_text(strip=True) == "Leave blank to search all of Germany."


def test_daily_schedule_is_optional_and_removable() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    scan_time = page.select_one("#scan-time")
    schedule_card = page.select_one(".schedule-status-card")
    assert scan_time is not None
    assert schedule_card is not None
    assert not {
        "d-flex",
        "flex-column",
        "flex-sm-row",
        "align-items-start",
        "justify-content-between",
        "gap-3",
        "border",
        "rounded",
        "p-3",
    } & set(schedule_card.get("class", []))
    assert not scan_time.has_attr("value")
    assert not scan_time.has_attr("required")
    assert page.select_one("#schedule-status").get_text(strip=True) == "Not scheduled"
    assert page.select_one("#remove-schedule").has_attr("disabled")


def test_linkedin_limit_defaults_to_50_with_opencli_bounds() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    linkedin_limit = page.select_one("#linkedin-limit")
    assert linkedin_limit is not None
    assert linkedin_limit.get("value") == "50"
    assert linkedin_limit.get("min") == "0"
    assert linkedin_limit.get("max") == "100"


def test_ai_provider_mock_exposes_configuration_flow_without_activation() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    ai_config = page.select_one("#ai-config")
    assert ai_config is not None
    assert ai_config.select_one("[data-add-ai-provider]") is not None
    assert ai_config.select_one("[data-edit-ai-provider]") is not None
    assert ai_config.select_one("[data-activate-ai-provider]") is None
    assert ai_config.select_one("#ai-provider-editor[hidden]") is not None
    assert {
        "ai-provider-name",
        "ai-provider-base-url",
        "ai-provider-api-key",
        "ai-provider-model",
        "ai-provider-effort",
    } <= {field.get("id") for field in ai_config.select("input, select")}
    assert ai_config.select_one("[data-discover-ai-models]") is not None
    assert ai_config.select_one("[data-save-ai-provider]") is not None
    assert ai_config.select_one("[data-cancel-ai-provider]") is not None


def test_advanced_settings_selects_cli_or_saved_api_model() -> None:
    page = BeautifulSoup(MOCK_PAGE.read_text(encoding="utf-8"), "html.parser")

    runtime = page.select_one("#ai-runtime")
    assert runtime is not None
    options = {
        option.get("value"): option.get_text(" ", strip=True)
        for option in runtime.select("option")
    }
    assert options == {
        "claude-code": "Claude Code CLI · sonnet",
        "api:deepseek": "DeepSeek API · deepseek-v4-flash",
    }
    assert page.select_one("#claude-code-settings") is not None
    assert page.select_one("#api-model-settings[hidden]") is not None
    assert page.select_one("#api-model-summary") is not None
    assert page.select_one("[data-edit-selected-api]") is None
