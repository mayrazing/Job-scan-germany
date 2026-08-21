from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from itertools import permutations
from typing import Any

import pytest

from job_scan.dedup import merge_occurrences
from job_scan.domain import (
    AIReview,
    AvailabilityEvent,
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.job_snapshot import JobSnapshotReference
from job_scan.normalization import content_hash
from job_scan.sources.base import FetchedOccurrence

NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
EARLIER = datetime(2026, 8, 2, 12, tzinfo=UTC)
POSTED = date(2026, 8, 1)


def fetched(
    source: SourceKind,
    external_id: str,
    *,
    source_instance: str | None = None,
    url: str | None = None,
    company: str = "Acme",
    title: str = "Backend Engineer",
    location: str = "Berlin",
    description: str = "Build reliable Python services for customers in Germany.",
    posted_at: date | None = POSTED,
    detail_complete: bool = True,
    fetch_error_code: str | None = None,
    company_industry_source: object | None = None,
    job_snapshot: JobSnapshotReference | None = None,
    job_snapshot_error_code: str | None = None,
) -> FetchedOccurrence:
    instance = source_instance or f"{source.value}.example"
    occurrence_url = url or f"https://{instance}/jobs/{external_id}"
    return FetchedOccurrence(
        source=source,
        source_instance=instance,
        external_id=external_id,
        url=occurrence_url,
        company=company,
        title=title,
        location=location,
        description=description if detail_complete else "",
        posted_at=posted_at,
        content_hash=content_hash(
            company,
            title,
            location,
            description if detail_complete else "",
        ),
        detail_complete=detail_complete,
        fetch_error_code=fetch_error_code,
        company_industry_source=company_industry_source,
        job_snapshot=job_snapshot,
        job_snapshot_error_code=job_snapshot_error_code,
    )


def empty_snapshot() -> Snapshot:
    return Snapshot(meta=StoreMeta(data_revision=7), jobs=[])


def canonical_key(source_occurrence_key: str) -> str:
    return hashlib.sha256(f"canonical\0{source_occurrence_key}".encode()).hexdigest()


def stored_occurrence(item: FetchedOccurrence, **updates: Any) -> SourceOccurrence:
    values: dict[str, Any] = {
        "source": item.source,
        "source_instance": item.source_instance,
        "external_id": item.external_id,
        "source_generation": 1,
        "url": item.url,
        "company": item.company,
        "title": item.title,
        "location": item.location,
        "description": item.description,
        "posted_at": item.posted_at,
        "content_hash": item.content_hash,
        "availability_status": AvailabilityStatus.ACTIVE,
        "detail_complete": item.detail_complete,
        "last_fetch_error_code": item.fetch_error_code,
        "identity_baseline_title": item.title,
        "identity_baseline_description": item.description,
    }
    values.update(updates)
    return SourceOccurrence(**values)


def stored_job(
    key: str,
    occurrences: list[SourceOccurrence],
    **updates: Any,
) -> JobRecord:
    primary = occurrences[0]
    values: dict[str, Any] = {
        "canonical_job_key": key,
        "source_occurrences": occurrences,
        "primary_source_occurrence_key": primary.source_occurrence_key,
        "company": primary.company,
        "title": primary.title,
        "location": primary.location,
        "url": primary.url,
        "description": primary.description,
        "posted_at": primary.posted_at,
        "content_hash": primary.content_hash,
        "first_seen": EARLIER,
        "last_seen": EARLIER,
        "availability_status": AvailabilityStatus.ACTIVE,
        "user_status_updated_at": EARLIER,
    }
    values.update(updates)
    return JobRecord(**values)


def review(job_key: str) -> AIReview:
    return AIReview(
        job_key=job_key,
        german_requirement="optional",
        visa_sponsorship="offered",
        existing_work_authorization="not_mentioned",
        citizenship_requirement="none",
        security_clearance="none",
        staffing_agency="no",
        eligibility_evidence=["Relocation support"],
        company_industry=None,
        company_industry_confidence="low",
        company_industry_evidence=[],
        score=88,
        reason="Strong match",
        confidence="high",
    )


def occurrence_keys(job: JobRecord) -> set[str]:
    return {item.source_job_key for item in job.source_occurrences}


def test_exact_final_job_url_merges_cross_source_and_records_evidence() -> None:
    url = "https://apply.example/jobs/REQ-420?jobId=420&utm_source=feed"
    left = fetched(
        SourceKind.LINKEDIN,
        "WD-420",
        url=url,
        company="Acme GmbH",
        title="Platform Engineer",
        location="Munich",
        description="Workday description which intentionally differs.",
    )
    right = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA-99",
        source_instance="default",
        url="https://apply.example/jobs/REQ-420?jobId=420",
        company="Different display company",
        title="Different display title",
        location="Hamburg",
        description="Jobsuche wording is unrelated to Workday wording.",
    )

    result = merge_occurrences(empty_snapshot(), [left, right], NOW)

    assert len(result.jobs) == 1
    merged = result.jobs[0]
    assert occurrence_keys(merged) == {left.source_job_key, right.source_job_key}
    evidence = [
        item
        for occurrence in merged.source_occurrences
        for item in occurrence.merge_evidence
    ]
    assert len(evidence) == 1
    assert evidence[0].rule == "job_url"
    assert evidence[0].normalized_url == "https://apply.example/jobs/REQ-420?jobId=420"
    assert evidence[0].other_source_occurrence_key in {
        f"{left.source_job_key}@1",
        f"{right.source_job_key}@1",
    }
    assert evidence[0].observed_at == NOW


def test_exact_fields_date_and_high_jd_similarity_merge() -> None:
    description = "Design and operate reliable distributed Python services for Europe."
    left = fetched(SourceKind.LINKEDIN, "1", description=description)
    right = fetched(
        SourceKind.INDEED,
        "2",
        description=description,
        posted_at=date(2026, 8, 31),
        url="https://teamtailor.example/positions/2",
    )

    result = merge_occurrences(empty_snapshot(), [left, right], NOW)

    assert len(result.jobs) == 1
    evidence = [
        evidence
        for item in result.jobs[0].source_occurrences
        for evidence in item.merge_evidence
    ]
    assert evidence[0].rule == "text_similarity"
    assert evidence[0].posted_at_delta_days == 30
    assert evidence[0].similarity == 1.0


