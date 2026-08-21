from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.thyssenkrupp import ThyssenkruppAdapter

ORIGIN = "https://jobs.thyssenkrupp.com"
FILTER_INFO_URL = f"{ORIGIN}/api/filter/info"
SEARCH_URL = f"{ORIGIN}/api/filter/query"
DETAIL_URL = f"{ORIGIN}/en/job/id/967315"
TKMS_DETAIL_URL = (
    "https://jobs.tkmsgroup.com/en/job/"
    "Softwareentwickler_in-im-Bereich-Net-und-IoT-Oberhausen/967315"
)


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Java"],
        "locations": ["Berlin"],
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
) -> ThyssenkruppAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return ThyssenkruppAdapter(
        app_config or config(),
        client,
        today=lambda: date(2026, 8, 13),
    )


def filter_info(*cities: tuple[str, str]) -> dict[str, object]:
    children = [
        {
            "value": f"data.locations.cityState:{city}, {state}",
            "label": city,
            "count": 1,
            "order": 0,
        }
        for city, state in cities
    ]
    return {
        "branches": [],
        "facetsCounts": {},
        "facetsOrders": {},
        "locationTree": [
            {
                "value": "data.locations.country:Germany",
                "label": "Germany",
                "count": len(children),
                "order": 0,
                "children": [
                    {
                        "value": "data.locations.state:Berlin",
                        "label": "Berlin",
                        "count": len(children),
                        "order": 0,
                        "children": children,
                    }
                ],
            }
        ],
        "postingDateCounts": {},
        "source": [],
    }


def listing(
    job_id: str = "967315",
    *,
    title: str = "Softwareentwickler:in im Bereich .Net und IoT (m/w/d)",
    locations: tuple[tuple[str, str, str], ...] = (
        ("Berlin", "Berlin", "Germany"),
    ),
) -> dict[str, object]:
    location_values = [
        {
            "address": f"Example 1, {city}, {country}",
            "city": city,
            "cityState": f"{city}, {state}",
            "country": country,
            "state": state,
            "stateShort": state,
            "zipCode": "10115",
        }
        for city, state, country in locations
    ]
    city, state, country = locations[0]
    return {
        "_geoloc": [{"lat": 52.52, "lng": 13.405}],
        "data": {
            "address": f"Example 1, {city}, {country}",
            "applicationEnd": "2099-12-31T22:59:59",
            "applicationUrl": (
                "/clients/tkag/apply/apply-tkag/apply.html"
                f"?jobId=example-{job_id}&langCode=en_GB"
            ),
            "city": city,
            "cityState": f"{city}, {state}",
            "company": "thyssenkrupp Automation Engineering GmbH",
            "contract": "Permanent",
            "country": country,
            "employmentType": ["Part-time"],
            "entryLevel": "Professionals",
            "google_employmentType": "PART_TIME",
            "id": job_id,
            "idClient": job_id,
            "idFS": job_id,
            "jobField": "Engineering",
            "jobNumber": "DE_EXAMPLE_1",
            "language": "EN",
            "locations": location_values,
            "postingDate": "2026-08-10T22:00:00",
            "postingDate_timestamp": 1786406400,
            "remote": ["Hybrid"],
            "source": "talentlink",
            "title": title,
        },
    }


def search_response(
    jobs: list[dict[str, object]],
    *,
    page: int,
    next_page: int | None,
    total_hits: int,
) -> dict[str, object]:
    return {
        "jobs": jobs,
        "page": page,
        "jobsPerPage": 20,
        "nextPage": next_page,
        "previousPage": None if page == 0 else page - 1,
        "totalHits": total_hits,
    }


def detail_html(
    *,
    job_id: str = "967315",
    locations: tuple[tuple[str, str, str], ...] = (
        ("Berlin", "Berlin", "Germany"),
        ("Hamburg", "Hamburg", "Germany"),
    ),
) -> str:
    job_locations = [
        {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressRegion": state,
                "addressCountry": country,
                "streetAddress": f"Example 1, {city}, {country}",
                "postalCode": "10115",
            },
        }
        for city, state, country in locations
    ]
    payload = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Softwareentwickler:in im Bereich .Net und IoT (m/w/d)",
        "description": (
            "<p>Build backend services.</p>"
            "<ul><li>Operate cloud workloads.</li></ul>"
        ),
        "identifier": {
            "@type": "PropertyValue",
            "name": "thyssenkrupp Automation Engineering GmbH",
            "value": job_id,
        },
        "datePosted": "2026-08-10",
        "validThrough": "2099-12-31",
        "employmentType": "PART_TIME",
        "hiringOrganization": {
            "@type": "Organization",
            "name": "thyssenkrupp Automation Engineering GmbH",
            "sameAs": ORIGIN,
        },
        "jobLocation": job_locations,
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script></head><body>"
        '<script>window.translations = {"closed": "This job is no longer online"};</script>'
        "</body></html>"
    )


