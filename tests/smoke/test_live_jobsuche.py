from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import PublicHttpClient
from job_scan.sources.jobsuche import JobsucheAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("JOB_SCAN_LIVE_JOBSUCHE") != "1",
    reason="Set JOB_SCAN_LIVE_JOBSUCHE=1 to run the live Jobsuche smoke test.",
)


class _OnePageClient(PublicHttpClient):
    """Fetch one live page and make that page terminal for adapter mapping."""

    request_count = 0

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if self.request_count:
            raise AssertionError("Jobsuche smoke must request exactly one page")
        payload = super().get_json(url, params=params, headers=headers)
        self.request_count += 1
        listings = payload.get("stellenangebote") or payload.get("ergebnisliste")
        if isinstance(listings, list):
            payload["maxErgebnisse"] = len(listings)
        return payload


def _synthetic_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        resume_path=tmp_path / "unused-synthetic-resume.pdf",
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256="sha256:" + "b" * 64,
        search_terms=["Software"],
        locations=[],
        german_level="A1",
        needs_visa_sponsorship=True,
        claude=ClaudeSettings(
            model="sonnet",
            effort="low",
        ),
        scheduler=SchedulerSettings(local_time="08:30"),
    )


def test_live_jobsuche_maps_one_nationwide_public_search_page_without_user_data(
    tmp_path: Path,
) -> None:
    client = _OnePageClient(tmp_path / "public-http-cache", min_interval_seconds=0)
    adapter = JobsucheAdapter(_synthetic_config(tmp_path), client, page_size=1)

    references = adapter.discover()

    assert client.request_count == 1
    assert references
    reference = references[0]
    assert reference.source is SourceKind.ARBEITSAGENTUR
    assert reference.external_id
    assert reference.listing_title
    assert reference.listing_company
    assert str(reference.detail_url).startswith("https://")
