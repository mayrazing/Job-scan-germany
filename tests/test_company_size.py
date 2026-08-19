from __future__ import annotations

import importlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import HttpUrl, ValidationError

from job_scan.anthropic_api import AnthropicApiResponseError
from job_scan.claude_process import ClaudeInvocation, ClaudeRequest, ClaudeTimeout
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import (
    AIReview,
    AvailabilityStatus,
    CompanySizeSource,
    JobRecord,
    MachineStatus,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
)

NOW = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


def company_size_module() -> ModuleType:
    try:
        return importlib.import_module("job_scan.company_size")
    except ModuleNotFoundError:
        pytest.fail("job_scan.company_size must provide company-level lookup and caching")


def config(minimum: int = 250, *, thinking_enabled: bool = True) -> AppConfig:
    return AppConfig.model_validate(
        {
            "resume_path": "/tmp/resume.pdf",
            "resume_sha256": "sha256:" + "a" * 64,
            "profile_sha256": "sha256:" + "b" * 64,
            "search_terms": ["backend"],
            "locations": ["Berlin"],
            "minimum_company_size": minimum,
            "german_level": "B1",
            "claude": ClaudeSettings(
                model="sonnet",
                effort="medium",
                thinking_enabled=thinking_enabled,
            ),
            "scheduler": SchedulerSettings(),
        }
    )


def review() -> AIReview:
    return AIReview(
        job_key="job",
        german_requirement="none",
        visa_sponsorship="offered",
        existing_work_authorization="not_required",
        citizenship_requirement="none",
        security_clearance="none",
        staffing_agency="no",
        company_industry=None,
        company_industry_confidence="low",
        company_industry_evidence=[],
        score=85,
        reason="Strong match.",
        confidence="high",
    )


def job(key: str, company: str = "Acme GmbH") -> JobRecord:
    occurrence = SourceOccurrence(
        source=SourceKind.LINKEDIN,
        source_instance="acme/jobs",
        external_id=key,
        source_generation=1,
        url=HttpUrl(f"https://jobs.example/{key}"),
        company=company,
        title=f"Role {key}",
        location="Berlin",
        description="Complete backend job description.",
        posted_at=date(2026, 8, 1),
        content_hash=f"hash-{key}",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
    )
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company=company,
        title=occurrence.title,
        location=occurrence.location,
        url=occurrence.url,
        description=occurrence.description,
        posted_at=occurrence.posted_at,
        content_hash=occurrence.content_hash,
        first_seen=NOW,
        last_seen=NOW,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=MachineStatus.ELIGIBLE,
        user_status_updated_at=NOW,
        ai_review=review().model_copy(update={"job_key": key}),
    )


class FakeLookup:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []
        self.locations: list[str | None] = []

    def lookup(
        self,
        company: str,
        _config: AppConfig,
        _checked_at: datetime,
        *,
        location: str | None = None,
    ) -> object:
        self.calls.append(company)
        self.locations.append(location)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeNativeLookup:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    def lookup(
        self,
        current: JobRecord,
        _config: AppConfig,
        _checked_at: datetime,
    ) -> object:
        self.calls.append(current.company)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def evidence(
    *,
    checked_at: datetime = NOW,
    band: str = "50-249",
    **overrides: object,
) -> object:
    module = company_size_module()
    counts = {
        "1-49": 25,
        "50-249": 120,
        "250-999": 500,
        "1000-9999": 4200,
        "10000+": 110000,
        "unknown": None,
    }
    values: dict[str, object] = {
        "company_name": "Acme GmbH",
        "band": band,
        "employee_count": counts[band],
        "source_url": (
            "https://www.acme.example/about" if band != "unknown" else None
        ),
        "source_title": "Acme company facts" if band != "unknown" else None,
        "checked_at": checked_at,
        "confidence": "high" if band != "unknown" else "low",
    }
    values.update(overrides)
    return module.CompanySizeEvidence.model_validate(values)


def service(
    tmp_path: Path,
    lookup: FakeLookup,
    native_lookup: FakeNativeLookup | None = None,
) -> object:
    module = company_size_module()
    return module.CompanySizeService(
        module.CompanySizeStore(tmp_path / "company-sizes.json"),
        lookup,
        native_lookup=native_lookup,
    )