def test_same_source_instance_different_external_ids_never_merge() -> None:
    left = fetched(SourceKind.LINKEDIN, "REQ-1", source_instance="acme/jobs")
    right = fetched(SourceKind.LINKEDIN, "REQ-2", source_instance="acme/jobs")

    result = merge_occurrences(empty_snapshot(), [left, right], NOW)

    assert len(result.jobs) == 2
    assert all(len(job.source_occurrences) == 1 for job in result.jobs)


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.example/careers",
        "https://acme.example/de/jobs",
        "https://acme.example/en/careers",
        "https://acme.example/company/jobs/search",
        "https://acme.example/jobs/search-results",
        "https://acme.example/login",
        "https://acme.example/login?jobId=42",
        "https://acme.example/login.html",
        "https://acme.example/login.htm",
        "https://acme.example/login.php",
        "https://acme.example/login.aspx",
        "https://acme.example/login.jsp",
        "https://acme.example/login.xhtml",
        "https://acme.example/login.min.xhtml",
        "https://acme.example/login.do;jsessionid=ABC123",
        "https://acme.example/jobs/login",
        "https://acme.example/jobs/login?jobId=42",
        "https://acme.example/jobs/login.xhtml",
        "https://acme.example/jobs/login.xhtml?jobId=42",
        "https://acme.example/jobs/login.min.xhtml?jobId=42",
        "https://acme.example/jobs/login.en-US.xhtml?jobId=42",
        "https://acme.example/jobs/login.v2.dev",
        "https://acme.example/jobs/login.dev?jobId=42",
        "https://acme.example/jobs/login%2Edev?jobId=42",
        "https://acme.example/jobs/login.min.admin?jobId=42",
        "https://acme.example/jobs/login.en-US.sales?jobId=42",
        "https://acme.example/careers/index.html",
        "https://acme.example/careers/index.xhtml",
        "https://acme.example/careers/index.en-US.html",
        "https://acme.example/careers/index.en_US.html",
        "https://acme.example/careers/index.de.html",
        "https://acme.example/careers/index.de.v2.1.min.html",
        "https://acme.example/jobs/search.php",
        "https://acme.example/jobs/search.do",
        "https://acme.example/jobs/search.action",
        "https://acme.example/jobs/search.min.xhtml",
        "https://acme.example/jobs/search.v2-beta.action",
        "https://acme.example/jobs/search.v2.1.action",
        "https://acme.example/jobs/search.en_US.v2.1.min.xhtml",
        "https://acme.example/jobs/search.de.v2.1.min.action",
        "https://acme.example/jobs",
        "https://acme.example/jobs/talent/pool",
        "https://acme.example/jobs/talent/pool?jobId=42",
        "https://acme.example/jobs/talent/pool.min.xhtml?jobId=42",
        "https://acme.example/jobs/talent/pool.xhtml",
        "https://acme.example/talent-pool",
        "https://acme.example/jobs/talent-pool.xhtml?jobId=42",
        "https://acme.example/jobs/talent.pool?jobId=42",
        "https://acme.example/jobs/talent.pool.xhtml?jobId=42",
        "https://acme.example/jobs/talent.pool.min.xhtml?jobId=42",
        "https://acme.example/jobs/talent.pool.dev?jobId=42",
        "https://acme.example/jobs/talent.pool.min.dev?jobId=42",
        "https://acme.example/jobs/talent.pool.en-US.dev?jobId=42",
        "https://acme.example/jobs/talent.pool.v2.dev",
        "https://acme.example/de/jobs/talent/pool.xhtml?jobId=42",
        "https://acme.example/talent%20pool",
        "https://acme.example/talent%25252520pool",
        "https://acme.example/jobs/general/application",
        "https://acme.example/jobs/general/application?jobId=42",
        "https://acme.example/jobs/general/application.xhtml",
        "https://acme.example/jobs/general/application.xhtml;jsessionid=ABC123",
        "https://acme.example/jobs/general.application.xhtml?jobId=42",
        "https://acme.example/jobs/unsolicited/application",
        "https://acme.example/jobs/unsolicited/application?jobId=42",
        "https://acme.example/jobs/unsolicited/application.xhtml",
        "https://acme.example/jobs/unsolicited.application.xhtml?jobId=42",
        "https://acme.example/l%6fgin",
        "https://acme.example/general%20application",
        "https://acme.example/general%5fapplication",
        "https://acme.example/general-application",
        "https://acme.example/jobs%2Fsearch",
        "https://acme.example/jobs%2525252Fsearch",
        "https://acme.example/jobs%252Ftalent%252Fpool",
        "https://acme.example/jobs%252Ftalent%252Fpool.xhtml",
        "https://acme.example/jobs%252Ftalent-pool",
        "https://acme.example/jobs%252Ftalent%2520pool",
        "https://acme.example/jobs%252Fgeneral%252Fapplication",
        "https://acme.example/jobs%252Funsolicited%252Fapplication",
        "https://acme.example/jobs%252Flogin",
        "https://acme.example/jobs%2Flogin?jobId=42",
        "https://acme.example/jobs%2Ftalent%2Epool?jobId=42",
        "https://acme.example/jobs;site=de%2Ftalent;pool=all%2Fpool.xhtml;view=public",
        "https://acme.example/jobs%2Ftalent%2Fpool",
        "https://acme.example/jobs%2Ftalent%20pool",
        "https://acme.example/jobs%2Fgeneral%20application",
        "https://acme.example/jobs%2Flogin",
    ],
)
def test_generic_urls_never_merge(url: str) -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "1",
        url=url,
        company="Acme",
        title="Backend Engineer",
        description="First unrelated description.",
    )
    right = fetched(
        SourceKind.ARBEITSAGENTUR,
        "2",
        source_instance="default",
        url=url,
        company="Other",
        title="Data Scientist",
        location="Hamburg",
        description="Second unrelated description.",
    )

    assert len(merge_occurrences(empty_snapshot(), [left, right], NOW).jobs) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.example/de/jobs/backend-engineer-42",
        "https://acme.example/jobs/backend%20engineer%2042",
        "https://acme.example/jobs%2Fbackend%20engineer%2042",
        "https://acme.example/jobs/REQ-42/apply",
        "https://acme.example/company/jobs/search?jobId=42",
        "https://acme.example/jobs/login-security-engineer-42?jobId=42",
        "https://acme.example/jobs/login%2Dsecurity%2Dengineer%2D42?jobId=42",
        "https://acme.example/jobs/talent-pool-manager-42",
        "https://acme.example/jobs%2Ftalent%2Dpool%2Dmanager%2D42",
        "https://acme.example/jobs/general-application-engineer-42",
        "https://acme.example/jobs/unsolicited-application-specialist-42",
        "https://acme.example/jobs/backend-engineer-42.html",
        "https://acme.example/jobs/backend-engineer-42.xhtml",
        "https://acme.example/jobs/backend-engineer-42.min.xhtml",
        "https://acme.example/jobs/login-security-engineer-42.xhtml?jobId=42",
        "https://acme.example/jobs/login.security.engineer.42?jobId=42",
        "https://acme.example/jobs%2Flogin%2Esecurity%2Eengineer%2E42?jobId=42",
        "https://acme.example/jobs/login.security.engineer.42.xhtml?jobId=42",
        "https://acme.example/jobs%2Flogin.security.engineer.42.xhtml?jobId=42",
        "https://acme.example/jobs/talent.pool.engineer.42",
        "https://acme.example/jobs/talent.pool.engineer.42?jobId=42",
        "https://acme.example/jobs/talent.pool.engineer.42.xhtml?jobId=42",
        "https://acme.example/jobs/talent/pool/backend-engineer-42?jobId=42",
        "https://acme.example/jobs/general/application/engineer-42?jobId=42",
        "https://acme.example/jobs/unsolicited/application/specialist-42?jobId=42",
        "https://acme.example/jobs%2Ftalent%2Fpool%2Fbackend-engineer-42?jobId=42",
        "https://acme.example/jobs/talent.pool.v2.dev?jobId=42",
        "https://acme.example/jobs/talent.pool.v2.1.dev?jobId=42",
        "https://acme.example/jobs/talent.pool.en-US.v2.dev?jobId=42",
        "https://acme.example/jobs/general.application.engineer.42?jobId=42",
        "https://acme.example/jobs/unsolicited.application.engineer.42?jobId=42",
        "https://acme.example/jobs/backend.engineer.42.xhtml",
        "https://acme.example/jobs/login.v2.engineer?jobId=42",
        "https://acme.example/jobs/login.min.engineer?jobId=42",
        "https://acme.example/jobs/login.en-US.engineer?jobId=42",
        "https://acme.example/jobs/login.v2.dev?jobId=42",
        "https://acme.example/jobs/login.security.dev",
        "https://acme.example/jobs/login/backend-engineer-42?jobId=42",
        "https://acme.example/jobs/login;jsessionid=A/backend-engineer-42?jobId=42",
        "https://acme.example/jobs%2Flogin%2Fbackend-engineer-42?jobId=42",
        "https://acme.example/jobs/REQ-42/apply.do",
        "https://acme.example/jobs/REQ-42/apply.action",
    ],
)
def test_job_specific_terminal_path_or_id_query_remains_url_evidence(url: str) -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "WD-42",
        url=url,
        company="First company",
        title="First role",
        description="First unrelated description.",
    )
    right = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA-42",
        source_instance="default",
        url=url,
        company="Second company",
        title="Second role",
        description="Second unrelated description.",
    )

    result = merge_occurrences(empty_snapshot(), [left, right], NOW)

    assert len(result.jobs) == 1
    assert [
        evidence.rule
        for occurrence in result.jobs[0].source_occurrences
        for evidence in occurrence.merge_evidence
    ] == ["job_url"]