@respx.mock
def test_discover_forwards_setup_filters_without_forcing_it_or_full_time(
    tmp_path: Path,
) -> None:
    respx.get(FILTER_INFO_URL, params={"locale": "en"}).mock(
        return_value=httpx.Response(200, json=filter_info(("Berlin", "Berlin")))
    )
    search_route = respx.post(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json=search_response(
                    [listing(), listing("JR0000014027")],
                    page=0,
                    next_page=1,
                    total_hits=3,
                ),
            ),
            httpx.Response(
                200,
                json=search_response([listing()], page=1, next_page=None, total_hits=3),
            ),
            httpx.Response(
                200,
                json=search_response([listing()], page=0, next_page=None, total_hits=1),
            ),
        ]
    )
    source = adapter(
        tmp_path,
        app_config=config(
            search_terms=["Java", "java", "Backend", " "],
            locations=["Berlin", " berlin "],
        ),
    )

    references = source.discover()

    assert [reference.external_id for reference in references] == [
        "967315",
        "JR0000014027",
    ]
    assert references[0].source is SourceKind.THYSSENKRUPP
    assert references[0].source_instance == "thyssenkrupp"
    assert references[0].listing_posted_at == date(2026, 8, 10)
    assert references[0].listing_location == "Berlin, Berlin, Germany"
    assert str(references[0].listing_application_url).startswith(
        f"{ORIGIN}/clients/tkag/apply/"
    )
    assert [json.loads(call.request.content) for call in search_route.calls] == [
        {
            "locale": "en",
            "page": 0,
            "searchQuery": "Java",
            "filter": {
                "data.locations.country": ["data.locations.country:Germany"],
                "data.locations.cityState": [
                    "data.locations.cityState:Berlin, Berlin"
                ],
                "data.postingDate_timestamp": [
                    "data.postingDate_timestamp >= 1785974400"
                ],
            },
        },
        {
            "locale": "en",
            "page": 1,
            "searchQuery": "Java",
            "filter": {
                "data.locations.country": ["data.locations.country:Germany"],
                "data.locations.cityState": [
                    "data.locations.cityState:Berlin, Berlin"
                ],
                "data.postingDate_timestamp": [
                    "data.postingDate_timestamp >= 1785974400"
                ],
            },
        },
        {
            "locale": "en",
            "page": 0,
            "searchQuery": "Backend",
            "filter": {
                "data.locations.country": ["data.locations.country:Germany"],
                "data.locations.cityState": [
                    "data.locations.cityState:Berlin, Berlin"
                ],
                "data.postingDate_timestamp": [
                    "data.postingDate_timestamp >= 1785974400"
                ],
            },
        },
    ]
    for request_body in (json.loads(call.request.content) for call in search_route.calls):
        assert "data.jobField" not in request_body["filter"]
        assert "data.employmentType" not in request_body["filter"]


@respx.mock
def test_discover_ignores_unresolved_city_when_another_city_is_supported(
    tmp_path: Path,
) -> None:
    respx.get(FILTER_INFO_URL, params={"locale": "en"}).mock(
        return_value=httpx.Response(200, json=filter_info(("Berlin", "Berlin")))
    )
    search_route = respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response(
                [listing(locations=(("Berlin", "Berlin", "Germany"),))],
                page=0,
                next_page=None,
                total_hits=1,
            ),
        )
    )
    source = adapter(
        tmp_path,
        app_config=config(locations=["Berlin", "Essen"]),
    )

    references = source.discover()

    assert [reference.external_id for reference in references] == ["967315"]
    request_filter = json.loads(search_route.calls[0].request.content)["filter"]
    assert request_filter["data.locations.cityState"] == [
        "data.locations.cityState:Berlin, Berlin"
    ]


@respx.mock
def test_discover_skips_search_when_no_configured_city_is_supported(
    tmp_path: Path,
) -> None:
    respx.get(FILTER_INFO_URL, params={"locale": "en"}).mock(
        return_value=httpx.Response(200, json=filter_info(("Berlin", "Berlin")))
    )
    search_route = respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response([], page=0, next_page=None, total_hits=0),
        )
    )
    source = adapter(tmp_path, app_config=config(locations=["Essen"]))

    assert source.discover() == []
    assert search_route.call_count == 0


@respx.mock
def test_fetch_detail_reads_full_jobposting_and_all_german_locations(tmp_path: Path) -> None:
    respx.get(FILTER_INFO_URL, params={"locale": "en"}).mock(
        return_value=httpx.Response(200, json=filter_info(("Berlin", "Berlin")))
    )
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json=search_response([listing()], page=0, next_page=None, total_hits=1),
        )
    )
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    source = adapter(tmp_path)
    reference = source.discover()[0]

    occurrence = source.fetch_detail(reference)

    assert occurrence.source is SourceKind.THYSSENKRUPP
    assert occurrence.source_instance == "thyssenkrupp"
    assert occurrence.external_id == "967315"
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.company == "thyssenkrupp Automation Engineering GmbH"
    assert occurrence.location == (
        "Berlin, Berlin, Germany; Hamburg, Hamburg, Germany"
    )
    assert occurrence.description == "Build backend services. Operate cloud workloads."
    assert occurrence.posted_at == date(2026, 8, 10)
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_fetch_detail_follows_official_tkms_redirect(tmp_path: Path) -> None:
    original = respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(308, headers={"Location": TKMS_DETAIL_URL})
    )
    redirected = respx.get(TKMS_DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html())
    )
    source = adapter(tmp_path)

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.description == "Build backend services. Operate cloud workloads."
    assert occurrence.detail_complete is True
    assert original.call_count == 1
    assert redirected.call_count == 1