def test_queries_one_unique_company_and_filters_every_matching_job(
    tmp_path: Path,
) -> None:
    lookup = FakeLookup(evidence())
    evaluator = service(tmp_path, lookup)
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one"), job("two")])

    evaluator.restore(snapshot, config(250))
    evaluator.apply(snapshot, config(250), NOW)

    assert lookup.calls == ["Acme GmbH"]
    assert {item.company_size.band for item in snapshot.jobs} == {"50-249"}
    assert {item.machine_status for item in snapshot.jobs} == {MachineStatus.EXCLUDED}
    assert all("company_too_small" in item.exclusion_reasons for item in snapshot.jobs)


def test_apply_reports_completed_unique_companies(tmp_path: Path) -> None:
    module = company_size_module()
    evaluator = service(tmp_path, FakeLookup(evidence()))
    progress: list[object] = []

    evaluator.apply(
        Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one"), job("two")]),
        config(),
        NOW,
        progress=progress.append,
    )

    assert progress == [
        module.CompanySizeProgress(completed_companies=0, total_companies=1),
        module.CompanySizeProgress(completed_companies=1, total_companies=1),
    ]


def test_completed_company_is_cached_before_a_later_lookup_fails(
    tmp_path: Path,
) -> None:
    module = company_size_module()
    cache = module.CompanySizeStore(tmp_path / "company-sizes.json")

    class FailingSecondLookup:
        def lookup(
            self,
            company: str,
            _config: AppConfig,
            _checked_at: datetime,
            *,
            location: str | None = None,
        ) -> object:
            del location
            if company == "Beta GmbH":
                raise RuntimeError("second lookup failed")
            return evidence(company_name=company)

    evaluator = module.CompanySizeService(cache, FailingSecondLookup())
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=0),
        jobs=[job("one"), job("two", "Beta GmbH")],
    )

    with pytest.raises(RuntimeError, match="second lookup failed"):
        evaluator.apply(snapshot, config(), NOW)

    assert list(cache.load()) == ["acme gmbh"]


def test_automatic_ai_lookup_uses_first_non_empty_job_location(
    tmp_path: Path,
) -> None:
    lookup = FakeLookup(evidence())
    evaluator = service(tmp_path, lookup)
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=0),
        jobs=[
            job("one").model_copy(update={"location": ""}),
            job("two").model_copy(update={"location": "22085 Hamburg"}),
        ],
    )

    evaluator.apply(snapshot, config(), NOW)

    assert lookup.calls == ["Acme GmbH"]
    assert lookup.locations == ["22085 Hamburg"]


def test_any_size_queries_and_enriches_without_filtering(tmp_path: Path) -> None:
    lookup = FakeLookup(evidence(band="1-49"))
    native = FakeNativeLookup(None)
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, lookup, native).apply(snapshot, config(0), NOW)

    assert native.calls == ["Acme GmbH"]
    assert lookup.calls == ["Acme GmbH"]
    assert snapshot.jobs[0].company_size.band == "1-49"
    assert snapshot.jobs[0].machine_status is MachineStatus.ELIGIBLE
    assert "company_too_small" not in snapshot.jobs[0].exclusion_reasons


def test_fresh_company_result_is_reused_without_another_ai_call(tmp_path: Path) -> None:
    first_lookup = FakeLookup(evidence())
    first = service(tmp_path, first_lookup)
    first.apply(Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")]), config(), NOW)
    second_lookup = FakeLookup(RuntimeError("must not be called"))
    second = service(tmp_path, second_lookup)
    later = NOW + timedelta(days=89)
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("two")])

    second.apply(snapshot, config(), later)

    assert second_lookup.calls == []
    assert snapshot.jobs[0].company_size.checked_at == NOW


def test_manual_refresh_bypasses_unknown_cache_and_updates_matching_company(
    tmp_path: Path,
) -> None:
    module = company_size_module()
    cache = module.CompanySizeStore(tmp_path / "company-sizes.json")
    cache.save({"acme gmbh": evidence(band="unknown")})
    lookup = FakeLookup(evidence(band="1000-9999", employee_count=4200))
    evaluator = module.CompanySizeService(cache, lookup)
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=0),
        jobs=[job("one"), job("two"), job("other", "Other GmbH")],
    )

    result = evaluator.lookup_for_job(snapshot.jobs[0], config(), NOW)
    evaluator.apply_refreshed(snapshot, "one", result, config())

    assert lookup.calls == ["Acme GmbH"]
    assert lookup.locations == ["Berlin"]
    assert [item.company_size.band for item in snapshot.jobs[:2]] == [
        "1000-9999",
        "1000-9999",
    ]
    assert snapshot.jobs[2].company_size is None
    assert cache.load()["acme gmbh"].band == "1000-9999"


