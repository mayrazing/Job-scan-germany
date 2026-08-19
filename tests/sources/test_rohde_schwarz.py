from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.domain import SourceKind
from job_scan.http_client import InvalidResponse, PublicHttpClient
from job_scan.sources import ExplicitlyClosed
from job_scan.sources.base import JobReference
from job_scan.sources.rohde_schwarz import RohdeSchwarzAdapter

ORIGIN = "https://www.rohde-schwarz.com"
SEARCH_URL = f"{ORIGIN}/us/career/jobs/career-jobboard_251573.html"
DETAIL_URL = (
    f"{ORIGIN}/us/career/jobs/"
    "embedded-software-m-w-d-engineer-wireless-systems_251563-1629385.html"
)
SEARCH_PATTERN = re.compile(re.escape(SEARCH_URL) + r"(?:\?.*)?$")


def config(**overrides: object) -> AppConfig:
    values: dict[str, object] = {
        "search_terms": ["Software Engineer"],
        "locations": [],
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
) -> RohdeSchwarzAdapter:
    client = PublicHttpClient(tmp_path / "cache", min_interval_seconds=0)
    return RohdeSchwarzAdapter(app_config or config(), client)


def listing_html(
    job_id: str,
    title: str,
    detail_path: str,
    *,
    city: str = "Berlin",
) -> str:
    return f"""
    <div class="module-accordion accordion-disabled" data-view="accordion-item">
      <div class="title">{html.escape(title)}</div>
      <div class="content">
        <div class="accordion-table-list">
          <div class="accordion-table-list-item column-1 hidden-to-l">
            <div class="accordion-table-list-item-info">
              <a class="favorite" data-job-id="{html.escape(job_id)}"></a>
            </div>
          </div>
          <div class="accordion-table-list-item column-2">
            <div class="accordion-table-list-item-title">
              <a class="accordion-table-list-item-title-link"
                 href="{html.escape(detail_path)}">{html.escape(title)}</a>
            </div>
          </div>
          <div class="accordion-table-list-item column-5">
            <div class="accordion-table-list-item-type">Location</div>
            <div class="accordion-table-list-item-info">Germany</div>
          </div>
          <div class="accordion-table-list-item column-6">
            <div class="accordion-table-list-item-type">City/region</div>
            <div class="accordion-table-list-item-info">{html.escape(city)}</div>
          </div>
        </div>
      </div>
    </div>
    """


def search_html(
    listings: list[tuple[str, str, str, str]],
    *,
    total: int | None = None,
    available_cities: tuple[str, ...] = (),
) -> str:
    rows = "".join(
        listing_html(job_id, title, detail_path, city=city)
        for job_id, title, detail_path, city in listings
    )
    count = len(listings) if total is None else total
    city_filters = "".join(
        f'<input name="filter[rsCity][]" value="{html.escape(city)}" type="checkbox">'
        for city in available_cities
    )
    return f"""
    <html><body>
      <div class="city-filters">{city_filters}</div>
      <div class="module-counter-big">
        <span class="counter">{count}</span><span>Job offerings</span>
      </div>
      <div class="module-tradeshow-results jobboard" data-view="jobboard-table">
        <div class="results">{rows}</div>
      </div>
    </body></html>
    """


OFFICIAL_SEVEN = [
    (
        "707",
        "Embedded Software (m/w/d) Engineer Wireless Systems",
        "/us/career/jobs/embedded-software-m-w-d-engineer-wireless-systems_251563-1629385.html",
        "Berlin",
    ),
    (
        "694",
        "Professional Software Quality and Automation Engineer (m/w/d)",
        "/us/career/jobs/professional-software-quality-and-automation-engineer-m-w-d_251563-1632271.html",
        "Haiger",
    ),
    (
        "702",
        "Software Automation Engineer im Bereich Netzwerkanalyse (m/w/d)",
        "/us/career/jobs/software-automation-engineer-im-bereich-netzwerkanalyse-m-w-d_251563-1637826.html",
        "Leipzig",
    ),
    (
        "547",
        "Junior Software Engineer (m/f/d) AI and Data Systems",
        "/us/career/jobs/junior-software-engineer-m-f-d-ai-and-data-systems_251563-1625229.html",
        "Berlin",
    ),
    (
        "1876",
        "Software Compliance Engineer (m/w/d)",
        "/us/career/jobs/software-compliance-engineer-m-w-d_251563-1649665.html",
        "Teisnach",
    ),
    (
        "798",
        "Senior Software Engineer (m/f/d) AI and Data Systems",
        "/us/career/jobs/senior-software-engineer-m-f-d-ai-and-data-systems_251563-1640768.html",
        "Berlin",
    ),
    (
        "605",
        "Technical Lead Cloud-Native Software Engineering (w/m/d)",
        "/us/career/jobs/technical-lead-cloud-native-software-engineering-w-m-d_251563-1631165.html",
        "Siegburg",
    ),
]


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode(), keep_blank_values=True)


