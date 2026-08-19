from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.telekom import TelekomAdapter

SEARCH_URL = "https://careers.telekom.com/api/jobs-proxy/search"
DETAIL_URL = "https://careers.telekom.com/en/jobs/senior-software-engineer-907522"


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Software Engineer"],
        "locations": ["Berlin", "Hamburg"],
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def listing(
    job_id: str,
    title: str,
    *,
    city: str = "Berlin,Hamburg",
    job_source: str = "eightfold",
) -> dict[str, str]:
    return {
        "requisition_id": job_id,
        "title": title,
        "job_title": title,
        "city": city,
        "experience_level": "Professional",
        "category": "Product Development",
        "apply_url": f"https://apply.example.com/{job_id}",
        "job_source": job_source,
    }


def search_payload(
    jobs: list[dict[str, str]],
    *,
    page: int = 1,
    number_of_pages: int = 1,
) -> dict[str, object]:
    return {
        "status_code": 200,
        "message": "Results found",
        "data": jobs,
        "locations": ["Berlin", "Hamburg", "Germany"],
        "countries": [{"name": "Germany", "count": len(jobs)}],
        "cities": [{"name": "Berlin, Germany", "count": len(jobs)}],
        "skills": ["Java", "Python"],
        "experience_level": ["Professional"],
        "category": ["Product Development"],
        "intake_year": [],
        "occupation": [],
        "career_vision": [],
        "pagination_info": {
            "count": len(jobs),
            "page": page,
            "next_page": None,
            "previous_page": None,
            "number_of_pages": number_of_pages,
        },
    }


@respx.mock
def test_discover_searches_each_term_with_combined_locations_and_deduplicates(
    tmp_path: Path,
) -> None:
    route = respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=search_payload([])),
            httpx.Response(
                200,
                json=search_payload(
                    [
                        listing("907522", "Senior Software Engineer"),
                        listing("shared", "Cloud Backend Engineer"),
                    ]
                ),
            ),
            httpx.Response(
                200,
                json=search_payload(
                    [
                        listing("shared", "Cloud Backend Engineer"),
                        listing(
                            "313284",
                            "Dual Study Program Computer Science",
                            city="Hamburg",
                            job_source="apprenticeship",
                        ),
                    ]
                ),
            ),
        ]
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(
        config(search_terms=["Software Engineer", "Backend Engineer"]),
        client,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == [
        "907522",
        "shared",
        "313284",
    ]
    assert references[0].source is SourceKind.TELEKOM
    assert references[0].source_instance == "telekom"
    assert references[0].listing_company == "Deutsche Telekom"
    assert references[0].listing_location == "Berlin,Hamburg"
    assert str(references[0].detail_url) == (
        "https://careers.telekom.com/en/jobs/senior-software-engineer-907522"
    )
    assert str(references[0].listing_application_url) == ("https://apply.example.com/907522")
    assert [dict(call.request.url.params) for call in route.calls] == [
        {
            "location": "Germany",
            "search": "",
            "job_source": "apprenticeship",
            "page": "1",
        },
        {
            "location": "Berlin;Hamburg",
            "search": "Software Engineer",
            "job_source": "apprenticeship",
            "page": "1",
        },
        {
            "location": "Berlin;Hamburg",
            "search": "Backend Engineer",
            "job_source": "apprenticeship",
            "page": "1",
        },
    ]
    assert [json.loads(call.request.content) for call in route.calls] == [
        {"locale": "en", "user_query": ""},
        {"locale": "en", "user_query": "Software Engineer"},
        {"locale": "en", "user_query": "Backend Engineer"},
    ]


@respx.mock
def test_discover_ignores_location_missing_from_official_list(tmp_path: Path) -> None:
    route = respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=search_payload([])),
            httpx.Response(
                200,
                json=search_payload([listing("907522", "Senior Software Engineer")]),
            ),
        ]
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(
        config(locations=["Berlin", "Nowherezzzz"]),
        client,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == ["907522"]
    assert route.calls[1].request.url.params["location"] == "Berlin"


@respx.mock
def test_discover_maps_frankfurt_am_main_without_matching_frankfurt_oder(
    tmp_path: Path,
) -> None:
    official_payload = search_payload([])
    official_payload["locations"] = ["Frankfurt", "Frankfurt (Oder)"]
    route = respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=official_payload),
            httpx.Response(
                200,
                json=search_payload([listing("907522", "Senior Software Engineer")]),
            ),
        ]
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(locations=["Frankfurt am Main"]), client)

    adapter.discover()

    assert route.calls[1].request.url.params["location"] == "Frankfurt"


@respx.mock
def test_discover_skips_job_search_when_no_location_maps_to_official_list(
    tmp_path: Path,
) -> None:
    route = respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=search_payload([]))
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(locations=["Nowherezzzz"]), client)

    assert adapter.discover() == []
    assert route.call_count == 1
    assert route.calls[0].request.url.params["search"] == ""
    assert route.calls[0].request.url.params["location"] == "Germany"