def test_manual_refresh_rejects_unverified_result_without_replacing_cache(
    tmp_path: Path,
) -> None:
    module = company_size_module()
    cached = evidence(band="50-249")
    cache = module.CompanySizeStore(tmp_path / "company-sizes.json")
    cache.save({"acme gmbh": cached})
    evaluator = module.CompanySizeService(
        cache,
        FakeLookup(evidence(band="unknown", checked_at=NOW + timedelta(days=1))),
    )
    current = job("one")

    with pytest.raises(
        module.CompanySizeLookupError,
        match="No reliable employee-count source was found.",
    ):
        evaluator.lookup_for_job(current, config(), NOW + timedelta(days=1))

    assert current.company_size is None
    assert cache.load()["acme gmbh"] == cached


def test_company_result_is_still_fresh_at_exactly_ninety_days(tmp_path: Path) -> None:
    first = service(tmp_path, FakeLookup(evidence()))
    first.apply(Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")]), config(), NOW)
    lookup = FakeLookup(RuntimeError("must not be called"))
    second = service(tmp_path, lookup)
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("two")])

    second.apply(snapshot, config(), NOW + timedelta(days=90))

    assert lookup.calls == []
    assert snapshot.jobs[0].company_size.checked_at == NOW


def test_company_result_is_refreshed_after_ninety_days(tmp_path: Path) -> None:
    first = service(tmp_path, FakeLookup(evidence()))
    first.apply(Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")]), config(), NOW)
    refreshed = evidence(checked_at=NOW + timedelta(days=91), band="250-999")
    lookup = FakeLookup(refreshed)
    second = service(tmp_path, lookup)
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("two")])

    second.apply(snapshot, config(), NOW + timedelta(days=91))

    assert lookup.calls == ["Acme GmbH"]
    assert snapshot.jobs[0].company_size.band == "250-999"
    assert snapshot.jobs[0].machine_status is MachineStatus.ELIGIBLE


def test_unknown_company_size_does_not_filter_the_job(tmp_path: Path) -> None:
    lookup = FakeLookup(evidence(band="unknown"))
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, lookup).apply(snapshot, config(10000), NOW)

    assert snapshot.jobs[0].machine_status is MachineStatus.ELIGIBLE
    assert snapshot.jobs[0].company_size.band == "unknown"
    assert "company_too_small" not in snapshot.jobs[0].exclusion_reasons


def test_native_company_range_is_used_before_ai_lookup(tmp_path: Path) -> None:
    native = FakeNativeLookup(
        evidence(
            band="unknown",
            reported_size="1000+",
            minimum_employees=1000,
            maximum_employees=None,
            source_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/123",
            source_title="Arbeitsagentur · Betriebsgröße",
            lookup_method="native",
            source_name="arbeitsagentur",
            confidence="high",
        )
    )
    ai = FakeLookup(RuntimeError("AI must not be called"))
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, ai, native).apply(snapshot, config(1000), NOW)

    assert native.calls == ["Acme GmbH"]
    assert ai.calls == []
    assert snapshot.jobs[0].company_size.reported_size == "1000+"
    assert snapshot.jobs[0].machine_status is MachineStatus.ELIGIBLE


@pytest.mark.parametrize(
    "machine_status",
    [MachineStatus.ELIGIBLE, MachineStatus.UNCERTAIN],
)
def test_missing_native_company_range_falls_back_to_ai_for_non_excluded_job(
    tmp_path: Path,
    machine_status: MachineStatus,
) -> None:
    native = FakeNativeLookup(None)
    ai = FakeLookup(evidence(band="1000-9999"))
    current = job("one")
    current.machine_status = machine_status
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[current])

    service(tmp_path, ai, native).apply(snapshot, config(1000), NOW)

    assert native.calls == ["Acme GmbH"]
    assert ai.calls == ["Acme GmbH"]
    assert snapshot.jobs[0].company_size.band == "1000-9999"


