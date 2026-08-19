from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx
import pytest
import respx
from pydantic import HttpUrl

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import CompanySizeSource, SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed, JobReference, run_source
from job_scan.sources.jobsuche import JobsucheAdapter, lookup_company_size

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "sources" / "jobsuche"
BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4"
SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
NATIONWIDE_SEARCH_URL = SEARCH_URL
NATIONWIDE_DETAIL_URL = (
    f"{BASE_URL}/jobdetails/MTAwMDAtMTIzNDU2LVM%3D"
)
NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


def load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"{name} must contain a JSON object")
    return cast(dict[str, Any], payload)


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["backend engineer"],
        "locations": ["Berlin"],
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(
            model="claude-sonnet-4-5",
            effort="medium",
        ),
        "scheduler": SchedulerSettings(local_time="08:00"),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter(tmp_path: Path, *, app_config: AppConfig | None = None, page_size: int = 100) -> JobsucheAdapter:
    client = PublicHttpClient(cache_dir=tmp_path / "cache", min_interval_seconds=0)
    return JobsucheAdapter(app_config or config(), client, page_size=page_size)


def one_listing_payload() -> dict[str, Any]:
    payload = load_fixture("search.json")
    payload["stellenangebote"] = payload["stellenangebote"][:1]
    payload["size"] = 1
    payload["maxErgebnisse"] = 1
    return payload


def reference() -> JobReference:
    return JobReference(
        source=SourceKind.ARBEITSAGENTUR,
        source_instance="default",
        external_id="10000-123456-S",
        detail_url=HttpUrl(f"{BASE_URL}/jobdetails/10000-123456-S"),
        platform_url=HttpUrl(
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
        ),
        listing_title="Senior Backend Engineer",
        listing_company="Example Systems GmbH",
        listing_location="10115 Berlin",
        listing_posted_at=date(2026, 7, 30),
    )


@respx.mock
def test_discover_maps_config_to_paginated_jobsuche_requests(tmp_path: Path) -> None:
    fixture = load_fixture("search.json")
    first_page = deepcopy(fixture)
    first_page["stellenangebote"] = fixture["stellenangebote"][:1]
    first_page.update(page=1, size=1, maxErgebnisse=2)
    second_page = deepcopy(fixture)
    second_page["stellenangebote"] = fixture["stellenangebote"][1:]
    second_page.update(page=2, size=1, maxErgebnisse=2)
    route = respx.get(SEARCH_URL).mock(
        side_effect=[httpx.Response(200, json=first_page), httpx.Response(200, json=second_page)]
    )
    jobsuche = adapter(tmp_path, page_size=1)

    references = jobsuche.discover()

    assert [item.external_id for item in references] == [
        "10000-123456-S",
        "10000-654321-S",
    ]
    assert route.call_count == 2
    for page_number, call in enumerate(route.calls, start=1):
        request = call.request
        assert request.url.path == "/jobboerse/jobsuche-service/pc/v6/jobs"
        assert request.headers["X-API-Key"] == "jobboerse-jobsuche"
        assert dict(request.url.params) == {
            "was": "backend engineer",
            "wo": "Berlin",
            "veroeffentlichtseit": "7",
            "page": str(page_number),
            "size": "1",
        }


@respx.mock
def test_discover_runs_each_search_term_and_location_pair(tmp_path: Path) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"stellenangebote": [], "page": 1, "size": 100, "maxErgebnisse": 0},
        )
    )
    jobsuche = adapter(
        tmp_path,
        app_config=config(
            search_terms=["backend", "platform"],
            locations=["Berlin", "Hamburg"],
        ),
    )

    assert jobsuche.discover() == []

    assert [
        (request.url.params["was"], request.url.params["wo"])
        for request in (call.request for call in route.calls)
    ] == [
        ("backend", "Berlin"),
        ("backend", "Hamburg"),
        ("platform", "Berlin"),
        ("platform", "Hamburg"),
    ]


@respx.mock
def test_discover_uses_v6_germany_filter_for_countrywide_search(
    tmp_path: Path,
) -> None:
    route = respx.get(NATIONWIDE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ergebnisliste": [
                    {
                        "referenznummer": "10000-123456-S",
                        "firma": "Example Systems GmbH",
                        "stellenangebotsTitel": "Java Backend Engineer",
                        "stellenlokationen": [
                            {
                                "adresse": {
                                    "plz": "10115",
                                    "ort": "Berlin",
                                    "land": "DEUTSCHLAND",
                                }
                            }
                        ],
                        "datumErsteVeroeffentlichung": "2026-07-30",
                        "externeURL": "https://jobs.example.org/apply/10000-123456-S",
                    }
                ],
                "page": 1,
                "size": 100,
                "maxErgebnisse": 1,
            },
        )
    )
    jobsuche = adapter(tmp_path, app_config=config(locations=[]))

    references = jobsuche.discover()

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {
        "was": "backend engineer",
        "wo": "Deutschland",
        "veroeffentlichtseit": "7",
        "page": "1",
        "size": "100",
    }
    assert len(references) == 1
    assert references[0].external_id == "10000-123456-S"
    assert references[0].listing_title == "Java Backend Engineer"
    assert references[0].listing_location == "10115 Berlin"
    assert str(references[0].detail_url) == NATIONWIDE_DETAIL_URL


