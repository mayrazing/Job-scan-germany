from __future__ import annotations

import importlib
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import (
    AvailabilityStatus,
    CompanyIndustryEvidence,
    JobRecord,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    StoreMeta,
)

NOW = datetime(2026, 8, 17, 9, tzinfo=UTC)


def industry_module():
    try:
        return importlib.import_module("job_scan.company_industry")
    except ModuleNotFoundError:
        pytest.fail("job_scan.company_industry must provide source-native industry lookup")


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "resume_path": Path("/tmp/unused.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "search_terms": ["backend"],
        "locations": ["Berlin"],
        "german_level": "A1",
        "claude": ClaudeSettings(model="sonnet", effort="low"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def job(
    key: str,
    company: str,
    *,
    source: dict[str, object] | None = None,
) -> JobRecord:
    occurrence = SourceOccurrence(
        source=(
            SourceKind(str(source["source_name"]))
            if source is not None
            else SourceKind.ARBEITSAGENTUR
        ),
        source_instance="default",
        external_id=key,
        source_generation=1,
        url=f"https://jobs.example/{key}",
        company=company,
        title="Backend Engineer",
        location="Berlin",
        description="Build backend services.",
        posted_at=date(2026, 8, 16),
        content_hash=f"sha256:{key}",
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=True,
        company_industry_source=source,
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
        user_status_updated_at=NOW,
    )


def test_reported_source_industry_applies_to_every_matching_company_and_is_cached(
    tmp_path: Path,
) -> None:
    module = industry_module()
    source = {
        "source_name": "smartrecruiters",
        "lookup_url": "https://api.smartrecruiters.com/v1/companies/Acme/postings/1",
        "public_url": "https://jobs.smartrecruiters.com/Acme/1",
        "source_title": "SmartRecruiters job posting",
        "reported_industry": "Industrial Automation",
    }
    first = job("one", "Acme GmbH", source=source)
    second = job("two", "Acme GmbH")
    unrelated = job("three", "Other GmbH")
    snapshot = Snapshot(
        meta=StoreMeta(data_revision=1),
        jobs=[first, second, unrelated],
    )
    store = module.CompanyIndustryStore(tmp_path / "company-industries.json")
    service = module.CompanyIndustryService(
        store,
        module.SourceNativeCompanyIndustryLookup(opencli_executable="missing-opencli"),
    )

    service.apply(snapshot, config(), NOW)

    assert first.company_industry is not None
    assert first.company_industry.industry == "Industrial Automation"
    assert first.company_industry.lookup_method == "native"
    assert second.company_industry == first.company_industry
    assert unrelated.company_industry is None
    assert store.load()["acme gmbh"].industry == "Industrial Automation"

    cached_only = job("four", "Acme GmbH")
    service.apply(
        Snapshot(meta=StoreMeta(data_revision=2), jobs=[cached_only]),
        config(),
        NOW + timedelta(days=1),
    )
    assert cached_only.company_industry is not None
    assert cached_only.company_industry.industry == "Industrial Automation"


def test_disabled_browser_source_cannot_enrich_from_historical_occurrence(
    tmp_path: Path,
) -> None:
    module = industry_module()
    source = {
        "source_name": "linkedin",
        "lookup_url": "https://www.linkedin.com/jobs/view/4423914728",
        "public_url": "https://www.linkedin.com/jobs/view/4423914728",
        "source_title": "LinkedIn company profile",
        "reported_industry": "Software Development",
    }
    item = job("disabled", "Acme GmbH", source=source)

    module.CompanyIndustryService(
        module.CompanyIndustryStore(tmp_path / "company-industries.json"),
        module.SourceNativeCompanyIndustryLookup(opencli_executable="missing-opencli"),
    ).apply(
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[item]),
        config(linkedin_enabled=False),
        NOW,
    )

    assert item.company_industry is None


def test_existing_stale_industry_is_not_cleaned(
    tmp_path: Path,
) -> None:
    module = industry_module()
    stale = CompanyIndustryEvidence(
        company_name="Acme GmbH",
        industry="Old industry",
        source_url="https://example.test/companies/acme",
        source_title="Old source profile",
        checked_at=NOW - timedelta(days=91),
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )
    item = job("stale", "Acme GmbH")
    item.company_industry = stale
    store = module.CompanyIndustryStore(tmp_path / "company-industries.json")
    store.save({"acme gmbh": stale})

    module.CompanyIndustryService(
        store,
        module.SourceNativeCompanyIndustryLookup(opencli_executable="missing-opencli"),
    ).apply(
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[item]),
        config(),
        NOW,
    )

    assert item.company_industry == stale
    assert store.load() == {"acme gmbh": stale}


def test_fresh_source_industry_replaces_an_older_cached_value(tmp_path: Path) -> None:
    module = industry_module()
    older = CompanyIndustryEvidence(
        company_name="Acme GmbH",
        industry="Old industry",
        source_url="https://example.test/companies/acme",
        source_title="Old source profile",
        checked_at=NOW - timedelta(days=1),
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )
    fresh = older.model_copy(
        update={
            "industry": "Industrial Automation",
            "checked_at": NOW,
            "source_name": "stepstone",
            "source_title": "StepStone company profile",
        }
    )
    item = job("fresh", "Acme GmbH")
    item.company_industry = fresh
    store = module.CompanyIndustryStore(tmp_path / "company-industries.json")
    store.save({"acme gmbh": older})

    module.CompanyIndustryService(
        store,
        module.SourceNativeCompanyIndustryLookup(opencli_executable="missing-opencli"),
    ).apply(
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[item]),
        config(),
        NOW,
    )

    assert item.company_industry == fresh
    assert store.load()["acme gmbh"] == fresh


