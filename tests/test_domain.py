from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from job_scan.domain import (
    AIReview,
    AvailabilityEvent,
    AvailabilityStatus,
    DuplicateEvidence,
    JobRecord,
    MachineStatus,
    MergeEvidence,
    PrimaryView,
    ReviewHistoryEntry,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
    UserStatus,
)
from job_scan.job_snapshot import JobSnapshotReference

OBSERVED_AT = datetime(2026, 8, 2, 10, 30, tzinfo=UTC)


def test_source_kind_accepts_linkedin_occurrences() -> None:
    assert SourceKind("linkedin") is SourceKind.LINKEDIN


def test_source_kind_accepts_indeed_occurrences() -> None:
    assert SourceKind("indeed") is SourceKind.INDEED


def test_source_occurrence_accepts_a_job_snapshot_reference() -> None:
    captured_at = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    item = occurrence(
        job_snapshot=JobSnapshotReference(
            snapshot_id=f"sha256:{'a' * 64}",
            captured_at=captured_at,
        )
    )

    assert item.job_snapshot is not None
    assert item.job_snapshot.captured_at == captured_at


def test_source_occurrence_keeps_old_records_without_a_snapshot() -> None:
    item = occurrence()

    assert item.job_snapshot is None
    assert item.job_snapshot_error_code is None


def occurrence(external_id: str = "REQ-42", **updates: Any) -> SourceOccurrence:
    values: dict[str, Any] = {
        "source": SourceKind.LINKEDIN,
        "source_instance": "acme/jobs",
        "external_id": external_id,
        "source_generation": 2,
        "url": f"https://acme.example/job/{external_id}",
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Berlin",
        "description": "Build APIs",
        "posted_at": date(2026, 8, 1),
        "content_hash": "sha256:abc",
        "availability_status": AvailabilityStatus.ACTIVE,
    }
    values.update(updates)
    return SourceOccurrence(**values)


def merge_evidence(
    similarity: float = 0.5, **updates: Any
) -> MergeEvidence:
    values: dict[str, Any] = {
        "other_source_occurrence_key": "linkedin:other:1@1",
        "rule": "text_similarity",
        "normalized_company": "acme",
        "normalized_title": "backend engineer",
        "normalized_location": "berlin",
        "similarity": similarity,
        "observed_at": OBSERVED_AT,
    }
    values.update(updates)
    return MergeEvidence(**values)


def duplicate_evidence(
    similarity: float = 0.5, **updates: Any
) -> DuplicateEvidence:
    values: dict[str, Any] = {
        "other_canonical_job_key": "canonical-2",
        "reason": "similarity_band",
        "similarity": similarity,
        "observed_at": OBSERVED_AT,
    }
    values.update(updates)
    return DuplicateEvidence(**values)


def availability_event(**updates: Any) -> AvailabilityEvent:
    values: dict[str, Any] = {
        "status": AvailabilityStatus.ACTIVE,
        "reason": "listed",
        "observed_at": OBSERVED_AT,
    }
    values.update(updates)
    return AvailabilityEvent(**values)


def review_history_entry(**updates: Any) -> ReviewHistoryEntry:
    values: dict[str, Any] = {
        "attempted_at": OBSERVED_AT,
        "content_hash": "sha256:abc",
        "profile_hash": "sha256:profile",
        "model": "claude-test",
        "outcome": "accepted",
    }
    values.update(updates)
    return ReviewHistoryEntry(**values)


def job(
    canonical_job_key: str,
    source_item: SourceOccurrence | None = None,
    **updates: Any,
) -> JobRecord:
    item = source_item or occurrence(canonical_job_key)
    values: dict[str, Any] = {
        "canonical_job_key": canonical_job_key,
        "source_occurrences": [item],
        "primary_source_occurrence_key": item.source_occurrence_key,
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Berlin",
        "url": item.url,
        "description": "Build APIs",
        "posted_at": date(2026, 8, 1),
        "content_hash": "sha256:abc",
        "first_seen": OBSERVED_AT,
        "last_seen": OBSERVED_AT,
        "availability_status": AvailabilityStatus.ACTIVE,
        "user_status_updated_at": OBSERVED_AT,
    }
    values.update(updates)
    return JobRecord(**values)


def review(score: int = 75, **updates: Any) -> AIReview:
    values: dict[str, Any] = {
        "job_key": "canonical-1",
        "german_requirement": "optional",
        "visa_sponsorship": "not_mentioned",
        "existing_work_authorization": "not_mentioned",
        "citizenship_requirement": "none",
        "security_clearance": "none",
        "staffing_agency": "no",
        "eligibility_evidence": ["English-speaking role"],
        "company_industry": None,
        "company_industry_confidence": "low",
        "company_industry_evidence": [],
        "score": score,
        "reason": "Relevant backend experience",
        "confidence": "high",
    }
    values.update(updates)
    return AIReview(**values)


def scored_job(score: int) -> JobRecord:
    return job("canonical-score", score=score)


