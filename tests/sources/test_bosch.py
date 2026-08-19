from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.base import JobReference
from job_scan.sources.bosch import BoschAdapter

CAREER_ORIGIN = "https://jobs.bosch.com"
CAREER_URL = f"{CAREER_ORIGIN}/en/"
SEARCH_API_ORIGIN = "https://bosch-i3-caas-api.e-spirit.cloud"
SEARCH_API_URL = (
    f"{SEARCH_API_ORIGIN}/bosch-i3-prod/bosch-de.jobs.content/_aggrs/get_jobs"
)
CITIES_API_URL = (
    f"{SEARCH_API_ORIGIN}/bosch-i3-prod/bosch-de.jobs.content/_aggrs/cities"
)
DETAIL_URL = (
    f"{CAREER_ORIGIN}/en/job/"
    "REF300001A-backend-software-engineer-f-m-div"
)
DE_DETAIL_URL = (
    f"{CAREER_ORIGIN}/en/job/"
    "DE00619415-junior-managers-program-trainee-start-your-leadership-journey"
    "-in-information-technology"
)


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Software Engineer"],
        "locations": [],
        "posted_within_days": 7,
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
    available_cities: tuple[str, ...] | None = None,
) -> BoschAdapter:
    resolved_config = app_config or config()
    if resolved_config.locations:
        cities = available_cities or tuple(
            {
                "Cologne": "Köln",
                "Hanover": "Hannover",
                "Munich": "München",
                "Nuremberg": "Nürnberg",
            }.get(city, city)
            for city in resolved_config.locations
        )
        respx.get(CITIES_API_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"_id": city, "workLocation": city, "count": 1}
                    for city in cities
                ],
            )
        )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return BoschAdapter(
        resolved_config,
        client,
        page_size=page_size,
        today=lambda: date(2026, 8, 11),
    )


def career_html() -> str:
    return """
    <html><head><script>
      window.EXTERNAL_CONFIG={
        jobsApi:{
          baseUrl:"https://bosch-i3-caas-api.e-spirit.cloud",
          tenant:"bosch-i3-prod",
          project:"bosch-de",
          collection:"jobs",
          apiKey:"public-frontend-token"
        },
        jobAdLinkPrefix:"https://jobs.bosch.com/en/job/"
      };
    </script></head></html>
    """


def listing(
    reference: str,
    title: str,
    *,
    released_date: str,
    city: str = "Berlin",
) -> dict[str, object]:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    return {
        "_id": reference,
        "name": title,
        "location": {
            "country": "de",
            "city": city,
            "latitude": "52.5200",
            "remote": False,
            "workLocation": city,
            "hybrid": False,
            "longitude": "13.4050",
        },
        "refNumber": reference,
        "jobUrl": f"{reference}-{slug}",
        "releasedDate": released_date,
        "function": {"id": "engineering", "label": "Engineering"},
        "language": {"code": "en", "label": "English", "labelNative": "English"},
        "customField": None,
        "positionType": {
            "valueId": "professional",
            "valueLabel": "Professional",
            "fieldLabel": "Join as",
            "fieldId": "position-type",
        },
        "country": {
            "valueId": "de",
            "valueLabel": "Germany",
            "fieldLabel": "Country",
            "fieldId": "COUNTRY",
        },
        "type_of_contract": {
            "valueId": "unlimited",
            "valueLabel": "Unlimited",
            "fieldLabel": "Contract type",
            "fieldId": "contract-type",
        },
        "working_hours": {
            "valueId": "full-time",
            "valueLabel": "Full-time",
            "fieldLabel": "Working time",
            "fieldId": "working-hours",
        },
        "division": {
            "valueId": "mobility",
            "valueLabel": "Mobility",
            "fieldLabel": "Division",
            "fieldId": "division",
        },
        "legal_entity": {
            "valueId": "bosch-gmbh",
            "valueLabel": "Robert Bosch GmbH",
            "fieldLabel": "Legal entity",
            "fieldId": "legal-entity",
        },
        "working_location": {
            "valueId": city.casefold(),
            "valueLabel": city,
            "fieldLabel": "Working location",
            "fieldId": "working-location",
        },
        "work_mode": "on-site",
    }