def test_excluded_job_does_not_trigger_ai_company_size_lookup(tmp_path: Path) -> None:
    native = FakeNativeLookup(None)
    ai = FakeLookup(RuntimeError("AI must not be called for excluded jobs"))
    current = job("one")
    current.machine_status = MachineStatus.EXCLUDED
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[current])

    service(tmp_path, ai, native).apply(snapshot, config(1000), NOW)

    assert ai.calls == []


def test_same_company_uses_native_source_from_any_candidate_job(tmp_path: Path) -> None:
    native_result = evidence(
        band="unknown",
        reported_size="1000+",
        minimum_employees=1000,
        maximum_employees=None,
        source_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/two",
        source_title="Arbeitsagentur · Betriebsgröße",
        lookup_method="native",
        source_name="arbeitsagentur",
        confidence="high",
    )

    class PerJobNativeLookup:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def lookup(
            self,
            current: JobRecord,
            _config: AppConfig,
            _checked_at: datetime,
        ) -> object | None:
            self.calls.append(current.canonical_job_key)
            return native_result if current.canonical_job_key == "two" else None

    native = PerJobNativeLookup()
    ai = FakeLookup(RuntimeError("AI must not be called"))
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=0),
        jobs=[job("one"), job("two")],
    )
    module = company_size_module()
    evaluator = module.CompanySizeService(
        module.CompanySizeStore(tmp_path / "company-sizes.json"),
        ai,
        native_lookup=native,
    )

    evaluator.apply(snapshot, config(1000), NOW)

    assert native.calls == ["one", "two"]
    assert ai.calls == []
    assert {item.company_size.reported_size for item in snapshot.jobs} == {"1000+"}


def test_company_range_crossing_minimum_is_kept_for_manual_verification(
    tmp_path: Path,
) -> None:
    native = FakeNativeLookup(
        evidence(
            band="unknown",
            reported_size="599-2000",
            minimum_employees=599,
            maximum_employees=2000,
            source_url="https://company.example/about",
            source_title="Company profile",
            lookup_method="native",
            source_name="indeed",
            confidence="high",
        )
    )
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, FakeLookup(RuntimeError("must not run")), native).apply(
        snapshot,
        config(1000),
        NOW,
    )

    current = snapshot.jobs[0]
    assert current.machine_status is MachineStatus.ELIGIBLE
    assert "company_too_small" not in current.exclusion_reasons
    assert "Company size crosses configured minimum" in current.labels


def test_company_range_entirely_below_minimum_is_excluded(tmp_path: Path) -> None:
    native = FakeNativeLookup(
        evidence(
            band="unknown",
            reported_size="250-999",
            minimum_employees=250,
            maximum_employees=999,
            source_url="https://company.example/about",
            source_title="Company profile",
            lookup_method="native",
            source_name="linkedin",
            confidence="high",
        )
    )
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, FakeLookup(RuntimeError("must not run")), native).apply(
        snapshot,
        config(1000),
        NOW,
    )

    assert snapshot.jobs[0].machine_status is MachineStatus.EXCLUDED
    assert "company_too_small" in snapshot.jobs[0].exclusion_reasons


def test_open_ended_range_starting_below_minimum_is_kept_for_verification(
    tmp_path: Path,
) -> None:
    native = FakeNativeLookup(
        evidence(
            band="unknown",
            reported_size="1000+",
            minimum_employees=1000,
            maximum_employees=None,
            source_url="https://company.example/about",
            source_title="Company profile",
            lookup_method="native",
            source_name="linkedin",
            confidence="high",
        )
    )
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, FakeLookup(RuntimeError("must not run")), native).apply(
        snapshot,
        config(10000),
        NOW,
    )

    current = snapshot.jobs[0]
    assert current.machine_status is MachineStatus.ELIGIBLE
    assert "company_too_small" not in current.exclusion_reasons
    assert "Company size crosses configured minimum" in current.labels


