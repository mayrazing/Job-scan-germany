from __future__ import annotations

from datetime import date

from pydantic import HttpUrl

from job_scan.domain import SourceKind
from job_scan.normalization import content_hash
from job_scan.sources.base import (
    FetchedOccurrence,
    JobReference,
    partial_from_reference,
    run_source,
)


class RecordingAdapter:
    source = SourceKind.LINKEDIN
    source_instance = "example/jobs"

    def __init__(
        self,
        listing_date: date | None,
        detail_date: date | None,
    ) -> None:
        self.listing_date = listing_date
        self.detail_date = detail_date
        self.fetch_count = 0

    def discover(self) -> list[JobReference]:
        return [
            JobReference(
                source=self.source,
                source_instance=self.source_instance,
                external_id="job-1",
                detail_url=HttpUrl("https://example.com/jobs/job-1"),
                listing_title="Backend Engineer",
                listing_company="Example GmbH",
                listing_location="Berlin",
                listing_posted_at=self.listing_date,
            )
        ]

    def fetch_detail(self, _reference: JobReference) -> FetchedOccurrence:
        self.fetch_count += 1
        description = "Build backend services."
        return FetchedOccurrence(
            source=self.source,
            source_instance=self.source_instance,
            external_id="job-1",
            url=HttpUrl("https://example.com/jobs/job-1"),
            company="Example GmbH",
            title="Backend Engineer",
            location="Berlin",
            description=description,
            posted_at=self.detail_date,
            content_hash=content_hash(
                "Example GmbH",
                "Backend Engineer",
                "Berlin",
                description,
            ),
            detail_complete=True,
        )


def test_run_source_skips_known_old_listing_before_detail_fetch() -> None:
    adapter = RecordingAdapter(date(2026, 7, 1), date(2026, 7, 1))

    result = run_source(adapter, posted_since=date(2026, 7, 28))

    assert adapter.fetch_count == 0
    assert result.occurrences == []
    assert result.discovered_source_job_keys == set()


def test_run_source_removes_old_job_found_only_in_detail() -> None:
    adapter = RecordingAdapter(None, date(2026, 7, 1))

    result = run_source(adapter, posted_since=date(2026, 7, 28))

    assert adapter.fetch_count == 1
    assert result.occurrences == []
    assert result.discovered_source_job_keys == set()


def test_run_source_keeps_job_when_source_omits_posting_date() -> None:
    adapter = RecordingAdapter(None, None)

    result = run_source(adapter, posted_since=date(2026, 7, 28))

    assert adapter.fetch_count == 1
    assert [item.external_id for item in result.occurrences] == ["job-1"]
    assert result.discovered_source_job_keys == {"linkedin:example/jobs:job-1"}


def test_partial_occurrence_keeps_source_platform_url() -> None:
    reference = JobReference(
        source=SourceKind.LINKEDIN,
        source_instance="default",
        external_id="job-1",
        detail_url=HttpUrl("https://www.linkedin.com/jobs/view/job-1"),
        listing_title="Backend Engineer",
        listing_company="Example GmbH",
        listing_location="Berlin",
        listing_application_url=HttpUrl("https://jobs.example.com/apply/job-1"),
    )

    occurrence = partial_from_reference(reference, "missing_full_description")

    assert str(occurrence.url) == "https://www.linkedin.com/jobs/view/job-1"


def test_partial_occurrence_keeps_company_industry_source_locator() -> None:
    reference = JobReference(
        source=SourceKind.LINKEDIN,
        source_instance="default",
        external_id="job-1",
        detail_url=HttpUrl("https://www.linkedin.com/jobs/view/42"),
        listing_title="Backend Engineer",
        listing_company="Example GmbH",
        listing_location="Berlin",
        listing_company_industry_source={
            "source_name": "linkedin",
            "lookup_url": "https://www.linkedin.com/jobs/view/42",
            "public_url": "https://www.linkedin.com/jobs/view/42",
            "source_title": "LinkedIn company profile",
        },
    )

    occurrence = partial_from_reference(reference, "missing_full_description")

    assert occurrence.company_industry_source is not None
    assert occurrence.company_industry_source.source_name == "linkedin"