def search_response(
    listings: list[dict[str, object]],
    *,
    total: int | None = None,
) -> dict[str, object]:
    return {
        "_embedded": {
            "rh:result": [
                {
                    "data": listings,
                    "filter_division": [],
                    "filter_function": [],
                    "filter_legal_entity": [],
                    "filter_position_types": [],
                    "filter_remote": [],
                    "filter_type_of_contract": [],
                    "filter_working_hours": [],
                    "meta": [{"count": len(listings) if total is None else total}],
                }
            ]
        }
    }


@respx.mock
def test_discover_uses_official_search_semantics_and_applies_seven_day_cutoff(
    tmp_path: Path,
) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    official_route = respx.get(SEARCH_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response(
                [
                    listing(
                        "REF300001A",
                        "Backend Software Engineer (f/m/div.)",
                        released_date="2026-08-05T10:00:00.000Z",
                    ),
                    listing(
                        "REF299999Z",
                        "Old Software Engineer (f/m/div.)",
                        released_date="2026-08-03T10:00:00.000Z",
                    ),
                ]
            ),
        )
    )

    references = adapter(tmp_path).discover()

    assert [reference.external_id for reference in references] == ["REF300001A"]
    assert references[0].source is SourceKind.BOSCH
    assert references[0].source_instance == "bosch"
    assert references[0].listing_company == "Robert Bosch GmbH"
    assert references[0].listing_location == "Berlin, Germany"
    assert str(references[0].detail_url) == DETAIL_URL
    request = official_route.calls[0].request
    assert request.headers["Authorization"] == "Bearer public-frontend-token"
    assert request.url.params["pagesize"] == "100"
    assert request.url.params["page"] == "1"
    assert json.loads(request.url.params["avars"]) == {
        "country": ["de"],
        "page_language": "en",
        "search_term": "Software Engineer",
        "sort": {"releasedDate": -1},
    }


@respx.mock
def test_discover_paginates_official_results(
    tmp_path: Path,
) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    shared = listing(
        "REF300001A",
        "Backend Software Engineer (f/m/div.)",
        released_date="2026-08-05T10:00:00.000Z",
    )
    final = listing(
        "REF300002B",
        "Cloud Software Engineer (f/m/div.)",
        released_date="2026-08-06T10:00:00.000Z",
    )
    official_route = respx.get(SEARCH_API_URL).mock(
        side_effect=[
            httpx.Response(200, json=search_response([shared], total=2)),
            httpx.Response(200, json=search_response([final], total=2)),
        ]
    )
    bosch = adapter(
        tmp_path,
        app_config=config(locations=["Berlin"]),
        page_size=1,
    )

    references = bosch.discover()

    assert [reference.external_id for reference in references] == [
        "REF300001A",
        "REF300002B",
    ]
    assert [call.request.url.params["page"] for call in official_route.calls] == [
        "1",
        "2",
    ]
    assert [
        json.loads(call.request.url.params["avars"])["city"]
        for call in official_route.calls
    ] == ["Berlin", "Berlin"]


@respx.mock
def test_discover_deduplicates_unique_setup_queries(tmp_path: Path) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    shared = listing(
        "REF300001A",
        "Backend Software Engineer (f/m/div.)",
        released_date="2026-08-05T10:00:00.000Z",
    )
    official_route = respx.get(SEARCH_API_URL).mock(
        side_effect=[
            httpx.Response(200, json=search_response([shared])),
            httpx.Response(200, json=search_response([shared])),
        ]
    )
    bosch = adapter(
        tmp_path,
        app_config=config(
            search_terms=["Software Engineer", "software engineer", "Backend Engineer"],
            locations=["Berlin", "berlin"],
        ),
    )

    references = bosch.discover()

    assert [reference.external_id for reference in references] == ["REF300001A"]
    assert [
        json.loads(call.request.url.params["avars"])["search_term"]
        for call in official_route.calls
    ] == ["Software Engineer", "Backend Engineer"]