def test_url_reused_by_multiple_ids_in_one_source_never_merges() -> None:
    shared_url = "https://apply.example/opening"
    occurrences = [
        fetched(
            SourceKind.LINKEDIN,
            "1",
            source_instance="acme/jobs",
            url=shared_url,
            company="One",
            title="One",
            description="One",
        ),
        fetched(
            SourceKind.LINKEDIN,
            "2",
            source_instance="acme/jobs",
            url=shared_url,
            company="Two",
            title="Two",
            description="Two",
        ),
        fetched(
            SourceKind.ARBEITSAGENTUR,
            "3",
            source_instance="default",
            url=shared_url,
            company="Three",
            title="Three",
            description="Three",
        ),
    ]

    result = merge_occurrences(empty_snapshot(), occurrences, NOW)

    assert len(result.jobs) == 3


def test_job_id_url_reused_by_historical_ids_never_merges() -> None:
    shared_url = "https://apply.example/opening?jobId=42"
    old = stored_occurrence(
        fetched(
            SourceKind.LINKEDIN,
            "OLD",
            source_instance="tenant/site",
            url=shared_url,
            company="Old company",
            title="Old role",
            description="Old description.",
        ),
        availability_status=AvailabilityStatus.CLOSED,
        closed_at=EARLIER,
    )
    current = stored_occurrence(
        fetched(
            SourceKind.LINKEDIN,
            "NEW",
            source_instance="tenant/site",
            url=shared_url,
            company="Current company",
            title="Current role",
            description="Current description.",
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                "old",
                [old],
                availability_status=AvailabilityStatus.CLOSED,
            ),
            stored_job("current", [current]),
        ],
    )
    incoming = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA",
        source_instance="default",
        url=shared_url,
        company="Unrelated company",
        title="Unrelated role",
        description="Unrelated description.",
    )

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 3
    incoming_job = next(job for job in result.jobs if incoming.source_job_key in occurrence_keys(job))
    assert occurrence_keys(incoming_job) == {incoming.source_job_key}


def test_new_occurrence_must_match_every_active_cluster_member() -> None:
    shared_url = "https://apply.example/jobs/REQ-77"
    via_url = fetched(
        SourceKind.LINKEDIN,
        "A",
        url=shared_url,
        company="Other",
        title="Other",
        location="Other",
        description="Unrelated A description.",
    )
    bridge = fetched(
        SourceKind.ARBEITSAGENTUR,
        "B",
        source_instance="default",
        url=shared_url,
        description="Identical bridge and C description.",
    )
    via_text = fetched(
        SourceKind.INDEED,
        "C",
        url="https://teamtailor.example/positions/C-3",
        description="Identical bridge and C description.",
    )

    result = merge_occurrences(empty_snapshot(), [via_url, bridge, via_text], NOW)

    assert len(result.jobs) == 2
    assert not any(
        {via_url.source_job_key, via_text.source_job_key} <= occurrence_keys(job)
        for job in result.jobs
    )


def test_closed_historical_namespace_blocks_different_external_id_merge() -> None:
    old = stored_occurrence(
        fetched(
            SourceKind.LINKEDIN,
            "OLD",
            source_instance="tenant/site",
            company="Old company",
            title="Old role",
            description="Old description.",
        ),
        availability_status=AvailabilityStatus.CLOSED,
        closed_at=EARLIER,
    )
    bridge = stored_occurrence(
        fetched(
            SourceKind.ARBEITSAGENTUR,
            "BA",
            source_instance="default",
            description="Shared complete description.",
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [old, bridge])],
    )
    incoming = fetched(
        SourceKind.LINKEDIN,
        "NEW",
        source_instance="tenant/site",
        description="Shared complete description.",
    )

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 2
    assert occurrence_keys(next(job for job in result.jobs if job.canonical_job_key == "cluster")) == {
        old.source_job_key,
        bridge.source_job_key,
    }


def test_complete_link_rejects_mixed_high_confidence_rules() -> None:
    shared_url = "https://apply.example/opening?jobId=42"
    via_url = stored_occurrence(
        fetched(
            SourceKind.LINKEDIN,
            "A",
            url=shared_url,
            company="Other company",
            title="Other role",
            location="Other location",
            description="Unrelated description.",
        )
    )
    via_text = stored_occurrence(
        fetched(
            SourceKind.STEPSTONE,
            "B",
            url="https://jobylon.example/jobs/B",
            description="Shared complete description.",
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [via_url, via_text])],
    )
    incoming = fetched(
        SourceKind.INDEED,
        "C",
        url=shared_url,
        description="Shared complete description.",
    )

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 2
    assert occurrence_keys(next(job for job in result.jobs if job.canonical_job_key == "cluster")) == {
        via_url.source_job_key,
        via_text.source_job_key,
    }


def test_complete_link_uses_rule_shared_by_every_pair_when_one_pair_overlaps() -> None:
    shared_url = "https://apply.example/opening?jobId=42"
    description = "Shared complete description."
    via_both = stored_occurrence(
        fetched(SourceKind.LINKEDIN, "A", url=shared_url, description=description)
    )
    via_text = stored_occurrence(
        fetched(
            SourceKind.STEPSTONE,
            "B",
            url="https://jobylon.example/jobs/B",
            description=description,
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [via_both, via_text])],
    )
    incoming = fetched(
        SourceKind.INDEED,
        "C",
        url=shared_url,
        description=description,
    )

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 1
    merged = result.jobs[0]
    assert occurrence_keys(merged) == {
        via_both.source_job_key,
        via_text.source_job_key,
        incoming.source_job_key,
    }
    added = next(
        occurrence
        for occurrence in merged.source_occurrences
        if occurrence.source_job_key == incoming.source_job_key
    )
    assert [evidence.rule for evidence in added.merge_evidence] == [
        "text_similarity",
        "text_similarity",
    ]


def test_input_permutations_produce_identical_snapshot() -> None:
    description = "One complete shared job description with enough stable content."
    occurrences = [
        fetched(SourceKind.LINKEDIN, "3", description=description),
        fetched(SourceKind.INDEED, "2", description=description),
        fetched(
            SourceKind.ARBEITSAGENTUR,
            "1",
            source_instance="default",
            description=description,
        ),
    ]

    dumped = [
        merge_occurrences(empty_snapshot(), ordering, NOW).model_dump(mode="json")
        for ordering in permutations(occurrences)
    ]

    assert all(value == dumped[0] for value in dumped[1:])
    assert len(dumped[0]["jobs"]) == 1


