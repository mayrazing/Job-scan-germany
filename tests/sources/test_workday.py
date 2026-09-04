from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.base import ListingFilteredOut
from job_scan.sources.workday import (
    AdvantechAdapter,
    HaierAdapter,
    JohnsonElectricAdapter,
    NexperiaAdapter,
    VosslohAdapter,
    WorkdaySiteAdapter,
)

TODAY = date(2026, 9, 4)
JOBS_URL = (
    "https://haier.wd3.myworkdayjobs.com"
    "/wday/cxs/haier/HaierEurope_Professional_Careers/jobs"
)
DETAIL_PATH = "/job/Brugherio-MB/Supplier-Risk-Management-Engineer_REQ-25518"
DETAIL_URL = (
    "https://haier.wd3.myworkdayjobs.com"
    "/HaierEurope_Professional_Careers/job/Brugherio-MB"
    "/Supplier-Risk-Management-Engineer_REQ-25518"
)
DETAIL_API_URL = (
    "https://haier.wd3.myworkdayjobs.com"
    "/wday/cxs/haier/HaierEurope_Professional_Careers"
    "/job/Brugherio-MB/Supplier-Risk-Management-Engineer_REQ-25518"
)


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Engineer"],
        "locations": [],
        "posted_within_days": 3,
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter(
    tmp_path: Path,
    *,
    app_config: AppConfig | None = None,
    adapter_type: type[WorkdaySiteAdapter] = HaierAdapter,
) -> WorkdaySiteAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return adapter_type(app_config or config(), client, today=lambda: TODAY)


def posting(
    external_id: str = "REQ-25518",
    title: str = "Supplier Risk Management Engineer",
    posted_on: str = "Posted 2 Days Ago",
) -> dict[str, object]:
    return {
        "title": title,
        "externalPath": DETAIL_PATH,
        "locationsText": "Brugherio (MB)",
        "postedOn": posted_on,
        "bulletFields": [external_id],
    }


def detail_payload() -> dict[str, object]:
    return {
        "jobPostingInfo": {
            "title": "Supplier Risk Management Engineer",
            "externalUrl": DETAIL_URL,
            "startDate": "2026-09-01",
            "location": "Brugherio (MB)",
            "additionalLocations": ["Vimercate (MB)"],
            "jobDescription": "<p>Own supplier risk.</p><h3>Tasks</h3><p>Audits</p>",
        }
    }


@respx.mock
def test_discover_paginates_dedupes_and_parses_postings(tmp_path: Path) -> None:
    respx.post(JOBS_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "total": 22,
                    "jobPostings": [
                        posting(posted_on="Posted 2 Days Ago"),
                        posting(
                            external_id="REQ-26604",
                            title="IoT Technology – Senior Manager",
                            posted_on="Posted 30+ Days Ago",
                        ),
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "total": 22,
                    "jobPostings": [
                        posting(external_id="REQ-26604"),
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "total": 1,
                    "jobPostings": [
                        posting(
                            external_id="REQ-30001",
                            posted_on="Posted Today",
                        ),
                    ],
                },
            ),
        ]
    )
    source = adapter(
        tmp_path,
        app_config=config(search_terms=["Engineer", "IoT"]),
    )

    references = source.discover()

    assert len(respx.calls) == 3
    first_body = respx.calls[0].request.read()
    assert b'"searchText":"Engineer"' in first_body
    assert b'"offset":0' in first_body
    assert [reference.external_id for reference in references] == [
        "REQ-25518",
        "REQ-26604",
        "REQ-30001",
    ]
    assert references[0].source is SourceKind.HAIER
    assert references[0].source_instance == "haier"
    assert references[0].listing_company == "Haier"
    assert references[0].listing_title == "Supplier Risk Management Engineer"
    assert references[0].listing_location == "Brugherio (MB)"
    assert str(references[0].detail_url) == DETAIL_URL
    assert references[0].listing_posted_at == date(2026, 9, 2)
    assert references[1].listing_posted_at == TODAY - timedelta(days=30)
    assert references[2].listing_posted_at == TODAY