@respx.mock
def test_fetch_detail_accepts_location_returned_by_official_city_query(
    tmp_path: Path,
) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(
            200,
            text=detail_html(
                locations=(("Hamburg", "Hamburg", "Germany"),)
            ),
        )
    )
    source = adapter(tmp_path)
    reference = source_reference()

    occurrence = source.fetch_detail(reference)

    assert occurrence.location == "Hamburg, Hamburg, Germany"


@respx.mock
def test_fetch_detail_rejects_a_page_for_another_job(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html(job_id="967999"))
    )
    source = adapter(tmp_path)

    with pytest.raises(InvalidResponse, match="did not match"):
        source.fetch_detail(source_reference())


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
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(status_code))
    source = adapter(tmp_path)

    with pytest.raises(ExplicitlyClosed) as error:
        source.fetch_detail(source_reference())

    assert error.value.source_job_key == "thyssenkrupp:thyssenkrupp:967315"
    assert error.value.reason == reason


def source_reference():
    from job_scan.sources.base import JobReference

    return JobReference(
        source=SourceKind.THYSSENKRUPP,
        source_instance="thyssenkrupp",
        external_id="967315",
        detail_url=DETAIL_URL,
        platform_url=DETAIL_URL,
        listing_title="Softwareentwickler:in im Bereich .Net und IoT (m/w/d)",
        listing_company="thyssenkrupp Automation Engineering GmbH",
        listing_location="Berlin, Berlin, Germany",
        listing_posted_at=date(2026, 8, 10),
    )


@respx.mock
def test_new_thyssenkrupp_job_carries_transient_snapshot_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    snapshot_html = (
        '<html data-job-scan-snapshot="thyssenkrupp:thyssenkrupp:967315">'
        "<body>Softwareentwickler IoT</body></html>"
    )
    monkeypatch.setattr(
        "job_scan.sources.thyssenkrupp.capture_browser_snapshot",
        lambda **_arguments: snapshot_html,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = ThyssenkruppAdapter(
        config(),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.job_snapshot_html == snapshot_html
    assert occurrence.job_snapshot_error_code is None


@respx.mock
def test_thyssenkrupp_snapshot_failure_does_not_discard_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    monkeypatch.setattr(
        "job_scan.sources.thyssenkrupp.capture_browser_snapshot",
        lambda **_arguments: None,
    )
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    source = ThyssenkruppAdapter(
        config(),
        client,
        capture_snapshot=lambda _reference: True,
    )

    occurrence = source.fetch_detail(source_reference())

    assert occurrence.description
    assert occurrence.job_snapshot_html is None
    assert occurrence.job_snapshot_error_code == "snapshot_capture_failed"


def test_snapshot_page_script_keeps_only_thyssenkrupp_job_information() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    from job_scan.sources.thyssenkrupp import _snapshot_script

    with sync_api.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <style>#job { color: rgb(0, 65, 101); }</style>
            <script type="application/ld+json">{"identifier":{"value":"967315"}}</script>
            <main class="outer-container">
              <section id="job"><h1>Softwareentwickler:in IoT</h1>
                <p>Berlin · thyssenkrupp Automation Engineering GmbH</p></section>
              <div class="metadata-actions">
                <div class="w-full"><h2>Job details</h2><table>
                  <tr><th>Published</th><td>10.08.2026</td></tr>
                  <tr><th>Job number</th><td>967315</td></tr>
                  <tr><th>Share job:</th><td><a href="https://example.com/share">LinkedIn</a></td></tr>
                </table></div>
                <div><a class="apply-now" href="https://example.com/apply">Apply now</a></div>
              </div>
              <article><h2>Your responsibilities</h2><p>Build backend services.</p>
                <h2>Your profile</h2><p>Java experience.</p>
                <h2>Your benefits</h2><p>Flexible working hours.</p></article>
            </main>
            <section id="benefits"><h2>Company benefits</h2><p>Generic company content.</p></section>
            <section><h2>Similar jobs</h2><p>Recommendation.</p></section>
            """
        )

        payload = page.evaluate(_snapshot_script("967315"))

        assert payload["status"] == "ok"
        html = payload["html"]
        assert 'data-job-scan-snapshot="thyssenkrupp:thyssenkrupp:967315"' in html
        assert "Softwareentwickler:in IoT" in html
        assert "Build backend services." in html
        assert "Java experience." in html
        assert "Flexible working hours." in html
        assert "rgb(0, 65, 101)" in html
        assert "Share job" not in html
        assert "Apply now" not in html
        assert "Generic company content." not in html
        assert "Recommendation." not in html
        assert "http://" not in html
        assert "https://" not in html
        browser.close()