def test_known_changes_apply_before_unknown_matching_in_every_permutation() -> None:
    known_old = fetched(
        SourceKind.LINKEDIN,
        "Z",
        source_instance="workday.example",
        title="Old role",
        description="Old complete description.",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("existing", [stored_occurrence(known_old)])],
    )
    unknown = fetched(
        SourceKind.ARBEITSAGENTUR,
        "A",
        source_instance="default",
        title="Old role",
        description="Old complete description.",
        url="https://jobsuche.example/jobs/A",
    )
    known_changed = fetched(
        SourceKind.LINKEDIN,
        "Z",
        source_instance="workday.example",
        title="New unrelated role",
        description="New unrelated description.",
    )

    dumped = [
        merge_occurrences(previous, ordering, NOW).model_dump(mode="json")
        for ordering in permutations([unknown, known_changed])
    ]

    assert all(value == dumped[0] for value in dumped[1:])
    assert len(dumped[0]["jobs"]) == 2
    existing = next(job for job in dumped[0]["jobs"] if job["canonical_job_key"] == "existing")
    assert [item["source_job_key"] for item in existing["source_occurrences"]] == [
        known_changed.source_job_key
    ]


def test_zero_raw_candidates_creates_deterministic_canonical() -> None:
    item = fetched(SourceKind.LINKEDIN, "REQ-9")

    result = merge_occurrences(empty_snapshot(), [item], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].canonical_job_key == canonical_key(f"{item.source_job_key}@1")
    assert result.jobs[0].possible_duplicates == []


def test_exactly_one_admissible_candidate_joins_existing_canonical() -> None:
    old = fetched(SourceKind.LINKEDIN, "old")
    old_occurrence = stored_occurrence(old)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("existing", [old_occurrence])],
    )
    new = fetched(
        SourceKind.INDEED,
        "new",
        description=old.description,
        url="https://teamtailor.example/positions/new-2",
    )

    result = merge_occurrences(previous, [new], NOW)

    assert [job.canonical_job_key for job in result.jobs] == ["existing"]
    assert occurrence_keys(result.jobs[0]) == {old.source_job_key, new.source_job_key}


def test_zero_admissible_candidates_creates_symmetric_conflict_evidence() -> None:
    bridge = fetched(SourceKind.LINKEDIN, "A", description="Shared description.")
    blocker = fetched(
        SourceKind.STEPSTONE,
        "B",
        company="Different",
        title="Different",
        location="Different",
        description="Different description.",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [stored_occurrence(bridge), stored_occurrence(blocker)])],
    )
    incoming = fetched(
        SourceKind.INDEED,
        "C",
        description="Shared description.",
        url="https://teamtailor.example/positions/C-3",
    )

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 2
    created = next(job for job in result.jobs if job.canonical_job_key != "cluster")
    existing = next(job for job in result.jobs if job.canonical_job_key == "cluster")
    assert {(item.other_canonical_job_key, item.reason) for item in created.possible_duplicates} == {
        ("cluster", "candidate_conflict")
    }
    assert [(item.other_canonical_job_key, item.reason) for item in existing.possible_duplicates] == [
        (created.canonical_job_key, "candidate_conflict")
    ]
    assert "Possible duplicate" in created.labels
    assert "Possible duplicate" in existing.labels


def test_multiple_admissible_candidates_create_symmetric_conflict_evidence() -> None:
    description = "Same high confidence description across all three records."
    first = stored_occurrence(fetched(SourceKind.LINKEDIN, "A", description=description))
    second = stored_occurrence(fetched(SourceKind.STEPSTONE, "B", description=description))
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("first", [first]), stored_job("second", [second])],
    )
    incoming = fetched(SourceKind.INDEED, "C", description=description)

    result = merge_occurrences(previous, [incoming], NOW)

    assert len(result.jobs) == 3
    created = next(job for job in result.jobs if job.canonical_job_key not in {"first", "second"})
    edges = {
        tuple(sorted((job.canonical_job_key, evidence.other_canonical_job_key)))
        for job in result.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    }
    assert edges == {
        tuple(sorted((created.canonical_job_key, "first"))),
        tuple(sorted((created.canonical_job_key, "second"))),
    }


def test_exactly_one_admissible_candidate_does_not_conflict_with_blocked_raw_candidate() -> None:
    description = "Same high confidence description across matching records."
    admissible = stored_occurrence(
        fetched(SourceKind.LINKEDIN, "A", description=description)
    )
    blocked_match = stored_occurrence(
        fetched(SourceKind.STEPSTONE, "B1", description=description)
    )
    blocked_member = stored_occurrence(
        fetched(
            SourceKind.GLASSDOOR,
            "B2",
            company="Different",
            title="Different",
            location="Different",
            description="Different description.",
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job("admissible", [admissible]),
            stored_job("blocked", [blocked_match, blocked_member]),
        ],
    )
    incoming = fetched(SourceKind.INDEED, "X", description=description)

    result = merge_occurrences(previous, [incoming], NOW)

    joined = next(job for job in result.jobs if job.canonical_job_key == "admissible")
    assert incoming.source_job_key in occurrence_keys(joined)
    assert all(
        evidence.reason != "candidate_conflict"
        for job in result.jobs
        for evidence in job.possible_duplicates
    )