@respx.mock
def test_discover_ignores_city_missing_from_official_location_list(
    tmp_path: Path,
) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    berlin = listing(
        "REF300001A",
        "Backend Software Engineer (f/m/div.)",
        released_date="2026-08-05T10:00:00.000Z",
    )
    official_route = respx.get(SEARCH_API_URL).mock(
        return_value=httpx.Response(200, json=search_response([berlin]))
    )
    bosch = adapter(
        tmp_path,
        app_config=config(locations=["Berlin", "Nowherezzzz"]),
        available_cities=("Berlin",),
    )

    references = bosch.discover()

    assert [reference.external_id for reference in references] == ["REF300001A"]
    assert [
        json.loads(call.request.url.params["avars"])["city"]
        for call in official_route.calls
    ] == ["Berlin"]


@respx.mock
def test_discover_skips_job_search_when_no_city_maps_to_official_list(
    tmp_path: Path,
) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    official_route = respx.get(SEARCH_API_URL).mock(
        return_value=httpx.Response(200, json=search_response([]))
    )
    bosch = adapter(
        tmp_path,
        app_config=config(locations=["Nowherezzzz"]),
        available_cities=("Berlin",),
    )

    assert bosch.discover() == []
    assert official_route.call_count == 0


@respx.mock
def test_discover_maps_city_to_all_official_sublocations(tmp_path: Path) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    official_route = respx.get(SEARCH_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response(
                [
                    listing(
                        "REF300001A",
                        "Backend Software Engineer (f/m/div.)",
                        released_date="2026-08-05T10:00:00.000Z",
                    )
                ]
            ),
        )
    )
    bosch = adapter(
        tmp_path,
        app_config=config(locations=["Berlin"]),
        available_cities=("Berlin - Charlottenburg", "Berlin - Mitte"),
    )

    references = bosch.discover()

    assert [reference.external_id for reference in references] == ["REF300001A"]
    assert [
        json.loads(call.request.url.params["avars"])["city"]
        for call in official_route.calls
    ] == ["Berlin - Charlottenburg", "Berlin - Mitte"]


@respx.mock
def test_discover_maps_frankfurt_am_main_without_matching_frankfurt_oder(
    tmp_path: Path,
) -> None:
    respx.get(CAREER_URL).mock(return_value=httpx.Response(200, text=career_html()))
    official_route = respx.get(SEARCH_API_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response(
                [
                    listing(
                        "REF300001A",
                        "Backend Software Engineer (f/m/div.)",
                        released_date="2026-08-05T10:00:00.000Z",
                    )
                ]
            ),
        )
    )
    bosch = adapter(
        tmp_path,
        app_config=config(locations=["Frankfurt am Main"]),
        available_cities=("Frankfurt - Gallus", "Frankfurt (Oder)"),
    )

    bosch.discover()

    assert [
        json.loads(call.request.url.params["avars"])["city"]
        for call in official_route.calls
    ] == ["Frankfurt - Gallus"]


