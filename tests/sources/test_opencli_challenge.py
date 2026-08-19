from __future__ import annotations

import pytest

from job_scan.sources.glassdoor import _COMPANY_PAGE_JS as GLASSDOOR_COMPANY_PAGE_JS
from job_scan.sources.indeed import _COMPANY_PAGE_JS as INDEED_COMPANY_PAGE_JS
from job_scan.sources.linkedin import _COMPANY_PAGE_JS as LINKEDIN_COMPANY_PAGE_JS
from job_scan.sources.opencli_challenge import wait_for_challenge_clearance
from job_scan.sources.stepstone import _COMPANY_PAGE_JS as STEPSTONE_COMPANY_PAGE_JS


@pytest.mark.parametrize(
    "script",
    [
        INDEED_COMPANY_PAGE_JS,
        STEPSTONE_COMPANY_PAGE_JS,
        GLASSDOOR_COMPANY_PAGE_JS,
        LINKEDIN_COMPANY_PAGE_JS,
    ],
)
def test_opencli_company_page_scripts_report_visible_challenge(script: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<title>Just a moment...</title>"
            "<main><div>Verify you are human</div><div>Company size</div>"
            "<div>51-200 employees</div><div>51-200 Mitarbeiter</div></main>"
        )

        payload = page.evaluate(script)

        assert payload["status"] == "challenge"
        browser.close()


def test_challenge_wait_does_not_read_again_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import job_scan.sources.opencli_challenge as challenge_module

    reads = 0
    clock = iter([0.0, 0.0, 90.0])

    def read() -> dict[str, str]:
        nonlocal reads
        reads += 1
        return {"status": "challenge"}

    monkeypatch.setattr(challenge_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(challenge_module.time, "sleep", lambda _seconds: None)

    result = wait_for_challenge_clearance(
        read,
        lambda payload: payload["status"] == "challenge",
        wait_seconds=90,
    )

    assert result == {"status": "challenge"}
    assert reads == 1


def test_challenge_poll_receives_only_the_remaining_wait_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    times = iter([0.0, 0.0, 0.25, 0.25])
    remaining_values: list[float] = []
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = wait_for_challenge_clearance(
        lambda: {"status": "challenge"},
        lambda payload: payload["status"] == "challenge",
        wait_seconds=1.0,
        read_with_timeout=lambda remaining: remaining_values.append(remaining) or {"status": "ok"},
    )

    assert result == {"status": "ok"}
    assert remaining_values == [0.75]


def test_linkedin_company_page_reports_sign_in_gate() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            "<title>Sign in | LinkedIn</title>"
            "<main><div>Sign in</div><div>Company size</div>"
            "<div>51-200 employees</div></main>"
        )

        payload = page.evaluate(LINKEDIN_COMPANY_PAGE_JS)

        assert payload["status"] == "challenge"
        browser.close()