def test_candidate_conflict_noop_preserves_edge_and_time_until_raw_match_disappears() -> None:
    description = "Shared high confidence description."
    bridge = fetched(SourceKind.LINKEDIN, "A", description=description)
    blocker = fetched(
        SourceKind.STEPSTONE,
        "B",
        company="Different",
        title="Different",
        location="Different",
        description="Different description.",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [stored_occurrence(bridge), stored_occurrence(blocker)])],
    )
    incoming = fetched(SourceKind.INDEED, "X", description=description)
    conflicted = merge_occurrences(previous, [incoming], NOW)
    created = next(job for job in conflicted.jobs if job.canonical_job_key != "cluster")

    unchanged = merge_occurrences(conflicted, [], datetime(2026, 8, 4, tzinfo=UTC))

    assert [
        evidence.observed_at
        for job in unchanged.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    ] == [NOW, NOW]

    changed_bridge = fetched(
        SourceKind.LINKEDIN,
        "A",
        company="No longer matching",
        title="No longer matching",
        location="Hamburg",
        description="No longer matching description.",
    )
    removed = merge_occurrences(
        unchanged,
        [changed_bridge],
        datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert all(job.possible_duplicates == [] for job in removed.jobs)
    assert all("Possible duplicate" not in job.labels for job in removed.jobs)
    assert created.canonical_job_key in {
        job.canonical_job_key for job in removed.jobs
    }


def test_legacy_candidate_conflict_without_decision_key_uses_safe_fallback() -> None:
    description = "Shared high confidence description."
    bridge = fetched(SourceKind.LINKEDIN, "A", description=description)
    blocker = fetched(
        SourceKind.STEPSTONE,
        "B",
        company="Different",
        title="Different",
        location="Different",
        description="Different description.",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [stored_occurrence(bridge), stored_occurrence(blocker)])],
    )
    incoming = fetched(SourceKind.INDEED, "X", description=description)
    conflicted = merge_occurrences(previous, [incoming], NOW)
    for job in conflicted.jobs:
        for evidence in job.possible_duplicates:
            evidence.decision_source_occurrence_key = None

    unchanged = merge_occurrences(
        conflicted,
        [],
        datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert [
        (evidence.decision_source_occurrence_key, evidence.observed_at)
        for job in unchanged.jobs
        for evidence in job.possible_duplicates
    ] == [(None, NOW), (None, NOW)]


def test_candidate_conflict_noop_uses_persisted_home_when_first_seen_ties() -> None:
    description = "Shared high confidence description."
    bridge = fetched(
        SourceKind.LINKEDIN,
        "Z",
        source_instance="workday.example",
        description=description,
    )
    blocker = fetched(
        SourceKind.STEPSTONE,
        "blocker",
        company="Different",
        title="Different",
        location="Different",
        description="Different description.",
    )
    bridge_occurrence = stored_occurrence(bridge)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                canonical_key(bridge_occurrence.source_occurrence_key),
                [bridge_occurrence, stored_occurrence(blocker)],
                first_seen=NOW,
                last_seen=NOW,
                user_status_updated_at=NOW,
            )
        ],
    )
    incoming = fetched(
        SourceKind.ARBEITSAGENTUR,
        "A",
        source_instance="default",
        description=description,
    )

    conflicted = merge_occurrences(previous, [incoming], NOW)
    decision_key = f"{incoming.source_job_key}@1"
    assert [
        evidence.decision_source_occurrence_key
        for job in conflicted.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    ] == [decision_key, decision_key]

    unchanged = merge_occurrences(
        conflicted,
        [],
        datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert [
        (evidence.decision_source_occurrence_key, evidence.observed_at)
        for job in unchanged.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    ] == [(decision_key, NOW), (decision_key, NOW)]


def test_rollover_candidate_conflict_noop_replays_old_generation_exclusion() -> None:
    old = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="Old role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    replacement_description = "Replacement role identity with stable matching content."
    replacement = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="New role",
        description=replacement_description,
        posted_at=date(2026, 3, 2),
    )
    old_generation = stored_occurrence(
        old,
        availability_status=AvailabilityStatus.CLOSED,
        closed_at=EARLIER,
    )
    active_bridge = stored_occurrence(
        fetched(
            SourceKind.INDEED,
            "bridge",
            title=replacement.title,
            description=replacement_description,
            posted_at=replacement.posted_at,
        )
    )
    blocked_match = stored_occurrence(
        fetched(
            SourceKind.STEPSTONE,
            "match",
            title=replacement.title,
            description=replacement_description,
            posted_at=replacement.posted_at,
        )
    )
    blocked_member = stored_occurrence(
        fetched(
            SourceKind.GLASSDOOR,
            "blocker",
            company="Different",
            title="Different",
            location="Different",
            description="Different description.",
            posted_at=replacement.posted_at,
        )
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job("old", [old_generation, active_bridge]),
            stored_job("blocked", [blocked_match, blocked_member]),
        ],
    )

    conflicted = merge_occurrences(previous, [replacement], NOW)
    decision_key = f"{replacement.source_job_key}@2"
    conflict = [
        evidence
        for job in conflicted.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    ]
    assert len(conflict) == 2
    assert all(
        evidence.decision_source_occurrence_key == decision_key
        and evidence.observed_at == NOW
        for evidence in conflict
    )

    unchanged = merge_occurrences(
        conflicted,
        [],
        datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert [
        (evidence.decision_source_occurrence_key, evidence.observed_at)
        for job in unchanged.jobs
        for evidence in job.possible_duplicates
        if evidence.reason == "candidate_conflict"
    ] == [(decision_key, NOW), (decision_key, NOW)]


@pytest.mark.parametrize(
    ("right_description", "expected_duplicate"),
    [
        ("abcdefghijklmnopqrsx", True),
        ("abcdefghij", False),
    ],
)
def test_similarity_band_controls_possible_duplicate_label(
    right_description: str, expected_duplicate: bool
) -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
        url="https://workday.example/jobs/A-1",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description=right_description,
        url="https://teamtailor.example/jobs/B-2",
    )

    result = merge_occurrences(empty_snapshot(), [left, right], NOW)

    assert len(result.jobs) == 2
    assert all(bool(job.possible_duplicates) is expected_duplicate for job in result.jobs)
    assert all(("Possible duplicate" in job.labels) is expected_duplicate for job in result.jobs)
    assert all(
        evidence.decision_source_occurrence_key is None
        for job in result.jobs
        for evidence in job.possible_duplicates
    )


def test_new_partial_creates_active_pending_source_job_with_listing_fields() -> None:
    partial = fetched(
        SourceKind.GLASSDOOR,
        "partial",
        url="https://acme.example/jobs/partial",
        company="Listing Company",
        title="Listing Title",
        location="Cologne",
        detail_complete=False,
        fetch_error_code="timeout",
    )

    result = merge_occurrences(empty_snapshot(), [partial], NOW)

    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert (job.company, job.title, str(job.url)) == (
        "Listing Company",
        "Listing Title",
        "https://acme.example/jobs/partial",
    )
    assert job.description == ""
    assert job.machine_status is MachineStatus.PENDING_SOURCE
    assert job.availability_status is AvailabilityStatus.ACTIVE


def test_existing_complete_occurrence_ignores_partial_content_but_records_error() -> None:
    complete = fetched(SourceKind.LINKEDIN, "REQ-1")
    occurrence = stored_occurrence(complete, detail_complete=True)
    existing_review = review("existing")
    previous_job = stored_job(
        "existing",
        [occurrence],
        machine_status=MachineStatus.ELIGIBLE,
        user_status=UserStatus.SAVED,
        ai_review=existing_review,
        score=88,
    )
    previous = Snapshot(meta=StoreMeta(data_revision=7), jobs=[previous_job])
    partial = fetched(
        SourceKind.LINKEDIN,
        "REQ-1",
        url="https://broken.example/jobs/changed",
        company="Broken listing company",
        title="Broken listing title",
        location="Unknown",
        detail_complete=False,
        fetch_error_code="timeout",
    )

    result = merge_occurrences(previous, [partial], NOW)

    job = result.jobs[0]
    updated = job.source_occurrences[0]
    assert (updated.company, updated.title, updated.location, updated.url) == (
        occurrence.company,
        occurrence.title,
        occurrence.location,
        occurrence.url,
    )
    assert (updated.description, updated.content_hash, updated.source_generation) == (
        occurrence.description,
        occurrence.content_hash,
        1,
    )
    assert updated.detail_complete is True
    assert updated.last_fetch_error_code == "timeout"
    assert job.last_seen == NOW
    assert job.machine_status is MachineStatus.ELIGIBLE
    assert job.ai_review == existing_review
    assert job.score == 88


def test_primary_prefers_complete_company_ats_over_jobsuche() -> None:
    description = "Shared description for deterministic text merge."
    jobsuche = fetched(
        SourceKind.ARBEITSAGENTUR,
        "BA-1",
        source_instance="default",
        description=description,
    )
    ats = fetched(
        SourceKind.LINKEDIN,
        "WD-1",
        company="Acme ATS",
        title="ATS title",
        location="Munich",
        description="Different body",
        url="https://apply.example/jobs/123?jobId=123",
    )
    jobsuche = jobsuche.model_copy(update={"url": ats.url})

    result = merge_occurrences(empty_snapshot(), [jobsuche, ats], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].primary_source_occurrence_key == f"{ats.source_job_key}@1"
    assert (result.jobs[0].company, result.jobs[0].title) == ("Acme ATS", "ATS title")