def test_legacy_ai_cache_is_replaced_by_native_source_result(tmp_path: Path) -> None:
    cache_path = tmp_path / "company-sizes.json"
    cache_path.write_text(
        json.dumps(
            {
                "entries": [
                    evidence(band="1000-9999").model_dump(mode="json")
                ]
            }
        ),
        encoding="utf-8",
    )
    native = FakeNativeLookup(
        evidence(
            band="unknown",
            reported_size="1000+",
            minimum_employees=1000,
            maximum_employees=None,
            source_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/123",
            source_title="Arbeitsagentur · Betriebsgröße",
            lookup_method="native",
            source_name="arbeitsagentur",
            confidence="high",
        )
    )
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, FakeLookup(RuntimeError("must not run")), native).apply(
        snapshot,
        config(1000),
        NOW,
    )

    assert native.calls == ["Acme GmbH"]
    assert snapshot.jobs[0].company_size.reported_size == "1000+"


@pytest.mark.parametrize(
    ("reported_size", "minimum", "maximum"),
    [
        ("1000+", 1000, None),
        ("10,001+ employees", 10001, None),
        ("5.001 bis 10.000", 5001, 10000),
        ("599-2000", 599, 2000),
    ],
)
def test_native_company_size_preserves_source_text_and_parses_bounds(
    reported_size: str,
    minimum: int,
    maximum: int | None,
) -> None:
    module = company_size_module()

    result = module.native_company_size_evidence(
        company="Acme GmbH",
        reported_size=reported_size,
        source_url="https://company.example/about",
        source_title="Company profile",
        source_name="linkedin",
        checked_at=NOW,
    )

    assert result is not None
    assert result.band == "unknown"
    assert result.reported_size == reported_size
    assert result.minimum_employees == minimum
    assert result.maximum_employees == maximum
    assert result.lookup_method == "native"


def test_unparseable_native_company_size_returns_no_evidence() -> None:
    module = company_size_module()

    result = module.native_company_size_evidence(
        company="Acme GmbH",
        reported_size="Size not disclosed",
        source_url="https://company.example/about",
        source_title="Company profile",
        source_name="indeed",
        checked_at=NOW,
    )

    assert result is None


def test_source_native_lookup_routes_arbeitsagentur_profile_to_official_api() -> None:
    module = company_size_module()

    class FakeHttpClient:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get_json(self, url: str, *, headers: object) -> dict[str, str]:
            self.urls.append(url)
            assert headers == {"X-API-Key": "jobboerse-jobsuche"}
            return {"betriebsgroesse": "1000+"}

    client = FakeHttpClient()
    current = job("one", "JetBrains GmbH")
    occurrence = current.source_occurrences[0]
    occurrence.source = SourceKind.ARBEITSAGENTUR
    occurrence.source_instance = "default"
    occurrence.company_size_source = CompanySizeSource(
        source_name="arbeitsagentur",
        lookup_url=(
            "https://rest.arbeitsagentur.de/vermittlung/"
            "ag-darstellung-service/pc/v1/arbeitgeberdarstellung/hash"
        ),
        public_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/one",
        source_title="Arbeitsagentur · Betriebsgröße",
    )

    result = module.SourceNativeCompanySizeLookup(client).lookup(
        current,
        config(1000),
        NOW,
    )

    assert client.urls == [str(occurrence.company_size_source.lookup_url)]
    assert result is not None
    assert result.reported_size == "1000+"
    assert result.source_name == "arbeitsagentur"


def test_source_collection_opens_one_company_profile_for_multiple_same_company_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = company_size_module()
    from job_scan.sources import linkedin

    calls: list[str] = []

    def missing_facts(external_id: str, *_args: object, **_kwargs: object) -> object:
        calls.append(external_id)
        return module.CompanyProfileFacts()

    monkeypatch.setattr(linkedin, "lookup_company_facts", missing_facts)
    evaluator = module.CompanySizeService(
        module.CompanySizeStore(tmp_path / "company-sizes.json"),
        FakeLookup(RuntimeError("AI is not part of source collection")),
        native_lookup=module.SourceNativeCompanySizeLookup(object()),
    )
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=0),
        jobs=[job("111"), job("222")],
    )

    evaluator.collect_native(snapshot, config(), NOW)

    assert calls == ["111"]