def detail_html(
    *,
    reference: str = "REF300001A",
    reference_attribute: str | None = None,
) -> str:
    reference_attribute = reference_attribute or f"JobID {reference}"
    return f"""
    <html><body>
      <div class="M-JobKeyFacts__termWrapper">
        <div class="M-JobKeyFacts__term">Bosch Location</div>
        <div class="M-JobKeyFacts__fact">Berlin</div>
      </div>
      <div class="ApplyButton"
           data-job-reference="{reference_attribute}"
           data-job-name="Backend Software Engineer (f/m/div.)"
           data-release-date="Job posted: 2026-08-05"
           data-apply-button-href="https://jobs.smartrecruiters.com/BoschGroup/123-backend-software-engineer">
      </div>
      <section aria-label="Your tasks-Your profile">
        <div class="a-text">
          <h2>Your tasks</h2>
          <div class="A-Text-RichText"><p>Build reliable backend services.</p></div>
        </div>
        <div class="a-text">
          <h2>Your profile</h2>
          <div class="A-Text-RichText"><p>Experience with Python and SQL.</p></div>
        </div>
      </section>
    </body></html>
    """


def reference() -> JobReference:
    return JobReference(
        source=SourceKind.BOSCH,
        source_instance="bosch",
        external_id="REF300001A",
        detail_url=DETAIL_URL,
        platform_url=DETAIL_URL,
        listing_title="Backend Software Engineer (f/m/div.)",
        listing_company="Robert Bosch GmbH",
        listing_location="Berlin, Germany",
        listing_posted_at=date(2026, 8, 5),
    )


def de_reference() -> JobReference:
    return JobReference(
        source=SourceKind.BOSCH,
        source_instance="bosch",
        external_id="DE00619415",
        detail_url=DE_DETAIL_URL,
        platform_url=DE_DETAIL_URL,
        listing_title="Junior Managers Program (Trainee) - Start your Leadership Journey",
        listing_company="Robert Bosch GmbH",
        listing_location="Gerlingen - Schillerhöhe, Germany",
        listing_posted_at=date(2026, 8, 7),
    )


@respx.mock
def test_fetch_detail_reads_complete_official_job_page(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html())
    )

    occurrence = adapter(tmp_path).fetch_detail(reference())

    assert occurrence.external_id == "REF300001A"
    assert occurrence.title == "Backend Software Engineer (f/m/div.)"
    assert occurrence.company == "Robert Bosch GmbH"
    assert occurrence.location == "Berlin, Germany"
    assert occurrence.posted_at == date(2026, 8, 5)
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.description == (
        "Your tasks Build reliable backend services. "
        "Your profile Experience with Python and SQL."
    )
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_fetch_detail_accepts_de_reference(tmp_path: Path) -> None:
    respx.get(DE_DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html(reference="DE00619415"))
    )

    occurrence = adapter(tmp_path).fetch_detail(de_reference())

    assert occurrence.external_id == "DE00619415"
    assert occurrence.company == "Robert Bosch GmbH"
    assert occurrence.posted_at == date(2026, 8, 5)
    assert occurrence.description == (
        "Your tasks Build reliable backend services. "
        "Your profile Experience with Python and SQL."
    )
    assert occurrence.detail_complete is True


@respx.mock
def test_fetch_detail_validates_the_listing_id_without_restricting_its_format(
    tmp_path: Path,
) -> None:
    future_reference = de_reference().model_copy(
        update={"external_id": "de_job.2026:42"}
    )
    respx.get(DE_DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(
                reference_attribute="Position reference de_job.2026:42"
            ),
        )
    )

    occurrence = adapter(tmp_path).fetch_detail(future_reference)

    assert occurrence.external_id == "de_job.2026:42"
    assert occurrence.detail_complete is True


@respx.mock
def test_fetch_detail_rejects_a_different_official_reference(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(reference="REF-DIFFERENT"),
        )
    )

    with pytest.raises(InvalidResponse, match="reference did not match"):
        adapter(tmp_path).fetch_detail(reference())


@pytest.mark.parametrize("status_code", [404, 410])
@respx.mock
def test_fetch_detail_marks_missing_official_page_closed(
    tmp_path: Path,
    status_code: int,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(status_code))

    with pytest.raises(ExplicitlyClosed) as error:
        adapter(tmp_path).fetch_detail(reference())

    assert error.value.source_job_key == (
        "bosch:bosch:REF300001A"
    )
