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
from job_scan.sources.dhl import DhlAdapter

ORIGIN = "https://careers.dhl.com"
SEARCH_URL = f"{ORIGIN}/amer/en/search-results"
SEARCH_PATTERN = re.compile(re.escape(SEARCH_URL) + r"(?:\?.*)?")
WIDGETS_URL = f"{ORIGIN}/widgets"
DETAIL_URL = f"{ORIGIN}/amer/en/job/DPDHGLOBALAV361651ENAMEREXTERNAL"
GERMANY_PLACE_ID = "ChIJa76xwh5ymkcRW-WRjmtd6HU"


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Java"],
        "locations": ["Berlin"],
        "german_level": "B1",
        "resume_path": Path("/tmp/resume.pdf"),
        "resume_sha256": "sha256:" + "a" * 64,
        "profile_sha256": "sha256:" + "b" * 64,
        "claude": ClaudeSettings(model="sonnet", effort="medium"),
        "scheduler": SchedulerSettings(),
    }
    values.update(overrides)
    return AppConfig.model_validate(values)


def adapter(tmp_path: Path, *, app_config: AppConfig | None = None) -> DhlAdapter:
    resolved_config = app_config or config()
    if resolved_config.locations:
        def location_response(request: httpx.Request) -> httpx.Response:
            city = json.loads(request.content)["keywords"]
            predictions = []
            if city != "Nowherezzzz":
                predictions.append(
                    {
                        "description": f"{city}, Germany",
                        "locationType": "city",
                        "place_id": f"place-{city}",
                    }
                )
            return httpx.Response(
                200,
                json={"placeAutoComplete": {"predictions": predictions}},
            )

        respx.post(WIDGETS_URL).mock(side_effect=location_response)
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return DhlAdapter(resolved_config, client)


def listing(
    job_seq_no: str,
    title: str,
    *,
    city: str = "Berlin",
    state: str = "Berlin",
    country: str = "Germany",
) -> dict[str, object]:
    location = f"{city}, {state}, {country}"
    return {
        "jobId": "AV-361651",
        "reqId": "AV-361651",
        "jobSeqNo": job_seq_no,
        "title": title,
        "country": country,
        "location": location,
        "multi_location": [location],
        "postedDate": "2026-08-10T06:16:38.660+0000",
        "applyUrl": (
            "https://dpdhlgroup.avature.net/de_DE/jobs/ApplicationMethods"
            "?jobId=361651&source=careers.dhl.com"
        ),
    }


def search_html(
    jobs: list[dict[str, object]],
    *,
    total_hits: int | None = None,
    status: int = 200,
) -> str:
    payload = {
        "eagerLoadRefineSearch": {
            "status": status,
            "hits": len(jobs),
            "totalHits": len(jobs) if total_hits is None else total_hits,
            "data": {"jobs": jobs},
        }
    }
    return f"<html><script>var phApp = {{}}; phApp.ddo = {json.dumps(payload)};</script></html>"


def detail_html(
    *,
    job_seq_no: str = "DPDHGLOBALAV361651ENAMEREXTERNAL",
    locations: tuple[str, ...] = ("Berlin, Berlin, Germany",),
) -> str:
    multi_location = []
    for location in locations:
        city, state, country = (part.strip() for part in location.split(",", 2))
        multi_location.append(
            {
                "city": city,
                "state": state,
                "country": country,
                "location": location,
            }
        )
    job = {
        "jobId": "AV-361651",
        "reqId": "AV-361651",
        "jobSeqNo": job_seq_no,
        "title": "Experte Softwareentwicklung (m/w/d)",
        "companyName": "DHL 2-Mann-Handling GmbH",
        "country": "Germany",
        "location": locations[0],
        "multi_location": multi_location,
        "datePosted": "2026-07-02T00:00:00.000+0000",
        "postedDate": "2026-08-10T06:16:38.660+0000",
        "description": (
            "<p>Build Java backend services.</p><p>Operate Spring Boot workloads on Kubernetes.</p>"
        ),
    }
    payload = {
        "jobDetail": {
            "status": 200,
            "hits": 1,
            "totalHits": 1,
            "data": {"job": job},
        }
    }
    return f"<html><script>var phApp = {{}}; phApp.ddo = {json.dumps(payload)};</script></html>"


