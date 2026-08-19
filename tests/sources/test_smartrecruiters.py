from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

import job_scan.sources.smartrecruiters as smartrecruiters_module
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.smartrecruiters import SmartRecruitersAdapter

LIST_URL = "https://api.smartrecruiters.com/v1/companies/BoschGroup/postings"
DETAIL_URL = f"{LIST_URL}/job-1"


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["software engineer"],
        "locations": ["Nuremberg"],
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
    page_size: int = 100,
) -> SmartRecruitersAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return SmartRecruitersAdapter(
        app_config or config(),
        client,
        company_identifier="BoschGroup",
        company_name="Bosch",
        page_size=page_size,
        today=lambda: date(2026, 8, 9),
    )


def listing(
    job_id: str,
    title: str,
    *,
    city: str = "Nürnberg",
    released_date: str = "2026-08-08T10:15:30.000Z",
) -> dict[str, object]:
    return {
        "id": job_id,
        "name": title,
        "company": {"identifier": "BoschGroup", "name": "Bosch Group"},
        "releasedDate": released_date,
        "location": {
            "city": city,
            "region": "BY",
            "country": "de",
            "fullLocation": f"{city}, BY, Germany",
        },
        "industry": {"id": "technology", "label": "Technology"},
        "department": {"id": "engineering", "label": "Engineering"},
        "function": {"id": "engineering", "label": "Engineering"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "experienceLevel": {"id": "mid-senior", "label": "Mid-Senior Level"},
    }


@respx.mock
def test_discover_queries_each_search_term_for_each_location_and_deduplicates(
    tmp_path: Path,
) -> None:
    route = respx.get(LIST_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [listing("java-berlin", "Senior Java Entwickler", city="Berlin")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [listing("shared", "Cloud Backend Engineer", city="Hamburg")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [listing("java-berlin", "Senior Java Entwickler", city="Berlin")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [listing("shared", "Cloud Backend Engineer", city="Hamburg")],
                },
            ),
        ]
    )
    value = config(
        search_terms=["Java", "Backend"],
        locations=["Berlin", "Hamburg"],
    )

    references = adapter(tmp_path, app_config=value).discover()

    assert [reference.external_id for reference in references] == [
        "java-berlin",
        "shared",
    ]
    assert [dict(call.request.url.params) for call in route.calls] == [
        {
            "country": "de",
            "q": "Java",
            "city": "Berlin",
            "limit": "100",
            "offset": "0",
        },
        {
            "country": "de",
            "q": "Java",
            "city": "Hamburg",
            "limit": "100",
            "offset": "0",
        },
        {
            "country": "de",
            "q": "Backend",
            "city": "Berlin",
            "limit": "100",
            "offset": "0",
        },
        {
            "country": "de",
            "q": "Backend",
            "city": "Hamburg",
            "limit": "100",
            "offset": "0",
        },
    ]


@respx.mock
def test_discover_preserves_listing_fields_from_api_search(
    tmp_path: Path,
) -> None:
    route = respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 2,
                "content": [
                    listing("job-1", "Softwareentwickler für Backend-Systeme"),
                    listing("job-2", "Elektriker/Instandhalter (w/m/div.)"),
                ],
            },
        )
    )
    references = adapter(tmp_path).discover()

    assert [reference.external_id for reference in references] == ["job-1", "job-2"]
    assert references[0].source is SourceKind.SMARTRECRUITERS
    assert references[0].source_instance == "boschgroup"
    assert references[0].listing_company == "Bosch Group"
    assert references[0].listing_location == "Nürnberg, BY, Germany"
    assert references[0].listing_posted_at == date(2026, 8, 8)
    assert dict(route.calls[0].request.url.params) == {
        "country": "de",
        "q": "software engineer",
        "city": "Nürnberg",
        "limit": "100",
        "offset": "0",
    }


@respx.mock
def test_discover_returns_api_matches_without_fetching_details(tmp_path: Path) -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [listing("job-1", "Softwareentwickler")],
            },
        )
    )
    detail_route = respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                **listing("job-1", "Softwareentwickler"),
                "active": True,
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "title": "Job description",
                            "text": "<p>Build Java backend services.</p>",
                        }
                    }
                },
            },
        )
    )
    smartrecruiters = adapter(tmp_path)

    references = smartrecruiters.discover()

    assert [reference.external_id for reference in references] == ["job-1"]
    assert detail_route.call_count == 0

    occurrence = smartrecruiters.fetch_detail(references[0])

    assert occurrence.description == (
        "Job description\nBuild Java backend services."
    )
    assert detail_route.call_count == 1