def test_review_models_have_no_target_lane_fields() -> None:
    assert "lane" not in AIReview.model_fields
    assert "lane" not in JobRecord.model_fields


def test_ai_review_requires_company_industry_fields() -> None:
    incomplete = review().model_dump()
    incomplete.pop("company_industry")
    incomplete.pop("company_industry_confidence")
    incomplete.pop("company_industry_evidence")

    with pytest.raises(ValidationError):
        AIReview.model_validate(incomplete)


def test_occurrence_computes_namespaced_keys() -> None:
    item = occurrence()

    assert item.source_job_key == "linkedin:acme/jobs:REQ-42"
    assert item.source_occurrence_key == "linkedin:acme/jobs:REQ-42@2"
    dumped = item.model_dump(mode="json")
    assert dumped["source_job_key"] == "linkedin:acme/jobs:REQ-42"
    assert dumped["source_occurrence_key"] == (
        "linkedin:acme/jobs:REQ-42@2"
    )


def test_snapshot_round_trip_dump_keeps_facts_and_rebuilds_computed_keys() -> None:
    snapshot = Snapshot(meta=StoreMeta(data_revision=1), jobs=[job("canonical-1")])

    dumped = snapshot.model_dump(mode="json", round_trip=True)
    dumped_occurrence = dumped["jobs"][0]["source_occurrences"][0]

    assert set(dumped) == set(Snapshot.model_fields)
    assert set(dumped["meta"]) == set(StoreMeta.model_fields) - {
        "global_job_deletions"
    }
    assert set(dumped["jobs"][0]) == set(JobRecord.model_fields) - {
        "global_status_deleted_at",
        "application_resume_id",
        "application_resume_filename",
            "expected_salary",
            "offer_salary",
            "manual_posted_at",
            "manual_company_size",
            "manual_company_industry",
            "resume_matches",
            "user_status_history",
        }
    assert set(dumped_occurrence) == set(SourceOccurrence.model_fields)
    assert "source_job_key" not in dumped_occurrence
    assert "source_occurrence_key" not in dumped_occurrence

    restored = Snapshot.model_validate(dumped)
    restored_occurrence = restored.jobs[0].source_occurrences[0]
    assert restored == snapshot
    assert restored_occurrence.source_job_key == "linkedin:acme/jobs:canonical-1"
    assert restored_occurrence.source_occurrence_key == (
        "linkedin:acme/jobs:canonical-1@2"
    )


def test_snapshot_round_trip_preserves_company_industry_evidence_and_source_locator() -> None:
    item = occurrence(
        company_industry_source={
            "source_name": "linkedin",
            "lookup_url": "https://www.linkedin.com/jobs/view/42",
            "public_url": "https://www.linkedin.com/jobs/view/42",
            "source_title": "LinkedIn company profile",
        }
    )
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=1),
        jobs=[
            job(
                "canonical-industry",
                item,
                company_industry={
                    "company_name": "Acme",
                    "industry": "Industrial Automation",
                    "source_url": "https://www.linkedin.com/company/acme/about/",
                    "source_title": "LinkedIn company profile",
                    "checked_at": OBSERVED_AT,
                    "confidence": "high",
                    "lookup_method": "native",
                    "source_name": "linkedin",
                    "evidence": [],
                },
            )
        ],
    )

    dumped = snapshot.model_dump(mode="json", round_trip=True)

    dumped_job = dumped["jobs"][0]
    dumped_occurrence = dumped_job["source_occurrences"][0]
    assert dumped_job["company_industry"]["industry"] == "Industrial Automation"
    assert dumped_job["company_industry"]["lookup_method"] == "native"
    assert dumped_occurrence["company_industry_source"]["source_name"] == "linkedin"
    assert Snapshot.model_validate(dumped) == snapshot


def test_duplicate_evidence_round_trip_keeps_optional_decision_occurrence_key() -> None:
    evidence = duplicate_evidence(
        reason="candidate_conflict",
        decision_source_occurrence_key="arbeitsagentur:default:A@1",
    )

    dumped = evidence.model_dump(mode="json", round_trip=True)

    assert dumped["decision_source_occurrence_key"] == "arbeitsagentur:default:A@1"
    assert DuplicateEvidence.model_validate(dumped) == evidence


def test_duplicate_evidence_accepts_legacy_data_without_decision_occurrence_key() -> None:
    legacy = duplicate_evidence(reason="candidate_conflict").model_dump(
        mode="json",
        round_trip=True,
    )
    legacy.pop("decision_source_occurrence_key", None)

    restored = DuplicateEvidence.model_validate(legacy)

    assert restored.decision_source_occurrence_key is None