@respx.mock
def test_discover_omits_posting_window_when_search_has_no_limit(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ergebnisliste": [],
                "page": 1,
                "size": 100,
                "maxErgebnisse": 0,
            },
        )
    )

    adapter(
        tmp_path,
        app_config=config(posted_within_days=None),
    ).discover()

    assert "veroeffentlichtseit" not in route.calls[0].request.url.params


@respx.mock
def test_fetch_detail_accepts_current_v6_listing_contract(tmp_path: Path) -> None:
    route = respx.get(NATIONWIDE_DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "referenznummer": "10000-123456-S",
                "firma": "Example Systems GmbH",
                "stellenangebotsTitel": "Java Backend Engineer",
                "stellenlokationen": [
                    {
                        "adresse": {
                            "plz": "10115",
                            "ort": "Berlin",
                            "land": "DEUTSCHLAND",
                        }
                    }
                ],
                "datumErsteVeroeffentlichung": "2026-07-30",
                "externeURL": "https://jobs.example.org/apply/10000-123456-S",
                "stellenangebotsBeschreibung": "A complete public Java backend job description.",
            },
        )
    )
    nationwide_reference = JobReference(
        source=SourceKind.ARBEITSAGENTUR,
        source_instance="default",
        external_id="10000-123456-S",
        detail_url=HttpUrl(NATIONWIDE_DETAIL_URL),
        listing_title="Java Backend Engineer",
        listing_company="Example Systems GmbH",
        listing_location="10115 Berlin",
        listing_posted_at=date(2026, 7, 30),
    )

    occurrence = adapter(tmp_path).fetch_detail(nationwide_reference)

    assert route.call_count == 1
    assert occurrence.external_id == "10000-123456-S"
    assert occurrence.company == "Example Systems GmbH"
    assert occurrence.title == "Java Backend Engineer"
    assert occurrence.location == "10115 Berlin"
    assert occurrence.posted_at == date(2026, 7, 30)
    assert occurrence.description == "A complete public Java backend job description."
    assert occurrence.detail_complete is True


@respx.mock
def test_fetch_detail_retains_official_employer_profile_for_company_size(
    tmp_path: Path,
) -> None:
    employer_hash = "WBy2dq1793NfyIndKqUbywqejG4d7VZeQPXXQqqRZiU="
    respx.get(NATIONWIDE_DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "referenznummer": "10000-123456-S",
                "firma": "JetBrains GmbH",
                "stellenangebotsTitel": "Senior Software Engineer",
                "stellenangebotsBeschreibung": "Build developer tools.",
                "arbeitgeberKundennummerHash": employer_hash,
            },
        )
    )
    nationwide_reference = JobReference(
        source=SourceKind.ARBEITSAGENTUR,
        source_instance="default",
        external_id="10000-123456-S",
        detail_url=HttpUrl(NATIONWIDE_DETAIL_URL),
        listing_title="Senior Software Engineer",
        listing_company="JetBrains GmbH",
        listing_location="Berlin",
    )

    occurrence = adapter(tmp_path).fetch_detail(nationwide_reference)

    source = occurrence.company_size_source
    assert source is not None
    assert source.source_name == "arbeitsagentur"
    assert str(source.lookup_url) == (
        "https://rest.arbeitsagentur.de/vermittlung/"
        "ag-darstellung-service/pc/v1/arbeitgeberdarstellung/"
        f"{quote(employer_hash, safe='')}"
    )
    assert str(source.public_url) == (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
    )


@respx.mock
def test_official_employer_profile_returns_betriebsgroesse(tmp_path: Path) -> None:
    employer_hash = "WBy2dq1793NfyIndKqUbywqejG4d7VZeQPXXQqqRZiU="
    lookup_url = (
        "https://rest.arbeitsagentur.de/vermittlung/"
        "ag-darstellung-service/pc/v1/arbeitgeberdarstellung/"
        f"{quote(employer_hash, safe='')}"
    )
    route = respx.get(lookup_url).mock(
        return_value=httpx.Response(200, json={"betriebsgroesse": "1000+"})
    )
    source = CompanySizeSource(
        source_name="arbeitsagentur",
        lookup_url=lookup_url,
        public_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S",
        source_title="Arbeitsagentur · Betriebsgröße",
    )
    client = PublicHttpClient(cache_dir=tmp_path / "cache", min_interval_seconds=0)

    result = lookup_company_size(source, "JetBrains GmbH", NOW, client)

    assert route.call_count == 1
    assert route.calls[0].request.headers["X-API-Key"] == "jobboerse-jobsuche"
    assert result is not None
    assert result.reported_size == "1000+"
    assert result.minimum_employees == 1000
    assert result.maximum_employees is None
    assert result.source_name == "arbeitsagentur"