def test_existing_mismatched_company_industry_is_not_cleaned(tmp_path: Path) -> None:
    module = industry_module()
    item = job("renamed", "New Company GmbH")
    existing = CompanyIndustryEvidence(
        company_name="Old Company GmbH",
        industry="Old industry",
        source_url="https://example.test/companies/old",
        source_title="Old source profile",
        checked_at=NOW,
        confidence="high",
        lookup_method="native",
        source_name="linkedin",
        evidence=[],
    )
    item.company_industry = existing

    module.CompanyIndustryService(
        module.CompanyIndustryStore(tmp_path / "company-industries.json"),
        module.SourceNativeCompanyIndustryLookup(opencli_executable="missing-opencli"),
    ).apply(
        Snapshot(meta=StoreMeta(data_revision=1), jobs=[item]),
        config(),
        NOW,
    )

    assert item.company_industry == existing


def test_company_industry_cache_rejects_jd_specific_ai_evidence(
    tmp_path: Path,
) -> None:
    module = industry_module()
    ai_industry = CompanyIndustryEvidence(
        company_name="Acme GmbH",
        industry="Industrial Automation",
        source_url="https://example.test/jobs/1",
        source_title="AI inference from complete job description",
        checked_at=NOW,
        confidence="medium",
        lookup_method="ai",
        source_name="ai",
        evidence=["We manufacture industrial robots."],
    )
    store = module.CompanyIndustryStore(tmp_path / "company-industries.json")

    with pytest.raises(module.CompanyIndustryStoreError):
        store.save({"acme gmbh": ai_industry})

    assert not (tmp_path / "company-industries.json").exists()


def test_company_profile_locator_without_reported_industry_does_not_open_opencli(
    tmp_path: Path,
) -> None:
    module = industry_module()
    executable = tmp_path / "opencli"
    calls_path = tmp_path / "calls.jsonl"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import pathlib",
                "import sys",
                f"calls = pathlib.Path({str(calls_path)!r})",
                "args = sys.argv[1:]",
                "with calls.open('a', encoding='utf-8') as output:",
                "    output.write(json.dumps(args) + '\\n')",
                "if args[0] != 'browser':",
                "    raise SystemExit(78)",
                "if args[2] == 'eval':",
                "    print(json.dumps({'status': 'ok', 'reported_industry': 'IT & Tech'}))",
                "else:",
                "    print(json.dumps({'status': 'ok'}))",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    source = {
        "source_name": "stepstone",
        "lookup_url": "https://www.stepstone.de/cmp/de/Acme-123/jobs",
        "public_url": "https://www.stepstone.de/cmp/de/Acme-123/jobs",
        "source_title": "StepStone company profile",
    }
    item = job("one", "Acme GmbH", source=source)
    snapshot = Snapshot(meta=StoreMeta(data_revision=1), jobs=[item])
    service = module.CompanyIndustryService(
        module.CompanyIndustryStore(tmp_path / "company-industries.json"),
        module.SourceNativeCompanyIndustryLookup(
            opencli_executable=executable,
            timeout_seconds=5,
        ),
    )

    service.apply(snapshot, config(), NOW)

    assert item.company_industry is None
    assert not calls_path.exists()


def test_company_profile_script_waits_for_async_industry_content() -> None:
    script = industry_module()._COMPANY_PAGE_JS

    assert "index < 20" in script
    assert "setTimeout" in script
    assert "reportedIndustry = readIndustry()" in script


def test_linkedin_job_locator_without_reported_industry_does_not_open_opencli(
    tmp_path: Path,
) -> None:
    module = industry_module()
    executable = tmp_path / "opencli"
    calls_path = tmp_path / "linkedin-calls.jsonl"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import pathlib",
                "import sys",
                f"calls = pathlib.Path({str(calls_path)!r})",
                "args = sys.argv[1:]",
                "with calls.open('a', encoding='utf-8') as output:",
                "    output.write(json.dumps(args) + '\\n')",
                "if args[:2] == ['linkedin', 'job-detail']:",
                "    print(json.dumps([{'company_url': 'https://www.linkedin.com/company/acme/life'}]))",
                "elif args[0] == 'browser' and args[2] == 'eval':",
                "    print(json.dumps({'status': 'ok', 'reported_industry': 'Industrial Automation'}))",
                "elif args[0] == 'browser':",
                "    print(json.dumps({'status': 'ok'}))",
                "else:",
                "    raise SystemExit(78)",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    source = {
        "source_name": "linkedin",
        "lookup_url": "https://www.linkedin.com/jobs/view/4423914728",
        "public_url": "https://www.linkedin.com/jobs/view/4423914728",
        "source_title": "LinkedIn company profile",
    }
    item = job("one", "Acme GmbH", source=source)
    snapshot = Snapshot(meta=StoreMeta(data_revision=1), jobs=[item])
    module.CompanyIndustryService(
        module.CompanyIndustryStore(tmp_path / "company-industries.json"),
        module.SourceNativeCompanyIndustryLookup(
            opencli_executable=executable,
            timeout_seconds=5,
        ),
    ).apply(snapshot, config(), NOW)

    assert item.company_industry is None
    assert not calls_path.exists()