@respx.mock
def test_discover_reads_the_seven_jobs_from_the_parameterized_official_page(
    tmp_path: Path,
) -> None:
    official_route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(200, text=search_html(OFFICIAL_SEVEN))
    )
    rohde_schwarz = adapter(tmp_path)

    references = rohde_schwarz.discover()

    assert [reference.external_id for reference in references] == [
        "707",
        "694",
        "702",
        "547",
        "1876",
        "798",
        "605",
    ]
    assert references[0].source is SourceKind.SUCCESSFACTORS
    assert references[0].source_instance == "rohdeschwarz"
    assert references[0].listing_location == "Berlin, Germany"
    assert str(references[0].detail_url) == DETAIL_URL
    assert official_route.call_count == 1
    assert _query(official_route.calls[0].request) == {
        "filter[rsCountry][]": ["Germany"],
        "term": ["Software Engineer"],
    }


@respx.mock
def test_discover_queries_each_unique_term_with_all_setup_cities_and_deduplicates(
    tmp_path: Path,
) -> None:
    shared = OFFICIAL_SEVEN[0]
    berlin_only = (
        "547",
        "Junior Software Engineer (m/f/d) AI and Data Systems",
        "/us/career/jobs/junior-software-engineer-m-f-d-ai-and-data-systems_251563-1625229.html",
        "Berlin",
    )
    official_route = respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(
                200,
                text=search_html([], available_cities=("Berlin", "Teisnach")),
            ),
            httpx.Response(200, text=search_html([shared])),
            httpx.Response(200, text=search_html([shared, berlin_only])),
        ]
    )
    rohde_schwarz = adapter(
        tmp_path,
        app_config=config(
            search_terms=["Software Engineer", "software engineer", "Backend Engineer"],
            locations=["Berlin", "Teisnach", "berlin"],
        ),
    )

    references = rohde_schwarz.discover()

    assert [reference.external_id for reference in references] == ["707", "547"]
    assert official_route.call_count == 3
    assert [_query(call.request) for call in official_route.calls] == [
        {
            "filter[rsCountry][]": ["Germany"],
            "term": [""],
        },
        {
            "filter[rsCity][]": ["Berlin", "Teisnach"],
            "filter[rsCountry][]": ["Germany"],
            "term": ["Software Engineer"],
        },
        {
            "filter[rsCity][]": ["Berlin", "Teisnach"],
            "filter[rsCountry][]": ["Germany"],
            "term": ["Backend Engineer"],
        },
    ]


@respx.mock
def test_discover_ignores_city_missing_from_official_filter_list(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(
                200,
                text=search_html([], available_cities=("Berlin", "Munich")),
            ),
            httpx.Response(200, text=search_html([OFFICIAL_SEVEN[0]])),
        ]
    )
    source = adapter(
        tmp_path,
        app_config=config(locations=["Berlin", "Nowherezzzz"]),
    )

    references = source.discover()

    assert [reference.external_id for reference in references] == ["707"]
    assert _query(route.calls[1].request)["filter[rsCity][]"] == ["Berlin"]


@respx.mock
def test_discover_skips_job_search_when_no_city_maps_to_official_list(
    tmp_path: Path,
) -> None:
    route = respx.get(SEARCH_PATTERN).mock(
        return_value=httpx.Response(
            200,
            text=search_html([], available_cities=("Berlin", "Munich")),
        )
    )
    source = adapter(
        tmp_path,
        app_config=config(locations=["Nowherezzzz"]),
    )

    assert source.discover() == []
    assert route.call_count == 1
    assert _query(route.calls[0].request)["term"] == [""]


@respx.mock
def test_discover_follows_the_official_thirty_result_offset(tmp_path: Path) -> None:
    first_page = [
        (
            str(job_id),
            f"Software job {job_id}",
            f"/us/career/jobs/software-job-{job_id}_251563-{1_600_000 + job_id}.html",
            "Munich",
        )
        for job_id in range(1, 31)
    ]
    final_job = (
        "31",
        "Software job 31",
        "/us/career/jobs/software-job-31_251563-1600031.html",
        "Munich",
    )
    official_route = respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(200, text=search_html(first_page, total=31)),
            httpx.Response(200, text=search_html([final_job], total=31)),
        ]
    )
    rohde_schwarz = adapter(tmp_path)

    references = rohde_schwarz.discover()

    assert len(references) == 31
    assert references[0].external_id == "1"
    assert references[-1].external_id == "31"
    assert [_query(call.request).get("offset") for call in official_route.calls] == [
        None,
        ["30"],
    ]


