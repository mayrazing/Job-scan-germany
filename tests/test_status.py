from datetime import UTC, date, datetime
from typing import Any

import pytest

from job_scan.domain import JobRecord
from job_scan.status import effective_status, primary_view

NOW = datetime(2026, 8, 2, 10, 30, tzinfo=UTC)


def job_record(
    machine: str,
    user: str,
    override: str | None,
    availability: str = "active",
) -> JobRecord:
    values: dict[str, Any] = {
        "canonical_job_key": "canonical-1",
        "primary_source_occurrence_key": "linkedin:acme/jobs:REQ-42@1",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Berlin",
        "url": "https://acme.example/job/REQ-42",
        "description": "Build APIs",
        "posted_at": date(2026, 8, 1),
        "content_hash": "sha256:abc",
        "first_seen": NOW,
        "last_seen": NOW,
        "availability_status": availability,
        "machine_status": machine,
        "user_status": user,
        "user_status_updated_at": NOW,
        "manual_override": override,
    }
    return JobRecord(**values)


@pytest.mark.parametrize(
    ("machine", "override", "expected"),
    [
        ("pending_source", None, "pending_source"),
        ("pending_source", "show", "pending_source"),
        ("pending", None, "pending"),
        ("pending", "show", "pending"),
        ("eligible", None, "eligible"),
        ("eligible", "show", "eligible"),
        ("excluded", None, "excluded"),
        ("excluded", "show", "eligible"),
        ("uncertain", None, "uncertain"),
        ("uncertain", "show", "uncertain"),
    ],
)
def test_effective_status_only_restores_manually_shown_excluded_jobs(
    machine: str, override: str | None, expected: str
) -> None:
    item = job_record(machine, "new", override)

    assert effective_status(item).value == expected


@pytest.mark.parametrize(
    ("machine", "user", "expected"),
    [
        ("pending_source", "new", "pending"),
        ("pending", "new", "pending"),
        ("eligible", "new", "recommended"),
        ("excluded", "new", "excluded"),
        ("uncertain", "new", "pending"),
        ("pending_source", "shortlisted", "pending"),
        ("pending", "shortlisted", "pending"),
        ("eligible", "shortlisted", "shortlisted"),
        ("excluded", "shortlisted", "excluded"),
        ("uncertain", "shortlisted", "pending"),
        ("pending_source", "applied", "applied"),
        ("pending", "applied", "applied"),
        ("eligible", "applied", "applied"),
        ("excluded", "applied", "applied"),
        ("uncertain", "applied", "applied"),
        ("pending_source", "rejected", "rejected"),
        ("pending", "rejected", "rejected"),
        ("eligible", "rejected", "rejected"),
        ("excluded", "rejected", "rejected"),
        ("uncertain", "rejected", "rejected"),
        ("pending_source", "ignored", "ignored"),
        ("pending", "ignored", "ignored"),
        ("eligible", "ignored", "ignored"),
        ("excluded", "ignored", "ignored"),
        ("uncertain", "ignored", "ignored"),
    ],
)
def test_primary_view_priority_for_every_user_and_effective_status_combination(
    machine: str, user: str, expected: str
) -> None:
    item = job_record(machine, user, None)

    actual = primary_view(item)

    assert actual is not None
    assert actual.value == expected


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("new", "recommended"),
        ("shortlisted", "shortlisted"),
    ],
)
def test_primary_view_uses_effective_status_after_manual_restore(user: str, expected: str) -> None:
    item = job_record("excluded", user, "show")

    actual = primary_view(item)

    assert actual is not None
    assert actual.value == expected


@pytest.mark.parametrize("availability", ["stale", "closed"])
@pytest.mark.parametrize(
    "user", ["new", "shortlisted", "rejected", "ignored"]
)
def test_non_active_availability_hides_unapplied_jobs(
    availability: str, user: str
) -> None:
    item = job_record("excluded", user, None, availability)

    assert primary_view(item) is None


@pytest.mark.parametrize("availability", ["stale", "closed"])
def test_non_active_applied_jobs_remain_in_applied(availability: str) -> None:
    item = job_record("excluded", "applied", None, availability)

    assert primary_view(item) is not None
    assert primary_view(item).value == "applied"