@respx.mock
def test_discover_uses_official_city_search_pagination_and_deduplication(
    tmp_path: Path,
) -> None:
    berlin = listing(
        "DPDHGLOBALAV361651ENAMEREXTERNAL",
        "Experte Softwareentwicklung (m/w/d)",
    )
    potsdam = listing(
        "DPDHGLOBALAV999999ENAMEREXTERNAL",
        "Backend Engineer",
        city="Potsdam",
        state="Brandenburg",
    )
    route = respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(200, text=search_html([berlin], total_hits=2)),
            httpx.Response(200, text=search_html([potsdam], total_hits=2)),
            httpx.Response(200, text=search_html([berlin])),
        ]
    )
    dhl = adapter(
        tmp_path,
        app_config=config(
            search_terms=["Java", "java", "Backend", " "],
            locations=["Berlin", " berlin "],
        ),
    )

    references = dhl.discover()

    assert [reference.external_id for reference in references] == [
        "DPDHGLOBALAV361651ENAMEREXTERNAL",
        "DPDHGLOBALAV999999ENAMEREXTERNAL",
    ]
    assert references[0].source is SourceKind.DHL
    assert references[0].source_instance == "dhl"
    assert references[0].listing_company == "DHL Group"
    assert references[0].listing_location == "Berlin, Berlin, Germany"
    assert references[0].listing_posted_at is None
    assert str(references[0].detail_url) == DETAIL_URL
    assert str(references[0].listing_application_url).startswith("https://dpdhlgroup.avature.net/")
    assert [dict(call.request.url.params) for call in route.calls] == [
        {
            "keywords": "Java",
            "from": "0",
            "p": "place-Berlin",
            "location": "Berlin, Germany",
        },
        {
            "keywords": "Java",
            "from": "1",
            "p": "place-Berlin",
            "location": "Berlin, Germany",
        },
        {
            "keywords": "Backend",
            "from": "0",
            "p": "place-Berlin",
            "location": "Berlin, Germany",
        },
    ]


@respx.mock
def test_discover_ignores_unresolved_city_when_another_city_is_supported(
    tmp_path: Path,
) -> None:
    search_route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [listing("DPDHGLOBALAV361651ENAMEREXTERNAL", "Backend Engineer")]
            ),
        )
    )
    dhl = adapter(
        tmp_path,
        app_config=config(locations=["Berlin", "Nowherezzzz"]),
    )

    references = dhl.discover()

    assert [reference.external_id for reference in references] == [
        "DPDHGLOBALAV361651ENAMEREXTERNAL"
    ]
    assert search_route.call_count == 1
    assert dict(search_route.calls[0].request.url.params)["location"] == "Berlin, Germany"


@respx.mock
def test_discover_skips_search_when_no_configured_city_is_supported(
    tmp_path: Path,
) -> None:
    search_route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(200, text=search_html([]))
    )
    dhl = adapter(
        tmp_path,
        app_config=config(locations=["Nowherezzzz"]),
    )

    assert dhl.discover() == []
    assert search_route.call_count == 0


@respx.mock
def test_discover_accepts_all_german_cities_when_setup_locations_are_empty(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                        city="Potsdam",
                        state="Brandenburg",
                    )
                ]
            ),
        )
    )
    dhl = adapter(tmp_path, app_config=config(locations=[]))

    references = dhl.discover()

    assert [reference.external_id for reference in references] == [
        "DPDHGLOBALAV361651ENAMEREXTERNAL"
    ]
    assert dict(route.calls[0].request.url.params)["location"] == "Germany"


@respx.mock
def test_discover_accepts_non_german_result_returned_by_official_query(
    tmp_path: Path,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV999999ENAMEREXTERNAL",
                        "Backend Engineer",
                        city="Prague",
                        state="Prague",
                        country="Czech Republic",
                    )
                ]
            ),
        )
    )
    dhl = adapter(tmp_path, app_config=config(locations=[]))

    references = dhl.discover()

    assert [reference.external_id for reference in references] == [
        "DPDHGLOBALAV999999ENAMEREXTERNAL"
    ]
    assert references[0].listing_location == "Prague, Prague, Czech Republic"