@respx.mock
def test_fetch_detail_builds_complete_occurrence(tmp_path: Path) -> None:
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [posting()]})
    )
    respx.get(DETAIL_API_URL).mock(
        return_value=httpx.Response(200, json=detail_payload())
    )
    source = adapter(tmp_path)

    (reference,) = source.discover()
    occurrence = source.fetch_detail(reference)

    assert occurrence.source is SourceKind.HAIER
    assert occurrence.source_instance == "haier"
    assert occurrence.external_id == "REQ-25518"
    assert occurrence.company == "Haier"
    assert occurrence.title == "Supplier Risk Management Engineer"
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.location == "Brugherio (MB); Vimercate (MB)"
    assert occurrence.description == "Own supplier risk. Tasks Audits"
    assert occurrence.posted_at == date(2026, 9, 1)
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    assert occurrence.content_hash


@respx.mock
def test_fetch_detail_filters_locations_outside_setup(tmp_path: Path) -> None:
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [posting()]})
    )
    respx.get(DETAIL_API_URL).mock(
        return_value=httpx.Response(200, json=detail_payload())
    )
    source = adapter(tmp_path, app_config=config(locations=["Munich"]))

    (reference,) = source.discover()
    with pytest.raises(ListingFilteredOut):
        source.fetch_detail(reference)


@respx.mock
def test_fetch_detail_keeps_matching_city_across_aliases(tmp_path: Path) -> None:
    payload = detail_payload()
    payload["jobPostingInfo"]["location"] = "München, Germany"
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [posting()]})
    )
    respx.get(DETAIL_API_URL).mock(return_value=httpx.Response(200, json=payload))
    source = adapter(tmp_path, app_config=config(locations=["Munich"]))

    (reference,) = source.discover()
    occurrence = source.fetch_detail(reference)

    assert occurrence.location == "München, Germany; Vimercate (MB)"


@respx.mock
def test_fetch_detail_treats_gone_postings_as_closed(tmp_path: Path) -> None:
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [posting()]})
    )
    respx.get(DETAIL_API_URL).mock(return_value=httpx.Response(404))
    source = adapter(tmp_path)

    (reference,) = source.discover()
    with pytest.raises(ExplicitlyClosed):
        source.fetch_detail(reference)


@respx.mock
def test_fetch_detail_rejects_identity_mismatch(tmp_path: Path) -> None:
    payload = detail_payload()
    payload["jobPostingInfo"]["externalUrl"] = (
        "https://haier.wd3.myworkdayjobs.com/HaierEurope_Professional_Careers"
        "/job/Brugherio-MB/Other_REQ-00000"
    )
    respx.post(JOBS_URL).mock(
        return_value=httpx.Response(200, json={"total": 1, "jobPostings": [posting()]})
    )
    respx.get(DETAIL_API_URL).mock(return_value=httpx.Response(200, json=payload))
    source = adapter(tmp_path)

    (reference,) = source.discover()
    with pytest.raises(InvalidResponse):
        source.fetch_detail(reference)


@pytest.mark.parametrize(
    ("adapter_type", "origin", "tenant", "site", "source", "instance", "company"),
    [
        (
            HaierAdapter,
            "https://haier.wd3.myworkdayjobs.com",
            "haier",
            "HaierEurope_Professional_Careers",
            SourceKind.HAIER,
            "haier",
            "Haier",
        ),
        (
            NexperiaAdapter,
            "https://nexperia.wd3.myworkdayjobs.com",
            "nexperia",
            "careers",
            SourceKind.NEXPERIA,
            "nexperia",
            "Nexperia",
        ),
        (
            VosslohAdapter,
            "https://vossloh.wd3.myworkdayjobs.com",
            "vossloh",
            "Vossloh_External_Careers",
            SourceKind.VOSSLOH,
            "vossloh",
            "Vossloh",
        ),
        (
            JohnsonElectricAdapter,
            "https://johnsonelectric.wd3.myworkdayjobs.com",
            "johnsonelectric",
            "Career_JE",
            SourceKind.JOHNSON_ELECTRIC,
            "johnson-electric",
            "Johnson Electric",
        ),
        (
            AdvantechAdapter,
            "https://advantech.wd3.myworkdayjobs.com",
            "advantech",
            "External",
            SourceKind.ADVANTECH,
            "advantech",
            "Advantech",
        ),
    ],
)
def test_adapters_bind_their_workday_sites(
    tmp_path: Path,
    adapter_type: type[WorkdaySiteAdapter],
    origin: str,
    tenant: str,
    site: str,
    source: SourceKind,
    instance: str,
    company: str,
) -> None:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    workday_adapter = adapter_type(config(), client)

    assert workday_adapter.source is source
    assert workday_adapter.source_instance == instance
    assert workday_adapter._jobs_api_url() == (
        f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    )
    assert workday_adapter._company_name == company