def test_domain_enums_accept_only_contract_values() -> None:
    assert {item.value for item in SourceKind} == {
        "arbeitsagentur",
        "bosch",
        "dallmeier",
        "dhl",
        "glassdoor",
        "indeed",
        "linkedin",
        "manual",
        "siemens",
        "simplify",
        "smartrecruiters",
        "stepstone",
        "successfactors",
        "telekom",
        "thyssenkrupp",
    }
    assert {item.value for item in MachineStatus} == {
        "pending_source",
        "pending",
        "eligible",
        "excluded",
        "uncertain",
    }
    assert {item.value for item in UserStatus} == {
        "new",
        "saved",
        "applied",
        "interviewing",
        "offer",
        "withdrawn",
        "rejected",
        "ignored",
    }
    assert {item.value for item in AvailabilityStatus} == {"active", "stale", "closed"}
    assert {item.value for item in PrimaryView} == {
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


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (merge_evidence, "similarity", 0.0),
        (merge_evidence, "similarity", 1.0),
        (duplicate_evidence, "similarity", 0.0),
        (duplicate_evidence, "similarity", 1.0),
        (review, "score", 0),
        (review, "score", 100),
        (scored_job, "score", 0),
        (scored_job, "score", 100),
    ],
)
def test_numeric_contracts_accept_inclusive_endpoints(
    factory: Any, field: str, value: float
) -> None:
    model: BaseModel = factory(value)

    assert getattr(model, field) == value


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (merge_evidence, -0.01),
        (merge_evidence, 1.01),
        (duplicate_evidence, -0.01),
        (duplicate_evidence, 1.01),
        (review, -1),
        (review, 101),
        (scored_job, -1),
        (scored_job, 101),
    ],
)
def test_numeric_contracts_reject_values_beyond_each_boundary(
    factory: Any, value: float
) -> None:
    with pytest.raises(ValidationError):
        factory(value)


@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (merge_evidence, "rule", "fuzzy_match"),
        (duplicate_evidence, "reason", "same_title"),
        (availability_event, "reason", "unknown"),
        (review_history_entry, "outcome", "skipped"),
        (lambda **updates: job("canonical-1", **updates), "manual_override", "hide"),
        (review, "german_requirement", "preferred"),
        (review, "visa_sponsorship", "maybe"),
        (review, "existing_work_authorization", "maybe"),
        (review, "citizenship_requirement", "eu_only"),
        (review, "security_clearance", "maybe"),
        (review, "staffing_agency", "maybe"),
        (review, "confidence", "certain"),
    ],
)
def test_models_reject_values_outside_schema_literals(
    factory: Any, field: str, value: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        factory(**{field: value})

    errors = caught.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == (field,)
    assert errors[0]["type"] == "literal_error"


def test_list_defaults_are_independent_between_models() -> None:
    first_occurrence = occurrence()
    second_occurrence = occurrence("REQ-43")
    first_occurrence.availability_events.append(
        AvailabilityEvent(status="active", reason="listed", observed_at=OBSERVED_AT)
    )
    first_occurrence.merge_evidence.append(
        MergeEvidence(
            other_source_occurrence_key=second_occurrence.source_occurrence_key,
            rule="job_url",
            normalized_url="https://acme.example/job/REQ-43",
            normalized_company="acme",
            normalized_title="backend engineer",
            normalized_location="berlin",
            observed_at=OBSERVED_AT,
        )
    )

    first_job = job("canonical-1", first_occurrence)
    second_job = job("canonical-2", second_occurrence)
    first_job.labels.append("python")
    first_job.exclusion_reasons.append("German required")
    first_job.possible_duplicates.append(
        DuplicateEvidence(
            other_canonical_job_key="canonical-2",
            reason="candidate_conflict",
            observed_at=OBSERVED_AT,
        )
    )

    assert second_occurrence.availability_events == []
    assert second_occurrence.merge_evidence == []
    assert second_job.labels == []
    assert second_job.exclusion_reasons == []
    assert second_job.possible_duplicates == []
    assert review(eligibility_evidence=[]).eligibility_evidence == []


def test_job_record_applies_independent_status_defaults() -> None:
    item = job("canonical-1")

    assert item.record_type == "job"
    assert item.machine_status is MachineStatus.PENDING
    assert item.user_status is UserStatus.NEW


@pytest.mark.parametrize("legacy_status", ["reviewed", "shortlisted"])
def test_job_record_migrates_legacy_saved_statuses(legacy_status: str) -> None:
    legacy = job("canonical-1").model_dump(mode="json")
    legacy["user_status"] = legacy_status

    item = JobRecord.model_validate(legacy)

    assert item.user_status is UserStatus.SAVED


def test_snapshot_rejects_duplicate_canonical_keys() -> None:
    with pytest.raises(ValidationError):
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[job("x"), job("x")])


def test_snapshot_rejects_duplicate_occurrence_keys_across_jobs() -> None:
    shared_identity = occurrence("REQ-42")

    with pytest.raises(ValidationError):
        Snapshot(
            meta=StoreMeta(data_revision=1),
            jobs=[job("canonical-1", shared_identity), job("canonical-2", shared_identity)],
        )


def test_store_meta_and_snapshot_have_jsonl_record_contract_defaults() -> None:
    snapshot = Snapshot(meta=StoreMeta(data_revision=1))

    assert snapshot.meta.model_dump() == {
        "record_type": "meta",
        "data_revision": 1,
        "generated_at": None,
    }
    assert snapshot.jobs == []