@pytest.mark.parametrize(
    "criterion",
    ["complete", "company_ats", "core_fields", "jd_length", "posted_at", "source", "key"],
)
def test_primary_order_within_same_availability_tier_keeps_existing_ranking(
    criterion: str,
) -> None:
    if criterion == "complete":
        preferred = fetched(SourceKind.INDEED, "preferred", detail_complete=True)
        other = fetched(SourceKind.LINKEDIN, "other", detail_complete=False)
    elif criterion == "company_ats":
        preferred = fetched(SourceKind.LINKEDIN, "preferred")
        other = fetched(
            SourceKind.ARBEITSAGENTUR,
            "other",
            source_instance="default",
        )
    elif criterion == "core_fields":
        preferred = fetched(SourceKind.INDEED, "preferred")
        other = fetched(SourceKind.LINKEDIN, "other", location="")
    elif criterion == "jd_length":
        preferred = fetched(
            SourceKind.INDEED,
            "preferred",
            description="A substantially longer complete job description.",
        )
        other = fetched(SourceKind.LINKEDIN, "other", description="Short JD.")
    elif criterion == "posted_at":
        preferred = fetched(
            SourceKind.INDEED,
            "preferred",
            posted_at=date(2026, 8, 2),
        )
        other = fetched(
            SourceKind.LINKEDIN,
            "other",
            posted_at=date(2026, 8, 1),
        )
    elif criterion == "source":
        preferred = fetched(SourceKind.LINKEDIN, "preferred")
        other = fetched(SourceKind.INDEED, "other")
    else:
        preferred = fetched(
            SourceKind.GLASSDOOR,
            "A",
            source_instance="a.example",
        )
        other = fetched(
            SourceKind.GLASSDOOR,
            "Z",
            source_instance="z.example",
        )

    preferred_occurrence = stored_occurrence(preferred)
    other_occurrence = stored_occurrence(other)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("existing", [other_occurrence, preferred_occurrence])],
    )

    result = merge_occurrences(previous, [], NOW)

    assert result.jobs[0].primary_source_occurrence_key == (
        preferred_occurrence.source_occurrence_key
    )


def test_new_membership_invalidates_review_even_when_primary_hash_is_unchanged() -> None:
    primary = fetched(SourceKind.LINKEDIN, "WD")
    existing_review = review("existing")
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                "existing",
                [stored_occurrence(primary)],
                machine_status=MachineStatus.ELIGIBLE,
                user_status=UserStatus.SAVED,
                manual_override="show",
                manual_override_content_hash=primary.content_hash,
                manual_override_profile_hash="profile",
                ai_review=existing_review,
                score=88,
            )
        ],
    )
    additional_source = fetched(
        SourceKind.INDEED,
        "TT",
        description=primary.description,
        url="https://teamtailor.example/jobs/TT",
    )

    result = merge_occurrences(previous, [additional_source], NOW)

    job = result.jobs[0]
    assert job.primary_source_occurrence_key == primary.source_job_key + "@1"
    assert job.content_hash == primary.content_hash
    assert job.machine_status is MachineStatus.PENDING
    assert job.ai_review is None
    assert job.score is None
    assert job.manual_override is None
    assert job.manual_override_content_hash is None
    assert job.manual_override_profile_hash is None
    assert job.user_status is UserStatus.SAVED
    assert job.user_status_updated_at == EARLIER


def test_linkedin_occurrence_enters_the_deduplicated_snapshot() -> None:
    linkedin = fetched(
        SourceKind.LINKEDIN,
        "4423914728",
        source_instance="default",
        url="https://jobs.example.com/apply/4423914728",
    )

    result = merge_occurrences(empty_snapshot(), [linkedin], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.LINKEDIN
    assert result.jobs[0].primary_source_occurrence_key == (
        "linkedin:default:4423914728@1"
    )


def test_deduplicated_occurrence_preserves_company_industry_source_locator() -> None:
    linkedin = fetched(
        SourceKind.LINKEDIN,
        "4423914728",
        source_instance="default",
        company_industry_source={
            "source_name": "linkedin",
            "lookup_url": "https://www.linkedin.com/jobs/view/4423914728",
            "public_url": "https://www.linkedin.com/jobs/view/4423914728",
            "source_title": "LinkedIn company profile",
        },
    )

    result = merge_occurrences(empty_snapshot(), [linkedin], NOW)

    source = result.jobs[0].source_occurrences[0].company_industry_source
    assert source is not None
    assert source.source_name == "linkedin"


def test_new_occurrence_preserves_the_captured_job_snapshot() -> None:
    reference = JobSnapshotReference(
        snapshot_id=f"sha256:{'a' * 64}",
        captured_at=NOW,
    )
    stepstone = fetched(
        SourceKind.STEPSTONE,
        "13889830",
        source_instance="de",
        job_snapshot=reference,
    )

    result = merge_occurrences(empty_snapshot(), [stepstone], NOW)

    occurrence = result.jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot == reference
    assert occurrence.job_snapshot_error_code is None


def test_existing_occurrence_is_not_backfilled_with_a_job_snapshot() -> None:
    existing = fetched(
        SourceKind.STEPSTONE,
        "13889830",
        source_instance="de",
    )
    stored = stored_occurrence(existing)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("existing-stepstone", [stored])],
    )
    incoming = existing.model_copy(
        update={
            "job_snapshot": JobSnapshotReference(
                snapshot_id=f"sha256:{'b' * 64}",
                captured_at=NOW,
            )
        }
    )

    result = merge_occurrences(previous, [incoming], NOW)

    occurrence = result.jobs[0].source_occurrences[0]
    assert occurrence.job_snapshot is None
    assert occurrence.job_snapshot_error_code is None