@pytest.mark.parametrize(
    ("configured_city", "dhl_city", "state"),
    [
        ("Munich", "München", "Bayern"),
        ("Cologne", "Köln", "Nordrhein-Westfalen"),
        ("Hanover", "Hannover", "Niedersachsen"),
        ("Nuremberg", "Nürnberg", "Bayern"),
    ],
)
@respx.mock
def test_english_setup_city_matches_german_dhl_listing_and_detail_city(
    tmp_path: Path,
    configured_city: str,
    dhl_city: str,
    state: str,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                        city=dhl_city,
                        state=state,
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(locations=(f"{dhl_city}, {state}, Germany",)),
        )
    )
    dhl = adapter(tmp_path, app_config=config(locations=[configured_city]))

    references = dhl.discover()
    occurrence = dhl.fetch_detail(references[0])

    assert occurrence.location == f"{dhl_city}, {state}, Germany"


@respx.mock
def test_fetch_detail_uses_detail_date_and_complete_job_data(tmp_path: Path) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                    )
                ]
            ),
        )
    )
    detail_route = respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    dhl = adapter(tmp_path)
    reference = dhl.discover()[0]

    occurrence = dhl.fetch_detail(reference)

    assert occurrence.source is SourceKind.DHL
    assert occurrence.source_instance == "dhl"
    assert occurrence.external_id == "DPDHGLOBALAV361651ENAMEREXTERNAL"
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.company == "DHL 2-Mann-Handling GmbH"
    assert occurrence.title == "Experte Softwareentwicklung (m/w/d)"
    assert occurrence.location == "Berlin, Berlin, Germany"
    assert occurrence.description == (
        "Build Java backend services. Operate Spring Boot workloads on Kubernetes."
    )
    assert occurrence.posted_at == date(2026, 7, 2)
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None
    assert detail_route.call_count == 1


@respx.mock
def test_fetch_detail_accepts_german_location_returned_by_official_city_search(
    tmp_path: Path,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(locations=("Hamburg, Hamburg, Germany",)),
        )
    )
    dhl = adapter(tmp_path)
    reference = dhl.discover()[0]

    occurrence = dhl.fetch_detail(reference)

    assert occurrence.location == "Hamburg, Hamburg, Germany"


@respx.mock
def test_fetch_detail_accepts_non_german_location_returned_by_official_query(
    tmp_path: Path,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(locations=("Prague, Prague, Czech Republic",)),
        )
    )
    dhl = adapter(tmp_path, app_config=config(locations=[]))
    reference = dhl.discover()[0]

    occurrence = dhl.fetch_detail(reference)

    assert occurrence.location == "Prague, Prague, Czech Republic"


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [(404, "http_404"), (410, "http_410")],
)
@respx.mock
def test_fetch_detail_marks_http_closure_statuses_closed(
    tmp_path: Path,
    status_code: int,
    reason: str,
) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(status_code))
    dhl = adapter(tmp_path)
    reference = dhl.discover()[0]

    with pytest.raises(ExplicitlyClosed) as error:
        dhl.fetch_detail(reference)

    assert error.value.source_job_key == ("dhl:dhl:DPDHGLOBALAV361651ENAMEREXTERNAL")
    assert error.value.reason == reason


@respx.mock
def test_fetch_detail_rejects_a_mismatched_job_identity(tmp_path: Path) -> None:
    respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html(
                [
                    listing(
                        "DPDHGLOBALAV361651ENAMEREXTERNAL",
                        "Experte Softwareentwicklung (m/w/d)",
                    )
                ]
            ),
        )
    )
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(job_seq_no="DPDHGLOBALAV999999ENAMEREXTERNAL"),
        )
    )
    dhl = adapter(tmp_path)
    reference = dhl.discover()[0]

    with pytest.raises(InvalidResponse, match="did not match"):
        dhl.fetch_detail(reference)