@respx.mock
def test_discover_rejects_an_empty_page_before_the_official_total(tmp_path: Path) -> None:
    first_page = [
        (
            str(job_id),
            f"Software job {job_id}",
            f"/us/career/jobs/software-job-{job_id}_251563-{1_600_000 + job_id}.html",
            "Munich",
        )
        for job_id in range(1, 31)
    ]
    respx.get(SEARCH_PATTERN).mock(
        side_effect=[
            httpx.Response(200, text=search_html(first_page, total=31)),
            httpx.Response(200, text=search_html([], total=31)),
        ]
    )
    rohde_schwarz = adapter(tmp_path)

    with pytest.raises(InvalidResponse, match="empty page before total jobs"):
        rohde_schwarz.discover()


def detail_html(*, identifier: str = "707", city: str = "Berlin") -> str:
    organization = {
        "@context": "https://schema.org/",
        "@type": "Organization",
        "name": "Rohde & Schwarz",
    }
    posting = {
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Embedded Software (m/w/d) Engineer Wireless Systems",
        "url": DETAIL_URL,
        "description": (
            "<div><h3>Einleitung</h3><p>Build embedded wireless systems.</p>"
            "<h3>Aufgaben</h3><ul><li>Develop C++ and Rust software.</li></ul></div>"
        ),
        "identifier": {
            "@type": "PropertyValue",
            "name": "Rohde & Schwarz GmbH & Co. KG",
            "value": identifier,
        },
        "datePosted": "2026-06-22T14:01:33.128+02:00",
        "hiringOrganization": {
            "@type": "Organization",
            "name": "Rohde & Schwarz GmbH & Co. KG",
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressCountry": "DE",
                "addressLocality": city,
                "postalCode": "12489",
            },
        },
    }
    return f"""
    <html><body>
      <script type="application/ld+json">{json.dumps(organization)}</script>
      <div id="job-content" class="module module-generic-content center">
        <div class="content-container"><div class="text">Primary content</div></div>
      </div>
      <div id="job-content-profile" class="module module-generic-content center">
        <div class="content-container"><div class="text">
          <h3>Qualifikationen</h3><ul><li>Fluent German and English.</li></ul>
        </div></div>
      </div>
      <div id="job-content-offer" class="module module-generic-content center">
        <div class="content-container"><div class="text">Apply now</div></div>
      </div>
      <div id="job-content-boilerplate" class="module module-generic-content muted center">
        <div class="content-container"><div class="text">Corporate boilerplate</div></div>
      </div>
      <script type="application/ld+json">{json.dumps(posting)}</script>
    </body></html>
    """


def reference() -> JobReference:
    return JobReference(
        source=SourceKind.SUCCESSFACTORS,
        source_instance="rohdeschwarz",
        external_id="707",
        detail_url=DETAIL_URL,
        platform_url=DETAIL_URL,
        listing_title="Embedded Software (m/w/d) Engineer Wireless Systems",
        listing_company="Rohde & Schwarz",
        listing_location="Berlin, Germany",
    )


@respx.mock
def test_fetch_detail_reads_jobposting_json_and_the_qualification_section(
    tmp_path: Path,
) -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=detail_html()))
    rohde_schwarz = adapter(tmp_path)

    occurrence = rohde_schwarz.fetch_detail(reference())

    assert occurrence.source is SourceKind.SUCCESSFACTORS
    assert occurrence.source_instance == "rohdeschwarz"
    assert occurrence.external_id == "707"
    assert str(occurrence.url) == DETAIL_URL
    assert occurrence.company == "Rohde & Schwarz GmbH & Co. KG"
    assert occurrence.title == "Embedded Software (m/w/d) Engineer Wireless Systems"
    assert occurrence.location == "Berlin, Germany"
    assert occurrence.description == (
        "Einleitung Build embedded wireless systems. Aufgaben Develop C++ and Rust software. "
        "Qualifikationen Fluent German and English."
    )
    assert occurrence.posted_at == date(2026, 6, 22)
    assert occurrence.detail_complete is True
    assert occurrence.fetch_error_code is None


@respx.mock
def test_fetch_detail_keeps_the_search_result_city_when_jsonld_disagrees(
    tmp_path: Path,
) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html(city="Munich"))
    )
    rohde_schwarz = adapter(tmp_path)

    occurrence = rohde_schwarz.fetch_detail(reference())

    assert occurrence.location == "Berlin, Germany"


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
    rohde_schwarz = adapter(tmp_path)

    with pytest.raises(ExplicitlyClosed) as error:
        rohde_schwarz.fetch_detail(reference())

    assert error.value.source_job_key == "successfactors:rohdeschwarz:707"
    assert error.value.reason == reason


@respx.mock
def test_fetch_detail_rejects_jobposting_for_another_listing(tmp_path: Path) -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text=detail_html(identifier="999"))
    )
    rohde_schwarz = adapter(tmp_path)

    with pytest.raises(InvalidResponse, match="identifier did not match"):
        rohde_schwarz.fetch_detail(reference())