def test_smartrecruiters_occurrence_enters_the_deduplicated_snapshot() -> None:
    smartrecruiters = fetched(
        SourceKind.SMARTRECRUITERS,
        "744000141479585",
        source_instance="boschgroup",
        url="https://jobs.smartrecruiters.com/BoschGroup/744000141479585",
    )

    result = merge_occurrences(empty_snapshot(), [smartrecruiters], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.SMARTRECRUITERS
    assert result.jobs[0].primary_source_occurrence_key == (
        "smartrecruiters:boschgroup:744000141479585@1"
    )


def test_bosch_occurrence_uses_the_bosch_source_key() -> None:
    bosch = fetched(
        SourceKind.BOSCH,
        "REF300001A",
        source_instance="bosch",
        url=(
            "https://jobs.bosch.com/en/job/"
            "REF300001A-backend-software-engineer-f-m-div"
        ),
    )

    result = merge_occurrences(empty_snapshot(), [bosch], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.BOSCH
    assert result.jobs[0].primary_source_occurrence_key == (
        "bosch:bosch:REF300001A@1"
    )


def test_telekom_occurrence_enters_the_deduplicated_snapshot() -> None:
    telekom = fetched(
        SourceKind.TELEKOM,
        "907522",
        source_instance="telekom",
        url="https://careers.telekom.com/en/jobs/x-907522",
    )

    result = merge_occurrences(empty_snapshot(), [telekom], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.TELEKOM
    assert result.jobs[0].primary_source_occurrence_key == (
        "telekom:telekom:907522@1"
    )


def test_siemens_occurrence_enters_the_deduplicated_snapshot() -> None:
    siemens = fetched(
        SourceKind.SIEMENS,
        "513387",
        source_instance="siemens",
        url="https://jobs.siemens.com/en_US/externaljobs/JobDetail/513387",
    )

    result = merge_occurrences(empty_snapshot(), [siemens], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.SIEMENS
    assert result.jobs[0].primary_source_occurrence_key == (
        "siemens:siemens:513387@1"
    )


def test_dhl_occurrence_enters_the_deduplicated_snapshot() -> None:
    dhl = fetched(
        SourceKind.DHL,
        "DPDHGLOBALAV361651ENAMEREXTERNAL",
        source_instance="dhl",
        url=(
            "https://careers.dhl.com/amer/en/job/"
            "DPDHGLOBALAV361651ENAMEREXTERNAL"
        ),
    )

    result = merge_occurrences(empty_snapshot(), [dhl], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.DHL
    assert result.jobs[0].primary_source_occurrence_key == (
        "dhl:dhl:DPDHGLOBALAV361651ENAMEREXTERNAL@1"
    )


def test_thyssenkrupp_occurrence_enters_the_deduplicated_snapshot() -> None:
    thyssenkrupp = fetched(
        SourceKind.THYSSENKRUPP,
        "967315",
        source_instance="thyssenkrupp",
        url="https://jobs.thyssenkrupp.com/en/job/id/967315",
    )

    result = merge_occurrences(empty_snapshot(), [thyssenkrupp], NOW)

    assert len(result.jobs) == 1
    assert (
        result.jobs[0].source_occurrences[0].source
        is SourceKind.THYSSENKRUPP
    )
    assert result.jobs[0].primary_source_occurrence_key == (
        "thyssenkrupp:thyssenkrupp:967315@1"
    )


def test_dallmeier_occurrence_enters_the_deduplicated_snapshot() -> None:
    dallmeier = fetched(
        SourceKind.DALLMEIER,
        "java-developer-w/m/d-backend",
        source_instance="dallmeier",
        url=(
            "https://www.dallmeier.com/about-us/careers/"
            "java-developer-w/m/d-backend"
        ),
    )

    result = merge_occurrences(empty_snapshot(), [dallmeier], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.DALLMEIER
    assert result.jobs[0].primary_source_occurrence_key == (
        "dallmeier:dallmeier:java-developer-w/m/d-backend@1"
    )


def test_successfactors_occurrence_enters_the_deduplicated_snapshot() -> None:
    rohde_schwarz = fetched(
        SourceKind.SUCCESSFACTORS,
        "1295",
        source_instance="rohdeschwarz",
        url=(
            "https://job.rohde-schwarz.com/job/"
            "Intern-EMC-Software-Development/1295-en_US"
        ),
    )

    result = merge_occurrences(empty_snapshot(), [rohde_schwarz], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].source_occurrences[0].source is SourceKind.SUCCESSFACTORS
    assert result.jobs[0].primary_source_occurrence_key == (
        "successfactors:rohdeschwarz:1295@1"
    )


def test_closed_source_id_reuse_rolls_generation_without_old_user_or_ai_state() -> None:
    old_fetched = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="Old Backend Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    old_occurrence = stored_occurrence(
        old_fetched,
        source_generation=3,
        availability_status=AvailabilityStatus.CLOSED,
        closed_at=EARLIER,
        availability_events=[],
    )
    old_review = review("old-canonical")
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                "old-canonical",
                [old_occurrence],
                availability_status=AvailabilityStatus.CLOSED,
                machine_status=MachineStatus.ELIGIBLE,
                user_status=UserStatus.APPLIED,
                manual_override="show",
                ai_review=old_review,
                score=88,
            )
        ],
    )
    replacement = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="New Security Role",
        description="z" * 100,
        posted_at=date(2026, 8, 1),
    )

    result = merge_occurrences(previous, [replacement], NOW)

    assert len(result.jobs) == 2
    old_job = next(job for job in result.jobs if job.canonical_job_key == "old-canonical")
    new_job = next(job for job in result.jobs if job.canonical_job_key != "old-canonical")
    assert old_job.user_status is UserStatus.APPLIED
    assert old_job.ai_review == old_review
    assert old_job.source_occurrences[0].availability_status is AvailabilityStatus.CLOSED
    assert old_job.source_occurrences[0].availability_events[-1].status is AvailabilityStatus.CLOSED
    assert new_job.source_occurrences[0].source_generation == 4
    assert new_job.user_status is UserStatus.NEW
    assert new_job.manual_override is None
    assert new_job.ai_review is None
    assert new_job.machine_status is MachineStatus.PENDING


def test_closed_rollover_appends_a_new_availability_audit_event() -> None:
    old_fetched = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="Old role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    old_occurrence = stored_occurrence(
        old_fetched,
        availability_status=AvailabilityStatus.CLOSED,
        closed_at=EARLIER,
        availability_events=[
            AvailabilityEvent(
                status=AvailabilityStatus.CLOSED,
                reason="explicitly_closed",
                observed_at=EARLIER,
            )
        ],
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                "old-canonical",
                [old_occurrence],
                availability_status=AvailabilityStatus.CLOSED,
            )
        ],
    )
    replacement = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="New role",
        description="z" * 100,
        posted_at=date(2026, 8, 1),
    )

    result = merge_occurrences(previous, [replacement], NOW)

    old_job = next(job for job in result.jobs if job.canonical_job_key == "old-canonical")
    events = old_job.source_occurrences[0].availability_events
    assert len(events) == 2
    assert events[-1].model_dump() == {
        "status": AvailabilityStatus.CLOSED,
        "reason": "explicitly_closed",
        "observed_at": NOW,
    }


def test_posted_date_sixty_days_later_rolls_active_generation() -> None:
    old_fetched = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="Old Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    old_occurrence = stored_occurrence(old_fetched)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("old-canonical", [old_occurrence])],
    )
    replacement = fetched(
        SourceKind.LINKEDIN,
        "REUSED",
        title="New Role",
        description="z" * 100,
        posted_at=date(2026, 3, 2),
    )

    result = merge_occurrences(previous, [replacement], NOW)

    assert len(result.jobs) == 2
    old_job = next(job for job in result.jobs if job.canonical_job_key == "old-canonical")
    new_job = next(job for job in result.jobs if job.canonical_job_key != "old-canonical")
    assert old_job.source_occurrences[0].availability_status is AvailabilityStatus.CLOSED
    assert old_job.availability_status is AvailabilityStatus.CLOSED
    assert new_job.source_occurrences[0].source_generation == 2
    assert new_job.availability_status is AvailabilityStatus.ACTIVE


@pytest.mark.parametrize(
    ("title", "description", "posted_at"),
    [
        ("Edited title", "a" * 100, date(2026, 3, 2)),
        ("Old Role", "z" * 100, date(2026, 3, 2)),
        ("Edited title", "z" * 100, date(2026, 3, 1)),
    ],
)
def test_title_only_jd_only_and_under_sixty_day_edits_keep_generation(
    title: str, description: str, posted_at: date
) -> None:
    old = fetched(
        SourceKind.LINKEDIN,
        "STABLE",
        title="Old Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    occurrence = stored_occurrence(old)
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("stable-canonical", [occurrence])],
    )
    edited = fetched(
        SourceKind.LINKEDIN,
        "STABLE",
        title=title,
        description=description,
        posted_at=posted_at,
    )

    result = merge_occurrences(previous, [edited], NOW)

    assert len(result.jobs) == 1
    assert result.jobs[0].canonical_job_key == "stable-canonical"
    assert result.jobs[0].source_occurrences[0].source_generation == 1


def test_generation_date_comparison_keeps_original_baseline_across_edits() -> None:
    old = fetched(
        SourceKind.LINKEDIN,
        "STABLE",
        title="Old Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("stable-canonical", [stored_occurrence(old)])],
    )
    day_59 = fetched(
        SourceKind.LINKEDIN,
        "STABLE",
        title="New Role",
        description="z" * 100,
        posted_at=date(2026, 3, 1),
    )
    first_edit = merge_occurrences(previous, [day_59], NOW)
    day_60 = day_59.model_copy(update={"posted_at": date(2026, 3, 2)})

    result = merge_occurrences(first_edit, [day_60], NOW)

    assert len(result.jobs) == 2
    assert sorted(
        occurrence.source_generation
        for job in result.jobs
        for occurrence in job.source_occurrences
    ) == [1, 2]