@respx.mock
def test_fetch_detail_uses_browser_page_when_no_external_url(tmp_path: Path) -> None:
    detail = load_fixture("detail.json")
    detail.pop("externeUrl")
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=detail)
    )

    occurrence = adapter(tmp_path).fetch_detail(reference())

    assert str(occurrence.url) == (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
    )


@respx.mock
def test_fetch_detail_maps_complete_public_contract(tmp_path: Path) -> None:
    detail = load_fixture("detail.json")
    detail["futureContractField"] = {"ignored": True}
    route = respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=detail)
    )
    jobsuche = adapter(tmp_path)

    occurrence = jobsuche.fetch_detail(reference())

    assert route.calls[0].request.headers["X-API-Key"] == "jobboerse-jobsuche"
    assert occurrence.source_job_key == "arbeitsagentur:default:10000-123456-S"
    assert str(occurrence.url) == (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
    )
    assert occurrence.company == "Example Systems GmbH"
    assert occurrence.title == "Senior Backend Engineer"
    assert occurrence.location == "10115 Berlin"
    assert occurrence.posted_at == date(2026, 7, 30)
    assert len(occurrence.description) > 100
    assert occurrence.content_hash == (
        "sha256:e90fc397a62294c2212cb44003ab096d66db21601df198bedff8dd2b6fff6918"
    )
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_invalid_listing_items_report_contract_errors_without_aborting_valid_item(
    tmp_path: Path,
) -> None:
    payload = one_listing_payload()
    valid = payload["stellenangebote"][0]
    missing_refnr = {key: value for key, value in valid.items() if key != "refnr"}
    missing_company = {key: value for key, value in valid.items() if key != "arbeitgeber"}
    missing_company["refnr"] = "10000-NO-COMPANY-S"
    missing_title = {key: value for key, value in valid.items() if key != "titel"}
    missing_title["refnr"] = "10000-NO-TITLE-S"
    payload["stellenangebote"] = [valid, missing_refnr, missing_company, missing_title]
    payload["maxErgebnisse"] = 4
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=load_fixture("detail.json"))
    )

    jobsuche = adapter(tmp_path)
    result = run_source(jobsuche)

    assert result.completed_listing is True
    assert result.discovered_source_job_keys == {
        "arbeitsagentur:default:10000-123456-S",
        "arbeitsagentur:default:10000-NO-COMPANY-S",
        "arbeitsagentur:default:10000-NO-TITLE-S",
    }
    assert [item.external_id for item in result.occurrences] == ["10000-123456-S"]
    assert [(error.category, error.item_key) for error in result.errors] == [
        ("contract", None),
        ("contract", "arbeitsagentur:default:10000-NO-COMPANY-S"),
        ("contract", "arbeitsagentur:default:10000-NO-TITLE-S"),
    ]
    assert [error.message for error in result.errors] == [
        "listing item missing required field: refnr",
        "listing item missing required field: arbeitgeber",
        "listing item missing required field: titel",
    ]
    assert jobsuche.drain_discovery_errors() == []


@pytest.mark.parametrize(
    ("description_value", "description_present"),
    [
        (None, False),
        (None, True),
        ("   ", True),
    ],
    ids=["absent", "null", "blank"],
)
@respx.mock
def test_incomplete_detail_returns_pending_source_ready_occurrence(
    tmp_path: Path,
    description_value: object,
    description_present: bool,
) -> None:
    incomplete = load_fixture("detail.json")
    if description_present:
        incomplete["stellenangebotsBeschreibung"] = description_value
    else:
        incomplete.pop("stellenangebotsBeschreibung")
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=incomplete)
    )

    occurrence = adapter(tmp_path).fetch_detail(reference())

    assert occurrence.source_job_key == "arbeitsagentur:default:10000-123456-S"
    assert str(occurrence.url) == (
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-123456-S"
    )
    assert occurrence.description == ""
    assert occurrence.detail_complete is False
    assert occurrence.fetch_error_code == "missing_full_description"


@respx.mock
def test_non_string_description_container_is_contract_failure(tmp_path: Path) -> None:
    malformed = load_fixture("detail.json")
    malformed["stellenangebotsBeschreibung"] = ["unexpected"]
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=malformed)
    )

    with pytest.raises(InvalidResponse, match="must be a string"):
        adapter(tmp_path).fetch_detail(reference())