def test_source_native_lookup_routes_stepstone_profile_to_stepstone_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = company_size_module()
    from job_scan.sources import stepstone

    current = job("14358591", "Example GmbH")
    occurrence = current.source_occurrences[0]
    occurrence.source = SourceKind.STEPSTONE
    occurrence.source_instance = "de"
    occurrence.external_id = "14358591"
    occurrence.company_size_source = CompanySizeSource(
        source_name="stepstone",
        lookup_url="https://www.stepstone.de/cmp/de/example-gmbh-12345/jobs",
        public_url="https://www.stepstone.de/cmp/de/example-gmbh-12345/jobs",
        source_title="StepStone company profile",
    )
    calls: list[tuple[CompanySizeSource, str, datetime]] = []

    def fake_lookup(
        source: CompanySizeSource,
        company: str,
        checked_at: datetime,
        **_kwargs: object,
    ) -> object:
        calls.append((source, company, checked_at))
        return module.CompanyProfileFacts(
            company_size=module.native_company_size_evidence(
                company=company,
                reported_size="51-250 Mitarbeiter",
                source_url=str(source.public_url),
                source_title=source.source_title,
                source_name="stepstone",
                checked_at=checked_at,
            )
        )

    monkeypatch.setattr(stepstone, "lookup_company_facts", fake_lookup)

    result = module.SourceNativeCompanySizeLookup(object()).lookup(
        current,
        config(250),
        NOW,
    )

    assert [(company, checked_at) for _source, company, checked_at in calls] == [
        ("Example GmbH", NOW)
    ]
    assert result is not None
    assert result.reported_size == "51-250 Mitarbeiter"
    assert result.source_name == "stepstone"


def test_source_native_lookup_routes_simplify_range_to_simplify_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = company_size_module()
    from job_scan.sources import simplify

    current = job("6921622b-85e7-4281-9339-1cfde1d0e877", "Example GmbH")
    occurrence = current.source_occurrences[0]
    occurrence.source = SourceKind.SIMPLIFY
    occurrence.source_instance = "de"
    occurrence.external_id = "6921622b-85e7-4281-9339-1cfde1d0e877"
    occurrence.company_size_source = CompanySizeSource(
        source_name="simplify",
        lookup_url=(
            "https://api.simplify.jobs/v2/job-posting/:id/"
            "6921622b-85e7-4281-9339-1cfde1d0e877/company"
        ),
        public_url=(
            "https://simplify.jobs/jobs?jobId="
            "6921622b-85e7-4281-9339-1cfde1d0e877"
        ),
        source_title="Simplify job posting",
        reported_size="51-200",
    )
    calls: list[tuple[CompanySizeSource, str, datetime]] = []

    def fake_lookup(
        source: CompanySizeSource,
        company: str,
        checked_at: datetime,
    ) -> object:
        calls.append((source, company, checked_at))
        return module.native_company_size_evidence(
            company=company,
            reported_size="51-200",
            source_url=str(source.public_url),
            source_title=source.source_title,
            source_name="simplify",
            checked_at=checked_at,
        )

    monkeypatch.setattr(simplify, "lookup_company_size", fake_lookup)

    result = module.SourceNativeCompanySizeLookup(object()).lookup(
        current,
        config(50),
        NOW,
    )

    assert [(company, checked_at) for _source, company, checked_at in calls] == [
        ("Example GmbH", NOW)
    ]
    assert result is not None
    assert result.reported_size == "51-200"
    assert result.source_name == "simplify"


def test_source_native_lookup_routes_glassdoor_profile_to_glassdoor_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = company_size_module()
    from job_scan.sources import glassdoor

    current = job("1010138743368", "Example GmbH")
    occurrence = current.source_occurrences[0]
    occurrence.source = SourceKind.GLASSDOOR
    occurrence.source_instance = "de"
    occurrence.external_id = "1010138743368"
    occurrence.company_size_source = CompanySizeSource(
        source_name="glassdoor",
        lookup_url=(
            "https://www.glassdoor.de/%C3%9Cberblick/"
            "Arbeit-bei-Example-GmbH-EI_IE12345.11,23.htm"
        ),
        public_url=(
            "https://www.glassdoor.de/%C3%9Cberblick/"
            "Arbeit-bei-Example-GmbH-EI_IE12345.11,23.htm"
        ),
        source_title="Glassdoor company profile",
    )
    calls: list[tuple[CompanySizeSource, str, datetime]] = []

    def fake_lookup(
        source: CompanySizeSource,
        company: str,
        checked_at: datetime,
        **_kwargs: object,
    ) -> object:
        calls.append((source, company, checked_at))
        return module.CompanyProfileFacts(
            company_size=module.native_company_size_evidence(
                company=company,
                reported_size="10000+ Mitarbeiter",
                source_url=str(source.public_url),
                source_title=source.source_title,
                source_name="glassdoor",
                checked_at=checked_at,
            )
        )

    monkeypatch.setattr(glassdoor, "lookup_company_facts", fake_lookup)

    result = module.SourceNativeCompanySizeLookup(object()).lookup(
        current,
        config(10000),
        NOW,
    )

    assert [(company, checked_at) for _source, company, checked_at in calls] == [
        ("Example GmbH", NOW)
    ]
    assert result is not None
    assert result.reported_size == "10000+ Mitarbeiter"
    assert result.source_name == "glassdoor"


