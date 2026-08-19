from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, model_validator

from job_scan.config import AppConfig
from job_scan.domain import (
    AvailabilityStatus,
    CompanyIndustryEvidence,
    CompanyIndustrySource,
    JobRecord,
    Snapshot,
    SourceOccurrence,
)

_CACHE_TTL = timedelta(days=90)
_COMPANY_PAGE_JS = r"""
(async () => {
  const normalize = (value) => (value || "").replace(/\s+/g, " ").trim();
  const readIndustry = () => {
    const lines = (document.body?.innerText || "")
      .split(/\n+/)
      .map(normalize)
      .filter(Boolean);
    const label = /^(Industry|Branche|Industrie|Sector|Sektor)$/i;
    for (let index = 0; index < lines.length; index += 1) {
      if (label.test(lines[index])) {
        const value = lines[index + 1] || "";
        if (value && !label.test(value)) return value;
      }
      const after = lines[index].match(/^(?:Industry|Branche|Industrie|Sector|Sektor)\s*[:·-]?\s+(.+)$/i);
      if (after?.[1]) return normalize(after[1]);
      const before = lines[index].match(/^(.+?)\s+(?:Industry|Branche|Industrie|Sector|Sektor)$/i);
      if (before?.[1]) return normalize(before[1]);
    }
    return "";
  };
  let reportedIndustry = readIndustry();
  for (let index = 0; index < 20 && !reportedIndustry; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    reportedIndustry = readIndustry();
  }
  return {status: "ok", reported_industry: reportedIndustry};
})()
""".strip()

COMPANY_INDUSTRY_PAGE_JS = _COMPANY_PAGE_JS


class CompanyIndustryStoreError(RuntimeError):
    """Report one unreadable or unwritable company-industry cache."""


def native_company_industry_evidence(
    *,
    company: str,
    reported_industry: str,
    source_url: str,
    source_title: str,
    source_name: Literal[
        "glassdoor", "linkedin", "indeed", "smartrecruiters", "stepstone"
    ],
    checked_at: datetime,
) -> CompanyIndustryEvidence | None:
    """Build one source-native industry after conservative whitespace validation."""
    industry = " ".join(reported_industry.split())
    if not industry or len(industry) > 300:
        return None
    return CompanyIndustryEvidence(
        company_name=company,
        industry=industry,
        source_url=HttpUrl(source_url),
        source_title=source_title,
        checked_at=checked_at,
        confidence="high",
        lookup_method="native",
        source_name=source_name,
        evidence=[],
    )