@respx.mock
def test_discover_follows_the_official_page_count(tmp_path: Path) -> None:
    route = respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=search_payload([])),
            httpx.Response(
                200,
                json=search_payload(
                    [listing("page-1", "Software Engineer")],
                    page=1,
                    number_of_pages=2,
                ),
            ),
            httpx.Response(
                200,
                json=search_payload(
                    [listing("page-2", "Backend Engineer")],
                    page=2,
                    number_of_pages=2,
                ),
            ),
        ]
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(), client)

    references = adapter.discover()

    assert [reference.external_id for reference in references] == ["page-1", "page-2"]
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "1", "2"]


@respx.mock
def test_discover_skips_blank_duplicate_terms_and_defaults_to_germany(
    tmp_path: Path,
) -> None:
    route = respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload([listing("907522", "Senior Software Engineer")]),
        )
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(
        config(
            search_terms=[" ", "Software Engineer", "software engineer"],
            locations=[],
        ),
        client,
    )

    references = adapter.discover()

    assert [reference.external_id for reference in references] == ["907522"]
    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {
        "location": "Germany",
        "search": "Software Engineer",
        "job_source": "apprenticeship",
        "page": "1",
    }


@respx.mock
def test_fetch_detail_reads_complete_jobposting_json(tmp_path: Path) -> None:
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload([listing("907522", "Senior Software Engineer")]),
        )
    )
    detail_route = respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<html><head><title>Senior Software Engineer</title>"
                '<script id="job-posting-jsonld" type="application/ld+json">'
                + json.dumps(
                    {
                        "@context": "https://schema.org",
                        "@type": "JobPosting",
                        "@id": (
                            "https://careers.telekom.com/en/jobs/"
                            "senior-software-engineer-907522#job-posting"
                        ),
                        "title": "Senior Software Engineer",
                        "description": (
                            "<p>Build Java backend services.</p><p>Work with cloud platforms.</p>"
                        ),
                        "datePosted": "2026-08-10T08:45:23.000Z",
                        "experienceRequirements": "Professional",
                        "hiringOrganization": {
                            "@type": "Organization",
                            "name": "T-Systems International GmbH",
                        },
                        "identifier": {
                            "@type": "PropertyValue",
                            "name": "Job ID",
                            "value": "senior-software-engineer-907522",
                        },
                        "url": (
                            "https://careers.telekom.com/en/jobs/senior-software-engineer-907522"
                        ),
                        "jobLocation": {
                            "@type": "Place",
                            "address": {
                                "@type": "PostalAddress",
                                "addressLocality": "Berlin,Hamburg",
                                "addressCountry": "Germany",
                            },
                        },
                    }
                )
                + "</script></head><body></body></html>"
            ),
        )
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(), client)
    reference = adapter.discover()[0]

    occurrence = adapter.fetch_detail(reference)

    assert occurrence.source is SourceKind.TELEKOM
    assert occurrence.source_instance == "telekom"
    assert occurrence.external_id == "907522"
    assert str(occurrence.url) == (
        "https://careers.telekom.com/en/jobs/senior-software-engineer-907522"
    )
    assert occurrence.company == "T-Systems International GmbH"
    assert occurrence.title == "Senior Software Engineer"
    assert occurrence.location == "Berlin,Hamburg, Germany"
    assert occurrence.description == ("Build Java backend services. Work with cloud platforms.")
    assert occurrence.posted_at.isoformat() == "2026-08-10"
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    assert detail_route.call_count == 1


@respx.mock
def test_fetch_detail_marks_the_custom_not_found_page_closed(tmp_path: Path) -> None:
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload([listing("907522", "Senior Software Engineer")]),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<html><head><title>Global Career Website</title></head>"
                "<body>Unfortunately, we can't find the page you're looking for."
                "</body></html>"
            ),
        )
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(), client)
    reference = adapter.discover()[0]

    with pytest.raises(ExplicitlyClosed) as error:
        adapter.fetch_detail(reference)

    assert error.value.source_job_key == "telekom:telekom:907522"
    assert error.value.reason == "page_closed_marker"


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(404, "http_404"), (410, "http_410")],
)
@respx.mock
def test_fetch_detail_marks_explicit_http_closure_statuses_closed(
    tmp_path: Path,
    status_code: int,
    reason: str,
) -> None:
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_payload([listing("907522", "Senior Software Engineer")]),
        )
    )
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(status_code))
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    adapter = TelekomAdapter(config(), client)
    reference = adapter.discover()[0]

    with pytest.raises(ExplicitlyClosed) as error:
        adapter.fetch_detail(reference)

    assert error.value.source_job_key == "telekom:telekom:907522"
    assert error.value.reason == reason