@pytest.mark.parametrize(
    ("source", "enabled_field"),
    [
        (SourceKind.LINKEDIN, "linkedin_enabled"),
        (SourceKind.INDEED, "indeed_de_enabled"),
        (SourceKind.STEPSTONE, "stepstone_de_enabled"),
        (SourceKind.GLASSDOOR, "glassdoor_de_enabled"),
        (SourceKind.SIMPLIFY, "simplify_de_enabled"),
    ],
)
def test_source_native_lookup_skips_disabled_opencli_source(
    monkeypatch: pytest.MonkeyPatch,
    source: SourceKind,
    enabled_field: str,
) -> None:
    module = company_size_module()
    current = job("disabled-source", "Example GmbH")
    current.source_occurrences[0].source = source
    calls: list[SourceKind] = []

    def record_lookup(
        _self: object,
        _company: str,
        occurrence: SourceOccurrence,
        _checked_at: datetime,
    ) -> object:
        calls.append(occurrence.source)
        return None

    monkeypatch.setattr(
        module.SourceNativeCompanySizeLookup,
        "_lookup_occurrence",
        record_lookup,
    )
    disabled_config = config().model_copy(update={enabled_field: False})

    result = module.SourceNativeCompanySizeLookup(object()).lookup(
        current,
        disabled_config,
        NOW,
    )

    assert result is None
    assert calls == []


def test_lookup_failure_marks_unknown_and_reuses_it_without_failing(tmp_path: Path) -> None:
    module = company_size_module()
    lookup = FakeLookup(module.CompanySizeLookupError("search failed"))
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("one")])

    service(tmp_path, lookup).apply(snapshot, config(), NOW)

    assert snapshot.jobs[0].company_size.band == "unknown"
    assert snapshot.jobs[0].machine_status is MachineStatus.ELIGIBLE
    assert (tmp_path / "company-sizes.json").exists()
    second_lookup = FakeLookup(module.CompanySizeLookupError("must not be called"))
    second_snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[job("two")])

    service(tmp_path, second_lookup).apply(
        second_snapshot,
        config(),
        NOW + timedelta(days=1),
    )

    assert second_lookup.calls == []
    assert second_snapshot.jobs[0].company_size.band == "unknown"


def test_restore_removes_old_company_filter_when_setting_is_relaxed(
    tmp_path: Path,
) -> None:
    current = job("one")
    current.machine_status = MachineStatus.EXCLUDED
    current.exclusion_reasons = ["company_too_small"]
    snapshot = Snapshot(meta=StoreMeta(data_revision=0), jobs=[current])

    service(tmp_path, FakeLookup(evidence())).restore(snapshot, config())

    assert current.machine_status is MachineStatus.ELIGIBLE
    assert current.exclusion_reasons == []


class RecordingInvoker:
    def __init__(self) -> None:
        self.requests: list[ClaudeRequest] = []

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        self.requests.append(request)
        return ClaudeInvocation(
            argv=["fake"],
            stdout=json.dumps(
                {
                    "structured_output": {
                        "band": "1000-9999",
                        "employee_count": 4200,
                        "source_url": "https://www.acme.example/report",
                        "source_title": "Annual report",
                        "confidence": "high",
                    }
                }
            ).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.1,
        )


class FailingInvoker:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def invoke(self, _request: ClaudeRequest) -> ClaudeInvocation:
        raise self.error