@respx.mock
def test_discover_paginates_and_deduplicates_results_across_locations(
    tmp_path: Path,
) -> None:
    route = respx.get(LIST_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "totalFound": 2,
                    "content": [listing("job-1", "Software Engineer", city="Berlin")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "totalFound": 2,
                    "content": [listing("job-2", "Platform Engineer", city="Berlin")],
                },
            ),
            httpx.Response(
                200,
                json={
                    "totalFound": 1,
                    "content": [listing("job-2", "Platform Engineer", city="Hamburg")],
                },
            ),
        ]
    )
    value = config(
        search_terms=["software engineer"],
        locations=["Berlin", "Hamburg"],
    )

    references = adapter(tmp_path, app_config=value, page_size=1).discover()

    assert [reference.external_id for reference in references] == ["job-1", "job-2"]
    assert [call.request.url.params["offset"] for call in route.calls] == ["0", "1", "0"]
    assert [call.request.url.params["city"] for call in route.calls] == [
        "Berlin",
        "Berlin",
        "Hamburg",
    ]


@respx.mock
def test_discover_filters_old_api_matches(tmp_path: Path) -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 2,
                "content": [
                    listing("recent", "Backend Entwickler"),
                    listing(
                        "old",
                        "Backend Entwickler",
                        released_date="2026-07-01T10:15:30.000Z",
                    ),
                ],
            },
        )
    )
    references = adapter(tmp_path).discover()

    assert [reference.external_id for reference in references] == ["recent"]


@respx.mock
def test_discover_uses_utc_today_for_the_default_date_cutoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smartrecruiters_module,
        "_utc_today",
        lambda: date(2026, 8, 9),
    )
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [
                    listing(
                        "cutoff-day",
                        "Software Engineer",
                        released_date="2026-08-02T23:30:00.000Z",
                    )
                ],
            },
        )
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    smartrecruiters = SmartRecruitersAdapter(
        config(),
        client,
        company_identifier="BoschGroup",
        company_name="Bosch",
    )

    references = smartrecruiters.discover()

    assert [reference.external_id for reference in references] == ["cutoff-day"]


@respx.mock
def test_fetch_detail_returns_public_posting_and_plain_complete_description(
    tmp_path: Path,
) -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [listing("job-1", "Software Engineer")],
            },
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                **listing("job-1", "Software Engineer"),
                "active": True,
                "postingUrl": "https://jobs.smartrecruiters.com/BoschGroup/job-1-software-engineer",
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "title": "Job description",
                            "text": "<p>Build <strong>backend systems</strong>.</p>",
                        },
                        "qualifications": {
                            "title": "Qualifications",
                            "text": "<ul><li>Python</li><li>SQL</li></ul>",
                        },
                    }
                },
            },
        )
    )
    smartrecruiters = adapter(tmp_path)
    reference = smartrecruiters.discover()[0]

    occurrence = smartrecruiters.fetch_detail(reference)

    assert str(occurrence.url) == (
        "https://jobs.smartrecruiters.com/BoschGroup/job-1-software-engineer"
    )
    assert occurrence.description == (
        "Job description\nBuild backend systems.\n\nQualifications\nPython SQL"
    )
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    source = occurrence.company_industry_source
    assert source is not None
    assert source.source_name == "smartrecruiters"
    assert source.reported_industry == "Technology"


@respx.mock
def test_fetch_detail_marks_inactive_posting_as_closed(tmp_path: Path) -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [listing("job-1", "Software Engineer")],
            },
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={**listing("job-1", "Software Engineer"), "active": False},
        )
    )
    smartrecruiters = adapter(tmp_path)
    reference = smartrecruiters.discover()[0]

    with pytest.raises(ExplicitlyClosed):
        smartrecruiters.fetch_detail(reference)


@respx.mock
def test_fetch_detail_rejects_a_different_posting_id(tmp_path: Path) -> None:
    respx.get(LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "totalFound": 1,
                "content": [listing("job-1", "Software Engineer")],
            },
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={**listing("other-job", "Software Engineer"), "active": True},
        )
    )
    smartrecruiters = adapter(tmp_path)
    reference = smartrecruiters.discover()[0]

    with pytest.raises(InvalidResponse, match="posting ID"):
        smartrecruiters.fetch_detail(reference)