@pytest.mark.parametrize(
    ("detail_response", "category", "error_code", "status_code"),
    [
        (httpx.ReadTimeout("fixture timeout"), "http", "timeout", None),
        (httpx.Response(403), "blocked", "blocked", None),
        (httpx.Response(500), "http", "http_500", 500),
        (httpx.Response(200, content=b"not JSON"), "contract", "invalid_response", None),
    ],
    ids=["timeout", "blocked", "5xx", "parse"],
)
@respx.mock
def test_detail_failure_keeps_discovered_key_error_and_partial(
    tmp_path: Path,
    detail_response: Exception | httpx.Response,
    category: str,
    error_code: str,
    status_code: int | None,
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=one_listing_payload()))
    detail_route = respx.get(f"{BASE_URL}/jobdetails/10000-123456-S")
    if isinstance(detail_response, Exception):
        detail_route.mock(side_effect=detail_response)
    else:
        detail_route.mock(return_value=detail_response)

    result = run_source(adapter(tmp_path))

    key = "arbeitsagentur:default:10000-123456-S"
    assert result.completed_listing is True
    assert result.discovered_source_job_keys == {key}
    assert result.explicitly_closed_source_job_keys == set()
    assert len(result.errors) == 1
    assert result.errors[0].category == category
    assert result.errors[0].item_key == key
    assert result.errors[0].status_code == status_code
    assert len(result.occurrences) == 1
    assert result.occurrences[0].source_job_key == key
    assert result.occurrences[0].description == ""
    assert result.occurrences[0].detail_complete is False
    assert result.occurrences[0].fetch_error_code == error_code


@pytest.mark.parametrize(
    "detail_response",
    [
        httpx.Response(404),
        httpx.Response(410),
        httpx.Response(200, json={"status": "CLOSED"}),
    ],
    ids=["404", "410", "closed-marker"],
)
@respx.mock
def test_explicitly_closed_detail_records_closure_without_partial(
    tmp_path: Path,
    detail_response: httpx.Response,
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=one_listing_payload()))
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=detail_response
    )

    result = run_source(adapter(tmp_path))

    key = "arbeitsagentur:default:10000-123456-S"
    assert result.completed_listing is True
    assert result.discovered_source_job_keys == {key}
    assert result.explicitly_closed_source_job_keys == {key}
    assert result.occurrences == []
    assert result.errors == []


@pytest.mark.parametrize(
    ("detail_response", "reason"),
    [
        (httpx.Response(404), "http_404"),
        (httpx.Response(410), "http_410"),
        (httpx.Response(200, json={"status": "CLOSED"}), "page_closed_marker"),
    ],
    ids=["404", "410", "closed-marker"],
)
@respx.mock
def test_fetch_detail_reports_explicit_closure_reason(
    tmp_path: Path,
    detail_response: httpx.Response,
    reason: str,
) -> None:
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=detail_response
    )

    with pytest.raises(ExplicitlyClosed) as raised:
        adapter(tmp_path).fetch_detail(reference())

    assert raised.value.source_job_key == "arbeitsagentur:default:10000-123456-S"
    assert raised.value.reason == reason


@respx.mock
def test_discovery_errors_do_not_leak_into_next_run(tmp_path: Path) -> None:
    invalid_payload = one_listing_payload()
    invalid_payload["stellenangebote"][0].pop("arbeitgeber")
    search_route = respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=invalid_payload),
            httpx.Response(200, json=one_listing_payload()),
        ]
    )
    respx.get(f"{BASE_URL}/jobdetails/10000-123456-S").mock(
        return_value=httpx.Response(200, json=load_fixture("detail.json"))
    )
    jobsuche = adapter(tmp_path)

    first_result = run_source(jobsuche)
    second_result = run_source(jobsuche)

    assert search_route.call_count == 2
    assert [error.message for error in first_result.errors] == [
        "listing item missing required field: arbeitgeber"
    ]
    assert second_result.errors == []
    assert [item.external_id for item in second_result.occurrences] == [
        "10000-123456-S"
    ]


@pytest.mark.parametrize(
    "listing_response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"unexpected": []}),
    ],
    ids=["non-2xx", "contract"],
)
@respx.mock
def test_listing_failure_marks_run_incomplete(
    tmp_path: Path, listing_response: httpx.Response
) -> None:
    respx.get(SEARCH_URL).mock(return_value=listing_response)

    result = run_source(adapter(tmp_path))

    assert result.completed_listing is False
    assert result.discovered_source_job_keys == set()
    assert result.explicitly_closed_source_job_keys == set()
    assert result.occurrences == []
    assert len(result.errors) == 1