class _CompanyIndustryCache(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    entries: list[CompanyIndustryEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_source_native_entries(self) -> _CompanyIndustryCache:
        if any(entry.lookup_method != "native" for entry in self.entries):
            raise ValueError("company-industry cache accepts only native evidence")
        return self


class CompanyIndustryStore:
    """Persist source-native company industries in one atomic local JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, CompanyIndustryEvidence]:
        """Return cached results keyed by normalized company name."""
        if not self._path.exists():
            return {}
        try:
            data = _CompanyIndustryCache.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError, ValidationError):
            raise CompanyIndustryStoreError(
                "Could not read company-industry cache."
            ) from None
        return {_company_key(item.company_name): item for item in data.entries}

    def save(self, entries: Mapping[str, CompanyIndustryEvidence]) -> None:
        """Atomically replace the cache in deterministic company-name order."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            temporary = Path(name)
            try:
                payload = _CompanyIndustryCache(
                    schema_version=1,
                    entries=[entries[key] for key in sorted(entries)],
                ).model_dump_json(indent=2)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(payload)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self._path)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        except (OSError, ValidationError):
            raise CompanyIndustryStoreError(
                "Could not save company-industry cache."
            ) from None


class SourceNativeCompanyIndustryLookup:
    """Read industries already reported while an active source fetched a job."""

    def __init__(
        self,
        *,
        opencli_executable: str | Path | None = None,
        timeout_seconds: int = 90,
    ) -> None:
        # Retain the old constructor shape for callers while forbidding a second
        # company-page visit dedicated only to industry.
        del opencli_executable, timeout_seconds

    def lookup(
        self,
        job: JobRecord,
        config: AppConfig,
        checked_at: datetime,
    ) -> CompanyIndustryEvidence | None:
        """Return the first industry already captured by an active source."""
        enabled = {
            "linkedin": config.linkedin_enabled,
            "indeed": config.indeed_de_enabled,
            "stepstone": config.stepstone_de_enabled,
            "glassdoor": config.glassdoor_de_enabled,
            "smartrecruiters": True,
        }
        priority = {
            "smartrecruiters": 0,
            "linkedin": 1,
            "indeed": 2,
            "stepstone": 3,
            "glassdoor": 4,
        }
        occurrences = sorted(
            (
                occurrence
                for occurrence in job.source_occurrences
                if occurrence.availability_status is AvailabilityStatus.ACTIVE
                and occurrence.company_industry_source is not None
                and enabled[occurrence.company_industry_source.source_name]
            ),
            key=lambda occurrence: (
                priority[_industry_source(occurrence).source_name],
                -occurrence.source_generation,
                occurrence.source_job_key,
            ),
        )
        for occurrence in occurrences:
            source = _industry_source(occurrence)
            industry = source.reported_industry
            if industry is None:
                continue
            result = native_company_industry_evidence(
                company=job.company,
                reported_industry=industry,
                source_url=str(source.public_url),
                source_title=source.source_title,
                source_name=source.source_name,
                checked_at=checked_at,
            )
            if result is not None:
                return result
        return None


class CompanyIndustryService:
    """Apply cached or source-native industries before semantic review."""

    def __init__(
        self,
        store: CompanyIndustryStore,
        native_lookup: SourceNativeCompanyIndustryLookup,
    ) -> None:
        self._store = store
        self._native_lookup = native_lookup

    def apply(
        self,
        snapshot: Snapshot,
        config: AppConfig,
        checked_at: datetime,
    ) -> None:
        """Enrich active companies from cache or current source locators."""
        candidates: dict[str, list[JobRecord]] = {}
        for job in snapshot.jobs:
            if job.availability_status is not AvailabilityStatus.ACTIVE:
                continue
            candidates.setdefault(_company_key(job.company), []).append(job)
        try:
            cached = self._store.load()
        except CompanyIndustryStoreError:
            cached = {}
        for key in sorted(candidates):
            jobs = candidates[key]
            current_results = [
                industry
                for job in jobs
                if (industry := job.company_industry) is not None
                and industry.lookup_method == "native"
                and _company_key(industry.company_name) == key
                and checked_at - industry.checked_at <= _CACHE_TTL
            ]
            current = max(
                current_results,
                key=lambda industry: industry.checked_at,
                default=None,
            )
            result = cached.get(key)
            if current is not None and (
                result is None or current.checked_at >= result.checked_at
            ):
                result = current
                cached[key] = current
                try:
                    self._store.save(cached)
                except CompanyIndustryStoreError:
                    pass
            if result is None or checked_at - result.checked_at > _CACHE_TTL:
                result = next(
                    (
                        found
                        for job in jobs
                        if (found := self._native_lookup.lookup(job, config, checked_at))
                        is not None
                    ),
                    None,
                )
                if result is not None:
                    cached[key] = result
                    try:
                        self._store.save(cached)
                    except CompanyIndustryStoreError:
                        pass
            if result is None:
                continue
            for job in snapshot.jobs:
                if _company_key(job.company) == key:
                    job.company_industry = result.model_copy(deep=True)


def _company_key(company: str) -> str:
    return " ".join(company.casefold().split())


def _industry_source(occurrence: SourceOccurrence) -> CompanyIndustrySource:
    source = occurrence.company_industry_source
    if source is None:
        raise AssertionError("filtered industry occurrence lost its source")
    return source


__all__ = [
    "CompanyIndustryService",
    "CompanyIndustryStore",
    "CompanyIndustryStoreError",
    "SourceNativeCompanyIndustryLookup",
]
