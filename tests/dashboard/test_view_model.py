from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, date, datetime, timedelta
from itertools import product
from typing import Literal, cast

import pytest
from pydantic import HttpUrl

from job_scan.dashboard.view_model import DashboardGroup, build_dashboard
from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    PrimaryView,
    Snapshot,
    StoreMeta,
    UserStatus,
)
from job_scan.status import primary_view

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _job(
    key: str,
    *,
    machine: MachineStatus = MachineStatus.ELIGIBLE,
    user: UserStatus = UserStatus.NEW,
    availability: AvailabilityStatus = AvailabilityStatus.ACTIVE,
    score: int | None = 70,
    posted_at: date | None = date(2026, 8, 1),
    status_updated_at: datetime = NOW,
    last_seen: datetime = NOW,
    override: Literal["show"] | None = None,
) -> JobRecord:
    return JobRecord(
        canonical_job_key=key,
        primary_source_occurrence_key=f"linkedin:{key}:req@1",
        company=f"Company {key}",
        title=f"Role {key}",
        location="Berlin",
        url=HttpUrl(f"https://jobs.example/{key}"),
        description=f"Description {key}",
        posted_at=posted_at,
        content_hash=f"sha256:{key}",
        first_seen=NOW - timedelta(days=10),
        last_seen=last_seen,
        availability_status=availability,
        machine_status=machine,
        user_status=user,
        user_status_updated_at=status_updated_at,
        manual_override=override,
        score=score,
        reason=f"Reason {key}",
        exclusion_reasons=[f"Exclusion {key}"],
        labels=[f"Label {key}"],
    )


def _snapshot(*jobs: JobRecord) -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=42), jobs=list(jobs))


def test_build_dashboard_uses_primary_view_for_every_machine_user_combination() -> None:
    jobs = [
        _job(f"{machine.value}-{user.value}", machine=machine, user=user)
        for machine, user in product(MachineStatus, UserStatus)
    ]

    dashboard = build_dashboard(_snapshot(*jobs))
    grouped_keys = {
        view: {card.canonical_key for card in group.cards}
        for view, group in dashboard.active_groups.items()
    }

    assert list(dashboard.active_groups) == [
        PrimaryView.RECOMMENDED,
        PrimaryView.PENDING,
        PrimaryView.SAVED,
        PrimaryView.EXCLUDED,
        PrimaryView.APPLIED,
        PrimaryView.INTERVIEWING,
        PrimaryView.OFFER,
        PrimaryView.WITHDRAWN,
        PrimaryView.REJECTED,
        PrimaryView.IGNORED,
    ]
    assert sum(len(keys) for keys in grouped_keys.values()) == len(jobs)
    for job in jobs:
        expected = primary_view(job)
        assert expected is not None
        assert job.canonical_job_key in grouped_keys[expected]


def test_build_dashboard_active_group_membership_is_immutable() -> None:
    dashboard = build_dashboard(_snapshot(_job("recommended")))
    active_groups = cast(
        MutableMapping[PrimaryView, DashboardGroup], dashboard.active_groups
    )

    with pytest.raises(TypeError):
        del active_groups[PrimaryView.RECOMMENDED]

    assert PrimaryView.RECOMMENDED in dashboard.active_groups


def test_dashboard_hides_non_active_jobs_except_applied() -> None:
    stale = _job(
        "stale",
        availability=AvailabilityStatus.STALE,
        machine=MachineStatus.EXCLUDED,
        user=UserStatus.SAVED,
        override="show",
    )
    closed = _job(
        "closed",
        availability=AvailabilityStatus.CLOSED,
        user=UserStatus.APPLIED,
    )

    dashboard = build_dashboard(_snapshot(stale, closed))

    assert sum(group.count for group in dashboard.active_groups.values()) == 1
    assert dashboard.active_groups[PrimaryView.APPLIED].cards[0].canonical_key == "closed"
    assert (
        dashboard.active_groups[PrimaryView.APPLIED].cards[0].availability_status
        is AvailabilityStatus.CLOSED
    )
    assert all(
        card.canonical_key != "stale"
        for group in dashboard.active_groups.values()
        for card in group.cards
    )


@pytest.mark.parametrize(
    ("view", "machine", "user"),
    [
        (PrimaryView.RECOMMENDED, MachineStatus.ELIGIBLE, UserStatus.NEW),
        (PrimaryView.SAVED, MachineStatus.ELIGIBLE, UserStatus.SAVED),
        (PrimaryView.PENDING, MachineStatus.PENDING, UserStatus.NEW),
        (PrimaryView.EXCLUDED, MachineStatus.EXCLUDED, UserStatus.NEW),
        (PrimaryView.APPLIED, MachineStatus.ELIGIBLE, UserStatus.APPLIED),
        (
            PrimaryView.INTERVIEWING,
            MachineStatus.ELIGIBLE,
            UserStatus.INTERVIEWING,
        ),
        (PrimaryView.OFFER, MachineStatus.ELIGIBLE, UserStatus.OFFER),
        (PrimaryView.WITHDRAWN, MachineStatus.ELIGIBLE, UserStatus.WITHDRAWN),
        (PrimaryView.REJECTED, MachineStatus.ELIGIBLE, UserStatus.REJECTED),
        (PrimaryView.IGNORED, MachineStatus.ELIGIBLE, UserStatus.IGNORED),
    ],
)
def test_active_groups_sort_by_score_posted_date_then_key(
    view: PrimaryView,
    machine: MachineStatus,
    user: UserStatus,
) -> None:
    jobs = [
        _job("z-low", machine=machine, user=user, score=50, posted_at=date(2026, 8, 3)),
        _job("z-old", machine=machine, user=user, score=90, posted_at=date(2026, 8, 1)),
        _job("z-new", machine=machine, user=user, score=90, posted_at=date(2026, 8, 2)),
        _job("a-new", machine=machine, user=user, score=90, posted_at=date(2026, 8, 2)),
    ]

    cards = build_dashboard(_snapshot(*jobs)).active_groups[view].cards

    assert [card.canonical_key for card in cards] == [
        "a-new",
        "z-new",
        "z-old",
        "z-low",
    ]