def test_legacy_complete_empty_identity_baseline_keeps_old_content_for_rollover() -> None:
    old = fetched(
        SourceKind.LINKEDIN,
        "LEGACY",
        title="Old Role",
        description="a" * 100,
        posted_at=date(2026, 1, 1),
    )
    legacy = stored_occurrence(
        old,
        identity_baseline_title="",
        identity_baseline_description="",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("legacy-canonical", [legacy])],
    )
    day_59 = fetched(
        SourceKind.LINKEDIN,
        "LEGACY",
        title="New Role",
        description="z" * 100,
        posted_at=date(2026, 3, 1),
    )

    edited = merge_occurrences(previous, [day_59], NOW)

    stored = edited.jobs[0].source_occurrences[0]
    assert stored.identity_baseline_title == "Old Role"
    assert stored.identity_baseline_description == "a" * 100

    day_60 = day_59.model_copy(update={"posted_at": date(2026, 3, 2)})
    result = merge_occurrences(edited, [day_60], datetime(2026, 8, 4, tzinfo=UTC))

    assert len(result.jobs) == 2
    assert sorted(
        occurrence.source_generation
        for job in result.jobs
        for occurrence in job.source_occurrences
    ) == [1, 2]


@pytest.mark.parametrize(
    "previous_availability",
    [AvailabilityStatus.ACTIVE, AvailabilityStatus.CLOSED],
)
def test_first_complete_detail_updates_partial_generation_and_identity_baseline(
    previous_availability: AvailabilityStatus,
) -> None:
    listing = fetched(
        SourceKind.LINKEDIN,
        "PENDING",
        title="Engineer listing title",
        description="",
        posted_at=date(2026, 1, 1),
        detail_complete=False,
        fetch_error_code="timeout",
    )
    stored = stored_occurrence(
        listing,
        availability_status=previous_availability,
        closed_at=EARLIER if previous_availability is AvailabilityStatus.CLOSED else None,
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[
            stored_job(
                "pending-canonical",
                [stored],
                availability_status=previous_availability,
                machine_status=MachineStatus.PENDING_SOURCE,
                user_status=UserStatus.SAVED,
            )
        ],
    )
    complete = fetched(
        SourceKind.LINKEDIN,
        "PENDING",
        title="Backend Engineer",
        description="First complete job description with reliable identity evidence.",
        posted_at=date(2026, 3, 2),
    )

    result = merge_occurrences(previous, [complete], NOW)

    assert len(result.jobs) == 1
    job = result.jobs[0]
    occurrence = job.source_occurrences[0]
    assert job.canonical_job_key == "pending-canonical"
    assert occurrence.source_generation == 1
    assert occurrence.detail_complete is True
    assert occurrence.identity_baseline_title == complete.title
    assert occurrence.identity_baseline_description == complete.description
    assert occurrence.availability_status is AvailabilityStatus.ACTIVE
    assert job.machine_status is MachineStatus.PENDING
    assert job.user_status is UserStatus.SAVED


def test_similarity_duplicate_evidence_and_labels_are_removed_after_content_change() -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
        url="https://workday.example/jobs/A-1",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsx",
        url="https://teamtailor.example/jobs/B-2",
    )
    duplicate = merge_occurrences(empty_snapshot(), [left, right], NOW)
    assert all(job.possible_duplicates for job in duplicate.jobs)
    changed = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghij",
        url="https://teamtailor.example/jobs/B-2",
    )

    result = merge_occurrences(duplicate, [changed], datetime(2026, 8, 4, tzinfo=UTC))

    assert all(job.possible_duplicates == [] for job in result.jobs)
    assert all("Possible duplicate" not in job.labels for job in result.jobs)


def test_candidate_conflict_evidence_is_removed_when_complete_link_becomes_valid() -> None:
    shared_description = "Shared description."
    bridge = fetched(
        SourceKind.LINKEDIN,
        "A",
        description=shared_description,
    )
    blocker = fetched(
        SourceKind.STEPSTONE,
        "B",
        company="Different",
        title="Different",
        location="Different",
        description="Different description.",
    )
    previous = Snapshot(
        meta=StoreMeta(data_revision=7),
        jobs=[stored_job("cluster", [stored_occurrence(bridge), stored_occurrence(blocker)])],
    )
    incoming = fetched(
        SourceKind.INDEED,
        "C",
        description=shared_description,
        url="https://teamtailor.example/positions/C-3",
    )
    conflicted = merge_occurrences(previous, [incoming], NOW)
    assert all(job.possible_duplicates for job in conflicted.jobs)
    repaired_blocker = fetched(
        SourceKind.STEPSTONE,
        "B",
        description=shared_description,
    )

    result = merge_occurrences(
        conflicted,
        [repaired_blocker],
        datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert all(job.possible_duplicates == [] for job in result.jobs)
    assert all("Possible duplicate" not in job.labels for job in result.jobs)


def test_unchanged_duplicate_edge_preserves_original_observed_at() -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
        url="https://workday.example/jobs/A-1",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsx",
        url="https://teamtailor.example/jobs/B-2",
    )
    duplicate = merge_occurrences(empty_snapshot(), [left, right], NOW)

    result = merge_occurrences(duplicate, [], datetime(2026, 8, 4, tzinfo=UTC))

    assert [
        evidence.observed_at
        for job in result.jobs
        for evidence in job.possible_duplicates
    ] == [NOW, NOW]


def test_duplicate_edge_content_change_with_same_similarity_uses_new_observed_at() -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
        url="https://workday.example/jobs/A-1",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsx",
        url="https://teamtailor.example/jobs/B-2",
    )
    duplicate = merge_occurrences(empty_snapshot(), [left, right], NOW)
    changed = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsy",
        url="https://teamtailor.example/jobs/B-2",
    )
    observed_at = datetime(2026, 8, 4, tzinfo=UTC)

    result = merge_occurrences(duplicate, [changed], observed_at)

    assert changed.content_hash != right.content_hash
    assert {
        evidence.similarity
        for job in duplicate.jobs
        for evidence in job.possible_duplicates
    } == {
        evidence.similarity
        for job in result.jobs
        for evidence in job.possible_duplicates
    }
    assert [
        evidence.observed_at
        for job in result.jobs
        for evidence in job.possible_duplicates
    ] == [observed_at, observed_at]


def test_duplicate_edge_reappearance_uses_new_symmetric_observed_at() -> None:
    left = fetched(
        SourceKind.LINKEDIN,
        "A",
        description="abcdefghijklmnopqrst",
        url="https://workday.example/jobs/A-1",
    )
    right = fetched(
        SourceKind.INDEED,
        "B",
        description="abcdefghijklmnopqrsx",
        url="https://teamtailor.example/jobs/B-2",
    )
    duplicate = merge_occurrences(empty_snapshot(), [left, right], NOW)
    changed = right.model_copy(
        update={
            "description": "abcdefghij",
            "content_hash": content_hash("Acme", "Backend Engineer", "Berlin", "abcdefghij"),
        }
    )
    removed = merge_occurrences(duplicate, [changed], datetime(2026, 8, 4, tzinfo=UTC))
    assert all(job.possible_duplicates == [] for job in removed.jobs)
    reappeared_at = datetime(2026, 8, 5, tzinfo=UTC)

    result = merge_occurrences(removed, [right], reappeared_at)

    assert [
        evidence.observed_at
        for job in result.jobs
        for evidence in job.possible_duplicates
    ] == [reappeared_at, reappeared_at]
    assert all(len(job.possible_duplicates) == 1 for job in result.jobs)