class InvalidResultInvoker:
    def invoke(self, _request: ClaudeRequest) -> ClaudeInvocation:
        return ClaudeInvocation(
            argv=["fake"],
            stdout=json.dumps(
                {
                    "structured_output": {
                        "band": "1-49",
                        "employee_count": 4200,
                        "source_url": "https://www.acme.example/report",
                        "source_title": "Annual report",
                        "confidence": "high",
                    }
                }
            ).encode(),
            stderr=b"",
            exit_code=0,
            duration_seconds=0.1,
        )


def test_ai_lookup_requests_web_search_and_requires_a_source() -> None:
    module = company_size_module()
    invoker = RecordingInvoker()
    lookup = module.AiCompanySizeLookup(invoker)

    result = lookup.lookup("Acme GmbH", config(thinking_enabled=False), NOW)

    assert result.band == "1000-9999"
    assert str(result.source_url) == "https://www.acme.example/report"
    assert result.checked_at == NOW
    assert len(invoker.requests) == 1
    assert invoker.requests[0].thinking_enabled is False
    assert invoker.requests[0].allow_web_search is True
    prompt = invoker.requests[0].prompt
    assert "Acme GmbH" in prompt
    assert "current employee-size band of the hiring employer" in prompt
    assert "current official or major job-platform company profile" in prompt
    assert "Never use a parent-group" in prompt
    assert "no more than three web searches and one page fetch" in prompt
    assert "set employee_count to null" in prompt
    assert '"format": "uri"' not in json.dumps(invoker.requests[0].json_schema)
    assert set(invoker.requests[0].json_schema["required"]) == set(
        invoker.requests[0].json_schema["properties"]
    )


def test_ai_lookup_uses_job_location_only_as_company_identity_context() -> None:
    module = company_size_module()
    invoker = RecordingInvoker()
    lookup = module.AiCompanySizeLookup(invoker)

    lookup.lookup(
        "Hanseatic Bank",
        config(),
        NOW,
        location="22085 Hamburg",
    )

    prompt = invoker.requests[0].prompt
    assert "Hanseatic Bank" in prompt
    assert "22085 Hamburg" in prompt
    assert "headquarters or registered address" in prompt


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ClaudeTimeout("late"), "AI search timed out."),
        (
            AnthropicApiResponseError("missing output"),
            "AI searched but did not return a usable result.",
        ),
    ],
)
def test_ai_lookup_reports_specific_safe_failure(
    error: Exception,
    message: str,
) -> None:
    module = company_size_module()
    lookup = module.AiCompanySizeLookup(FailingInvoker(error))

    with pytest.raises(module.CompanySizeLookupError, match=message):
        lookup.lookup("Acme GmbH", config(), NOW)


def test_ai_lookup_reports_unusable_result_when_valid_json_is_inconsistent() -> None:
    module = company_size_module()
    lookup = module.AiCompanySizeLookup(InvalidResultInvoker())

    with pytest.raises(
        module.CompanySizeLookupError,
        match="AI searched but did not return a usable result.",
    ):
        lookup.lookup("Acme GmbH", config(), NOW)


def test_company_size_cache_rejects_timezone_naive_check_time() -> None:
    module = company_size_module()

    with pytest.raises(ValidationError):
        module.CompanySizeEvidence(
            company_name="Acme GmbH",
            band="50-249",
            source_url="https://www.acme.example/about",
            checked_at=NOW.replace(tzinfo=None),
            confidence="high",
        )


def test_company_size_cache_rejects_count_outside_reported_band() -> None:
    module = company_size_module()

    with pytest.raises(ValidationError):
        module.CompanySizeEvidence(
            company_name="Acme GmbH",
            band="10000+",
            employee_count=120,
            source_url="https://www.acme.example/about",
            checked_at=NOW,
            confidence="high",
        )


@pytest.mark.parametrize(
    "source_url",
    [
        "http://public.example/about",
        "https://localhost/about",
        "https://127.0.0.1/internal",
        "https://10.0.0.1/internal",
        "https://user:password@public.example/about",
    ],
)
def test_company_size_cache_rejects_non_public_source_urls(source_url: str) -> None:
    module = company_size_module()

    with pytest.raises(ValidationError):
        module.CompanySizeEvidence(
            company_name="Acme GmbH",
            band="50-249",
            source_url=source_url,
            checked_at=NOW,
            confidence="high",
        )
